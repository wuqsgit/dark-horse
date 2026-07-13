import unittest

from alpha_engine.volume_price import evaluate_alpha_volume_price
from trader.execution import _evaluate_alpha_breakout_bars, _promote_confirmed_alpha_probe


def _features(alpha_volume=1.8, futures_volume=1.5, oi4=0.0, oi24=0.0, trend=75):
    return {
        "returns": {"ret_15m": 0.5, "ret_1h": 1.0, "ret_6h": 4.0, "pct_24h": 5.0},
        "volume": {"alpha_volume_growth_6h": alpha_volume},
        "depth": {"spread_pct": 0.05, "imbalance": 1.2, "bid_depth": 100, "ask_depth": 90},
        "risk": {"range_24h_pct": 10, "pullback_from_high_pct": 3},
        "futures_sync": {
            "available": True,
            "futures_volume_growth_6h": futures_volume,
            "oi_change_4h": oi4,
            "oi_change_24h": oi24,
            "funding_rate": 0.00005,
            "sync_score": 65,
        },
        "alpha_trend": {
            "trend_continuation_score": trend,
            "trend_state": "trend_candidate",
            "volume_regime": "warmup",
            "reasons": [],
        },
    }


class AlphaEntryConfirmationTest(unittest.TestCase):
    def test_ub_snapshot_is_blocked_by_dual_volume_gate(self):
        result = evaluate_alpha_volume_price(_features(1.8026, 1.396, -0.004428, -0.005824, 77.15))
        self.assertFalse(result["allow_long"])
        self.assertEqual(result["state"], "alpha_entry_confirmation_missing")

    def test_negative_oi_requires_strong_volume_waiver(self):
        blocked = evaluate_alpha_volume_price(_features(2.5, 1.8, -0.004, -0.005, 78))
        allowed = evaluate_alpha_volume_price(_features(3.2, 2.1, -0.004, -0.005, 78))
        self.assertFalse(blocked["allow_long"])
        self.assertTrue(allowed["allow_long"])

    def test_breakout_requires_next_bar_to_hold_with_volume(self):
        bars = [
            {"time": str(i), "high": high, "close": close, "quote_vol": volume}
            for i, (high, close, volume) in enumerate([
                (10.0, 9.8, 100), (10.2, 10.0, 110), (10.1, 9.9, 90), (10.3, 10.1, 100),
                (10.8, 10.5, 150), (10.7, 10.4, 120),
            ])
        ]
        ok, _, _ = _evaluate_alpha_breakout_bars(bars)
        self.assertTrue(ok)
        bars[-1]["close"] = 10.2
        ok, _, _ = _evaluate_alpha_breakout_bars(bars)
        self.assertFalse(ok)

    def test_trend_68_to_72_only_promotes_after_dual_market_and_breakout_confirmation(self):
        raw = _features(alpha_volume=2.2, futures_volume=1.8, oi4=0.01, oi24=0.0, trend=68.9)
        blocked = evaluate_alpha_volume_price(raw)
        self.assertFalse(blocked["allow_long"])

        still_blocked = _promote_confirmed_alpha_probe(blocked, raw, breakout_confirmed=False)
        promoted = _promote_confirmed_alpha_probe(blocked, raw, breakout_confirmed=True)

        self.assertFalse(still_blocked["allow_long"])
        self.assertTrue(promoted["allow_long"])
        self.assertEqual(promoted["action"], "normal_review_probe")
        self.assertEqual(promoted["state"], "alpha_trend_probe_confirmed_15m")

    def test_probe_promotion_never_compensates_for_weak_dual_market_confirmation(self):
        raw = _features(alpha_volume=2.2, futures_volume=1.2, oi4=0.01, oi24=0.0, trend=70.0)
        blocked = evaluate_alpha_volume_price(raw)

        result = _promote_confirmed_alpha_probe(blocked, raw, breakout_confirmed=True)

        self.assertFalse(result["allow_long"])


if __name__ == "__main__":
    unittest.main()
