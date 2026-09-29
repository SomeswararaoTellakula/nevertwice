"""OpenAI-compatible chat client (Groq by default) hardened for tool calling.

Free-tier LLM APIs fail in predictable ways during a live demo. This client turns
each of them into a recoverable event instead of a crash:

* 429 rate limits  -> wait for ``retry-after`` (bounded), then switch to the fallback model
* 5xx / timeouts   -> exponential backoff, then the fallback model
* 413 / too large  -> ``LLMError(kind="too_large")`` so the caller can shrink its context
* ``tool_use_failed`` (Groq) -> ``LLMError`` carrying ``failed_generation`` for salvage
* unsupported optional params -> retried without them
* malformed tool-call JSON -> repaired where possible, otherwise reported per call
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx

log = logging.getLogger("nevertwice.llm")

Emit = Callable[[dict[str, Any]], Awaitable[None]]

OPTIONAL_PARAMS = ("reasoning_effort", "max_completion_tokens", "response_format", "tool_choice", "parallel_tool_calls")
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
FALLBACK_REASON = {
    "rate_limit": "hit its rate limit",
    "tool_use_failed": "returned a malformed tool call",
    "server": "had a server error",
    "network": "was unreachable",
    "empty": "returned an empty answer",
    "bad_request": "rejected the request",
}


class LLMError(Exception):
    """kind: auth | rate_limit | too_large | tool_use_failed | bad_request | server | network | empty"""

    def __init__(
        self, message: str, kind: str, *, status: int | None = None, failed_generation: str | None = None, model: str = ""
    ):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.failed_generation = failed_generation
        self.model = model

    @property
    def can_fallback(self) -> bool:
        return self.kind in {"rate_limit", "server", "network", "tool_use_failed", "empty", "bad_request"}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    parse_error: str | None = None


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0

    def assistant_message(self) -> dict[str, Any]:
        """The message to append to the conversation before tool results."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.raw_arguments or json.dumps(call.arguments)},
                }
                for call in self.tool_calls
            ]
        return msg


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        fallback_model: str = "",
        reasoning_effort: str = "",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_rate_limit_wait: float = 25.0,
    ) -> None:
        self.model = model
        self.fallback_model = fallback_model if fallback_model and fallback_model != model else ""
        self.reasoning_effort = reasoning_effort
        self.max_rate_limit_wait = max_rate_limit_wait
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=10.0),
            transport=transport,
        )
        self._unsupported: set[str] = set()

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def models(self) -> list[str]:
        return [m for m in (self.model, self.fallback_model) if m]

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        json_mode: bool = False,
        max_tokens: int = 1500,
        temperature: float = 0.2,
        emit: Emit | None = None,
        models: list[str] | None = None,
        fallback_on_tool_error: bool = True,
    ) -> LLMResponse:
        """Call the model, falling back to the next model on recoverable errors.

        `models` overrides the model order for this call. With `fallback_on_tool_error=False`, a malformed
        tool call is raised immediately so the caller can salvage the model's work before trying another model.
        """
        order = [m for m in (models or self.models) if m]
        last: LLMError | None = None
        for index, model in enumerate(order):
            try:
                return await self._chat_model(
                    model, messages, tools=tools, tool_choice=tool_choice, json_mode=json_mode,
                    max_tokens=max_tokens, temperature=temperature, emit=emit,
                )
            except LLMError as exc:
                exc.model = exc.model or model
                last = exc
                has_next = index + 1 < len(order)
                if exc.kind == "tool_use_failed" and not fallback_on_tool_error:
                    raise
                if not (exc.can_fallback and has_next):
                    raise
                if emit:
                    await emit({"type": "warning", "message": f"{model} {FALLBACK_REASON.get(exc.kind, 'failed')}; retried with {order[index + 1]}."})
                log.warning("LLM %s failed (%s), falling back", model, exc)
        assert last is not None
        raise last

    async def _chat_model(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
        json_mode: bool,
        max_tokens: int,
        temperature: float,
        emit: Emit | None,
    ) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice or "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.reasoning_effort and "gpt-oss" in model:
            body["reasoning_effort"] = self.reasoning_effort
        for name in self._unsupported:
            _drop_param(body, name)

        attempts = 0
        server_retries = 0
        rate_waited = 0.0
        while True:
            attempts += 1
            started = time.perf_counter()
            try:
                resp = await self._http.post("/chat/completions", json=body)
            except httpx.TimeoutException as exc:
                if server_retries < 2:
                    server_retries += 1
                    await asyncio.sleep(1.5 * server_retries)
                    continue
                raise LLMError(f"LLM request timed out ({exc})", "network") from exc
            except httpx.TransportError as exc:
                if server_retries < 2:
                    server_retries += 1
                    await asyncio.sleep(1.5 * server_retries)
                    continue
                raise LLMError(f"Cannot reach the LLM API: {exc}", "network") from exc

            latency_ms = int((time.perf_counter() - started) * 1000)
            if resp.status_code == 200:
                return _parse_response(resp, model, latency_ms)

            error = _error_payload(resp)
            message = str(error.get("message") or resp.text[:300])
            code = str(error.get("code") or "")

            if resp.status_code in (401, 403):
                raise LLMError("LLM API key was rejected — check LLM_API_KEY.", "auth", status=resp.status_code)

            if resp.status_code == 429:
                wait = _retry_after_seconds(resp, message)
                if rate_waited + wait <= self.max_rate_limit_wait:
                    rate_waited += wait
                    if emit:
                        await emit({"type": "warning", "message": f"Rate limited by {model}; retrying in {wait:.0f}s"})
                    await asyncio.sleep(wait)
                    continue
                raise LLMError(f"Rate limit reached for {model}", "rate_limit", status=429)

            if resp.status_code == 413 or "context_length" in code or "too large" in message.lower():
                raise LLMError(f"Request too large for {model}: {message}", "too_large", status=resp.status_code)

            if resp.status_code == 400 and (code == "tool_use_failed" or "failed_generation" in error):
                raise LLMError(
                    f"Model produced an invalid tool call: {message}",
                    "tool_use_failed",
                    status=400,
                    failed_generation=str(error.get("failed_generation") or ""),
                )

            if resp.status_code == 400:
                unsupported = _find_unsupported_param(body, message)
                if unsupported and attempts <= len(OPTIONAL_PARAMS) + 1:
                    log.info("Dropping unsupported LLM parameter %s for %s", unsupported, model)
                    self._unsupported.add(unsupported)
                    _drop_param(body, unsupported)
                    continue
                raise LLMError(f"LLM rejected the request: {message}", "bad_request", status=400)

            if resp.status_code >= 500 or resp.status_code == 408:
                if server_retries < 2:
                    server_retries += 1
                    await asyncio.sleep(1.5 * server_retries)
                    continue
                raise LLMError(f"LLM server error {resp.status_code}: {message}", "server", status=resp.status_code)

            raise LLMError(f"LLM error {resp.status_code}: {message}", "bad_request", status=resp.status_code)


