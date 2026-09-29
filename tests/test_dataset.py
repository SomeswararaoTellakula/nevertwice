import unittest
from datetime import datetime

from nevertwice.config import ROOT
from nevertwice.dataset import Dataset, all_seed_items, resolution_memory_items
from nevertwice.schemas import Alert, Resolution

DATA = ROOT / "data"


class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = Dataset.load(DATA)

    def test_incidents_are_well_formed(self):
        ids = [i["id"] for i in self.ds.incidents]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 12)
        for inc in self.ds.incidents:
            start = datetime.fromisoformat(inc["started_at"])
            end = datetime.fromisoformat(inc["resolved_at"])
            self.assertLess(start, end, inc["id"])
            outcomes = {r["outcome"] for r in inc["remediations"]}
            self.assertIn("worked", outcomes, inc["id"])
            self.assertTrue(outcomes <= {"worked", "failed", "made_worse"})
            worked_text = " ".join(f"{r['action']} {r.get('command', '')}" for r in inc["remediations"] if r["outcome"] == "worked").lower()
            self.assertTrue(any(k.lower() in worked_text for k in inc["fix_keywords"]), f"{inc['id']} fix keywords not in worked fix")

    def test_seed_items_are_valid_for_retain(self):
        items = all_seed_items(self.ds)
        docs = [i["document_id"] for i in items]
        self.assertEqual(len(docs), len(set(docs)), "document ids must be unique (retain replaces by id)")
        for item in items:
            self.assertTrue(item["content"].strip())
            datetime.fromisoformat(item["timestamp"])
            self.assertTrue(all(isinstance(t, str) and t for t in item["tags"]))
            self.assertTrue(all(isinstance(v, str) for v in item["metadata"].values()))
            self.assertLess(len(item["content"]), 6000)
        self.assertTrue(any("outcome:made_worse" in i["tags"] for i in items))

    def test_every_remediation_item_names_its_incident(self):
        for item in all_seed_items(self.ds):
            if "kind:remediation" in item["tags"]:
                iid = item["metadata"]["incident_id"]
                self.assertIn(iid, item["context"])
                self.assertIn(iid, item["content"])

    def test_alerts_validate(self):
        for raw in self.ds.alerts:
            alert = Alert(**{k: v for k, v in raw.items() if k in Alert.model_fields})
            self.assertTrue(alert.logs)
        steps = [a for a in self.ds.alerts if a.get("learning_demo")]
        self.assertEqual(sorted(a["learning_demo"]["step"] for a in steps), [1, 2])
        sug = next(a for a in steps if a["learning_demo"]["step"] == 1)["suggested_resolution"]
        Resolution(service="search-api", **sug)

    def test_resolution_items(self):
        res = Resolution(title="Search broke for Telugu", service="search-api", root_cause="analysis-icu plugin missing on new nodes",
                         worked=["exclude nodes"], failed=["restart nodes"], recommendation_feedback="no")
        items = resolution_memory_items(res, "INC-2341", self.ds.alert("ALT-505"), {"top_action": "Restart"})
        kinds = [i["metadata"]["kind"] for i in items]
        self.assertEqual(kinds.count("remediation"), 2)
        self.assertIn("feedback", kinds)
        self.assertIn("Unknown tokenizer type", items[0]["content"])


if __name__ == "__main__":
    unittest.main()
