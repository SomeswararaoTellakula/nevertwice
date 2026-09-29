"""HTTP API + static UI for NeverTwice (Starlette/ASGI).

Streaming endpoints use Server-Sent Events so the UI can show each memory lookup
and reasoning step as it happens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .agent import TriageAgent, top_action
from .config import Settings
from .dataset import Dataset, resolution_memory_items, resolution_to_incident
from .hindsight_client import HindsightClient, HindsightError
from .llm import LLMClient, LLMError
from .memory import IncidentMemory
from .schemas import Alert, AskRequest, Resolution, now_iso
from .store import JsonStore

log = logging.getLogger("nevertwice.server")
STATIC_DIR = Path(__file__).resolve().parent / "static"
Emit = Callable[[dict[str, Any]], Awaitable[None]]


class AppState:
    def __init__(self, settings: Settings, hindsight: HindsightClient, llm: LLMClient) -> None:
        self.settings = settings
        self.hindsight = hindsight
        self.llm = llm
        self.dataset = Dataset.load(settings.data_dir)
        self.store = JsonStore(settings.runtime_dir)
        self.memory = IncidentMemory(hindsight, settings.bank_id)
        self.agent = TriageAgent(llm, self.memory, self.known_incidents)
        self._status_cache: tuple[float, dict[str, Any]] | None = None
        self._id_lock = asyncio.Lock()

    def known_incidents(self) -> dict[str, dict[str, Any]]:
        known = dict(self.dataset.incident_index)
        for inc in self.store.live_incidents():
            if inc.get("id"):
                known[str(inc["id"]).upper()] = inc
        return known

    async def next_incident_id(self) -> str:
        async with self._id_lock:
            numbers = [int(k.split("-")[1]) for k in self.known_incidents() if k.split("-")[-1].isdigit()]
            return f"INC-{(max(numbers) if numbers else 2000) + 1}"


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, HindsightError):
        if exc.status == 404:
            return f"Memory bank not found. Run `python scripts/seed_memory.py` first. ({exc})"
        return f"Hindsight memory error: {exc}"
    if isinstance(exc, LLMError):
        return f"LLM error ({exc.kind}): {exc}"
    if isinstance(exc, ValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []))
        return f"Invalid input{(' for ' + loc) if loc else ''}: {first.get('msg', str(exc))}"
    return f"Unexpected error: {type(exc).__name__}: {exc}"


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


def event_stream(producer: Callable[[Emit], Awaitable[None]]) -> StreamingResponse:
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def emit(event: dict[str, Any]) -> None:
        await queue.put(event)

    async def runner() -> None:
        try:
            await producer(emit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface every failure to the UI
            log.exception("stream failed")
            await queue.put({"type": "error", "message": friendly_error(exc)})
        finally:
            await queue.put(None)

    async def generator() -> AsyncIterator[str]:
        task = asyncio.create_task(runner())
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    yield _sse({"type": "done"})
                    break
                yield _sse(event)
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise ValueError("Request body must be JSON.")
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object.")
    return body


def _bad_request(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


# ------------------------------------------------------------------ routes
async def index(_: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def status(request: Request) -> JSONResponse:
    st: AppState = request.app.state.nevertwice
    if st._status_cache and time.monotonic() - st._status_cache[0] < 10:
        return JSONResponse(st._status_cache[1])
    s = st.settings
    hindsight: dict[str, Any] = {"base_url": s.hindsight_base_url, "bank_id": s.bank_id, "ok": False, "error": None, "total_memories": None}
    try:
        data = await asyncio.wait_for(st.hindsight.list_memories(s.bank_id, limit=1), timeout=12)
        hindsight["ok"] = True
        hindsight["total_memories"] = data.get("total")
    except HindsightError as exc:
        hindsight["error"] = friendly_error(exc)
    except asyncio.TimeoutError:
        hindsight["error"] = f"Hindsight did not answer within 12s at {s.hindsight_base_url}."
    payload = {
        "config_problems": s.problems(),
        "hindsight": hindsight,
        "llm": {"base_url": s.llm_base_url, "model": s.llm_model, "fallback_model": s.llm_fallback_model, "configured": bool(s.llm_api_key)},
        "live_incidents": len(st.store.live_incidents()),
    }
    st._status_cache = (time.monotonic(), payload)
    return JSONResponse(payload)


async def alerts(request: Request) -> JSONResponse:
    st: AppState = request.app.state.nevertwice
    triaged = {}
    for entry in st.store.triage_log():
        triaged.setdefault(entry.get("alert_id"), []).append(entry.get("mode"))
    resolved = {inc.get("alert_id") for inc in st.store.live_incidents() if inc.get("alert_id")}
    items = []
    for alert in st.dataset.alerts:
        items.append({**alert, "triaged_modes": sorted(set(triaged.get(alert["id"], []))), "resolved": alert["id"] in resolved})
    services = [svc["name"] for svc in st.dataset.company.get("services", [])]
    return JSONResponse({"alerts": items, "services": services})


async def incidents(request: Request) -> JSONResponse:
    st: AppState = request.app.state.nevertwice
    rows = []
    for inc in st.known_incidents().values():
        rows.append({
            "id": inc.get("id"), "title": inc.get("title"), "service": inc.get("service"), "severity": inc.get("severity"),
            "started_at": inc.get("started_at"), "source": inc.get("source", "history"),
        })
    rows.sort(key=lambda r: str(r.get("started_at") or ""))
    return JSONResponse({"incidents": rows})


async def incident_detail(request: Request) -> JSONResponse:
    st: AppState = request.app.state.nevertwice
    iid = request.path_params["incident_id"].upper()
    inc = st.known_incidents().get(iid)
    if not inc:
        return _bad_request(f"Unknown incident {iid}", 404)
    return JSONResponse(inc)


async def triage(request: Request) -> Response:
    st: AppState = request.app.state.nevertwice
    try:
        body = await _json_body(request)
    except ValueError as exc:
        return _bad_request(str(exc))
    mode = str(body.get("mode") or "memory")
    if mode not in ("memory", "baseline", "compare"):
        return _bad_request("mode must be 'memory', 'baseline' or 'compare'.")
    raw_alert = body.get("alert")
    if not raw_alert and body.get("alert_id"):
        raw_alert = st.dataset.alert(str(body["alert_id"]))
        if raw_alert is None:
            return _bad_request(f"Unknown alert {body['alert_id']}", 404)
    if not isinstance(raw_alert, dict):
        return _bad_request("Provide 'alert_id' or an 'alert' object.")
    try:
        alert = Alert(**{k: v for k, v in raw_alert.items() if k in Alert.model_fields})
    except ValidationError as exc:
        return _bad_request(friendly_error(exc), 422)
    if not st.settings.llm_api_key:
        return _bad_request("LLM_API_KEY is not set. Add it to .env and restart.", 503)

    lanes = ["baseline", "memory"] if mode == "compare" else [mode]

    async def produce(emit: Emit) -> None:
        for lane in lanes:
            async def lane_emit(event: dict[str, Any], _lane: str = lane) -> None:
                await emit({**event, "lane": _lane})

            await lane_emit({"type": "start", "alert_id": alert.id})
            try:
                result = await st.agent.triage(alert, use_memory=(lane == "memory"), emit=lane_emit)
            except (LLMError, HindsightError) as exc:
                await lane_emit({"type": "error", "message": friendly_error(exc)})
                continue
            report = result.report
            await st.store.log_triage({
                "id": result.id, "ts": now_iso(), "alert_id": alert.id, "service": alert.service, "title": alert.title,
                "mode": lane, "confidence": report.confidence, "memories_recalled": result.meta["memories_recalled"],
                "top_action": top_action(report), "incidents_cited": result.meta["evidence"]["verified"],
                "latency_ms": result.meta["latency_ms"], "model": result.meta["model"], "feedback": None,
            })
            await lane_emit({"type": "report", **result.to_dict()})

    return event_stream(produce)


async def resolve(request: Request) -> Response:
    st: AppState = request.app.state.nevertwice
    try:
        body = await _json_body(request)
        res = Resolution(**{k: v for k, v in body.items() if k in Resolution.model_fields})
    except ValueError as exc:  # includes ValidationError
        return _bad_request(friendly_error(exc) if isinstance(exc, ValidationError) else str(exc), 422)

    async def produce(emit: Emit) -> None:
        incident_id = await st.next_incident_id()
        alert = st.dataset.alert(res.alert_id) if res.alert_id else None
        if alert and not body.get("started_at"):
            res.started_at = alert.get("fired_at") or res.started_at
        recommendation = st.store.get_triage(res.triage_id) if res.triage_id else None
        items = resolution_memory_items(res, incident_id, alert, recommendation)
        await emit({"type": "incident", "incident_id": incident_id, "items": len(items)})
        outcome = await st.memory.retain_and_wait(items, emit)
        incident = resolution_to_incident(res, incident_id)
        incident["alert_id"] = res.alert_id
        incident["retained_items"] = len(items)
        incident["memory_status"] = "stored" if outcome.get("completed") else "processing"
        await st.store.add_incident(incident)
        if res.triage_id:
            await st.store.update_triage(res.triage_id, feedback=res.recommendation_feedback or None, resolved_incident=incident_id)
        st._status_cache = None
        await emit({"type": "resolved", "incident": incident, "retain": outcome})

    return event_stream(produce)


async def ask(request: Request) -> JSONResponse:
    st: AppState = request.app.state.nevertwice
    try:
        req = AskRequest(**await _json_body(request))
    except (ValueError, TypeError) as exc:
        return _bad_request(friendly_error(exc) if isinstance(exc, ValidationError) else str(exc), 422)
    started = time.perf_counter()
    try:
        answer = await st.memory.ask(req.question)
    except HindsightError as exc:
        return _bad_request(friendly_error(exc), 502)
    answer["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return JSONResponse(answer)


async def memory_overview(request: Request) -> JSONResponse:
    st: AppState = request.app.state.nevertwice
    return JSONResponse(await st.memory.overview())


async def memory_list(request: Request) -> JSONResponse:
    st: AppState = request.app.state.nevertwice
    fact_type = request.query_params.get("type") or None
    if fact_type not in (None, "world", "experience", "observation"):
        return _bad_request("type must be world, experience or observation.")
    q = (request.query_params.get("q") or "").strip()[:200] or None
    try:
        return JSONResponse(await st.memory.list_memories(fact_type, q))
    except HindsightError as exc:
        return _bad_request(friendly_error(exc), 502)


async def refresh_model(request: Request) -> JSONResponse:
    st: AppState = request.app.state.nevertwice
    model_id = request.path_params["model_id"]
    try:
        resp = await st.hindsight.refresh_mental_model(st.settings.bank_id, model_id)
    except HindsightError as exc:
        return _bad_request(friendly_error(exc), 502)
    st.memory._models_cache = None
    return JSONResponse({"ok": True, "response": resp})


async def learning(request: Request) -> JSONResponse:
    st: AppState = request.app.state.nevertwice
    entries = st.store.triage_log()
    score = {"yes": 1.0, "partly": 0.5, "no": 0.0}
    summary: dict[str, dict[str, Any]] = {}
    for mode in ("baseline", "memory"):
        rows = [e for e in entries if e.get("mode") == mode]
        rated = [e for e in rows if e.get("feedback") in score]
        summary[mode] = {
            "triages": len(rows),
            "rated": len(rated),
            "accuracy": round(sum(score[e["feedback"]] for e in rated) / len(rated), 3) if rated else None,
            "avg_memories": round(sum(e.get("memories_recalled") or 0 for e in rows) / len(rows), 1) if rows else 0,
            "high_confidence": sum(1 for e in rows if e.get("confidence") == "high"),
        }
    eval_path = st.settings.runtime_dir / "eval_results.json"
    evaluation = None
    if eval_path.exists():
        try:
            evaluation = json.loads(eval_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            evaluation = None
    quality = None
    quality_path = st.settings.runtime_dir / "alert_eval.json"
    if quality_path.exists():
        try:
            quality = json.loads(quality_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            quality = None
    return JSONResponse({"entries": entries[-100:], "summary": summary, "evaluation": evaluation, "quality": quality})


# --------------------------------------------------------------------- app
def create_app(
    settings: Settings | None = None,
    *,
    hindsight: HindsightClient | None = None,
    llm: LLMClient | None = None,
) -> Starlette:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        owned: list[Any] = []
        hs = hindsight
        if hs is None:
            hs = HindsightClient(settings.hindsight_base_url, settings.hindsight_api_key)
            owned.append(hs)
        lm = llm
        if lm is None:
            lm = LLMClient(
                settings.llm_base_url, settings.llm_api_key, settings.llm_model,
                fallback_model=settings.llm_fallback_model, reasoning_effort=settings.llm_reasoning_effort,
            )
            owned.append(lm)
        app.state.nevertwice = AppState(settings, hs, lm)
        for problem in settings.problems():
            log.warning("Config: %s", problem)
        try:
            yield
        finally:
            for client in owned:
                await client.aclose()

    routes = [
        Route("/", index),
        Route("/api/status", status),
        Route("/api/alerts", alerts),
        Route("/api/incidents", incidents),
        Route("/api/incidents/{incident_id}", incident_detail),
        Route("/api/triage", triage, methods=["POST"]),
        Route("/api/resolve", resolve, methods=["POST"]),
        Route("/api/ask", ask, methods=["POST"]),
        Route("/api/memory/overview", memory_overview),
        Route("/api/memory/list", memory_list),
        Route("/api/memory/mental-models/{model_id}/refresh", refresh_model, methods=["POST"]),
        Route("/api/learning", learning),
        Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
    ]
    return Starlette(routes=routes, lifespan=lifespan)
