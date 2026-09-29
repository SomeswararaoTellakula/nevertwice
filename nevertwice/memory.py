"""NeverTwice's memory layer: how incident knowledge flows into and out of Hindsight.

Write path (learning)
    seed history / resolved incidents  --retain-->  Hindsight bank
    Hindsight extracts facts + entities + time, consolidates observations and
    refreshes the mental models ("living runbooks") automatically.

Read path (triage)
    live alert --recall x4 (similar / worked / failed / conventions, tag-scoped)-->
    compact, referenced context (M1..Mn) + relevant runbook excerpts --> the agent.

Ask path
    free-form question --reflect--> grounded answer that respects the bank's
    mission, disposition and directives, with the evidence it used.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .hindsight_client import HindsightClient, HindsightError, operation_ids_from
from .schemas import INCIDENT_ID_RE, Alert

log = logging.getLogger("nevertwice.memory")

Emit = Callable[[dict[str, Any]], Awaitable[None]]


async def _noop(_: dict[str, Any]) -> None:
    return None


# --------------------------------------------------------------- bank setup
BANK_NAME = "BasketBolt on-call memory (NeverTwice)"
BANK_MISSION = (
    "Institutional memory for BasketBolt's SRE on-call team: past incidents, their symptoms and log "
    "signatures, root causes, which remediation steps worked, failed or made things worse, the changes "
    "that triggered incidents, open postmortem action items, and team conventions and escalation owners."
)
REFLECT_MISSION = (
    "You are NeverTwice, a senior SRE who has been on call for every BasketBolt incident. Answer like an "
    "incident commander: fastest safe mitigation first, cite incident IDs (INC-xxxx) for every claim, "
    "and warn about fixes that backfired before."
)
RETAIN_MISSION = (
    "Extract operational facts: services, symptoms, exact error and log signatures, root causes, each "
    "remediation action with its outcome (worked / did not work / made things worse), commands, feature "
    "flags, CronJobs, database roles, owners, dates, and whether action items are open or done. Always keep "
    "incident IDs such as INC-2104 and exact identifiers verbatim."
)
OBSERVATIONS_MISSION = (
    "Consolidate into durable operational knowledge: recurring failure modes per service with their log "
    "signatures, fixes that reliably work, fixes that repeatedly fail or backfire, open risks, and owners."
)
DISPOSITION = {"disposition_skepticism": 4, "disposition_literalism": 4, "disposition_empathy": 2}

DIRECTIVES = [
    {
        "name": "Cite incident evidence",
        "content": "When recommending an action, cite the past incident ID (for example INC-2104) that supports it. "
        "If no past incident supports it, say it is general guidance.",
    },
    {
        "name": "Destructive actions need approval",
        "content": "Never recommend FLUSHALL/FLUSHDB, DROP/TRUNCATE, resetting Kafka consumer offsets to latest, or "
        "changing an RDS instance class during an incident without stating that it needs incident-commander approval.",
    },
    {
        "name": "Separate what worked from what backfired",
        "content": "Always separate fixes that worked before from fixes that failed or made things worse, and say which incident showed it.",
    },
]

MENTAL_MODELS = [
    {
        "id": "failure-patterns",
        "name": "Recurring failure patterns",
        "source_query": "Which incident patterns recur at BasketBolt? For each pattern give the service, the log "
        "signature, what triggered it (deploy, schedule, traffic, vendor), the fix that worked, and the incident IDs.",
    },
    {
        "id": "fixes-that-backfired",
        "name": "Fixes that backfired",
        "source_query": "Which remediation actions were tried during past incidents and did not work or made things "
        "worse? For each: the action, why it failed, the service and incident ID. Note any case where the same action "
        "was actually the right fix.",
    },
    {
        "id": "open-risks",
        "name": "Open postmortem action items",
        "source_query": "Which postmortem action items are still open, which incidents raised them, and what risk "
        "does each leave in production?",
    },
    {
        "id": "oncall-conventions",
        "name": "On-call rules and escalation",
        "source_query": "What are the on-call rules: pre-approved actions, forbidden actions, and the escalation owner "
        "for each service?",
    },
]


# Runbooks whose excerpts are fed into triage (matched by id, or by name if the server assigned its own ids).
TRIAGE_RUNBOOK_IDS = {"fixes-that-backfired", "failure-patterns", "open-risks"}
TRIAGE_RUNBOOK_NAMES = {m["name"].lower() for m in MENTAL_MODELS if m["id"] in TRIAGE_RUNBOOK_IDS}


# ------------------------------------------------------------ data classes
@dataclass
class MemoryItem:
    ref: str
    id: str
    text: str
    type: str
    context: str
    date: str
    group: str
    incident_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref, "id": self.id, "text": self.text, "type": self.type, "context": self.context,
            "date": self.date, "group": self.group, "incident_ids": self.incident_ids,
        }


GROUP_TITLES = {
    "failed": "Fixes that FAILED or made things WORSE before",
    "worked": "Fixes that WORKED before",
    "similar": "Similar past incidents and changes",
    "conventions": "On-call rules and owners",
}
GROUP_BUDGET = {"failed": 1300, "worked": 1300, "similar": 2600, "conventions": 700}


@dataclass
class MemoryContext:
    items: list[MemoryItem] = field(default_factory=list)
    runbooks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recall_ms: int = 0

    @property
    def incident_ids(self) -> list[str]:
        counts: dict[str, int] = {}
        for item in self.items:
            for iid in item.incident_ids:
                counts[iid] = counts.get(iid, 0) + 1
        return sorted(counts, key=lambda k: (-counts[k], k))

    def by_ref(self) -> dict[str, MemoryItem]:
        return {item.ref.upper(): item for item in self.items}

    def render(self, scale: float = 1.0) -> str:
        """Compact text for the LLM prompt. `scale` < 1 shrinks it when the model says the prompt is too large."""
        if not self.items and not self.runbooks:
            return "No relevant memories were found in Hindsight for this alert."
        sections: list[str] = []
        for group in ("similar", "worked", "failed", "conventions"):
            budget = int(GROUP_BUDGET[group] * scale)
            lines: list[str] = []
            used = 0
            for item in (i for i in self.items if i.group == group):
                line = f"[{item.ref}] {item.date or 'undated'} | {item.context or item.type}: {_clip(item.text, int(420 * scale))}"
                if used + len(line) > budget and lines:
                    break
                lines.append(line)
                used += len(line)
            if lines:
                sections.append(f"## {GROUP_TITLES[group]}\n" + "\n".join(lines))
        for runbook in self.runbooks:
            excerpt = _clip(runbook.get("excerpt", ""), int(700 * scale))
            if excerpt:
                sections.append(f"## Living runbook (Hindsight mental model): {runbook['name']}\n{excerpt}")
        return "\n\n".join(sections)


# ------------------------------------------------------------------ memory
class IncidentMemory:
    def __init__(self, client: HindsightClient, bank_id: str) -> None:
        self.client = client
        self.bank_id = bank_id
        self._models_cache: tuple[float, list[dict[str, Any]]] | None = None
        self.poll_interval = 2.0  # seconds between Hindsight operation polls

    # ------------------------------------------------------------ setup
    async def setup_bank(self, log_fn: Callable[[str], None] = print) -> None:
        """Create/configure the bank: mission, disposition, directives. Idempotent."""
        await self.client.upsert_bank(
            self.bank_id, name=BANK_NAME, mission=BANK_MISSION, reflect_mission=REFLECT_MISSION, **DISPOSITION
        )
        log_fn(f"Bank '{self.bank_id}' ready (mission + disposition skepticism=4, literalism=4, empathy=2).")
        try:
            await self.client.update_bank_config(
                self.bank_id,
                {"retain_mission": RETAIN_MISSION, "observations_mission": OBSERVATIONS_MISSION, "reflect_mission": REFLECT_MISSION},
            )
            log_fn("Retain/observation/reflect missions configured.")
        except HindsightError as exc:
            log_fn(f"Note: could not set per-operation missions ({exc}); continuing with the bank mission.")
        try:
            existing = {str(d.get("name", "")).strip().lower() for d in await self.client.list_directives(self.bank_id)}
            for directive in DIRECTIVES:
                if directive["name"].lower() in existing:
                    continue
                await self.client.create_directive(self.bank_id, name=directive["name"], content=directive["content"])
                log_fn(f"Directive added: {directive['name']}")
        except HindsightError as exc:
            log_fn(f"Note: directives unavailable on this server ({exc}).")

    async def setup_mental_models(self, log_fn: Callable[[str], None] = print) -> list[str]:
        """Create the living runbooks. Returns operation ids to wait on."""
        ops: list[str] = []
        existing = {str(m.get("id")): m for m in await self.client.list_mental_models(self.bank_id)}
        by_name = {str(m.get("name", "")).lower(): str(m.get("id")) for m in existing.values()}
        for model in MENTAL_MODELS:
            current_id = model["id"] if model["id"] in existing else by_name.get(model["name"].lower())
            if current_id:
                try:
                    resp = await self.client.refresh_mental_model(self.bank_id, current_id)
                    ops += operation_ids_from(resp)
                    log_fn(f"Mental model refreshed: {model['name']}")
                except HindsightError as exc:
                    log_fn(f"Note: could not refresh {model['name']}: {exc}")
                continue
            resp = await self.client.create_mental_model(
                self.bank_id, name=model["name"], source_query=model["source_query"], model_id=model["id"], max_tokens=2048
            )
            ops += operation_ids_from(resp)
            log_fn(f"Mental model created: {model['name']}")
        self._models_cache = None
        return ops

    # --------------------------------------------------------------- read
    async def gather_context(self, alert: Alert, emit: Emit = _noop) -> MemoryContext:
        started = time.perf_counter()
        ctx = MemoryContext()
        log_lines = _key_log_lines(alert.logs, 2)
        similar_q = _clip(
            f"{alert.title}. Service {alert.service}. {alert.metrics} {log_lines} Recent changes: {alert.recent_changes}", 900
        )
        symptom = _clip(f"{alert.service}: {alert.title}. {log_lines}", 500)
        plans = [
            ("similar", "Recalling similar incidents", dict(query=similar_q, budget="mid", max_tokens=1800)),
            ("worked", "Recalling fixes that worked", dict(query=f"fix that resolved {symptom}", budget="low", max_tokens=900, tags=["outcome:worked"], types=["world", "experience"])),
            ("failed", "Recalling fixes that failed", dict(query=f"remediation that failed or made things worse for {symptom}", budget="low", max_tokens=900, tags=["outcome:failed", "outcome:made_worse"], tags_match="any", types=["world", "experience"])),
            ("conventions", "Recalling on-call rules", dict(query=f"on-call rules, forbidden actions and escalation owner for {alert.service}", budget="low", max_tokens=500, tags=["kind:convention"])),
        ]

        async def run(group: str, label: str, kwargs: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
            await emit({"type": "step", "id": f"recall_{group}", "label": label, "status": "running"})
            try:
                data = await self.client.recall(self.bank_id, **kwargs)
                results = [r for r in data.get("results", []) if isinstance(r, dict) and r.get("text")]
                await emit({"type": "step", "id": f"recall_{group}", "label": label, "status": "done", "detail": f"{len(results)} memories"})
                return group, results
            except HindsightError as exc:
                ctx.warnings.append(f"{label} failed: {exc}")
                await emit({"type": "step", "id": f"recall_{group}", "label": label, "status": "error", "detail": str(exc)[:200]})
                return group, []

        async def runbooks() -> list[dict[str, Any]]:
            await emit({"type": "step", "id": "runbooks", "label": "Reading living runbooks", "status": "running"})
            try:
                models = await self.mental_models()
            except HindsightError as exc:
                ctx.warnings.append(f"Mental models unavailable: {exc}")
                await emit({"type": "step", "id": "runbooks", "label": "Reading living runbooks", "status": "error", "detail": str(exc)[:200]})
                return []
            keywords = _keywords(alert)
            picked = []
            for model in models:
                if model.get("id") not in TRIAGE_RUNBOOK_IDS and str(model.get("name", "")).lower() not in TRIAGE_RUNBOOK_NAMES:
                    continue
                excerpt = relevant_excerpt(str(model.get("content") or ""), keywords, 700)
                if excerpt:
                    picked.append({"id": model.get("id"), "name": model.get("name"), "excerpt": excerpt})
            await emit({"type": "step", "id": "runbooks", "label": "Reading living runbooks", "status": "done", "detail": f"{len(picked)} relevant"})
            return picked

        results = await asyncio.gather(*(run(g, lbl, kw) for g, lbl, kw in plans), runbooks())
        grouped: dict[str, list[dict[str, Any]]] = {g: r for g, r in results[:-1]}  # type: ignore[misc]
        ctx.runbooks = results[-1]  # type: ignore[assignment]

        seen: set[str] = set()
        counter = 0
        for group in ("failed", "worked", "conventions", "similar"):
            limit = 10 if group == "similar" else 6
            for raw in grouped.get(group, [])[:limit]:
                key = str(raw.get("id") or raw.get("text"))[:200]
                if key in seen:
                    continue
                seen.add(key)
                counter += 1
                ctx.items.append(_to_item(raw, f"M{counter}", group))
        ctx.recall_ms = int((time.perf_counter() - started) * 1000)
        await emit({"type": "memories", "items": [i.to_dict() for i in ctx.items], "runbooks": ctx.runbooks, "recall_ms": ctx.recall_ms})
        return ctx

    async def search(self, query: str, focus: str = "any") -> list[dict[str, Any]]:
        """Used by the agent's `search_incident_memory` tool."""
        kwargs: dict[str, Any] = {"query": _clip(query, 600), "budget": "low", "max_tokens": 900}
        if focus == "worked":
            kwargs.update(tags=["outcome:worked"])
        elif focus == "failed":
            kwargs.update(tags=["outcome:failed", "outcome:made_worse"], tags_match="any")
        elif focus == "changes":
            kwargs.update(tags=["kind:change"])
        data = await self.client.recall(self.bank_id, **kwargs)
        return [r for r in data.get("results", []) if isinstance(r, dict) and r.get("text")][:8]

    async def mental_models(self, max_age: float = 60.0) -> list[dict[str, Any]]:
        if self._models_cache and time.monotonic() - self._models_cache[0] < max_age:
            return self._models_cache[1]
        models = await self.client.list_mental_models(self.bank_id)
        full: list[dict[str, Any]] = []
        for model in models:
            if not model.get("content") and model.get("id"):
                try:
                    model = {**model, **await self.client.get_mental_model(self.bank_id, str(model["id"]))}
                except HindsightError:
                    pass
            full.append(model)
        self._models_cache = (time.monotonic(), full)
        return full

    async def ask(self, question: str) -> dict[str, Any]:
        data = await self.client.reflect(self.bank_id, question, budget="mid", include_facts=True)
        based_on = data.get("based_on") or {}
        memories = based_on.get("memories") if isinstance(based_on, dict) else based_on
        names: dict[str, str] = {}
        if isinstance(based_on, dict) and based_on.get("mental_models"):
            try:  # reflect returns model ids; show the runbook names people recognise
                names = {str(m.get("id")): str(m.get("name") or m.get("id")) for m in await self.mental_models()}
            except HindsightError:
                names = {}
        return {
            "text": data.get("text", ""),
            "memories": [
                {"id": m.get("id"), "text": m.get("text"), "type": m.get("type"), "context": m.get("context"),
                 "date": (m.get("occurred_start") or "")[:10], "incident_ids": _incident_ids(m)}
                for m in (memories or []) if isinstance(m, dict)
            ][:12],
            "mental_models": [
                {"id": m.get("id"), "name": m.get("name") or names.get(str(m.get("id"))) or m.get("id")}
                for m in (based_on.get("mental_models") or []) if isinstance(m, dict)
            ] if isinstance(based_on, dict) else [],
            "directives": [
                {"id": d.get("id"), "name": d.get("name")}
                for d in (based_on.get("directives") or []) if isinstance(d, dict)
            ] if isinstance(based_on, dict) else [],
            "usage": data.get("usage") or {},
        }

    async def overview(self) -> dict[str, Any]:
        errors: list[str] = []

        async def safe(coro: Awaitable[Any], default: Any) -> Any:
            try:
                return await coro
            except HindsightError as exc:
                log.info("overview call failed: %s", exc)
                errors.append(str(exc))
                return default

        async def count(fact_type: str) -> int | None:
            data = await safe(self.client.list_memories(self.bank_id, fact_type=fact_type, limit=1), None)
            return int(data.get("total", 0)) if isinstance(data, dict) else None

        stats, models, directives, world, experience, observation = await asyncio.gather(
            safe(self.client.stats(self.bank_id), {}),
            safe(self.mental_models(max_age=10), []),
            safe(self.client.list_directives(self.bank_id), []),
            count("world"), count("experience"), count("observation"),
        )
        return {
            "bank_id": self.bank_id,
            # Only report an error when nothing could be read at all (e.g. Hindsight unreachable or bad key).
            "error": errors[0] if errors and not models and world is None and experience is None else None,
            "stats": stats if isinstance(stats, dict) else {},
            "counts": {"world": world, "experience": experience, "observation": observation},
            "mental_models": [
                {"id": m.get("id"), "name": m.get("name"), "content": m.get("content") or "",
                 "source_query": m.get("source_query") or "",
                 "last_refreshed_at": m.get("last_refreshed_at") or m.get("updated_at") or m.get("created_at") or ""}
                for m in models
            ],
            "directives": [{"id": d.get("id"), "name": d.get("name"), "content": d.get("content")} for d in directives],
        }

    async def list_memories(self, fact_type: str | None, q: str | None, limit: int = 40) -> dict[str, Any]:
        data = await self.client.list_memories(self.bank_id, fact_type=fact_type or None, q=q or None, limit=limit)
        items = []
        for m in data.get("items", []):
            if not isinstance(m, dict):
                continue
            items.append({
                "id": m.get("id"), "text": m.get("text"), "type": m.get("fact_type") or m.get("type"),
                "context": m.get("context") or "", "date": str(m.get("date") or m.get("occurred_start") or "")[:10],
                "tags": m.get("tags") or [], "incident_ids": _incident_ids(m),
            })
        return {"items": items, "total": data.get("total", len(items))}

    # -------------------------------------------------------------- write
    async def retain_and_wait(self, items: list[dict[str, Any]], emit: Emit = _noop, timeout: float = 180.0) -> dict[str, Any]:
        """Retain asynchronously and wait until Hindsight has extracted the facts."""
        await emit({"type": "step", "id": "retain", "label": f"Sending {len(items)} memories to Hindsight", "status": "running"})
        resp = await self.client.retain(self.bank_id, items, run_async=True)
        ops = operation_ids_from(resp)
        await emit({"type": "step", "id": "retain", "label": f"Sending {len(items)} memories to Hindsight", "status": "done", "detail": f"{len(ops)} operation(s)"})
        if not ops:  # server processed synchronously
            return {"operations": 0, "completed": True}
        await emit({"type": "step", "id": "extract", "label": "Hindsight is extracting facts and entities", "status": "running"})

        async def progress(done: int, total: int, _: list[dict[str, Any]]) -> None:
            await emit({"type": "progress", "done": done, "total": total})

        try:
            finished = await self.client.wait_for_operations(
                self.bank_id, ops, timeout=timeout, poll_interval=self.poll_interval, on_progress=progress
            )
        except HindsightError as exc:
            await emit({"type": "step", "id": "extract", "label": "Hindsight is extracting facts and entities", "status": "error", "detail": str(exc)[:200]})
            return {"operations": len(ops), "completed": False, "error": str(exc)}
        failed = [f for f in finished if str(f.get("status", "")).lower() in ("failed", "error", "cancelled")]
        status = "error" if failed else "done"
        detail = f"{len(failed)} failed: {failed[0].get('error_message') or failed[0].get('error')}" if failed else "facts stored"
        await emit({"type": "step", "id": "extract", "label": "Hindsight is extracting facts and entities", "status": status, "detail": detail})
        self._models_cache = None
        return {"operations": len(ops), "completed": not failed, "failed": len(failed)}


