import os
import tempfile
import unittest
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from ai_service.main import create_app
from ai_service.service import EntryQualityService
from ai_service.storage import AIStore
from tests.test_ai_quality_service import FakeBackend, candidate


class FakeLabeler:
    def label_pending(self):
        return {"checked": 3, "labeled": 2, "waiting_for_candles": 1, "missing": 0}


class AIServiceAPITest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = AIStore(os.path.join(self.tmp.name, "ai.db"))
        self.service = EntryQualityService(
            store, FakeBackend(), model_dir=os.path.join(self.tmp.name, "models"),
            now_fn=lambda: datetime(2026, 7, 14, 11, tzinfo=timezone.utc),
        )
        self.client = TestClient(create_app(self.service, labeler=FakeLabeler(), start_scheduler=False))

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_and_evaluation_are_observable(self):
        evaluation = self.client.post("/v1/entry-quality/evaluate", json=candidate())
        status = self.client.get("/v1/status")

        self.assertEqual(evaluation.status_code, 200)
        self.assertEqual(evaluation.json()["decision"], "collecting")
        self.assertEqual(status.json()["models"]["alpha"]["decisions_today"]["collecting"], 1)

    def test_manual_label_endpoint_runs_same_maintenance_operation(self):
        response = self.client.post("/v1/outcomes/label")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["labeled"], 2)

    def test_batch_observation_endpoint_collects_without_gating(self):
        response = self.client.post("/v1/entry-quality/observe", json={"candidates": [candidate()]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 1)
        self.assertEqual(self.service.store.list_decisions(), [])


if __name__ == "__main__":
    unittest.main()
