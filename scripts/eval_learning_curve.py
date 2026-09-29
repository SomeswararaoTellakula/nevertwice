"""Measure the learning curve: does NeverTwice get better as Hindsight remembers more incidents?

Replays BasketBolt's 15 historical incidents in chronological order against a
*fresh* evaluation bank. For each incident it:

  1. turns the incident into a live alert (title, symptoms, logs, the related change),
  2. asks NeverTwice to triage it WITH memory of every earlier incident (and, with
     --baseline, WITHOUT memory),
  3. scores the report: does a top-2 recommended action contain the fix that
     actually resolved the incident (its `fix_keywords`)?
  4. retains the incident into memory, so later incidents can benefit.

Results go to runtime/eval_results.json and appear in the app's Learning view.

    python scripts/eval_learning_curve.py --baseline

Uses roughly 2 LLM calls per incident per mode; the default pause keeps it inside
Groq's free-tier limits (expect roughly 10-15 minutes).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nevertwice.agent import TriageAgent  # noqa: E402
from nevertwice.config import Settings  # noqa: E402
from nevertwice.dataset import Dataset, company_memory_items, incident_memory_items  # noqa: E402
from nevertwice.hindsight_client import HindsightClient, HindsightError  # noqa: E402
from nevertwice.llm import LLMClient, LLMError  # noqa: E402
from nevertwice.memory import IncidentMemory  # noqa: E402
from nevertwice.schemas import Alert, TriageReport, now_iso  # noqa: E402

try:
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover
    pass


async def _quiet(_: dict[str, Any]) -> None:
    return None


def incident_to_alert(inc: dict[str, Any], changes: dict[str, dict[str, Any]]) -> Alert:
    change = changes.get(inc.get("related_change") or "")
    return Alert(
        id=f"EVAL-{inc['id']}",
        title=inc["title"],
        service=inc["service"],
        severity=inc["severity"],
        fired_at=inc["started_at"],
        source=inc.get("detected_by", ""),
        metrics="; ".join(inc.get("symptoms", [])),
        logs=inc.get("logs", ""),
        recent_changes=change["summary"] if change else "",
    )


def fix_hit(report: TriageReport, keywords: list[str]) -> bool:
    top = report.recommended_actions[:2]
    blob = " ".join(f"{a.title} {a.command} {a.why}" for a in top).lower()
    return any(k.lower() in blob for k in keywords)


def repeat_ids(incidents: list[dict[str, Any]]) -> set[str]:
    """Incidents whose fix was already seen in an earlier incident (a recurring failure mode)."""
    seen: set[str] = set()
    repeats: set[str] = set()
    for inc in incidents:
        kws = {k.lower() for k in inc.get("fix_keywords", [])}
        if kws & seen:
            repeats.add(inc["id"])
        seen |= kws
    return repeats


async def run_eval(
    client: HindsightClient,
    llm: LLMClient,
    dataset: Dataset,
    bank_id: str,
    *,
    baseline: bool,
    pause: float,
    log=print,
) -> dict[str, Any]:
    memory = IncidentMemory(client, bank_id)
    await memory.setup_bank(lambda _m: None)
    try:
        await client.clear_memories(bank_id)
    except HindsightError as exc:
        log(f"(could not clear eval bank: {exc})")
    handbook = company_memory_items(dataset.company)[:1]  # services/owners only: no hindsight leakage
    await memory.retain_and_wait(handbook)

    learned: dict[str, dict[str, Any]] = {}  # only incidents already in memory count as verifiable evidence
    agent = TriageAgent(llm, memory, lambda: learned)
    changes = {c["id"]: c for c in dataset.company.get("changes", [])}
    repeats = repeat_ids(dataset.incidents)
    points: list[dict[str, Any]] = []

    for n, inc in enumerate(dataset.incidents):
        alert = incident_to_alert(inc, changes)
        point: dict[str, Any] = {
            "n_learned": n, "incident_id": inc["id"], "service": inc["service"], "title": inc["title"],
            "repeat": inc["id"] in repeats,
        }
        try:
            result = await agent.triage(alert, use_memory=True, emit=_quiet)
            point.update(
                memory_hit=fix_hit(result.report, inc["fix_keywords"]),
                memories_recalled=result.meta["memories_recalled"],
                cited=result.meta["evidence"]["verified"],
                confidence=result.report.confidence,
            )
        except (LLMError, HindsightError) as exc:
            point.update(memory_hit=None, error=str(exc))
        if baseline:
            await asyncio.sleep(pause)
            try:
                base = await agent.triage(alert, use_memory=False, emit=_quiet)
                point["baseline_hit"] = fix_hit(base.report, inc["fix_keywords"])
            except LLMError as exc:
                point["baseline_hit"] = None
                point["baseline_error"] = str(exc)
        points.append(point)
        log(
            f"{n:2d} {inc['id']} {'repeat' if point['repeat'] else 'first '} "
            f"memory={_fmt(point.get('memory_hit'))} baseline={_fmt(point.get('baseline_hit'))} "
            f"recalled={point.get('memories_recalled', '-')} cited={','.join(point.get('cited', [])) or '-'}"
        )
        await memory.retain_and_wait(incident_memory_items(inc))
        learned[inc["id"].upper()] = inc
        await asyncio.sleep(pause)

    def rate(rows: list[dict[str, Any]], key: str) -> float | None:
        vals = [r[key] for r in rows if isinstance(r.get(key), bool)]
        return round(sum(vals) / len(vals), 3) if vals else None

    rep = [p for p in points if p["repeat"]]
    return {
        "generated_at": now_iso(),
        "model": llm.model,
        "bank_id": bank_id,
        "points": points,
        "summary": {
            "memory_hit_rate": rate(points, "memory_hit"),
            "baseline_hit_rate": rate(points, "baseline_hit"),
            "repeat_memory_hit_rate": rate(rep, "memory_hit"),
            "repeat_baseline_hit_rate": rate(rep, "baseline_hit"),
            "repeats": len(rep),
            "incidents": len(points),
        },
    }


def _fmt(value: Any) -> str:
    return {True: "HIT ", False: "miss", None: " -- "}.get(value, " -- ")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--baseline", action="store_true", help="also run the no-memory baseline for comparison")
    parser.add_argument("--pause", type=float, default=6.0, help="seconds between LLM calls (rate limits)")
    parser.add_argument("--bank", default="", help="evaluation bank id (default: <HINDSIGHT_BANK_ID>-eval)")
    args = parser.parse_args()

    s = Settings.from_env()
    if s.problems():
        for p in s.problems():
            print(p)
        return 2
    bank = args.bank or f"{s.bank_id}-eval"
    dataset = Dataset.load(s.data_dir)
    print(f"Evaluating on fresh bank '{bank}' with {s.llm_model} ({len(dataset.incidents)} incidents)\n")
    started = time.monotonic()
    async with HindsightClient(s.hindsight_base_url, s.hindsight_api_key) as client:
        llm = LLMClient(s.llm_base_url, s.llm_api_key, s.llm_model, fallback_model=s.llm_fallback_model, reasoning_effort=s.llm_reasoning_effort)
        try:
            results = await run_eval(client, llm, dataset, bank, baseline=args.baseline, pause=args.pause)
        finally:
            await llm.aclose()
    out = s.runtime_dir / "eval_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    sm = results["summary"]
    print(f"\nFix found in top-2 actions (all incidents): memory={sm['memory_hit_rate']} baseline={sm['baseline_hit_rate']}")
    print(f"On recurring failure modes ({sm['repeats']} incidents): memory={sm['repeat_memory_hit_rate']} baseline={sm['repeat_baseline_hit_rate']}")
    print(f"Saved {out} in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