# ------------------------------------------------------------------ helpers
def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _key_log_lines(logs: str, n: int) -> str:
    lines = [ln.strip() for ln in (logs or "").splitlines() if ln.strip()]
    scored = sorted(lines, key=lambda ln: (0 if re.search(r"ERROR|FATAL|WARN|error|fail|timeout", ln) else 1))
    # Drop timestamps: they add tokens but not meaning for similarity search.
    cleaned = [re.sub(r"^\S*\d{4}-\d{2}-\d{2}T\S+\s*", "", ln) for ln in scored[:n]]
    return " ".join(_clip(ln, 260) for ln in cleaned)


def _keywords(alert: Alert) -> list[str]:
    words = {alert.service.lower()}
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_.:-]{3,}", f"{alert.title} {alert.logs[:600]}"):
        low = token.lower()
        if low not in {"error", "warn", "failed", "request", "after", "with", "from", "service", "rate"}:
            words.add(low)
    return sorted(words, key=len, reverse=True)[:25]


def relevant_excerpt(content: str, keywords: list[str], limit: int) -> str:
    """Pick the paragraphs/bullets of a mental model that mention the alert's service or signature."""
    if not content:
        return ""
    blocks = [b.strip() for b in re.split(r"\n(?=\s*(?:[-*]|\d+\.|#{1,4})\s)|\n{2,}", content) if b.strip()]
    hits = [b for b in blocks if any(k in b.lower() for k in keywords)]
    chosen = hits or []
    out, used = [], 0
    for block in chosen:
        block = _clip(block, 360)
        if used + len(block) > limit:
            break
        out.append(block)
        used += len(block)
    return "\n".join(out)


def _incident_ids(raw: dict[str, Any]) -> list[str]:
    blob = " ".join(
        str(raw.get(k) or "") for k in ("text", "context", "document_id")
    )
    meta = raw.get("metadata")
    if isinstance(meta, dict) and meta.get("incident_id"):
        blob += " " + str(meta["incident_id"])
    for tag in raw.get("tags") or []:
        blob += " " + str(tag)
    found = [m.upper() for m in INCIDENT_ID_RE.findall(blob)]
    return list(dict.fromkeys(found))


def _to_item(raw: dict[str, Any], ref: str, group: str) -> MemoryItem:
    date = str(raw.get("occurred_start") or raw.get("mentioned_at") or raw.get("date") or "")[:10]
    return MemoryItem(
        ref=ref,
        id=str(raw.get("id") or ""),
        text=str(raw.get("text") or "").strip(),
        type=str(raw.get("type") or raw.get("fact_type") or ""),
        context=str(raw.get("context") or "").strip(),
        date=date,
        group=group,
        incident_ids=_incident_ids(raw),
    )
