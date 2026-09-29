"""Score NeverTwice's triage quality on the demo alerts, with and without memory.

Each alert has a checklist in data/alert_checks.json: steps it MUST recommend, steps it must NOT recommend
(fixes that backfired before), and incidents it should cite. The script triages each alert several times
(the model varies between runs), grades every answer, and prints a pass-rate table.

    python scripts/eval_alerts.py                     # memory mode, ALT-501..504, 3 runs each
    python scripts/eval_alerts.py --baseline          # also run without memory, for the before/after
    python scripts/eval_alerts.py --alerts ALT-501 --runs 5

Triage only reads from Hindsight, so this never changes your memory bank. Results are saved to
runtime/alert_eval.json. Expect ~1 minute per triage on Groq's free tier (rate limits).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nevertwice.agent import REDUCE_RE, VERIFY_RE, TriageAgent  # noqa: E402
from nevertwice.config import Settings  # noqa: E402
from nevertwice.dataset import Dataset  # noqa: E402
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


EXEMPT_RE = re.compile(REDUCE_RE.pattern + r"|\b(pause|stop|suspend|halt|disable)", re.I)


def grade(report: TriageReport, cited: list[str], checks: dict[str, Any], use_memory: bool) -> dict[str, Any]:
    return grade_steps([(a.title, a.command) for a in report.recommended_actions], cited, checks, use_memory)


def grade_steps(pairs: list[tuple[str, str]] | list[list[str]], cited: list[str], checks: dict[str, Any], use_memory: bool) -> dict[str, Any]:
    titles = [t for t, _ in pairs]
    steps = [f"{t} {c}".lower() for t, c in pairs]
    actions = [s for t, s in zip(titles, steps) if not VERIFY_RE.match(t.strip())]
    missing = []
    for group in checks.get("must_do", []):
        if not any(re.search(p, s) for p in group for s in steps):
            missing.append(" or ".join(group))
    bad = []
    for pattern in checks.get("must_not", []):
        for t, s in zip(titles, steps):
            if not VERIFY_RE.match(t.strip()) and not EXEMPT_RE.search(t) and re.search(pattern, s):
                bad.append(t)
    bad = list(dict.fromkeys(bad))
    want = checks.get("must_cite", [])
    cite_ok = (not use_memory) or not want or any(i in cited for i in want)
    return {
        "passed": not missing and not bad and cite_ok,
        "missing": missing,
        "recommended_bad_fix": bad,
        "cited_ok": cite_ok,
        "has_actions": bool(actions),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--alerts", nargs="*", default=["ALT-501", "ALT-502", "ALT-503", "ALT-504"])
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--baseline", action="store_true", help="also score the no-memory answer")
    parser.add_argument("--pause", type=float, default=20.0, help="seconds between LLM calls (free-tier rate limits)")
    parser.add_argument("--regrade", action="store_true", help="re-score the last saved run with the current checklist (no LLM calls)")
    args = parser.parse_args()
    if args.regrade:
        return regrade()

    s = Settings.from_env()
    if s.problems():
        print("\n".join(s.problems()))
        return 2
    checks_all = json.loads((s.data_dir / "alert_checks.json").read_text(encoding="utf-8"))
    ds = Dataset.load(s.data_dir)
    modes = ["memory", "baseline"] if args.baseline else ["memory"]
    results: list[dict[str, Any]] = []
    started = time.monotonic()
    async with HindsightClient(s.hindsight_base_url, s.hindsight_api_key) as hs:
        llm = LLMClient(s.llm_base_url, s.llm_api_key, s.llm_model, fallback_model=s.llm_fallback_model, reasoning_effort=s.llm_reasoning_effort)
        agent = TriageAgent(llm, IncidentMemory(hs, s.bank_id), lambda: ds.incident_index)
        try:
            for alert_id in args.alerts:
                raw = ds.alert(alert_id)
                checks = checks_all.get(alert_id)
                if not raw or not checks:
                    print(f"skip {alert_id}: no alert or no checklist")
                    continue
                alert = Alert(**{k: v for k, v in raw.items() if k in Alert.model_fields})
                for run in range(1, args.runs + 1):
                    for mode in modes:
                        row: dict[str, Any] = {"alert": alert_id, "run": run, "mode": mode}
                        try:
                            res = await agent.triage(alert, use_memory=(mode == "memory"), emit=_quiet)
                            row.update(grade(res.report, res.meta["evidence"]["verified"], checks, mode == "memory"))
                            row.update(model=res.meta["model"], guard_moved=len(res.meta.get("guard") or []),
                                       steps=[[a.title, a.command] for a in res.report.recommended_actions],
                                       cited=res.meta["evidence"]["verified"])
                        except (LLMError, HindsightError) as exc:
                            row.update(passed=None, error=str(exc))
                        results.append(row)
                        status = {True: "PASS", False: "FAIL", None: "ERR "}[row.get("passed")]
                        detail = ""
                        if row.get("recommended_bad_fix"):
                            detail += f" bad fix: {row['recommended_bad_fix'][0][:60]}"
                        if row.get("missing"):
                            detail += f" missing: {row['missing'][0][:50]}"
                        if row.get("guard_moved"):
                            detail += f" (guard moved {row['guard_moved']})"
                        if row.get("error"):
                            detail += f" {row['error'][:80]}"
                        print(f"{alert_id} run {run} {mode:8s} {status}{detail}", flush=True)
                        await asyncio.sleep(args.pause)
        finally:
            await llm.aclose()

    summary = print_summary(results)
    out = s.runtime_dir / "alert_eval.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"generated_at": now_iso(), "model": s.llm_model, "summary": summary, "results": results}, indent=2), encoding="utf-8")
    print(f"Saved {out} ({time.monotonic() - started:.0f}s)")
    return 0


def print_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    print("\nPass rate (answers that recommend the right fix, avoid known-bad fixes and cite evidence):")
    summary: dict[str, Any] = {}
    for mode in ("memory", "baseline"):
        rows = [r for r in results if r["mode"] == mode and r.get("passed") is not None]
        if not rows:
            continue
        passed = sum(1 for r in rows if r["passed"])
        bad = sum(1 for r in rows if r.get("recommended_bad_fix"))
        summary[mode] = {"passed": passed, "total": len(rows), "recommended_bad_fix": bad}
        print(f"  {mode:8s} {passed}/{len(rows)} passed · recommended a known-bad fix in {bad}/{len(rows)}")
    return summary


def regrade() -> int:
    s = Settings.from_env()
    path = s.runtime_dir / "alert_eval.json"
    if not path.exists():
        print(f"No saved run at {path}")
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    checks_all = json.loads((s.data_dir / "alert_checks.json").read_text(encoding="utf-8"))
    rows = [r for r in data["results"] if "steps" in r]
    if not rows:
        print("That run was saved by an older version without the answers, so it can't be re-scored. Run the script again.")
        return 1
    for r in rows:
        r.update(grade_steps(r["steps"], r.get("cited", []), checks_all[r["alert"]], r["mode"] == "memory"))
    data["summary"] = print_summary(rows)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
