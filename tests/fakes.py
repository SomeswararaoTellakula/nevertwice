"""Test doubles: an in-memory Hindsight server and a scriptable OpenAI-compatible LLM.

FakeHindsight implements the subset of the Hindsight HTTP API that NeverTwice uses and
returns only fields documented in the API reference, so tests fail if the app
starts relying on anything undocumented.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Callable

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

TOKEN_RE = re.compile(r"[a-z0-9_:-]{3,}")


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


class FakeHindsight:
    def __init__(self, *, strict_include: bool = False, fail_recall_tags: bool = False, server_ids: bool = False) -> None:
        self.banks: dict[str, dict[str, Any]] = {}
        self.memories: dict[str, list[dict[str, Any]]] = {}
        self.models: dict[str, dict[str, dict[str, Any]]] = {}
        self.directives: dict[str, list[dict[str, Any]]] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str, Any]] = []
        self.strict_include = strict_include
        self.fail_recall_tags = fail_recall_tags
        self.server_ids = server_ids  # ignore client-supplied mental model ids, like a server that assigns its own
        self.app = Starlette(routes=self._routes())

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self.app)

    # ------------------------------------------------------------ helpers
    def _op(self, bank: str, kind: str) -> str:
        op_id = str(uuid.uuid4())
        self.operations[op_id] = {"id": op_id, "type": kind, "status": "pending", "polls": 0, "bank": bank}
        return op_id

    async def _log(self, request: Request) -> Any:
        body = None
        if request.method in ("POST", "PUT", "PATCH"):
            raw = await request.body()
            body = json.loads(raw) if raw else None
        self.requests.append((request.method, request.url.path, body))
        return body

    def _routes(self) -> list[Route]:
        b = "/v1/default/banks/{bank}"
        return [
            Route("/v1/default/banks", self.list_banks, methods=["GET"]),
            Route(b, self.put_bank, methods=["PUT"]),
            Route(b + "/config", self.patch_config, methods=["PATCH"]),
            Route(b + "/stats", self.stats, methods=["GET"]),
            Route(b + "/memories", self.retain, methods=["POST"]),
            Route(b + "/memories", self.clear, methods=["DELETE"]),
            Route(b + "/memories/recall", self.recall, methods=["POST"]),
            Route(b + "/memories/list", self.list_memories, methods=["GET"]),
            Route(b + "/reflect", self.reflect, methods=["POST"]),
            Route(b + "/mental-models", self.list_models, methods=["GET"]),
            Route(b + "/mental-models", self.create_model, methods=["POST"]),
            Route(b + "/mental-models/{mid}", self.get_model, methods=["GET"]),
            Route(b + "/mental-models/{mid}/refresh", self.refresh_model, methods=["POST"]),
            Route(b + "/directives", self.list_directives, methods=["GET"]),
            Route(b + "/directives", self.create_directive, methods=["POST"]),
            Route(b + "/operations/{op}", self.get_operation, methods=["GET"]),
        ]

    def _bank_or_404(self, bank: str) -> JSONResponse | None:
        if bank not in self.banks:
            return JSONResponse({"detail": f"Bank {bank} not found"}, status_code=404)
        return None

    # ------------------------------------------------------------ banks
    async def list_banks(self, request: Request) -> JSONResponse:
        await self._log(request)
        return JSONResponse({"banks": [{"bank_id": k, **v} for k, v in self.banks.items()]})

    async def put_bank(self, request: Request) -> JSONResponse:
        body = await self._log(request) or {}
        bank = request.path_params["bank"]
        if "disposition" in body:
            return JSONResponse({"detail": [{"loc": ["body", "disposition"], "msg": "extra fields not permitted"}]}, status_code=422)
        self.banks.setdefault(bank, {}).update(body)
        self.memories.setdefault(bank, [])
        return JSONResponse({"bank_id": bank, **self.banks[bank]})

    async def patch_config(self, request: Request) -> JSONResponse:
        body = await self._log(request) or {}
        bank = request.path_params["bank"]
        if (resp := self._bank_or_404(bank)) is not None:
            return resp
        if "updates" not in body:
            return JSONResponse({"detail": [{"loc": ["body", "updates"], "msg": "Field required"}]}, status_code=422)
        self.banks[bank].setdefault("config", {}).update(body["updates"])
        return JSONResponse({"bank_id": bank, "config": self.banks[bank]["config"]})

    async def stats(self, request: Request) -> JSONResponse:
        await self._log(request)
        bank = request.path_params["bank"]
        if (resp := self._bank_or_404(bank)) is not None:
            return resp
        return JSONResponse({"bank_id": bank, "total_nodes": len(self.memories[bank])})

    # ----------------------------------------------------------- memories
    async def retain(self, request: Request) -> JSONResponse:
        body = await self._log(request) or {}
        bank = request.path_params["bank"]
        self.banks.setdefault(bank, {})  # Hindsight auto-creates banks on first write
        store = self.memories.setdefault(bank, [])
        items = body.get("items")
        if not isinstance(items, list) or not items:
            return JSONResponse({"detail": [{"loc": ["body", "items"], "msg": "Field required"}]}, status_code=422)
        for item in items:
            if not isinstance(item.get("content"), str) or not item["content"].strip():
                return JSONResponse({"detail": [{"loc": ["body", "items", "content"], "msg": "required"}]}, status_code=422)
            meta = item.get("metadata")
            if meta is not None and not all(isinstance(v, str) for v in meta.values()):
                return JSONResponse({"detail": [{"loc": ["body", "items", "metadata"], "msg": "str values"}]}, status_code=422)
            doc = item.get("document_id")
            if doc:
                store[:] = [m for m in store if m["document_id"] != doc]
            for sentence in [s for s in re.split(r"(?<=[.!?])\s+|\n", item["content"]) if len(s.strip()) > 15]:
                store.append({
                    "id": str(uuid.uuid4()), "text": sentence.strip(), "type": "world",
                    "context": item.get("context", ""), "document_id": doc or "",
                    "tags": item.get("tags", []), "occurred_start": item.get("timestamp"),
                    "metadata": meta or {},
                })
        response: dict[str, Any] = {"success": True, "bank_id": bank, "items_count": len(items), "async": bool(body.get("async"))}
        if body.get("async"):
            response["operation_id"] = self._op(bank, "retain")
        return JSONResponse(response)

    async def clear(self, request: Request) -> JSONResponse:
        await self._log(request)
        self.memories[request.path_params["bank"]] = []
        return JSONResponse({"success": True})

    def _match(self, bank: str, query: str, tags: list[str] | None, tags_match: str, types: list[str] | None) -> list[dict[str, Any]]:
        q = _tokens(query)
        scored = []
        for mem in self.memories.get(bank, []):
            if types and mem["type"] not in types:
                continue
            if tags:
                have = set(mem["tags"])
                ok = have.issuperset(tags) if tags_match.startswith("all") else bool(have & set(tags))
                if not ok:
                    continue
            score = len(q & _tokens(mem["text"] + " " + mem["context"]))
            if score:
                scored.append((score, mem))
        scored.sort(key=lambda x: -x[0])
        return [m for _, m in scored]

    @staticmethod
    def _public(mem: dict[str, Any]) -> dict[str, Any]:
        # Only fields documented for RecallResult.
        return {
            "id": mem["id"], "text": mem["text"], "type": mem["type"], "entities": [],
            "context": mem["context"], "chunk_id": None, "occurred_start": mem["occurred_start"], "occurred_end": None,
        }

    async def recall(self, request: Request) -> JSONResponse:
        body = await self._log(request) or {}
        bank = request.path_params["bank"]
        if (resp := self._bank_or_404(bank)) is not None:
            return resp
        if not body.get("query"):
            return JSONResponse({"detail": [{"loc": ["body", "query"], "msg": "Field required"}]}, status_code=422)
        if self.fail_recall_tags and body.get("tags"):
            return JSONResponse({"detail": "tag index unavailable"}, status_code=500)
        hits = self._match(bank, body["query"], body.get("tags"), body.get("tags_match", "any"), body.get("types"))
        limit = max(1, int(body.get("max_tokens", 4096)) // 150)
        return JSONResponse({"results": [self._public(m) for m in hits[:limit]], "entities": {}, "chunks": {}})

    async def list_memories(self, request: Request) -> JSONResponse:
        await self._log(request)
        bank = request.path_params["bank"]
        if (resp := self._bank_or_404(bank)) is not None:
            return resp
        rows = self.memories.get(bank, [])
        ftype = request.query_params.get("type")
        if ftype:
            rows = [m for m in rows if m["type"] == ftype]
        q = request.query_params.get("q")
        if q:
            rows = [m for m in rows if q.lower() in m["text"].lower()]
        limit = int(request.query_params.get("limit", 100))
        offset = int(request.query_params.get("offset", 0))
        items = [
            {"id": m["id"], "text": m["text"], "fact_type": m["type"], "entities": [], "context": m["context"],
             "date": m["occurred_start"], "state": "valid", "tags": m["tags"], "metadata": m["metadata"]}
            for m in rows[offset: offset + limit]
        ]
        return JSONResponse({"items": items, "total": len(rows), "limit": limit, "offset": offset})

    async def reflect(self, request: Request) -> JSONResponse:
        body = await self._log(request) or {}
        bank = request.path_params["bank"]
        if (resp := self._bank_or_404(bank)) is not None:
            return resp
        include = body.get("include")
        if self.strict_include and include is not None:
            return JSONResponse({"detail": [{"loc": ["body", "include"], "msg": "not supported"}]}, status_code=422)
        if include is not None and not isinstance(include.get("facts", {}), dict):
            return JSONResponse({"detail": [{"loc": ["body", "include", "facts"], "msg": "Input should be an object"}]}, status_code=422)
        hits = self._match(bank, body["query"], body.get("tags"), "any", None)[:5]
        text = "Based on memory: " + " ".join(h["text"] for h in hits[:3]) if hits else "I have no relevant memories."
        out: dict[str, Any] = {"text": text}
        if include is not None:
            out["based_on"] = {
                "memories": [{"id": h["id"], "text": h["text"], "type": h["type"], "context": h["context"], "occurred_start": h["occurred_start"]} for h in hits],
                "mental_models": [{"id": m, "text": v["content"][:50]} for m, v in list(self.models.get(bank, {}).items())[:1]],
                "directives": [{"id": d["id"], "name": d["name"], "content": d["content"]} for d in self.directives.get(bank, [])],
            }
        return JSONResponse(out)

    # ------------------------------------------------------- mental models
    async def list_models(self, request: Request) -> JSONResponse:
        await self._log(request)
        bank = request.path_params["bank"]
        items = [{k: v for k, v in m.items() if k != "content"} for m in self.models.get(bank, {}).values()]
        return JSONResponse({"items": items, "total": len(items), "limit": 100, "offset": 0})

    def _generate(self, bank: str, model: dict[str, Any]) -> None:
        hits = self._match(bank, model["source_query"], None, "any", None)[:8]
        model["content"] = "\n".join(f"- {h['text']} ({h['context']})" for h in hits) or "No data yet."
        model["last_refreshed_at"] = "2026-09-28T10:00:00+05:30"

    async def create_model(self, request: Request) -> JSONResponse:
        body = await self._log(request) or {}
        bank = request.path_params["bank"]
        if not body.get("name") or not body.get("source_query"):
            return JSONResponse({"detail": [{"loc": ["body", "source_query"], "msg": "Field required"}]}, status_code=422)
        mid = (None if self.server_ids else body.get("id")) or str(uuid.uuid4())
        model = {"id": mid, "bank_id": bank, "name": body["name"], "source_query": body["source_query"], "tags": body.get("tags", []), "trigger": body.get("trigger", {})}
        self._generate(bank, model)
        self.models.setdefault(bank, {})[mid] = model
        return JSONResponse({"operation_id": self._op(bank, "mental_model"), "status": "pending"})

    async def get_model(self, request: Request) -> JSONResponse:
        await self._log(request)
        model = self.models.get(request.path_params["bank"], {}).get(request.path_params["mid"])
        if not model:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return JSONResponse(model)

    async def refresh_model(self, request: Request) -> JSONResponse:
        await self._log(request)
        bank = request.path_params["bank"]
        model = self.models.get(bank, {}).get(request.path_params["mid"])
        if not model:
            return JSONResponse({"detail": "not found"}, status_code=404)
        self._generate(bank, model)
        return JSONResponse({"operation_id": self._op(bank, "refresh")})

    # ---------------------------------------------------------- directives
    async def list_directives(self, request: Request) -> JSONResponse:
        await self._log(request)
        return JSONResponse({"items": self.directives.get(request.path_params["bank"], [])})

    async def create_directive(self, request: Request) -> JSONResponse:
        body = await self._log(request) or {}
        bank = request.path_params["bank"]
        d = {"id": str(uuid.uuid4()), "name": body["name"], "content": body["content"], "is_active": body.get("is_active", True)}
        self.directives.setdefault(bank, []).append(d)
        return JSONResponse(d)

    # ---------------------------------------------------------- operations
    async def get_operation(self, request: Request) -> JSONResponse:
        await self._log(request)
        op = self.operations.get(request.path_params["op"])
        if not op:
            return JSONResponse({"detail": "not found"}, status_code=404)
        op["polls"] += 1
        if op["polls"] >= 2:
            op["status"] = "completed"
        return JSONResponse({k: v for k, v in op.items() if k not in ("polls", "bank")})


# ---------------------------------------------------------------- fake LLM
def tool_call_response(name: str, arguments: dict[str, Any] | str, model: str = "fake-model") -> dict[str, Any]:
    args = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {
        "id": "chatcmpl-1", "model": model,
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": f"call_{uuid.uuid4().hex[:6]}", "type": "function", "function": {"name": name, "arguments": args}}],
        }}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def text_response(content: str, model: str = "fake-model") -> dict[str, Any]:
    return {
        "id": "chatcmpl-2", "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
        "usage": {},
    }


def smart_triage(body: dict[str, Any]) -> dict[str, Any]:
    """Default behaviour: cite whatever incident memory it was given, like a good model would."""
    user = next((m["content"] for m in body["messages"] if m["role"] == "user"), "")
    has_memory = "INCIDENT MEMORY" in user
    incidents = list(dict.fromkeys(re.findall(r"INC-\d{4}", user)))
    refs = list(dict.fromkeys(re.findall(r"\[(M\d+)\]", user)))
    if has_memory and incidents:
        args = {
            "summary": f"Matches {incidents[0]}.",
            "likely_root_cause": f"Same failure mode as {incidents[0]}.",
            "confidence": "high",
            "recommended_actions": [
                {"title": "Check pg_stat_activity for idle in transaction sessions", "command": "psql -c 'select 1'", "why": "verify", "evidence": [refs[0] if refs else incidents[0]]},
                {"title": "Suspend inventory-reconcile and pg_terminate_backend idle sessions", "command": "kubectl patch cronjob inventory-reconcile", "why": f"Worked in {incidents[0]}", "evidence": [incidents[0]]},
            ],
            "avoid_actions": [{"title": "Rolling restart", "why": "Relapsed before", "evidence": [incidents[0], "INC-9999"]}],
            "similar_incidents": [{"incident_id": i, "similarity": "same signature"} for i in incidents[:2]],
            "escalate_to": "Priya Raman",
        }
    else:
        args = {
            "summary": "Generic triage.",
            "likely_root_cause": "Database connection pressure.",
            "confidence": "low",
            "recommended_actions": [{"title": "Restart the service", "why": "general guidance"}, {"title": "Scale out pods", "why": "more capacity"}],
            "avoid_actions": [],
        }
    if body.get("response_format"):
        return text_response(json.dumps(args), model=body["model"])
    return tool_call_response("submit_triage", args, model=body["model"])


class FakeLLM:
    """Scriptable chat-completions endpoint. Queue behaviours; otherwise answer smartly."""

    def __init__(self) -> None:
        self.script: list[Callable[[dict[str, Any]], httpx.Response]] = []
        self.requests: list[dict[str, Any]] = []

    def queue(self, *handlers: Callable[[dict[str, Any]], httpx.Response]) -> None:
        self.script.extend(handlers)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.script:
            return self.script.pop(0)(body)
        return httpx.Response(200, json=smart_triage(body))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def status(code: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> Callable[[dict[str, Any]], httpx.Response]:
    return lambda _body: httpx.Response(code, json=payload, headers=headers or {})


def ok(payload_fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], httpx.Response]:
    return lambda body: httpx.Response(200, json=payload_fn(body))