# ----------------------------------------------------------------- helpers
def _parse_response(resp: httpx.Response, model: str, latency_ms: int) -> LLMResponse:
    try:
        data = resp.json()
        choice = data["choices"][0]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"Unexpected LLM response shape: {resp.text[:200]}", "server") from exc
    message = choice.get("message") or {}
    content = strip_thinking(message.get("content") or "")
    calls: list[ToolCall] = []
    for i, raw in enumerate(message.get("tool_calls") or []):
        fn = raw.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        if isinstance(raw_args, dict):
            args, error, raw_text = raw_args, None, json.dumps(raw_args)
        else:
            raw_text = str(raw_args)
            args, error = parse_json_object(raw_text)
        calls.append(
            ToolCall(
                id=str(raw.get("id") or f"call_{i}"),
                name=str(fn.get("name") or ""),
                arguments=args or {},
                raw_arguments=raw_text,
                parse_error=error,
            )
        )
    if not content and not calls:
        raise LLMError("The model returned an empty response.", "empty")
    return LLMResponse(
        content=content,
        tool_calls=calls,
        model=str(data.get("model") or model),
        finish_reason=str(choice.get("finish_reason") or ""),
        usage=data.get("usage") or {},
        latency_ms=latency_ms,
    )


def strip_thinking(text: str) -> str:
    text = THINK_RE.sub("", text or "")
    if "<think>" in text and "</think>" not in text:  # truncated reasoning block
        text = text.split("<think>", 1)[0]
    return text.strip()


def parse_json_object(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a JSON object from model output, tolerating code fences, prose and trailing commas."""
    if not text or not text.strip():
        return None, "empty arguments"
    candidate = strip_thinking(text).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()
    attempts = [candidate]
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        attempts.append(candidate[start : end + 1])
    last_error = "no JSON object found"
    for attempt in attempts:
        for variant in (attempt, re.sub(r",\s*([}\]])", r"\1", attempt)):
            try:
                value = json.loads(variant)
            except json.JSONDecodeError as exc:
                last_error = str(exc)
                continue
            if isinstance(value, dict):
                return value, None
            last_error = "JSON is not an object"
    return None, last_error


def salvage_tool_call(
    failed_generation: str, tool_name: str, accept: Callable[[dict[str, Any]], bool] | None = None
) -> dict[str, Any] | None:
    """Recover the arguments of `tool_name` from Groq's `failed_generation` text.

    Models sometimes put the right arguments under the wrong tool name (gpt-oss has been seen calling a
    non-existent `json` tool). `accept` lets the caller recognise its payload whatever the tool was called.
    """
    if not failed_generation:
        return None
    obj, _ = parse_json_object(failed_generation)
    if not obj:
        return None
    candidates: list[Any] = [obj]
    if isinstance(obj.get("tool_calls"), list):
        candidates.extend(obj["tool_calls"])
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        fn = cand.get("function") if isinstance(cand.get("function"), dict) else cand
        name = fn.get("name")
        args = fn.get("arguments", fn.get("parameters"))
        if args is None:
            continue
        if isinstance(args, str):
            args, _ = parse_json_object(args)
        if isinstance(args, dict) and (name == tool_name or (accept is not None and accept(args))):
            return args
    if accept is not None and accept(obj):
        return obj
    if accept is None and "name" not in obj and ("summary" in obj or "likely_root_cause" in obj):
        return obj  # the model emitted the arguments directly
    return None


def _error_payload(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {"message": resp.text[:300]}
    if isinstance(data, dict):
        err = data.get("error", data)
        if isinstance(err, dict):
            return err
        return {"message": str(err)}
    return {"message": str(data)[:300]}


def _retry_after_seconds(resp: httpx.Response, message: str) -> float:
    header = resp.headers.get("retry-after")
    if header:
        try:
            return max(1.0, min(float(header), 30.0))
        except ValueError:
            pass
    match = re.search(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", message)
    if match:
        minutes = float(match.group(1) or 0)
        value = float(match.group(2))
        seconds = value / 1000 if match.group(3) == "ms" else value
        return max(1.0, min(minutes * 60 + seconds, 30.0))
    return 5.0


def _find_unsupported_param(body: dict[str, Any], message: str) -> str | None:
    lowered = message.lower()
    for name in OPTIONAL_PARAMS:
        if name in body and name in lowered:
            return name
    return None


def _drop_param(body: dict[str, Any], name: str) -> None:
    if name not in body:
        return
    value = body.pop(name)
    if name == "max_completion_tokens":
        body["max_tokens"] = value
