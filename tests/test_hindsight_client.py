import unittest

from nevertwice.hindsight_client import HindsightClient, HindsightError, operation_ids_from
from tests.fakes import FakeHindsight

BANK = "test-bank"


class HindsightClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeHindsight()
        self.client = HindsightClient("http://hindsight.test", "secret", transport=self.fake.transport(), max_retries=0)

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_auth_header_and_bank_upsert(self):
        await self.client.upsert_bank(BANK, name="Bank", mission="m", disposition_skepticism=4)
        self.assertIn(BANK, self.fake.banks)
        self.assertEqual(self.fake.banks[BANK]["disposition_skepticism"], 4)

    async def test_config_update_uses_updates_wrapper(self):
        await self.client.upsert_bank(BANK, name="Bank")
        await self.client.update_bank_config(BANK, {"retain_mission": "x"})
        self.assertEqual(self.fake.banks[BANK]["config"]["retain_mission"], "x")

    async def test_retain_async_and_wait(self):
        await self.client.upsert_bank(BANK, name="Bank")
        resp = await self.client.retain(BANK, [
            {"content": "Checkout failed with HikariCP pool exhaustion in INC-2104.", "context": "INC-2104 postmortem",
             "timestamp": "2026-04-18T11:04:00+05:30", "document_id": "INC-2104:pm", "tags": ["service:checkout-api"],
             "metadata": {"incident_id": "INC-2104", "n": 3}},
        ], run_async=True)
        ops = operation_ids_from(resp)
        self.assertEqual(len(ops), 1)
        seen = []
        done = await self.client.wait_for_operations(BANK, ops, poll_interval=0.01, on_progress=lambda d, t, _: seen.append((d, t)))
        self.assertEqual(done[0]["status"], "completed")
        self.assertEqual(seen[-1], (1, 1))
        # metadata values are coerced to strings (the API requires string values)
        self.assertEqual(self.fake.memories[BANK][0]["metadata"]["n"], "3")

    async def test_retain_rejects_empty_content(self):
        with self.assertRaises(ValueError):
            await self.client.retain(BANK, [{"content": "  "}])

    async def test_retain_replaces_same_document(self):
        await self.client.upsert_bank(BANK, name="Bank")
        item = {"content": "The rolling restart did not work at all.", "document_id": "doc-1"}
        await self.client.retain(BANK, [item])
        await self.client.retain(BANK, [item])
        self.assertEqual(len(self.fake.memories[BANK]), 1)

    async def test_recall_with_tags(self):
        await self.client.upsert_bank(BANK, name="Bank")
        await self.client.retain(BANK, [
            {"content": "Rolling restart of checkout-api did not work.", "tags": ["outcome:failed"]},
            {"content": "Suspending inventory-reconcile fixed checkout-api.", "tags": ["outcome:worked"]},
        ])
        data = await self.client.recall(BANK, "checkout-api fix", tags=["outcome:worked"], tags_match="any")
        self.assertEqual(len(data["results"]), 1)
        self.assertIn("Suspending", data["results"][0]["text"])
        body = self.fake.requests[-1][2]
        self.assertEqual(body["tags"], ["outcome:worked"])
        self.assertEqual(body["tags_match"], "any")

    async def test_missing_bank_raises_404(self):
        with self.assertRaises(HindsightError) as ctx:
            await self.client.recall("nope", "anything")
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("not found", str(ctx.exception))

    async def test_reflect_includes_facts(self):
        await self.client.upsert_bank(BANK, name="Bank")
        await self.client.retain(BANK, [{"content": "NovaPay degraded and upi_psp_routing was flipped to upixpress."}])
        data = await self.client.reflect(BANK, "What fixed NovaPay degradation?")
        self.assertIn("upi_psp_routing", data["text"])
        self.assertTrue(data["based_on"]["memories"])

    async def test_reflect_falls_back_when_include_rejected(self):
        fake = FakeHindsight(strict_include=True)
        client = HindsightClient("http://h.test", transport=fake.transport(), max_retries=0)
        await client.upsert_bank(BANK, name="Bank")
        data = await client.reflect(BANK, "anything")
        self.assertIn("text", data)
        self.assertNotIn("include", fake.requests[-1][2])
        await client.aclose()

    async def test_mental_models_and_directives(self):
        await self.client.upsert_bank(BANK, name="Bank")
        await self.client.retain(BANK, [{"content": "Scaling consumers did not help with the poison record."}])
        resp = await self.client.create_mental_model(BANK, name="Backfired", source_query="what did not help", model_id="mm1")
        self.assertTrue(operation_ids_from(resp))
        models = await self.client.list_mental_models(BANK)
        self.assertEqual(models[0]["id"], "mm1")
        full = await self.client.get_mental_model(BANK, "mm1")
        self.assertIn("poison", full["content"])
        await self.client.create_directive(BANK, name="Cite", content="Cite incidents")
        self.assertEqual(len(await self.client.list_directives(BANK)), 1)

    async def test_list_memories_filters(self):
        await self.client.upsert_bank(BANK, name="Bank")
        await self.client.retain(BANK, [{"content": "Alpha fact about checkout. Beta fact about payments."}])
        data = await self.client.list_memories(BANK, fact_type="world", q="payments", limit=5)
        self.assertEqual(data["total"], 1)


if __name__ == "__main__":
    unittest.main()
