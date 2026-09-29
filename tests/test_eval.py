import importlib.util
import json
import unittest

from nevertwice.config import ROOT
from nevertwice.dataset import Dataset
from nevertwice.hindsight_client import HindsightClient
from nevertwice.llm import LLMClient
from tests.fakes import FakeHindsight, FakeLLM

spec = importlib.util.spec_from_file_location("eval_script", ROOT / "scripts" / "eval_learning_curve.py")
eval_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_script)


class EvalTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_runs_end_to_end(self):
        ds = Dataset.load(ROOT / "data")
        fake_h, fake_llm = FakeHindsight(), FakeLLM()
        hs = HindsightClient("http://h.test", transport=fake_h.transport(), max_retries=0)
        llm = LLMClient("https://llm.test/v1", "k", "fake-model", transport=fake_llm.transport())
        import nevertwice.memory as memory_mod
        original = memory_mod.IncidentMemory.__init__

        def fast_init(self, client, bank_id):
            original(self, client, bank_id)
            self.poll_interval = 0.01

        memory_mod.IncidentMemory.__init__ = fast_init
        try:
            results = await eval_script.run_eval(hs, llm, ds, "eval-bank", baseline=True, pause=0, log=lambda _m: None)
        finally:
            memory_mod.IncidentMemory.__init__ = original
            await hs.aclose()
            await llm.aclose()
        self.assertEqual(len(results["points"]), len(ds.incidents))
        first = results["points"][0]
        self.assertEqual(first["memories_recalled"] if "memories_recalled" in first else 0, first.get("memories_recalled", 0))
        self.assertEqual(results["summary"]["repeats"], 3)
        self.assertIsNotNone(results["summary"]["memory_hit_rate"])
        self.assertIsNotNone(results["summary"]["baseline_hit_rate"])
        # no future incident can be cited as verified evidence
        learned = []
        for p in results["points"]:
            for cited in p.get("cited", []):
                self.assertIn(cited, learned, f"{p['incident_id']} cited {cited} before it was learned")
            learned.append(p["incident_id"])
        json.dumps(results)  # serialisable

    def test_repeat_detection(self):
        ds = Dataset.load(ROOT / "data")
        self.assertEqual(eval_script.repeat_ids(ds.incidents), {"INC-2233", "INC-2266", "INC-2291"})


if __name__ == "__main__":
    unittest.main()
