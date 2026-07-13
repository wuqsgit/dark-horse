import unittest

from shared.market_phase import detect_market_phase


class MarketPhaseTest(unittest.TestCase):
    def test_detects_trend_up_when_ema_price_and_futures_confirm(self):
        phase = detect_market_phase(
            "BTCUSDT",
            {
                "current_price": 110,
                "ema20": 105,
                "ema20_50_ratio": 1.01,
                "ema20_slope": 0.8,
                "trend_score": 70,
            },
            {"oi_change_pct": 0.03},
            {"dual_market_volume": {"synchronized": True}},
        )

        self.assertEqual(phase["phase"], "trend_up")
        self.assertTrue(phase["allow_roll"])
        self.assertEqual(phase["exit_style"], "trail")

    def test_detects_range_when_ema_is_flat_and_choppy(self):
        phase = detect_market_phase(
            "B2USDT",
            {
                "current_price": 100,
                "ema20": 100,
                "ema20_50_ratio": 1.001,
                "ema20_slope": 0.05,
                "trend_score": 55,
            },
            {},
            {},
        )

        self.assertEqual(phase["phase"], "range")
        self.assertFalse(phase["allow_roll"])
        self.assertEqual(phase["exit_style"], "partial_profit")

    def test_alpha_volume_without_futures_confirmation_is_breakout_pending(self):
        phase = detect_market_phase(
            "AKEUSDT",
            {
                "current_price": 1.01,
                "ema20": 1.0,
                "ema20_50_ratio": 1.003,
                "ema20_slope": 0.35,
                "trend_score": 63,
            },
            {},
            {
                "dual_market_volume": {
                    "alpha_spot_volume_ratio_6h": 3.2,
                    "futures_volume_ratio_6h": 0.8,
                    "synchronized": False,
                }
            },
        )

        self.assertEqual(phase["phase"], "breakout_pending")
        self.assertEqual(phase["position_style"], "probe")
        self.assertFalse(phase["allow_roll"])

    def test_detects_breakdown_risk_when_price_loses_ema_and_slope(self):
        phase = detect_market_phase(
            "RIVERUSDT",
            {
                "current_price": 94,
                "ema20": 100,
                "ema20_50_ratio": 0.996,
                "ema20_slope": -0.45,
                "trend_score": 42,
            },
            {},
            {},
        )

        self.assertEqual(phase["phase"], "breakdown_risk")
        self.assertEqual(phase["exit_style"], "tighten")
        self.assertFalse(phase["allow_roll"])


if __name__ == "__main__":
    unittest.main()
