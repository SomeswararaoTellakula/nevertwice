import importlib.util
import json
import unittest

from nevertwice.config import ROOT
from nevertwice.schemas import TriageReport

spec = importlib.util.spec_from_file_location("eval_alerts", ROOT / "scripts" / "eval_alerts.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
CHECKS = json.loads((ROOT / "data" / "alert_checks.json").read_text())


class GradeTests(unittest.TestCase):
    def test_real_run_answer_fails_because_of_restart(self):
        r = TriageReport.from_loose({"recommended_actions": [
            {"title": "Verify DB connection usage vs limit", "command": "psql -c 'select count(*) from pg_stat_activity'"},
            {"title": "Temporarily reduce inventory-sync reconcile parallelism", "command": "kubectl set env deployment/inventory-sync RECONCILE_WORKERS=4"},
            {"title": "Terminate idle-in-transaction sessions", "command": "psql -c 'select pg_terminate_backend(pid) ...'"},
            {"title": "Perform a rolling restart of checkout-api pods", "command": "kubectl rollout restart deployment/checkout-api"},
        ]})
        g = mod.grade(r, ["INC-2104"], CHECKS["ALT-501"], True)
        self.assertFalse(g["passed"])
        self.assertEqual(g["recommended_bad_fix"], ["Perform a rolling restart of checkout-api pods"])
        r.recommended_actions.pop()
        self.assertTrue(mod.grade(r, ["INC-2104"], CHECKS["ALT-501"], True)["passed"])

    def test_reducing_or_pausing_reconcile_is_not_a_bad_fix(self):
        # Seen live: these good steps were wrongly failed by the first checklist.
        for title, cmd in [("Stop extra inventory-sync workers to reduce lock contention", "kubectl scale deploy/inventory-sync -n inventory --replicas=4"),
                           ("Pause the new reconcile workers to reduce lock contention", "kubectl scale deploy/inventory-reconcile --replicas=0")]:
            g = mod.grade_steps([(title, cmd), ("Terminate idle-in-transaction sessions", "psql -c 'select pg_terminate_backend(pid)'")], ["INC-2291"], CHECKS["ALT-501"], True)
            self.assertTrue(g["passed"], (title, g))
        bad = mod.grade_steps([("Scale up HikariCP pool size for checkout-api", "")], [], CHECKS["ALT-501"], False)
        self.assertEqual(bad["recommended_bad_fix"], ["Scale up HikariCP pool size for checkout-api"])

    def test_missing_fix_and_citation(self):
        r = TriageReport.from_loose({"recommended_actions": [{"title": "Scale out checkout-api", "command": "kubectl scale --replicas=20"}]})
        g = mod.grade(r, [], CHECKS["ALT-501"], True)
        self.assertFalse(g["passed"])
        self.assertTrue(g["missing"])
        self.assertFalse(g["cited_ok"])
        self.assertTrue(mod.grade(r, [], CHECKS["ALT-501"], False)["cited_ok"])  # baseline is not asked to cite

    def test_every_demo_alert_has_a_checklist(self):
        import re
        for alert_id in ["ALT-501", "ALT-502", "ALT-503", "ALT-504", "ALT-506"]:
            self.assertIn(alert_id, CHECKS)
            for p in CHECKS[alert_id]["must_not"] + [q for g in CHECKS[alert_id]["must_do"] for q in g]:
                re.compile(p)


if __name__ == "__main__":
    unittest.main()
