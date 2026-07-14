import os
import tempfile
import unittest

from ai_service.storage import AIStore


class AIQualityStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AIStore(os.path.join(self.tmp.name, "ai.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_samples_are_deduplicated_by_model_symbol_side_template_and_hour(self):
        sample = {
            "model_key": "alpha",
            "symbol": "B2USDT",
            "side": "LONG",
            "template": "alpha_trend_probe",
            "observed_at": "2026-07-14T10:05:00Z",
            "entry_price": 0.5,
            "stop_pct": 0.04,
            "features": {"score": 82.0},
        }
        first_id, first_created = self.store.add_sample(sample)
        second_id, second_created = self.store.add_sample({**sample, "observed_at": "2026-07-14T10:55:00Z"})
        third_id, third_created = self.store.add_sample({**sample, "observed_at": "2026-07-14T11:01:00Z"})

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first_id, second_id)
        self.assertTrue(third_created)
        self.assertNotEqual(first_id, third_id)
        self.assertEqual(self.store.sample_counts("alpha")["total"], 2)

    def test_decision_counters_only_include_current_utc_day(self):
        base = {
            "account_id": 1,
            "model_key": "alpha",
            "symbol": "B2USDT",
            "model_version": "alpha-v1",
            "quality_score": 70,
            "mode": "live",
            "features": {"score": 82},
            "reasons": ["trend confirmed"],
        }
        self.store.record_decision({**base, "decision": "allow", "observed_at": "2026-07-14T01:00:00Z"})
        self.store.record_decision({**base, "decision": "reject", "observed_at": "2026-07-14T02:00:00Z"})
        self.store.record_decision({**base, "decision": "allow", "observed_at": "2026-07-13T23:00:00Z"})

        counters = self.store.decision_counts("alpha", "2026-07-14")
        self.assertEqual(counters, {"allow": 1, "probe": 0, "reject": 1, "collecting": 0, "total": 2})

    def test_cleanup_removes_only_expired_samples_and_decisions(self):
        base = {
            "model_key": "alpha", "symbol": "B2USDT", "side": "LONG",
            "template": "probe", "entry_price": 0.5, "stop_pct": 0.04,
            "features": {"score": 82},
        }
        self.store.add_sample({**base, "observed_at": "2025-01-01T10:00:00Z"})
        self.store.add_sample({**base, "observed_at": "2026-07-14T10:00:00Z"})
        for observed_at in ("2025-01-01T10:00:00Z", "2026-07-14T10:00:00Z"):
            self.store.record_decision({
                **base, "observed_at": observed_at, "decision": "collecting", "mode": "collecting",
            })

        result = self.store.cleanup("2026-01-01T00:00:00Z")

        self.assertEqual(result, {"samples": 1, "decisions": 1})
        self.assertEqual(self.store.sample_counts("alpha")["total"], 1)


if __name__ == "__main__":
    unittest.main()
