import asyncio
import unittest

import httpx

from nevertwice.llm import LLMClient, LLMError, parse_json_object, salvage_tool_call, strip_thinking
from tests.fakes import FakeLLM, ok, status, text_response, tool_call_response


def make_llm(fake: FakeLLM, **kw) -> LLMClient:
    return LLMClient("https://api.example.test/v1", "key", "main-model", fallback_model="backup-model",
                     reasoning_effort="low", transport=fake.transport(), max_rate_limit_wait=kw.pop("wait", 2), **kw)


class ParsingTests(unittest.TestCase):
    def test_parse_plain_and_fenced_json(self):
        self.assertEqual(parse_json_object('{"a": 1}')[0], {"a": 1})
        self.assertEqual(parse_json_object('```json\n{"a": 1,}\n```')[0], {"a": 1})
        self.assertEqual(parse_json_object('Sure! {"a": [1, 2,],}')[0], {"a": [1, 2]})
        self.assertIsNone(parse_json_object("no json here")[0])
        self.assertIsNone(parse_json_object("[1,2]")[0])

    def test_strip_thinking(self):
        self.assertEqual(strip_thinking("<think>hmm</think>Answer"), "Answer")
        self.assertEqual(strip_thinking("Answer<think>unterminated"), "Answer")

    def test_salvage_tool_call(self):
        gen = '{"name": "submit_triage", "arguments": {"summary": "x", "confidence": "high"}}'
        self.assertEqual(salvage_tool_call(gen, "submit_triage")["summary"], "x")
        gen2 = '<function=submit_triage>{"summary": "direct", "likely_root_cause": "y"}</function>'
        self.assertEqual(salvage_tool_call(gen2, "submit_triage")["summary"], "direct")
        gen3 = '{"name": "submit_triage", "arguments": "{\\"summary\\": \\"s\\"}"}'
        self.assertEqual(salvage_tool_call(gen3, "submit_triage")["summary"], "s")
        self.assertIsNone(salvage_tool_call("", "submit_triage"))


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_call_parsed_and_reasoning_effort_sent(self):
        fake = FakeLLM()
        fake.queue(ok(lambda b: tool_call_response("submit_triage", {"summary": "ok"})))
        llm = make_llm(fake)
        resp = await llm.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "submit_triage"}}])
        self.assertEqual(resp.tool_calls[0].name, "submit_triage")
        self.assertEqual(resp.tool_calls[0].arguments, {"summary": "ok"})
        self.assertNotIn("reasoning_effort", fake.requests[0])  # only sent to gpt-oss models
        await llm.aclose()

    async def test_reasoning_effort_for_gpt_oss(self):
        fake = FakeLLM()
        fake.queue(ok(lambda b: text_response("hello")))
        llm = LLMClient("https://x/v1", "k", "openai/gpt-oss-120b", reasoning_effort="low", transport=fake.transport())
        await llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(fake.requests[0]["reasoning_effort"], "low")
        await llm.aclose()

    async def test_rate_limit_retry_then_success(self):
        fake = FakeLLM()
        fake.queue(status(429, {"error": {"message": "Rate limit reached. Please try again in 1.2s."}}, {"retry-after": "1"}),
                   ok(lambda b: text_response("done")))
        events = []

        async def emit(e):
            events.append(e)

        llm = make_llm(fake)
        resp = await llm.chat([{"role": "user", "content": "hi"}], emit=emit)
        self.assertEqual(resp.content, "done")
        self.assertTrue(any("Rate limited" in e.get("message", "") for e in events))
        await llm.aclose()

    async def test_rate_limit_falls_back_to_second_model(self):
        fake = FakeLLM()
        limit = status(429, {"error": {"message": "try again in 30s"}}, {"retry-after": "30"})
        fake.queue(limit, ok(lambda b: text_response(f"from {b['model']}")))
        llm = make_llm(fake, wait=1)
        resp = await llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content, "from backup-model")
        await llm.aclose()

    async def test_unsupported_param_is_dropped(self):
        fake = FakeLLM()
        fake.queue(status(400, {"error": {"message": "property 'max_completion_tokens' is unsupported"}}),
                   ok(lambda b: text_response("fine")))
        llm = make_llm(fake)
        resp = await llm.chat([{"role": "user", "content": "hi"}], max_tokens=321)
        self.assertEqual(resp.content, "fine")
        self.assertEqual(fake.requests[1]["max_tokens"], 321)
        self.assertNotIn("max_completion_tokens", fake.requests[1])
        await llm.aclose()

    async def test_auth_error_is_not_retried(self):
        fake = FakeLLM()
        fake.queue(status(401, {"error": {"message": "Invalid API Key"}}))
        llm = make_llm(fake)
        with self.assertRaises(LLMError) as ctx:
            await llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.kind, "auth")
        self.assertEqual(len(fake.requests), 1)
        await llm.aclose()

    async def test_tool_use_failed_carries_generation(self):
        fake = FakeLLM()
        bad = status(400, {"error": {"message": "Failed to call a function", "code": "tool_use_failed", "failed_generation": "{bad"}})
        fake.queue(bad, bad)
        llm = make_llm(fake)
        with self.assertRaises(LLMError) as ctx:
            await llm.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "t"}}])
        self.assertEqual(ctx.exception.kind, "tool_use_failed")
        self.assertEqual(ctx.exception.failed_generation, "{bad")
        await llm.aclose()

    async def test_tool_error_can_be_raised_without_fallback(self):
        fake = FakeLLM()
        bad = status(400, {"error": {"message": "Failed to call a function", "code": "tool_use_failed", "failed_generation": "{}"}})
        fake.queue(bad)
        llm = make_llm(fake)
        with self.assertRaises(LLMError) as ctx:
            await llm.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function", "function": {"name": "t"}}], fallback_on_tool_error=False)
        self.assertEqual(ctx.exception.model, "main-model")
        self.assertEqual(len(fake.requests), 1)
        await llm.aclose()

    async def test_models_override(self):
        fake = FakeLLM()
        fake.queue(ok(lambda b: text_response(b["model"])))
        llm = make_llm(fake)
        resp = await llm.chat([{"role": "user", "content": "hi"}], models=["backup-model"])
        self.assertEqual(resp.content, "backup-model")
        await llm.aclose()

    def test_salvage_accepts_misnamed_tool(self):
        gen = '{"name": "json", "arguments": {"summary": "s", "recommended_actions": []}}'
        accept = lambda d: "summary" in d  # noqa: E731
        self.assertIsNone(salvage_tool_call(gen, "submit_triage"))
        self.assertEqual(salvage_tool_call(gen, "submit_triage", accept=accept)["summary"], "s")

    async def test_too_large_is_reported(self):
        fake = FakeLLM()
        fake.queue(status(413, {"error": {"message": "Request too large for model"}}))
        llm = make_llm(fake)
        with self.assertRaises(LLMError) as ctx:
            await llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.kind, "too_large")
        await llm.aclose()

    async def test_server_error_retries(self):
        fake = FakeLLM()
        fake.queue(status(503, {"error": {"message": "overloaded"}}), ok(lambda b: text_response("recovered")))
        llm = make_llm(fake)
        resp = await llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(resp.content, "recovered")
        await llm.aclose()

    async def test_network_error_then_fallback(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] <= 3:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json=text_response("via fallback"))

        llm = LLMClient("https://x/v1", "k", "a", fallback_model="b", transport=httpx.MockTransport(handler))
        resp = await asyncio.wait_for(llm.chat([{"role": "user", "content": "hi"}]), timeout=20)
        self.assertEqual(resp.content, "via fallback")
        await llm.aclose()


if __name__ == "__main__":
    unittest.main()
