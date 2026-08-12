import unittest

from alpha_engine.strategy.models import AlphaSignalState, StateRecord
from alpha_engine.strategy.setup_rules import detect_setup
from alpha_engine.strategy.trigger_rules import evaluate_trigger


class AlphaStrategyRulesTest(unittest.TestCase):
    def test_detects_ake_style_accumulation(self):
        result = detect_setup(
            {
                "range_2h_pct": 1.9,
                "quote_volume_ratio_1h": 2.4,
                "absorption_score": 78,
                "ret_2h": 0.4,
                "ema20_slope_1h": -0.1,
                "distance_from_high_24h": -2.0,
            }
        )

        self.assertTrue(result.detected)
        self.assertEqual(result.setup_type, "accumulation")
        self.assertGreaterEqual(result.score, 60)

    def test_detects_trend_continuation_after_volume_contraction(self):
        result = detect_setup(
            {
                "range_2h_pct": 8.0,
                "quote_volume_ratio_1h": 0.8,
                "absorption_score": 45,
                "ret_2h": -3.5,
                "ema20_slope_1h": 1.2,
                "ema20_distance_15m": 1.0,
                "distance_from_high_24h": -11.0,
                "pre_breakout_volume_contraction": 0.82,
                "higher_lows_6x1h": 4,
            }
        )

        self.assertTrue(result.detected)
        self.assertEqual(result.setup_type, "continuation")

    def test_long_upper_wick_does_not_count_as_normal_ignition(self):
        result = evaluate_trigger(
            {
                "ret_15m": 8.6,
                "ret_1h": 9.0,
                "ret_6h": 12.0,
                "quote_volume_ratio_15m": 6.1,
                "close_location": 0.36,
                "upper_wick_ratio": 0.58,
                "breakout_distance_pct": 6.9,
                "current_price": 1.1,
            }
        )

        self.assertFalse(result.trigger_detected)
        self.assertTrue(result.overheated)
        self.assertIn("weak_close_location", result.reasons)

    def test_clean_breakout_is_detected(self):
        result = evaluate_trigger(
            {
                "ret_15m": 5.2,
                "ret_1h": 7.0,
                "ret_6h": 9.0,
                "quote_volume_ratio_15m": 2.2,
                "close_location": 0.82,
                "upper_wick_ratio": 0.12,
                "breakout_distance_pct": 1.3,
                "current_price": 1.1,
            }
        )

        self.assertTrue(result.trigger_detected)
        self.assertFalse(result.overheated)

    def test_clean_overheated_ignition_is_detected_for_wait_retest(self):
        result = evaluate_trigger(
            {
                "ret_15m": 10.0,
                "ret_1h": 16.0,
                "ret_6h": 20.0,
                "quote_volume_ratio_15m": 4.0,
                "close_location": 0.82,
                "upper_wick_ratio": 0.12,
                "breakout_distance_pct": 4.0,
                "price_efficiency_score": 80,
                "current_price": 1.1,
            }
        )

        self.assertTrue(result.trigger_detected)
        self.assertTrue(result.overheated)
        self.assertIn("overheated_ignition_detected", result.reasons)

    def test_inefficient_volume_spike_does_not_trigger(self):
        result = evaluate_trigger(
            {
                "ret_15m": 5.0,
                "ret_1h": 6.0,
                "ret_6h": 8.0,
                "quote_volume_ratio_15m": 8.0,
                "close_location": 0.80,
                "upper_wick_ratio": 0.10,
                "breakout_distance_pct": 1.0,
                "price_efficiency_score": 35,
                "current_price": 1.05,
            }
        )

        self.assertFalse(result.trigger_detected)
        self.assertIn("price_efficiency_too_low", result.reasons)

    def test_square_extreme_bearishness_detects_sentiment_reversal(self):
        features = {
            "square_sentiment_available": 1,
            "square_sentiment_age_minutes": 5,
            "square_bearish_ratio": 0.84,
            "square_effective_post_count": 24,
            "square_unique_authors": 20,
            "square_top3_author_share": 0.15,
            "square_bearish_shift_24h": 0.34,
            "square_substantive_risk_count": 0,
            "alpha_discovery_score": 85,
            "spot_volume_ratio_15m": 2.0,
            "futures_volume_ratio_15m": 1.8,
            "volume_sync_score": 0.9,
            "spread_pct_current": 0.10,
            "ret_15m": 0.3,
            "quote_volume_ratio_15m": 1.8,
            "close_location": 0.62,
            "upper_wick_ratio": 0.30,
            "breakout_distance_pct": -0.2,
        }

        setup = detect_setup(features)
        trigger = evaluate_trigger(
            features,
            setup_type=setup.setup_type,
        )

        self.assertTrue(setup.detected)
        self.assertEqual(setup.setup_type, "sentiment_reversal")
        self.assertTrue(trigger.trigger_detected)
        self.assertIn("square_reversal_price_stabilized", trigger.reasons)

    def test_square_substantive_risk_blocks_sentiment_reversal(self):
        result = detect_setup(
            {
                "square_sentiment_available": 1,
                "square_sentiment_age_minutes": 2,
                "square_bearish_ratio": 0.90,
                "square_effective_post_count": 30,
                "square_unique_authors": 25,
                "square_top3_author_share": 0.12,
                "square_bearish_shift_24h": 0.40,
                "square_substantive_risk_count": 1,
                "alpha_discovery_score": 90,
                "spot_volume_ratio_15m": 2.0,
                "futures_volume_ratio_15m": 2.0,
                "volume_sync_score": 1.0,
                "spread_pct_current": 0.05,
            }
        )

        self.assertNotEqual(result.setup_type, "sentiment_reversal")


if __name__ == "__main__":
    unittest.main()
