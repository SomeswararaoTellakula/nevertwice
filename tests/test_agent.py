import json
import unittest

from nevertwice.agent import TriageAgent, top_action
from nevertwice.config import ROOT
from nevertwice.dataset import Dataset, all_seed_items
from nevertwice.hindsight_client import HindsightClient
from nevertwice.llm import LLMClient
from nevertwice.memory import IncidentMemory, relevant_excerpt
from nevertwice.schemas import Alert, TriageReport
from tests.fakes import FakeHindsight, FakeLLM, ok, status, text_response

BANK = "agent-bank"


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ds = Dataset.load(ROOT / "data")
        self.fake_h = FakeHindsight()
        self.hs = HindsightClient("http://h.test", transport=self.fake_h.transport(), max_retries=0)
        self.memory = IncidentMemory(self.hs, BANK)
        self.memory.poll_interval = 0.01
        await self.memory.setup_bank(lambda _m: None)
        await self.hs.retain(BANK, all_seed_items(self.ds))
        await self.memory.setup_mental_models(lambda _m: None)
        self.fake_llm = FakeLLM()
        self.llm = LLMClient("https://llm.test/v1", "k", "openai/gpt-oss-120b", fallback_model="openai/gpt-oss-20b",
                             reasoning_effort="low", transport=self.fake_llm.transport(), max_rate_limit_wait=1)
        self.agent = TriageAgent(self.llm, self.memory, lambda: self.ds.incident_index)
        raw = self.ds.alert("ALT-501")
        self.alert = Alert(**{k: v for k, v in raw.items() if k in Alert.model_fields})
        self.events = []

    async def asyncTearDown(self):
        await self.hs.aclose()
        await self.llm.aclose()

    async def emit(self, event):
        self.events.append(event)

    async def test_bank_setup_configures_mission_directives_models(self):
        bank = self.fake_h.banks[BANK]
        self.assertIn("mission", bank)
        self.assertEqual(bank["disposition_skepticism"], 4)
        self.assertIn("retain_mission", bank["config"])
        self.assertEqual(len(self.fake_h.directives[BANK]), 3)
        self.assertEqual(len(self.fake_h.models[BANK]), 4)
        # idempotent
        await self.memory.setup_bank(lambda _m: None)
        await self.memory.setup_mental_models(lambda _m: None)
        self.assertEqual(len(self.fake_h.directives[BANK]), 3)
        self.assertEqual(len(self.fake_h.models[BANK]), 4)

    async def test_memory_triage_grounds_and_verifies_evidence(self):
        result = await self.agent.triage(self.alert, use_memory=True, emit=self.emit)
        meta = result.meta
        self.assertGreater(meta["memories_recalled"], 3)
        self.assertEqual(result.report.confidence, "high")
        self.assertIn("INC-9999", meta["evidence"]["unverified"])  # invented ID is flagged
        self.assertTrue(meta["evidence"]["verified"])
        self.assertTrue(all(i in self.ds.incident_index for i in meta["evidence"]["verified"]))
        self.assertTrue(meta["incidents_recalled"])
        # recall was scoped with outcome tags
        recall_bodies = [b for (m, p, b) in self.fake_h.requests if p.endswith("/memories/recall")]
        self.assertTrue(any(b.get("tags") == ["outcome:worked"] for b in recall_bodies))
        self.assertTrue(any(b.get("tags") == ["outcome:failed", "outcome:made_worse"] for b in recall_bodies))
        types = [e["type"] for e in self.events]
        self.assertIn("memories", types)
        # the prompt actually contained memory
        prompt = self.fake_llm.requests[0]["messages"][1]["content"]
        self.assertIn("INCIDENT MEMORY", prompt)
        self.assertRegex(prompt, r"\[M1\]")
        self.assertIn("INC-2104", prompt)

    async def test_baseline_has_no_memory(self):
        result = await self.agent.triage(self.alert, use_memory=False, emit=self.emit)
        self.assertEqual(result.meta["memories_recalled"], 0)
        self.assertEqual(result.report.confidence, "low")
        self.assertNotIn("INCIDENT MEMORY", json.dumps(self.fake_llm.requests[0]))
        self.assertFalse(any(p.endswith("/recall") for (_, p, _) in self.fake_h.requests[-3:]))

    async def test_memory_ref_resolves_to_incident(self):
        result = await self.agent.triage(self.alert, use_memory=True, emit=self.emit)
        first = result.report.recommended_actions[0]
        self.assertTrue(any(e.startswith("M") for e in first.evidence))
        self.assertTrue(any(e.startswith("INC-") for e in first.evidence), first.evidence)

    async def test_search_tool_round_trip(self):
        self.fake_llm.queue(ok(lambda b: {
            "id": "x", "model": b["model"], "choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "search_incident_memory", "arguments": json.dumps({"query": "inventory_svc role idle in transaction", "focus": "any"})}}]}}],
        }))
        result = await self.agent.triage(self.alert, use_memory=True, emit=self.emit)
        self.assertEqual(result.meta["tool_calls"][0]["tool"], "search_incident_memory")
        second = self.fake_llm.requests[1]["messages"]
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-2]["tool_calls"][0]["id"], "c1")

    async def test_salvages_tool_use_failed(self):
        args = {"summary": "salvaged", "likely_root_cause": "x", "confidence": "medium", "recommended_actions": [{"title": "Do it", "why": "INC-2104"}]}
        gen = json.dumps({"name": "submit_triage", "arguments": args})
        bad = status(400, {"error": {"message": "Failed to call a function", "code": "tool_use_failed", "failed_generation": gen}})
        self.fake_llm.queue(bad, bad)
        result = await self.agent.triage(self.alert, use_memory=True, emit=self.emit)
        self.assertEqual(result.report.summary, "salvaged")

    async def test_backfire_guard_moves_restart_using_memory_lookup(self):
        args = {"summary": "pool exhausted", "likely_root_cause": "locks", "confidence": "high",
                "recommended_actions": [
                    {"title": "Terminate idle-in-transaction sessions", "command": "psql -c 'select pg_terminate_backend(pid)'", "why": "INC-2291"},
                    {"title": "Perform a rolling restart of checkout-api pods", "command": "kubectl rollout restart deployment/checkout-api -n checkout", "why": "frees connections"},
                ]}
        self.fake_llm.queue(ok(lambda b: __import__("tests.fakes", fromlist=["x"]).tool_call_response("submit_triage", args, model=b["model"])))
        result = await self.agent.triage(self.alert, use_memory=True, emit=self.emit)
        titles = [a.title for a in result.report.recommended_actions]
        self.assertEqual(titles, ["Terminate idle-in-transaction sessions"])
        self.assertEqual(result.meta["guard"][0]["pattern"], "restart")
        self.assertTrue(set(result.meta["guard"][0]["incidents"]) & {"INC-2104", "INC-2291"}, result.meta["guard"])
        self.assertEqual(result.report.avoid_actions[0].title, "Perform a rolling restart of checkout-api pods")
        guard_steps = [e for e in self.events if e.get("id") == "guard" and e.get("status") == "done"]
        self.assertIn("moved 1 step", guard_steps[0]["detail"])

    async def test_misnamed_tool_call_is_salvaged_without_extra_calls(self):
        # Seen live: gpt-oss-120b on Groq called a non-existent "json" tool with the full report as arguments.
        args = {"summary": "from json tool", "likely_root_cause": "locks", "confidence": "high",
                "recommended_actions": [{"title": "Suspend inventory-reconcile", "why": "INC-2104"}]}
        gen = json.dumps({"name": "json", "arguments": args})
        msg = "Tool call validation failed: attempted to call tool 'json' which was not in request.tools"
        self.fake_llm.queue(status(400, {"error": {"message": msg, "code": "tool_use_failed", "failed_generation": gen}}))
        result = await self.agent.triage(self.alert, use_memory=True, emit=self.emit)
        self.assertEqual(result.report.summary, "from json tool")
        self.assertEqual(result.meta["model"], "openai/gpt-oss-120b")
        self.assertEqual(len(self.fake_llm.requests), 1)
        self.assertFalse([e for e in self.events if e.get("type") == "warning"])

    async def test_unsalvageable_tool_call_retries_on_fallback_model(self):
        bad = status(400, {"error": {"message": "Failed to call a function", "code": "tool_use_failed", "failed_generation": "garbage"}})
        self.fake_llm.queue(bad)
        result = await self.agent.triage(self.alert, use_memory=True, emit=self.emit)
        self.assertTrue(result.report.summary)
        self.assertEqual([r["model"] for r in self.fake_llm.requests], ["openai/gpt-oss-120b", "openai/gpt-oss-20b"])
        notes = [e["message"] for e in self.events if e.get("type") == "warning"]
        self.assertEqual(notes, ["openai/gpt-oss-120b returned a malformed tool call; retried with openai/gpt-oss-20b."])

    async def test_json_fallback_when_tools_keep_failing(self):
        bad = status(400, {"error": {"message": "Failed to call a function", "code": "tool_use_failed", "failed_generation": "garbage"}})
        self.fake_llm.queue(bad, bad)
        result = await self.agent.triage(self.alert, use_memory=True, emit=self.emit)
        self.assertTrue(result.report.recommended_actions)
        self.assertTrue(any(e.get("type") == "warning" and "JSON mode" in e.get("message", "") for e in self.events))
        self.assertEqual(self.fake_llm.requests[-1].get("response_format"), {"type": "json_object"})

    async def test_shrinks_prompt_when_too_large(self):
        self.fake_llm.queue(status(413, {"error": {"message": "Request too large"}}))
        result = await self.agent.triage(self.alert, use_memory=True, emit=self.emit)
        self.assertTrue(result.report.summary)
        first = len(self.fake_llm.requests[0]["messages"][1]["content"])
        second = len(self.fake_llm.requests[1]["messages"][1]["content"])
        self.assertLess(second, first)

    async def test_content_json_without_tool_call(self):
        self.fake_llm.queue(ok(lambda b: text_response(json.dumps({"summary": "as text", "recommended_actions": [{"title": "A", "why": "b"}]}))))
        result = await self.agent.triage(self.alert, use_memory=True, emit=self.emit)
        self.assertEqual(result.report.summary, "as text")

    async def test_recall_failures_degrade_gracefully(self):
        fake = FakeHindsight(fail_recall_tags=True)
        hs = HindsightClient("http://h.test", transport=fake.transport(), max_retries=0)
        mem = IncidentMemory(hs, BANK)
        await mem.setup_bank(lambda _m: None)
        await hs.retain(BANK, all_seed_items(self.ds))
        agent = TriageAgent(self.llm, mem, lambda: self.ds.incident_index)
        result = await agent.triage(self.alert, use_memory=True, emit=self.emit)
        self.assertTrue(result.report.summary)
        self.assertTrue(any(e.get("status") == "error" for e in self.events if e.get("type") == "step"))
        await hs.aclose()

    async def test_runbooks_found_when_server_assigns_ids(self):
        fake = FakeHindsight(server_ids=True)
        hs = HindsightClient("http://h.test", transport=fake.transport(), max_retries=0)
        mem = IncidentMemory(hs, BANK)
        await mem.setup_bank(lambda _m: None)
        await hs.retain(BANK, all_seed_items(self.ds))
        await mem.setup_mental_models(lambda _m: None)
        await mem.setup_mental_models(lambda _m: None)  # second run refreshes by name, creates nothing new
        self.assertEqual(len(fake.models[BANK]), 4)
        ctx = await mem.gather_context(self.alert)
        self.assertTrue(ctx.runbooks, "runbook excerpts should be matched by name")
        await hs.aclose()

    def test_report_from_loose(self):
        r = TriageReport.from_loose({"summary": ["a", "b"], "confidence": "Moderate", "recommended_actions": ["Just a string", {"action": "Rollback", "evidence": "INC-2317, M2"}], "avoid_actions": {"title": "Scale"}})
        self.assertEqual(r.confidence, "medium")
        self.assertEqual(r.recommended_actions[1].evidence, ["INC-2317", "M2"])
        self.assertEqual(r.avoid_actions[0].title, "Scale")
        self.assertEqual(top_action(TriageReport.from_loose({"recommended_actions": [{"title": "Check logs"}, {"title": "Rollback"}]})), "Rollback")

    def test_relevant_excerpt(self):
        content = "- checkout-api restarts relapse (INC-2104)\n- search-api restart caused red cluster\n\nUnrelated paragraph"
        self.assertIn("checkout-api", relevant_excerpt(content, ["checkout-api"], 500))
        self.assertNotIn("search-api", relevant_excerpt(content, ["checkout-api"], 500))
        self.assertEqual(relevant_excerpt(content, ["nothing-matches"], 500), "")


if __name__ == "__main__":
    unittest.main()
