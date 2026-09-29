import unittest

from nevertwice.agent import backfire_guard
from nevertwice.memory import MemoryContext, MemoryItem
from nevertwice.schemas import TriageReport


def mem(ref, group, text, context, ids):
    return MemoryItem(ref=ref, id=ref, text=text, type="world", context=context, date="", group=group, incident_ids=ids)


FAILED_RESTART = mem("M1", "failed", "The rolling restart of checkout-api did not work; errors returned after 4 minutes.",
                     "INC-2104 remediation — attempt that did NOT work (checkout-api)", ["INC-2104"])
FAILED_SCALE = mem("M2", "failed", "Scaling checkout-api HPA minReplicas from 12 to 20 made things worse.",
                   "INC-2104 remediation — attempt that made things WORSE (checkout-api)", ["INC-2104"])
WORKED = mem("M3", "worked", "Suspending inventory-reconcile and terminating idle sessions fixed checkout-api.",
             "INC-2104 remediation — fix that worked (checkout-api)", ["INC-2104"])


def report(*titles_and_commands):
    return TriageReport.from_loose({"summary": "s", "recommended_actions": [{"title": t, "command": c, "why": "w"} for t, c in titles_and_commands]})


class GuardTests(unittest.TestCase):
    def test_moves_restart_seen_in_real_run(self):
        r = report(("Terminate idle-in-transaction sessions", "psql -c 'select pg_terminate_backend(pid) ...'"),
                   ("If latency remains high, perform a rolling restart of checkout-api pods", "kubectl rollout restart deployment/checkout-api -n checkout"))
        moved = backfire_guard(r, MemoryContext(items=[FAILED_RESTART, WORKED]), "checkout-api")
        self.assertEqual([a.title for a in r.recommended_actions], ["Terminate idle-in-transaction sessions"])
        self.assertEqual(moved[0]["pattern"], "restart")
        self.assertEqual(moved[0]["incidents"], ["INC-2104"])
        self.assertIn("backfire guard", r.avoid_actions[0].why)
        self.assertIn("INC-2104", r.avoid_actions[0].evidence)

    def test_moves_scale_out_but_not_scale_down_or_checks(self):
        r = report(("Scale out checkout-api", "kubectl scale deploy/checkout-api --replicas=20"),
                   ("Reduce inventory-sync reconcile workers back to 4", "kubectl scale deploy/inventory-sync --replicas=4"),
                   ("Check HPA replicas and connection budget", "kubectl get hpa"))
        moved = backfire_guard(r, MemoryContext(items=[FAILED_SCALE]), "checkout-api")
        self.assertEqual([m["title"] for m in moved], ["Scale out checkout-api"])
        self.assertEqual(len(r.recommended_actions), 2)

    def test_ignores_other_services_and_fixes_that_usually_work(self):
        r = report(("Rolling restart of payments-gateway", "kubectl rollout restart deploy/payments-gateway"))
        self.assertEqual(backfire_guard(r, MemoryContext(items=[FAILED_RESTART]), "payments-gateway"), [])
        worked_restart = mem("M9", "worked", "A rolling restart of payments-gateway loaded the new secret and worked.",
                             "INC-2340 remediation — fix that worked (payments-gateway)", ["INC-2340"])
        failed_restart = mem("M8", "failed", "A rolling restart of payments-gateway did not help.",
                             "INC-9000 remediation — attempt that did NOT work (payments-gateway)", ["INC-9000"])
        self.assertEqual(backfire_guard(r, MemoryContext(items=[worked_restart, failed_restart]), "payments-gateway"), [])

    def test_no_duplicate_when_already_avoided(self):
        r = TriageReport.from_loose({"recommended_actions": [{"title": "Rolling restart of checkout-api", "command": "kubectl rollout restart deploy/checkout-api"}],
                                     "avoid_actions": [{"title": "Rolling restart", "why": "relapsed"}]})
        moved = backfire_guard(r, MemoryContext(items=[FAILED_RESTART]), "checkout-api")
        self.assertEqual(len(r.avoid_actions), 1)
        self.assertEqual(moved[0]["shown_as"], "Rolling restart")
        self.assertIn("INC-2104", r.avoid_actions[0].evidence)
        self.assertEqual(r.recommended_actions, [])

    def test_restart_hidden_in_a_reduce_step_is_still_caught(self):
        # Seen in a real run: "reduce max pool ... (then rollout restart)" slipped past the scale-down exemption.
        r = report(("Pause or throttle inventory-sync workers", "kubectl scale deployment inventory-sync -n inventory --replicas=4"),
                   ("If pool still exhausted, temporarily reduce checkout-api HikariCP max pool",
                    "kubectl edit configmap checkout-api-hikari-config -n checkout && set maxPoolSize=30 (then rollout restart)"))
        moved = backfire_guard(r, MemoryContext(items=[FAILED_RESTART, FAILED_SCALE]), "checkout-api")
        self.assertEqual([a.title for a in r.recommended_actions], ["Pause or throttle inventory-sync workers"])
        self.assertEqual([m["pattern"] for m in moved], ["restart"])


if __name__ == "__main__":
    unittest.main()
