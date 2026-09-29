"""Check that NeverTwice can reach Hindsight and the LLM before a demo.

    python scripts/check_setup.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nevertwice.config import Settings  # noqa: E402
from nevertwice.hindsight_client import HindsightClient, HindsightError  # noqa: E402
from nevertwice.llm import LLMClient, LLMError  # noqa: E402

try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover
    pass

PING_TOOL = {
    "type": "function",
    "function": {
        "name": "report_status",
        "description": "Report that the system is ready.",
        "parameters": {"type": "object", "properties": {"ready": {"type": "boolean"}}, "required": ["ready"]},
    },
}


async def main() -> int:
    s = Settings.from_env()
    ok = True
    print("NeverTwice setup check\n")
    for problem in s.problems():
        print(f"[config] {problem}")
        ok = False

    async with HindsightClient(s.hindsight_base_url, s.hindsight_api_key, timeout=20) as hs:
        try:
            data = await hs.list_memories(s.bank_id, limit=1)
            print(f"[ok] Hindsight reachable at {s.hindsight_base_url}; bank '{s.bank_id}' has {data.get('total')} memories")
            if not data.get("total"):
                print("     -> the bank is empty: run  python scripts/seed_memory.py")
            models = await hs.list_mental_models(s.bank_id)
            print(f"[ok] {len(models)} mental model(s): {', '.join(str(m.get('name')) for m in models) or 'none yet'}")
        except HindsightError as exc:
            ok = False
            print(f"[fail] Hindsight: {exc}")
            if exc.status == 404:
                print("     -> the bank does not exist yet: run  python scripts/seed_memory.py")

    if s.llm_api_key:
        llm = LLMClient(s.llm_base_url, s.llm_api_key, s.llm_model, fallback_model=s.llm_fallback_model, reasoning_effort=s.llm_reasoning_effort)
        try:
            resp = await llm.chat(
                [{"role": "user", "content": "Call report_status with ready=true."}],
                tools=[PING_TOOL],
                tool_choice={"type": "function", "function": {"name": "report_status"}},
                max_tokens=200,
            )
            called = [c.name for c in resp.tool_calls]
            print(f"[ok] LLM {resp.model} answered in {resp.latency_ms} ms; tool calls: {called or 'none'}")
        except LLMError as exc:
            ok = False
            print(f"[fail] LLM ({exc.kind}): {exc}")
        finally:
            await llm.aclose()

    print("\nAll good - start with: python run.py" if ok else "\nFix the items above, then re-run this check.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
