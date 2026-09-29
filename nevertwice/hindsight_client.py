"""Async client for the Hindsight HTTP API (https://hindsight.vectorize.io/api-reference).

Hindsight gives NeverTwice its long-term memory:

* ``retain``  – store incident write-ups; Hindsight extracts facts, entities and time.
* ``recall``  – multi-strategy search (semantic + keyword + graph + temporal).
* ``reflect`` – an agentic answer grounded in mental models, observations and facts.
* mental models / directives – living runbooks and hard rules for the bank.

The client talks to the REST API directly with ``httpx`` so it works identically
against Hindsight Cloud and a self-hosted server. Optional request features that
differ between server versions are sent first and dropped automatically when the
server rejects them with a validation error (HTTP 422).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import quote

import httpx

log = logging.getLogger("nevertwice.hindsight")

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
TERMINAL_OPERATION_STATES = {"completed", "failed", "cancelled", "succeeded", "error"}


class HindsightError(Exception):
    """Raised when Hindsight cannot be reached or returns an error."""

    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def _describe_error(resp: httpx.Response) -> str:
    detail: Any
    try:
        payload = resp.json()
        detail = payload.get("detail") or payload.get("error") or payload.get("message") or payload
    except ValueError:
        detail = resp.text[:300]
    if isinstance(detail, list):  # FastAPI validation errors
        parts = []
        for item in detail[:3]:
            if isinstance(item, dict):
                loc = ".".join(str(p) for p in item.get("loc", []))
                parts.append(f"{loc}: {item.get('msg')}")
            else:
                parts.append(str(item))
        detail = "; ".join(parts)
    hint = ""
    if resp.status_code in (401, 403):
        hint = " Check HINDSIGHT_API_KEY."
    elif resp.status_code == 404:
        hint = " (not found)"
    return f"Hindsight {resp.request.method} {resp.request.url.path} failed with HTTP {resp.status_code}: {detail}{hint}"


class HindsightClient:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        timeout: float = 90.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 2,
    ) -> None:
        headers = {"Accept": "application/json", "User-Agent": "nevertwice/1.0"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "HindsightClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ core
    @staticmethod
    def bank_path(bank_id: str) -> str:
        return f"/v1/default/banks/{quote(bank_id, safe='')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
        timeout: float | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                kwargs: dict[str, Any] = {"json": json, "params": params}
                if timeout is not None:
                    kwargs["timeout"] = timeout
                resp = await self._http.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                last_error = HindsightError(f"Hindsight timed out on {method} {path}: {exc}")
            except httpx.TransportError as exc:
                last_error = HindsightError(f"Cannot reach Hindsight at {self.base_url}: {exc}")
            else:
                if resp.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                    delay = _retry_delay(resp, attempt)
                    log.warning("Hindsight %s %s -> %s, retrying in %.1fs", method, path, resp.status_code, delay)
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code >= 400:
                    body: Any
                    try:
                        body = resp.json()
                    except ValueError:
                        body = resp.text
                    raise HindsightError(_describe_error(resp), resp.status_code, body)
                if not resp.content:
                    return {}
                try:
                    return resp.json()
                except ValueError:
                    return {"raw": resp.text}
            if attempt < self.max_retries:
                await asyncio.sleep(0.8 * (2**attempt))
        assert last_error is not None
        raise last_error

    async def _request_with_fallbacks(self, method: str, path: str, bodies: Iterable[dict[str, Any]], **kw: Any) -> Any:
        """Try progressively simpler request bodies when the server rejects optional fields (422)."""
        error: HindsightError | None = None
        for body in bodies:
            try:
                return await self._request(method, path, json=body, **kw)
            except HindsightError as exc:
                if exc.status not in (400, 422):
                    raise
                error = exc
                log.info("Hindsight rejected optional fields on %s %s, retrying simpler body: %s", method, path, exc)
        assert error is not None
        raise error

    # --------------------------------------------------------------- server
    async def version(self) -> dict[str, Any]:
        return await self._request("GET", "/version")

    async def list_banks(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/v1/default/banks")
        if isinstance(data, dict):
            return list(data.get("banks") or data.get("items") or [])
        return list(data or [])

    # ---------------------------------------------------------------- banks
    async def upsert_bank(self, bank_id: str, **fields: Any) -> dict[str, Any]:
        """Create or update a bank (PUT). Unknown optional fields are dropped on 422."""
        clean = {k: v for k, v in fields.items() if v is not None}
        minimal = {k: clean[k] for k in ("name", "mission") if k in clean}
        bodies = [clean, {**clean, "bank_id": bank_id}, minimal]
        return await self._request_with_fallbacks("PUT", self.bank_path(bank_id), _dedupe(bodies))

    async def update_bank_config(self, bank_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        """Partially update bank configuration (missions, disposition, ...)."""
        path = f"{self.bank_path(bank_id)}/config"
        return await self._request_with_fallbacks("PATCH", path, [{"updates": updates}, updates])

    async def get_bank_config(self, bank_id: str) -> dict[str, Any]:
        return await self._request("GET", f"{self.bank_path(bank_id)}/config")

    async def stats(self, bank_id: str) -> dict[str, Any]:
        return await self._request("GET", f"{self.bank_path(bank_id)}/stats")

    async def clear_memories(self, bank_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"{self.bank_path(bank_id)}/memories")

    # --------------------------------------------------------------- memory
    async def retain(self, bank_id: str, items: list[dict[str, Any]], *, run_async: bool = False) -> dict[str, Any]:
        """Store memory items. Each item: content, context, timestamp, document_id, tags, metadata."""
        if not items:
            raise ValueError("retain() needs at least one item")
        payload_items = []
        for item in items:
            content = str(item.get("content", "")).strip()
            if not content:
                raise ValueError("every retained item needs non-empty 'content'")
            entry = {k: v for k, v in item.items() if v not in (None, "", [], {})}
            entry["content"] = content
            if "metadata" in entry:
                entry["metadata"] = {str(k): str(v) for k, v in entry["metadata"].items() if v is not None}
            payload_items.append(entry)
        body = {"items": payload_items, "async": run_async}
        without_metadata = {
            "items": [{k: v for k, v in it.items() if k != "metadata"} for it in payload_items],
            "async": run_async,
        }
        return await self._request_with_fallbacks(
            "POST", f"{self.bank_path(bank_id)}/memories", _dedupe([body, without_metadata]), timeout=180.0
        )

    async def recall(
        self,
        bank_id: str,
        query: str,
        *,
        types: list[str] | None = None,
        budget: str = "mid",
        max_tokens: int = 2048,
        tags: list[str] | None = None,
        tags_match: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": query, "budget": budget, "max_tokens": max_tokens}
        if types:
            body["types"] = types
        if tags:
            body["tags"] = tags
            if tags_match:
                body["tags_match"] = tags_match
        data = await self._request("POST", f"{self.bank_path(bank_id)}/memories/recall", json=body)
        if not isinstance(data, dict):
            return {"results": []}
        data.setdefault("results", [])
        return data

    async def reflect(
        self,
        bank_id: str,
        query: str,
        *,
        budget: str = "low",
        max_tokens: int | None = None,
        tags: list[str] | None = None,
        include_facts: bool = True,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        base: dict[str, Any] = {"query": query, "budget": budget}
        if max_tokens:
            base["max_tokens"] = max_tokens
        if tags:
            base["tags"] = tags
        full = dict(base)
        if include_facts:
            full["include"] = {"facts": {}}
        if response_schema:
            full["response_schema"] = response_schema
        bodies = [full]
        if include_facts:
            bodies.append({**full, "include": {"facts": True}})
        bodies.append(base)
        data = await self._request_with_fallbacks(
            "POST", f"{self.bank_path(bank_id)}/reflect", _dedupe(bodies), timeout=180.0
        )
        if not isinstance(data, dict):
            return {"text": str(data)}
        data.setdefault("text", "")
        return data

    async def list_memories(
        self,
        bank_id: str,
        *,
        fact_type: str | None = None,
        q: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        params: list[tuple[str, Any]] = [("limit", limit), ("offset", offset)]
        if fact_type:
            params.append(("type", fact_type))
        if q:
            params.append(("q", q))
        for tag in tags or []:
            params.append(("tags", tag))
        data = await self._request("GET", f"{self.bank_path(bank_id)}/memories/list", params=params)
        if not isinstance(data, dict):
            return {"items": [], "total": 0}
        data.setdefault("items", [])
        data.setdefault("total", len(data["items"]))
        return data

    # --------------------------------------------------------- mental models
    async def list_mental_models(self, bank_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"{self.bank_path(bank_id)}/mental-models", params={"limit": 100})
        if isinstance(data, dict):
            return list(data.get("items") or data.get("mental_models") or [])
        return list(data or [])

    async def get_mental_model(self, bank_id: str, model_id: str) -> dict[str, Any]:
        return await self._request("GET", f"{self.bank_path(bank_id)}/mental-models/{quote(model_id, safe='')}")

    async def create_mental_model(
        self,
        bank_id: str,
        *,
        name: str,
        source_query: str,
        model_id: str | None = None,
        tags: list[str] | None = None,
        max_tokens: int | None = None,
        refresh_after_consolidation: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "source_query": source_query}
        if model_id:
            body["id"] = model_id
        if tags:
            body["tags"] = tags
        if max_tokens:
            body["max_tokens"] = max_tokens
        full = {**body, "trigger": {"refresh_after_consolidation": refresh_after_consolidation}}
        no_id = {k: v for k, v in full.items() if k != "id"}
        return await self._request_with_fallbacks(
            "POST", f"{self.bank_path(bank_id)}/mental-models", _dedupe([full, body, no_id])
        )

    async def refresh_mental_model(self, bank_id: str, model_id: str) -> dict[str, Any]:
        return await self._request(
            "POST", f"{self.bank_path(bank_id)}/mental-models/{quote(model_id, safe='')}/refresh", json={}
        )

    async def delete_mental_model(self, bank_id: str, model_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"{self.bank_path(bank_id)}/mental-models/{quote(model_id, safe='')}")

    # ------------------------------------------------------------ directives
    async def list_directives(self, bank_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"{self.bank_path(bank_id)}/directives")
        if isinstance(data, dict):
            return list(data.get("items") or data.get("directives") or [])
        return list(data or [])

    async def create_directive(self, bank_id: str, *, name: str, content: str) -> dict[str, Any]:
        body = {"name": name, "content": content, "is_active": True}
        return await self._request_with_fallbacks(
            "POST", f"{self.bank_path(bank_id)}/directives", [body, {"name": name, "content": content}]
        )

    # ------------------------------------------------------------ operations
    async def get_operation(self, bank_id: str, operation_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"{self.bank_path(bank_id)}/operations/{quote(operation_id, safe='')}"
        )

    async def wait_for_operations(
        self,
        bank_id: str,
        operation_ids: Iterable[str],
        *,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
        on_progress: Callable[[int, int, list[dict[str, Any]]], Awaitable[None] | None] | None = None,
    ) -> list[dict[str, Any]]:
        """Poll async operations until all are terminal (or timeout). Returns final states."""
        pending = [op for op in dict.fromkeys(operation_ids) if op]
        finished: dict[str, dict[str, Any]] = {}
        total = len(pending)
        deadline = time.monotonic() + timeout
        while pending:
            still: list[str] = []
            for op_id in pending:
                try:
                    state = await self.get_operation(bank_id, op_id)
                except HindsightError as exc:
                    if exc.status == 404:  # already cleaned up => treat as completed
                        finished[op_id] = {"id": op_id, "status": "completed"}
                        continue
                    raise
                status = str(state.get("status", "")).lower()
                if status in TERMINAL_OPERATION_STATES:
                    finished[op_id] = state
                else:
                    still.append(op_id)
            pending = still
            if on_progress is not None:
                maybe = on_progress(total - len(pending), total, list(finished.values()))
                if asyncio.iscoroutine(maybe):
                    await maybe
            if not pending:
                break
            if time.monotonic() > deadline:
                raise HindsightError(
                    f"Timed out after {timeout:.0f}s waiting for {len(pending)} Hindsight operation(s) to finish."
                )
            await asyncio.sleep(poll_interval)
        return list(finished.values())


def operation_ids_from(response: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    if not isinstance(response, dict):
        return ids
    if response.get("operation_id"):
        ids.append(str(response["operation_id"]))
    for op in response.get("operation_ids") or []:
        if op:
            ids.append(str(op))
    return list(dict.fromkeys(ids))


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    header = resp.headers.get("retry-after")
    if header:
        try:
            return min(float(header), 20.0)
        except ValueError:
            pass
    return 1.0 * (2**attempt)


def _dedupe(bodies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    for body in bodies:
        if body not in seen:
            seen.append(body)
    return seen
