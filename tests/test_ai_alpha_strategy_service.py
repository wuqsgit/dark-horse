import tempfile
import unittest
from pathlib import Path

from ai_service.alpha_strategy_service import AlphaStrategyService
from ai_service.storage import AIStore


def _payload():
    return {
        "request_id": "AKEUSDT:2026-07-28T04:00:00Z:setup:v3",
        "market_env": "mainnet",
        "alpha_symbol": "AKEALPHAUSDT",
        "futures_symbol": "AKEUSDT",
        "stage": "setup",
        "setup_type": "accumulation",
        "candle_close_time": "2026-07-28T04:00:00Z",
        "feature_schema_version": 4,
        "feature_quality": {"status": "ready", "coverage": 0.9},
        "features": {
            "range_2h_pct": 1.9,
            "absorption_score": 78,
        },
    }


class AIAlphaStrategyServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AIStore(Path(self.temp_dir.name) / "ai.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_collecting_mode_persists_stage_sample_idempotently(self):
        service = AlphaStrategyService(self.store)

        first = service.evaluate(_payload())
        second = service.evaluate(_payload())

        self.assertEqual(first["status"], "collecting")
        self.assertFalse(first["applied"])
        self.assertEqual(second["status"], "collecting")
        self.assertEqual(self.store.alpha_strategy_sample_counts()["total"], 1)

    def test_injected_predictor_returns_shadow_probabilities(self):
        service = AlphaStrategyService(
            self.store,
            predictor=lambda payload: {
                "model_versions": {"setup": "setup-v1"},
                "p_setup_success": 0.76,
                "p_followthrough": 0.0,
                "p_fakeout": 0.24,
                "expected_r": 0.35,
                "reasons": ["ake_style_accumulation"],
            },
        )

        result = service.evaluate(_payload())

        self.assertEqual(result["status"], "shadow")
        self.assertFalse(result["applied"])
        self.assertEqual(result["recommended_action"], "watch")
        self.assertEqual(result["p_setup_success"], 0.76)
        self.assertEqual(result["model_versions"]["setup"], "setup-v1")

    def test_feature_schema_mismatch_is_rejected(self):
        payload = _payload()
        payload["feature_schema_version"] = 2

        with self.assertRaises(ValueError):
            AlphaStrategyService(self.store).evaluate(payload)

    def test_zero_fakeout_probability_is_not_replaced_by_default(self):
        payload = _payload()
        payload["stage"] = "trigger"
        service = AlphaStrategyService(
            self.store,
            predictor=lambda _: {
                "p_setup_success": 0.8,
                "p_followthrough": 0.8,
                "p_fakeout": 0.0,
            },
        )

        result = service.evaluate(payload)

        self.assertEqual(result["recommended_action"], "probe")
        self.assertEqual(result["p_fakeout"], 0.0)

    def test_setup_samples_bootstrap_hourly_trigger_counterfactuals(self):
        AlphaStrategyService(self.store).evaluate(_payload())

        created = self.store.backfill_alpha_trigger_samples()
        trigger_samples = self.store.pending_alpha_strategy_samples(
            before_time="2026-07-29T00:00:00Z",
            limit=10,
        )

        self.assertEqual(created, 1)
        self.assertEqual(len(trigger_samples), 2)
        trigger = next(row for row in trigger_samples if row["stage"] == "trigger")
        self.assertEqual(trigger["model_key"], "alpha_trigger_v1_mainnet")

    def test_testnet_market_environment_is_rejected(self):
        payload = _payload()
        payload["market_env"] = "testnet"

        with self.assertRaisesRegex(ValueError, "must be mainnet"):
            AlphaStrategyService(self.store).evaluate(payload)

    def test_status_reports_live_when_live_model_is_available(self):
        service = AlphaStrategyService(
            self.store,
            predictor=lambda _: {"p_setup_success": 0.8},
            execution_mode="live",
        )

        self.assertEqual(service.status()["status"], "live")


if __name__ == "__main__":
    unittest.main()
