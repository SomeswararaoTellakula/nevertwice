"""The NeverTwice triage agent.

Two modes run the *same* model with the *same* instructions:

* memory mode   – Hindsight context is recalled first (4 tag-scoped recalls + living
                  runbooks) and the model may call `search_incident_memory` /
                  `ask_incident_memory` for more before submitting its triage.
* baseline mode – no memory at all. This is what a stateless chatbot gives you,
                  and it is shown side by side in the UI.

The loop is defensive: invalid tool calls are salvaged or retried, an oversized
prompt is shrunk, and if tool calling keeps failing the agent falls back to
JSON-mode generation so the on-call engineer always gets a report.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .hindsight_client import HindsightError
from .llm import LLMClient, LLMError, parse_json_object, salvage_tool_call
from .memory import IncidentMemory, MemoryContext, MemoryItem
from .memory import _to_item as memory_item
from .schemas import INCIDENT_ID_RE, MEMORY_REF_RE, Alert, AvoidAction, RecommendedAction, TriageReport

log = logging.getLogger("nevertwice.agent")

Emit = Callable[[dict[str, Any]], Awaitable[None]]

MAX_TOOL_ROUNDS = 2  # extra memory lookups before the report is forced (keeps free-tier token use low)

COMPANY_BRIEF = (
    "BasketBolt runs 10-minute grocery delivery in Hyderabad, Bengaluru and Pune on AWS ap-south-1: EKS, Argo CD, "
    "RDS PostgreSQL (orders-db), Redis (dispatch-cache), Kafka, OpenSearch, Flagship feature flags. Times are IST."
)

SYSTEM_MEMORY = f"""You are NeverTwice, the on-call incident copilot for BasketBolt's SRE team. {COMPANY_BRIEF}
You are given a live alert and INCIDENT MEMORY recalled from Hindsight (the team's long-term memory). Memory lines are referenced as [M1], [M2], ...

Rules:
- Ground recommendations in memory when it is relevant. Cite evidence as incident IDs (INC-2104) and/or memory refs (M3). Never invent incident IDs.
- Put every action that failed or made things worse in a similar past incident into avoid_actions, with its evidence. Never put such an action in recommended_actions unless you say exactly what is different this time.
- Read the log lines for the mechanism before choosing a fix: e.g. "lock wait" means something is holding row locks, so find and stop the lock holder rather than adding capacity.
- Only give commands you are sure are valid. If unsure of exact flags, describe the step in words instead of inventing options.
- Prefer small, reversible mitigations. Do not change database instances, limits or cluster-wide settings in the middle of an incident.
- Check whether a remembered fix really applies: if the alert differs from the past incident (different trigger, new change, new role/node/version), say what to verify first.
- Order recommended_actions: verification step, then fastest safe mitigation, then permanent fix. Give concrete commands.
- Mark anything destructive or needing sign-off with requires_approval=true.
- If memory has nothing relevant, say so in the summary, use confidence "low", and give sound general SRE guidance.
- You may call search_incident_memory or ask_incident_memory if the provided memory is insufficient, then call submit_triage exactly once."""

SYSTEM_BASELINE = f"""You are an on-call incident assistant for BasketBolt's SRE team. {COMPANY_BRIEF}
You have NO access to past incidents, postmortems, runbooks or team history — only the alert below and general SRE knowledge.
Give the best triage you can: likely root cause, concrete actions (verification, mitigation, fix) and things to avoid. Do not invent past incident IDs. Call submit_triage exactly once."""

_ACTION_ITEM = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short imperative action"},
        "command": {"type": "string", "description": "Exact command or console step, if any"},
        "why": {"type": "string", "description": "Why this helps, referencing evidence"},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "Incident IDs (INC-2104) or memory refs (M3)"},
        "requires_approval": {"type": "boolean"},
    },
    "required": ["title", "why"],
}

SUBMIT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_triage",
        "description": "Submit the final triage report for the on-call engineer.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "2-3 sentences for the incident channel"},
                "likely_root_cause": {"type": "string"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "recommended_actions": {"type": "array", "items": _ACTION_ITEM, "maxItems": 6},
                "avoid_actions": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "why": {"type": "string"},
                            "evidence": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["title", "why"],
                    },
                },
                "similar_incidents": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "properties": {"incident_id": {"type": "string"}, "similarity": {"type": "string"}},
                        "required": ["incident_id", "similarity"],
                    },
                },
                "escalate_to": {"type": "string", "description": "Person or team to page next"},
                "open_questions": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
            },
            "required": ["summary", "likely_root_cause", "confidence", "recommended_actions"],
        },
    },
}

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_incident_memory",
        "description": "Search the team's incident memory (Hindsight recall) for more facts.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, e.g. a log signature or component"},
                "focus": {"type": "string", "enum": ["any", "worked", "failed", "changes"]},
            },
            "required": ["query"],
        },
    },
}

ASK_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_incident_memory",
        "description": "Ask a reasoning question over the whole incident history (Hindsight reflect). Slower; use sparingly.",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
}

JSON_SCHEMA_HINT = (
    '{"summary": str, "likely_root_cause": str, "confidence": "low"|"medium"|"high", '
    '"recommended_actions": [{"title": str, "command": str, "why": str, "evidence": [str], "requires_approval": bool}], '
    '"avoid_actions": [{"title": str, "why": str, "evidence": [str]}], '
    '"similar_incidents": [{"incident_id": str, "similarity": str}], "escalate_to": str, "open_questions": [str]}'
)


@dataclass
class TriageResult:
    id: str
    mode: str
    report: TriageReport
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "mode": self.mode, "report": self.report.model_dump(), "meta": self.meta}


def alert_prompt(alert: Alert) -> str:
    parts = [
        f"LIVE ALERT {alert.id or ''}".strip(),
        f"Service: {alert.service} | Severity: {alert.severity} | Fired: {alert.fired_at}",
        f"Title: {alert.title}",
    ]
    if alert.source:
        parts.append(f"Source: {alert.source}")
    if alert.metrics:
        parts.append(f"Metrics: {alert.metrics}")
    if alert.recent_changes:
        parts.append(f"Recent changes: {alert.recent_changes}")
    if alert.logs:
        parts.append(f"Logs:\n{alert.logs[:2500]}")
    return "\n".join(parts)


class TriageAgent:
    def __init__(self, llm: LLMClient, memory: IncidentMemory | None, known_incidents: Callable[[], dict[str, Any]]):
        self.llm = llm
        self.memory = memory
        self.known_incidents = known_incidents

    async def triage(self, alert: Alert, *, use_memory: bool, emit: Emit) -> TriageResult:
        started = time.perf_counter()
        triage_id = f"TRI-{uuid.uuid4().hex[:8]}"
        mode = "memory" if use_memory else "baseline"
        ctx = MemoryContext()
        tool_calls_made: list[dict[str, Any]] = []

        if use_memory:
            if self.memory is None:
                raise HindsightError("Hindsight is not configured, so memory mode is unavailable.")
            ctx = await self.memory.gather_context(alert, emit)
            for warning in ctx.warnings:
                await emit({"type": "warning", "message": warning})

        await emit({"type": "step", "id": "reason", "label": "Reasoning over the alert" + (" and memory" if use_memory else ""), "status": "running"})
        report, model_used = await self._run_llm(alert, ctx, use_memory, emit, tool_calls_made)
        await emit({"type": "step", "id": "reason", "label": "Reasoning over the alert" + (" and memory" if use_memory else ""), "status": "done", "detail": model_used})

        guard = await self._guard(report, ctx, alert.service, emit) if use_memory else []
        evidence = self._verify_evidence(report, ctx, use_memory)
        latency = int((time.perf_counter() - started) * 1000)
        meta = {
            "model": model_used,
            "latency_ms": latency,
            "recall_ms": ctx.recall_ms,
            "memories_recalled": len(ctx.items),
            "runbooks_used": [r["name"] for r in ctx.runbooks],
            "incidents_recalled": self._timeline(ctx.incident_ids),
            "evidence": evidence,
            "memory_refs_cited": sorted({ref for ref in evidence["refs"]}),
            "tool_calls": tool_calls_made,
            "guard": guard,
            "alert_id": alert.id,
            "service": alert.service,
        }
        return TriageResult(id=triage_id, mode=mode, report=report, meta=meta)

    # ---------------------------------------------------------------- llm loop
    async def _run_llm(
        self, alert: Alert, ctx: MemoryContext, use_memory: bool, emit: Emit, tool_log: list[dict[str, Any]]
    ) -> tuple[TriageReport, str]:
        scale = 1.0
        for _shrink in range(3):
            messages = self._messages(alert, ctx, use_memory, scale)
            try:
                return await self._tool_loop(messages, use_memory, emit, tool_log)
            except LLMError as exc:
                if exc.kind == "too_large" and scale > 0.4:
                    scale *= 0.6
                    await emit({"type": "warning", "message": "Prompt too large for the model's limit; compressing memory context."})
                    continue
                if exc.kind in ("auth", "rate_limit", "network"):
                    raise  # a JSON-mode retry would hit the same wall
                await emit({"type": "warning", "message": f"Tool calling failed ({exc.kind}); retrying in JSON mode."})
                return await self._json_fallback(self._messages(alert, ctx, use_memory, scale * 0.8), emit)
        return await self._json_fallback(self._messages(alert, ctx, use_memory, 0.35), emit)

    def _messages(self, alert: Alert, ctx: MemoryContext, use_memory: bool, scale: float) -> list[dict[str, Any]]:
        if use_memory:
            user = f"{alert_prompt(alert)}\n\nINCIDENT MEMORY (recalled from Hindsight just now):\n{ctx.render(scale)}"
            return [{"role": "system", "content": SYSTEM_MEMORY}, {"role": "user", "content": user}]
        return [{"role": "system", "content": SYSTEM_BASELINE}, {"role": "user", "content": alert_prompt(alert)}]

    async def _tool_loop(
        self, messages: list[dict[str, Any]], use_memory: bool, emit: Emit, tool_log: list[dict[str, Any]]
    ) -> tuple[TriageReport, str]:
        tools = [SUBMIT_TOOL, SEARCH_TOOL, ASK_TOOL] if use_memory else [SUBMIT_TOOL]
        invalid_retries = 0
        models: list[str] | None = None  # None = the configured order (main model first)
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            final_round = round_no == MAX_TOOL_ROUNDS or not use_memory
            tool_choice: Any = {"type": "function", "function": {"name": "submit_triage"}} if final_round else "auto"
            try:
                resp = await self.llm.chat(
                    messages,
                    tools=[SUBMIT_TOOL] if final_round else tools,
                    tool_choice=tool_choice,
                    max_tokens=1800,
                    emit=emit,
                    models=models,
                    fallback_on_tool_error=False,  # salvage this model's answer before trying another model
                )
            except LLMError as exc:
                if exc.kind == "tool_use_failed":
                    salvaged = salvage_tool_call(exc.failed_generation or "", "submit_triage", accept=looks_like_report)
                    if salvaged:
                        return TriageReport.from_loose(salvaged), exc.model or self.llm.model
                    if invalid_retries < 1:
                        invalid_retries += 1
                        others = [m for m in self.llm.models if m != (exc.model or self.llm.model)]
                        if others:
                            models = others
                            await emit({"type": "warning", "message": f"{exc.model or self.llm.model} returned a malformed tool call; retried with {others[0]}."})
                        messages = messages + [{"role": "user", "content": "Your last tool call was not valid. Call the submit_triage tool with the full report as its arguments."}]
                        continue
                raise

            submit = next((c for c in resp.tool_calls if c.name == "submit_triage"), None)
            if submit is not None:
                if submit.parse_error or not submit.arguments:
                    salvaged = salvage_tool_call(submit.raw_arguments, "submit_triage") or parse_json_object(submit.raw_arguments)[0]
                    if salvaged:
                        return TriageReport.from_loose(salvaged), resp.model
                    if invalid_retries < 1:
                        invalid_retries += 1
                        messages = messages + [resp.assistant_message(), _tool_result(submit.id, "ERROR: arguments were not valid JSON. Call submit_triage again.")]
                        continue
                    raise LLMError("submit_triage arguments could not be parsed", "tool_use_failed")
                return TriageReport.from_loose(submit.arguments), resp.model

            if not resp.tool_calls:
                parsed, _ = parse_json_object(resp.content)
                if parsed and ("summary" in parsed or "recommended_actions" in parsed):
                    return TriageReport.from_loose(parsed), resp.model
                messages = messages + [
                    {"role": "assistant", "content": resp.content[:2000]},
                    {"role": "user", "content": "Now call submit_triage with your triage."},
                ]
                continue

            messages = messages + [resp.assistant_message()]
            for call in resp.tool_calls:
                result_text = await self._run_tool(call.name, call.arguments, call.parse_error, emit, tool_log)
                messages.append(_tool_result(call.id, result_text))
        raise LLMError("The model did not submit a triage report", "empty")

    async def _run_tool(
        self, name: str, args: dict[str, Any], parse_error: str | None, emit: Emit, tool_log: list[dict[str, Any]]
    ) -> str:
        if parse_error:
            return f"ERROR: could not parse arguments ({parse_error}). Fix the JSON and retry."
        if self.memory is None:
            return "ERROR: memory is not available."
        try:
            if name == "search_incident_memory":
                query = str(args.get("query") or "").strip()
                focus = str(args.get("focus") or "any")
                if not query:
                    return "ERROR: query is required."
                await emit({"type": "step", "id": f"tool_{len(tool_log)}", "label": f"Agent searched memory: “{query[:80]}”", "status": "running"})
                results = await self.memory.search(query, focus)
                tool_log.append({"tool": name, "query": query, "focus": focus, "results": len(results)})
                await emit({"type": "step", "id": f"tool_{len(tool_log) - 1}", "label": f"Agent searched memory: “{query[:80]}”", "status": "done", "detail": f"{len(results)} memories"})
                if not results:
                    return "No matching memories."
                lines = []
                for r in results:
                    ids = ",".join(INCIDENT_ID_RE.findall(f"{r.get('text', '')} {r.get('context', '')}"))
                    lines.append(f"- ({r.get('context') or r.get('type')}{' ' + ids if ids else ''}) {str(r.get('text'))[:400]}")
                return "\n".join(lines)
            if name == "ask_incident_memory":
                question = str(args.get("question") or "").strip()
                if not question:
                    return "ERROR: question is required."
                await emit({"type": "step", "id": f"tool_{len(tool_log)}", "label": f"Agent asked memory: “{question[:80]}”", "status": "running"})
                answer = await self.memory.ask(question)
                tool_log.append({"tool": name, "question": question, "sources": len(answer.get("memories", []))})
                await emit({"type": "step", "id": f"tool_{len(tool_log) - 1}", "label": f"Agent asked memory: “{question[:80]}”", "status": "done", "detail": f"{len(answer.get('memories', []))} sources"})
                return str(answer.get("text") or "No answer.")[:2500]
        except HindsightError as exc:
            await emit({"type": "warning", "message": f"Memory tool failed: {exc}"})
            return f"ERROR: memory lookup failed ({exc}). Continue with what you have."
        return f"ERROR: unknown tool {name}."

    async def _json_fallback(self, messages: list[dict[str, Any]], emit: Emit) -> tuple[TriageReport, str]:
        instruction = (
            "Respond with ONLY a JSON object (no prose) with this shape:\n" + JSON_SCHEMA_HINT
        )
        msgs = [dict(m) for m in messages]
        msgs[0] = {"role": "system", "content": msgs[0]["content"].replace("call submit_triage exactly once", "return the JSON report").replace("Call submit_triage exactly once.", "Return the JSON report.") + "\n" + instruction}
        last_error: Exception | None = None
        for json_mode in (True, False):
            try:
                resp = await self.llm.chat(msgs, json_mode=json_mode, max_tokens=1800, emit=emit)
            except LLMError as exc:
                last_error = exc
                if exc.kind in ("auth", "rate_limit", "network"):
                    raise
                continue
            parsed, error = parse_json_object(resp.content)
            if not parsed:  # some models answer with a tool call even when asked for plain JSON
                parsed = next((c.arguments for c in resp.tool_calls if looks_like_report(c.arguments)), None)
            if parsed:
                return TriageReport.from_loose(parsed), resp.model
            last_error = LLMError(f"Model did not return JSON: {error}", "empty")
        assert last_error is not None
        raise last_error

    # --------------------------------------------------------------- post
    async def _guard(self, report: TriageReport, ctx: MemoryContext, service: str, emit: Emit) -> list[dict[str, Any]]:
        """Check every risky recommended step against memory before the engineer sees it."""
        risky = risky_steps(report)
        if not risky:
            return []
        await emit({"type": "step", "id": "guard", "label": "Checking the plan against past failures", "status": "running"})
        extra: list[MemoryItem] = []
        if self.memory is not None:
            for name in list(dict.fromkeys(name for _, name in risky))[:3]:
                query = f"{name} of {service} did not work or made things worse"
                try:
                    for group in ("failed", "worked"):
                        for raw in await self.memory.search(query, group):
                            item = memory_item(raw, f"G{len(extra) + 1}", group)
                            if item.id and item.id in {m.id for m in ctx.items + extra}:
                                continue
                            extra.append(item)
                except HindsightError as exc:
                    await emit({"type": "warning", "message": f"Backfire check skipped: {exc}"})
                    break
        moved = backfire_guard(report, MemoryContext(items=ctx.items + extra), service)
        detail = f"moved {len(moved)} step{'s' if len(moved) != 1 else ''} to Don't do this" if moved else "no known backfires"
        await emit({"type": "step", "id": "guard", "label": "Checking the plan against past failures", "status": "done", "detail": detail})
        return moved

    def _verify_evidence(self, report: TriageReport, ctx: MemoryContext, use_memory: bool) -> dict[str, Any]:
        """Map cited refs to incidents and flag any incident ID the agent could not have seen."""
        refs = ctx.by_ref()
        recalled = set(ctx.incident_ids)
        known = {k.upper() for k in self.known_incidents()}
        verified: set[str] = set()
        unverified: set[str] = set()
        cited_refs: set[str] = set()

        def resolve(tokens: list[str]) -> list[str]:
            out: list[str] = []
            for token in tokens:
                for m in MEMORY_REF_RE.finditer(token.upper()):
                    ref = f"M{m.group(1)}"
                    if ref in refs:
                        cited_refs.add(ref)
                        if ref not in out:
                            out.append(ref)
                        for iid in refs[ref].incident_ids:
                            if iid not in out:
                                out.append(iid)
                for iid in INCIDENT_ID_RE.findall(token.upper()):
                    if iid not in out:
                        out.append(iid)
            return out

        for action in [*report.recommended_actions, *report.avoid_actions]:
            action.evidence = resolve(action.evidence + INCIDENT_ID_RE.findall(action.why.upper()))
        texts = [report.summary, report.likely_root_cause] + [s.incident_id for s in report.similar_incidents]
        texts += [e for a in [*report.recommended_actions, *report.avoid_actions] for e in a.evidence]
        for iid in {i.upper() for t in texts for i in INCIDENT_ID_RE.findall(t.upper())}:
            if use_memory and (iid in recalled or iid in known):
                verified.add(iid)
            else:
                unverified.add(iid)
        report.similar_incidents = [s for s in report.similar_incidents if s.incident_id.strip()]
        return {"verified": sorted(verified), "unverified": sorted(unverified), "refs": sorted(cited_refs)}

    def _timeline(self, incident_ids: list[str]) -> list[dict[str, Any]]:
        known = {k.upper(): v for k, v in self.known_incidents().items()}
        out = []
        for iid in incident_ids[:12]:
            inc = known.get(iid)
            if inc:
                out.append({"id": iid, "title": inc.get("title"), "service": inc.get("service"), "date": str(inc.get("started_at", ""))[:10], "severity": inc.get("severity")})
            else:
                out.append({"id": iid, "title": "", "service": "", "date": "", "severity": ""})
        return out


# ---------------------------------------------------------------- backfire guard
# Risky remediation "shapes". A recommended step is moved to avoid_actions when the SAME shape is recorded in
# memory as a failed / made-worse fix for the SAME service more often than as a fix that worked.
BACKFIRE_PATTERNS: dict[str, re.Pattern[str]] = {
    "restart": re.compile(r"\b(rollout restart|rolling restart|restart(ed|ing)?)\b"),
    "scale out": re.compile(r"\b(scal(e|ed|ing)|replicas?|minreplicas|hpa)\b"),
    "raise timeout": re.compile(r"(rais|increas|bump)\w*.{0,40}timeout|timeout.{0,30}\b(30000|30 ?s)\b"),
    "database instance/limits change": re.compile(r"instance class|modify-db-instance|max[_ -]?connections"),
    "cache eviction/flush": re.compile(r"maxmemory-policy|allkeys-lru|flushall|flushdb"),
    "retry job during degradation": re.compile(r"payment-retry|retry job"),
    "switch sms provider": re.compile(r"(switch|chang)\w*.{0,20}provider|textnest"),
}
VERIFY_RE = re.compile(r"^(check|verify|confirm|inspect|look|review|investigate|monitor|measure|watch|query)\b", re.I)
REDUCE_RE = re.compile(r"\b(reduce|down|lower|decrease|back to|revert|roll ?back|throttl)", re.I)


def risky_steps(report: TriageReport) -> list[tuple[RecommendedAction, str]]:
    """Recommended steps shaped like fixes that often backfire (restart, scale out, ...)."""
    out = []
    for action in report.recommended_actions:
        names = candidate_patterns(action)
        if names:
            out.append((action, names[0]))
    return out


# A "reduce/lower/roll back" step is safe for scaling and limits (scaling *down* is not scaling out), but a restart
# tucked into it ("lower the pool, then rollout restart") is still a restart.
REDUCE_SAFE = {"scale out", "raise timeout", "database instance/limits change"}


def candidate_patterns(action: RecommendedAction) -> list[str]:
    """Backfire patterns a recommended step matches, after the verify-only and scale-down exemptions."""
    if VERIFY_RE.match(action.title.strip()):
        return []
    reducing = bool(REDUCE_RE.search(action.title))
    text = f"{action.title} {action.command}".lower()
    return [name for name, pattern in BACKFIRE_PATTERNS.items()
            if pattern.search(text) and not (reducing and name in REDUCE_SAFE)]


def backfire_guard(report: TriageReport, ctx: MemoryContext, service: str) -> list[dict[str, Any]]:
    """Move recommended steps that memory says backfired before (for this service) into avoid_actions."""
    svc = service.lower()
    failed = [m for m in ctx.items if m.group == "failed" and svc in f"{m.text} {m.context}".lower()]
    worked = [m for m in ctx.items if m.group == "worked" and svc in f"{m.text} {m.context}".lower()]
    if not failed:
        return []
    moved: list[dict[str, Any]] = []
    keep: list[RecommendedAction] = []
    for action in report.recommended_actions:
        hit = None
        for name in candidate_patterns(action):
            pattern = BACKFIRE_PATTERNS[name]
            bad = [m for m in failed if pattern.search(m.text.lower())]
            good = [m for m in worked if pattern.search(m.text.lower())]
            if len(bad) > len(good):
                hit = (name, bad)
                break
        if hit is None:
            keep.append(action)
            continue
        name, bad = hit
        ids = list(dict.fromkeys(i for m in bad for i in m.incident_ids))[:3]
        refs = [m.ref for m in bad if m.ref.startswith("M")][:2]  # G# refs come from guard lookups, not the lane
        proof = ", ".join(ids) if ids else "past incidents"
        why = f"Moved here by the backfire guard: memory shows this kind of fix ({name}) failed or made things worse before for {service} ({proof})."
        existing = next((a for a in report.avoid_actions if pattern_matches(f"{a.title} {a.why}", name)), None)
        if existing is None:
            existing = AvoidAction(title=action.title, why=why, evidence=ids + refs)
            report.avoid_actions.insert(0, existing)
        else:  # the model already warned about this kind of fix: attach the guard's proof to that card
            existing.evidence = list(dict.fromkeys(existing.evidence + ids + refs))
        moved.append({"title": action.title, "shown_as": existing.title, "pattern": name, "incidents": ids, "refs": refs})
    report.recommended_actions = keep
    return moved


def pattern_matches(text: str, name: str) -> bool:
    pattern = BACKFIRE_PATTERNS.get(name)
    return bool(pattern and pattern.search(text.lower()))


def looks_like_report(data: Any) -> bool:
    """True when a dict carries triage-report fields (used to salvage mis-named tool calls)."""
    return isinstance(data, dict) and any(k in data for k in ("summary", "likely_root_cause", "recommended_actions"))


def _tool_result(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def top_action(report: TriageReport) -> str:
    """The first non-verification action is what we score for learning feedback."""
    for action in report.recommended_actions:
        if not re.match(r"^(check|verify|confirm|inspect|look|review|investigate)\b", action.title.strip(), re.I):
            return action.title
    return report.recommended_actions[0].title if report.recommended_actions else ""


def report_json(report: TriageReport) -> str:
    return json.dumps(report.model_dump(), ensure_ascii=False)
