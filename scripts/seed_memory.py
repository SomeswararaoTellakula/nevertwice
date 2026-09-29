"""Load BasketBolt's incident history into a Hindsight memory bank.

    python scripts/seed_memory.py            # create/configure bank, retain history, build runbooks
    python scripts/seed_memory.py --reset    # wipe the bank's memories + local app state first

Idempotent: every item has a stable document_id, so re-running replaces instead of duplicating.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nevertwice.config import Settings  # noqa: E402
from nevertwice.dataset import Dataset, all_seed_items  # noqa: E402
from nevertwice.hindsight_client import HindsightClient, HindsightError, operation_ids_from  # noqa: E402
from nevertwice.memory import IncidentMemory  # noqa: E402
from nevertwice.store import JsonStore  # noqa: E402

try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover
    pass


def say(msg: str) -> None:
    print(msg, flush=True)


async def seed(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if "hindsight.vectorize.io" in settings.hindsight_base_url and not settings.hindsight_api_key:
        say("HINDSIGHT_API_KEY is missing. Create one at https://ui.hindsight.vectorize.io and put it in .env")
        return 2
    dataset = Dataset.load(settings.data_dir)
    items = all_seed_items(dataset)
    say(f"Hindsight: {settings.hindsight_base_url}  bank: {settings.bank_id}")
    say(f"Seed data: {len(dataset.incidents)} incidents -> {len(items)} memory items")

    async with HindsightClient(settings.hindsight_base_url, settings.hindsight_api_key) as client:
        memory = IncidentMemory(client, settings.bank_id)
        try:
            await memory.setup_bank(say)
        except HindsightError as exc:
            say(f"\nCould not create the bank: {exc}")
            return 1

        if args.reset:
            say("Reset: clearing all memories in the bank ...")
            try:
                await client.clear_memories(settings.bank_id)
            except HindsightError as exc:
                say(f"  could not clear memories: {exc}")
            await JsonStore(settings.runtime_dir).reset()
            say("  local app state cleared")

        ops: list[str] = []
        started = time.monotonic()
        for i in range(0, len(items), args.batch_size):
            batch = items[i : i + args.batch_size]
            try:
                resp = await client.retain(settings.bank_id, batch, run_async=True)
            except HindsightError as exc:
                say(f"\nRetain failed for items {i + 1}-{i + len(batch)}: {exc}")
                return 1
            ops += operation_ids_from(resp)
            say(f"  queued items {i + 1}-{i + len(batch)} of {len(items)}")

        if ops and not args.no_wait:
            say(f"Waiting for Hindsight to extract facts ({len(ops)} operation(s)); this takes a few minutes ...")

            def progress(done: int, total: int, _: list) -> None:
                say(f"  extraction {done}/{total} done ({time.monotonic() - started:.0f}s)")

            try:
                finished = await client.wait_for_operations(settings.bank_id, ops, timeout=args.timeout, poll_interval=5, on_progress=progress)
            except HindsightError as exc:
                say(f"  {exc}\n  Extraction continues in the background; re-run later or check the Hindsight UI.")
                finished = []
            failed = [f for f in finished if str(f.get("status", "")).lower() in ("failed", "error", "cancelled")]
            if failed:
                say(f"  WARNING: {len(failed)} operation(s) failed, e.g. {failed[0].get('error_message') or failed[0].get('error') or failed[0]}")

        if not args.skip_models:
            say("Creating living runbooks (mental models) ...")
            try:
                model_ops = await memory.setup_mental_models(say)
                if model_ops and not args.no_wait:
                    await client.wait_for_operations(settings.bank_id, model_ops, timeout=args.timeout, poll_interval=5)
                    say("  runbooks generated")
            except HindsightError as exc:
                say(f"  Note: mental models not created ({exc})")

        try:
            probe = await client.recall(settings.bank_id, "checkout-api HikariCP pool exhausted", budget="low", max_tokens=600)
            say(f"\nSanity recall 'checkout-api HikariCP pool exhausted' -> {len(probe.get('results', []))} memories")
            for r in probe.get("results", [])[:3]:
                say(f"  - {str(r.get('text'))[:110]}")
        except HindsightError as exc:
            say(f"Sanity recall failed: {exc}")

    say(f"\nDone in {time.monotonic() - started:.0f}s. Start the app with: python run.py")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="clear the bank's memories and local app state first")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for extraction")
    parser.add_argument("--no-wait", action="store_true", help="queue work and exit without waiting")
    parser.add_argument("--skip-models", action="store_true", help="do not create mental models")
    return asyncio.run(seed(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
