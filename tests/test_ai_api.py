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

    def test_entry_quality_status_is_lightweight(self):
        response = self.client.get("/v1/entry-quality/status")

        self.assertEqual(response.status_code, 200)
        self.assertIn("models", response.json())
        self.assertIn("maintenance", response.json())
        self.assertNotIn("alpha_strategy_v2", response.json())

    def test_manual_label_endpoint_runs_same_maintenance_operation(self):
        response = self.client.post("/v1/outcomes/label")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["labeled"], 2)

    def test_batch_observation_endpoint_collects_without_gating(self):
        response = self.client.post("/v1/entry-quality/observe", json={"candidates": [candidate()]})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["created"], 1)
        self.assertEqual(self.service.store.list_decisions(), [])

    def test_alpha_strategy_v2_collects_shadow_sample_through_api(self):
        payload = {
            "request_id": "AKEUSDT:2026-07-28T04:00:00Z:setup:v3",
            "market_env": "testnet",
            "alpha_symbol": "AKEALPHAUSDT",
            "futures_symbol": "AKEUSDT",
            "stage": "setup",
            "setup_type": "accumulation",
            "candle_close_time": "2026-07-28T04:00:00Z",
            "feature_schema_version": 3,
            "feature_quality": {"status": "ready", "coverage": 0.9},
            "features": {"range_2h_pct": 1.9, "absorption_score": 78},
        }

        evaluation = self.client.post(
            "/v2/alpha-strategy/evaluate",
            json=payload,
        )
        status = self.client.get("/v2/alpha-strategy/status")

        self.assertEqual(evaluation.status_code, 200)
        self.assertEqual(evaluation.json()["status"], "collecting")
        self.assertEqual(status.json()["feature_schema_version"], 3)
        self.assertEqual(status.json()["samples"]["total"], 1)


if __name__ == "__main__":
    unittest.main()
