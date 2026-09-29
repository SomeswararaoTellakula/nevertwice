import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from starlette.testclient import TestClient

from nevertwice.config import ROOT, Settings
from nevertwice.dataset import Dataset, all_seed_items
from nevertwice.hindsight_client import HindsightClient
from nevertwice.llm import LLMClient
from nevertwice.server import create_app
from tests.fakes import FakeHindsight, FakeLLM

BANK = "server-bank"


def read_events(response) -> list[dict]:
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = replace(
            Settings.from_env(),
            hindsight_base_url="http://h.test", hindsight_api_key="k", bank_id=BANK,
            llm_base_url="https://llm.test/v1", llm_api_key="k", llm_model="openai/gpt-oss-120b",
            llm_fallback_model="openai/gpt-oss-20b", data_dir=ROOT / "data", runtime_dir=Path(self.tmp.name),
        )
        self.fake_h = FakeHindsight()
        self.fake_llm = FakeLLM()
        self.hs = HindsightClient("http://h.test", transport=self.fake_h.transport(), max_retries=0)
        self.llm = LLMClient("https://llm.test/v1", "k", "openai/gpt-oss-120b", transport=self.fake_llm.transport())
        app = create_app(self.settings, hindsight=self.hs, llm=self.llm)
        self.client = TestClient(app)
        self.client.__enter__()
        state = app.state.nevertwice
        state.memory.poll_interval = 0.01
        self.state = state
        # seed the fake bank
        async def seed():
            await state.memory.setup_bank(lambda _m: None)
            await self.hs.retain(BANK, all_seed_items(Dataset.load(ROOT / "data")))
            await state.memory.setup_mental_models(lambda _m: None)

        self.client.portal.call(seed)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_index_and_static(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("NeverTwice", r.text)
        self.assertEqual(self.client.get("/static/app.js").status_code, 200)
        self.assertEqual(self.client.get("/static/styles.css").status_code, 200)

    def test_status(self):
        data = self.client.get("/api/status").json()
        self.assertTrue(data["hindsight"]["ok"], data)
        self.assertGreater(data["hindsight"]["total_memories"], 50)
        self.assertEqual(data["config_problems"], [])

    def test_status_reports_missing_bank(self):
        self.state.settings = replace(self.settings, bank_id="missing-bank")
        data = self.client.get("/api/status").json()
        self.assertFalse(data["hindsight"]["ok"])
        self.assertIn("seed_memory.py", data["hindsight"]["error"])

    def test_alerts_and_incidents(self):
        alerts = self.client.get("/api/alerts").json()
        self.assertEqual(len(alerts["alerts"]), 6)
        self.assertIn("checkout-api", alerts["services"])
        incidents = self.client.get("/api/incidents").json()["incidents"]
        self.assertEqual(len(incidents), 15)
        self.assertEqual(self.client.get("/api/incidents/inc-2104").json()["id"], "INC-2104")
        self.assertEqual(self.client.get("/api/incidents/INC-1").status_code, 404)

    def test_triage_compare_stream(self):
        with self.client.stream("POST", "/api/triage", json={"alert_id": "ALT-501", "mode": "compare"}) as r:
            self.assertEqual(r.status_code, 200)
            self.assertIn("text/event-stream", r.headers["content-type"])
            events = read_events(r)
        reports = [e for e in events if e["type"] == "report"]
        self.assertEqual([e["lane"] for e in reports], ["baseline", "memory"])
        self.assertEqual(reports[0]["meta"]["memories_recalled"], 0)
        self.assertGreater(reports[1]["meta"]["memories_recalled"], 0)
        self.assertEqual(events[-1]["type"], "done")
        log = self.client.get("/api/learning").json()
        self.assertEqual(log["summary"]["memory"]["triages"], 1)
        self.assertEqual(log["summary"]["baseline"]["triages"], 1)

    def test_triage_custom_alert(self):
        alert = {"title": "notification-svc OTP SMS rejected", "service": "notification-svc", "logs": "err_code=DLT_TEMPLATE_MISMATCH"}
        with self.client.stream("POST", "/api/triage", json={"alert": alert, "mode": "memory"}) as r:
            events = read_events(r)
        self.assertTrue(any(e["type"] == "report" for e in events), events[-3:])

    def test_triage_validation(self):
        self.assertEqual(self.client.post("/api/triage", json={"alert_id": "ALT-501", "mode": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/api/triage", json={"alert_id": "ALT-999"}).status_code, 404)
        self.assertEqual(self.client.post("/api/triage", json={"alert": {"title": "x"}}).status_code, 422)
        self.assertEqual(self.client.post("/api/triage", content=b"not json", headers={"content-type": "application/json"}).status_code, 400)

    def test_triage_llm_failure_is_reported_in_stream(self):
        from tests.fakes import status
        auth = status(401, {"error": {"message": "bad key"}})
        self.fake_llm.queue(auth)
        with self.client.stream("POST", "/api/triage", json={"alert_id": "ALT-502", "mode": "baseline"}) as r:
            events = read_events(r)
        errors = [e for e in events if e["type"] == "error"]
        self.assertTrue(errors)
        self.assertIn("LLM_API_KEY", errors[0]["message"])
        self.assertEqual(events[-1]["type"], "done")

    def test_learning_loop_resolve_then_recall(self):
        # step 1: novel alert, then teach NeverTwice the resolution
        with self.client.stream("POST", "/api/triage", json={"alert_id": "ALT-505", "mode": "memory"}) as r:
            triage_id = next(e for e in read_events(r) if e["type"] == "report")["id"]
        alert = next(a for a in self.client.get("/api/alerts").json()["alerts"] if a["id"] == "ALT-505")
        body = {**alert["suggested_resolution"], "service": "search-api", "severity": "SEV2", "alert_id": "ALT-505",
                "triage_id": triage_id, "recommendation_feedback": "no"}
        with self.client.stream("POST", "/api/resolve", json=body) as r:
            events = read_events(r)
        resolved = next(e for e in events if e["type"] == "resolved")
        new_id = resolved["incident"]["id"]
        self.assertEqual(new_id, "INC-2341")
        self.assertTrue(resolved["retain"]["completed"])
        self.assertTrue(any(e["type"] == "progress" for e in events))
        # the new knowledge is in Hindsight, and the next similar alert recalls it
        with self.client.stream("POST", "/api/triage", json={"alert_id": "ALT-506", "mode": "memory"}) as r:
            report = next(e for e in read_events(r) if e["type"] == "report")
        recalled = [i["id"] for i in report["meta"]["incidents_recalled"]]
        self.assertIn(new_id, recalled)
        self.assertIn(new_id, report["meta"]["evidence"]["verified"])
        entries = self.client.get("/api/learning").json()["entries"]
        first = next(e for e in entries if e["id"] == triage_id)
        self.assertEqual(first["feedback"], "no")
        self.assertEqual(first["resolved_incident"], new_id)
        alerts = self.client.get("/api/alerts").json()["alerts"]
        self.assertTrue(next(a for a in alerts if a["id"] == "ALT-505")["resolved"])
        # next id increments
        self.assertEqual(self.client.portal.call(self.state.next_incident_id), "INC-2342")

    def test_learning_includes_answer_quality(self):
        self.assertIsNone(self.client.get("/api/learning").json()["quality"])
        data = {"generated_at": "2026-09-28T10:00:00+05:30", "model": "m", "summary": {"memory": {"passed": 3, "total": 3, "recommended_bad_fix": 0}},
                "results": [{"alert": "ALT-502", "run": 1, "mode": "memory", "passed": True}]}
        (Path(self.tmp.name) / "alert_eval.json").write_text(json.dumps(data))
        self.assertEqual(self.client.get("/api/learning").json()["quality"]["summary"]["memory"]["passed"], 3)

    def test_resolve_validation(self):
        r = self.client.post("/api/resolve", json={"title": "x", "service": "s", "root_cause": "short"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("error", r.json())

    def test_ask(self):
        r = self.client.post("/api/ask", json={"question": "What fixed NovaPay degradation?"})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["text"])
        self.assertTrue(data["memories"])
        self.assertEqual(self.client.post("/api/ask", json={"question": ""}).status_code, 422)

    def test_memory_overview_and_list(self):
        data = self.client.get("/api/memory/overview").json()
        self.assertEqual(len(data["mental_models"]), 4)
        self.assertTrue(all(m["content"] for m in data["mental_models"]))
        self.assertEqual(len(data["directives"]), 3)
        self.assertGreater(data["counts"]["world"], 50)
        listing = self.client.get("/api/memory/list", params={"type": "world", "q": "HikariCP"}).json()
        self.assertTrue(listing["items"])
        self.assertTrue(any("INC-" in " ".join(i["incident_ids"]) for i in listing["items"]))
        self.assertEqual(self.client.get("/api/memory/list", params={"type": "bogus"}).status_code, 400)
        r = self.client.post("/api/memory/mental-models/fixes-that-backfired/refresh")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.post("/api/memory/mental-models/nope/refresh").status_code, 502)


if __name__ == "__main__":
    unittest.main()
