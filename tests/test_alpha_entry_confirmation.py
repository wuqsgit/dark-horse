import unittest

from alpha_engine.volume_price import evaluate_alpha_volume_price
from alpha_engine.volume_regime import classify_alpha_volume_regime
from trader.config import TRADING_CONFIG
from trader.execution import (
    ExecutionEngine,
    _alpha_discovery_position_factor,
    _alpha_position_factor,
    _evaluate_alpha_breakout_bars,
)


class FakeDepthExchange:
    def __init__(self, ask="100.2"):
        self.ask = ask

    def get_depth(self, symbol, limit):
        return {
            "bids": [["100", "10"]],
            "asks": [[self.ask, "10"]],
        }


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
        self.assertEqual(result["state"], "alpha_entry_conditions_missing")

    def test_only_oi_collapse_is_blocked_without_waiver_logic(self):
        blocked = evaluate_alpha_volume_price(_features(4.0, 1.8, -0.004, -0.05, 78))
        allowed = evaluate_alpha_volume_price(_features(4.0, 1.8, -0.004, -0.049, 78))
        self.assertFalse(blocked["allow_long"])
        self.assertEqual(blocked["state"], "alpha_oi_collapse")
        self.assertTrue(allowed["allow_long"])

    def test_dual_market_sync_enters_normal_position_with_degraded_spread(self):
        raw = _features(alpha_volume=4.05, futures_volume=1.85, oi4=-0.0012, oi24=-0.015, trend=61.25)
        raw["alpha_trend"]["volume_regime"] = "impulse"
        raw["depth"]["spread_pct"] = 0.20
        raw["returns"] = {"ret_15m": 0.0, "ret_1h": -0.26, "ret_6h": -0.74, "pct_24h": -6.27}
        raw["futures_sync"]["sync_score"] = 65

        result = evaluate_alpha_volume_price(raw)

        self.assertTrue(result["allow_long"])
        self.assertEqual(result["action"], "normal_review")
        self.assertEqual(result["state"], "alpha_volume_impulse_entry")
        self.assertGreater(result["max_position_factor"], 0.5)
        self.assertLess(result["max_position_factor"], 1.0)

    def test_dual_market_sync_uses_normal_position_at_tight_spread(self):
        raw = _features(alpha_volume=4.05, futures_volume=1.85, trend=61.25)
        raw["alpha_trend"]["volume_regime"] = "impulse"

        result = evaluate_alpha_volume_price(raw)

        self.assertEqual(result["state"], "alpha_volume_impulse_entry")
        self.assertEqual(result["max_position_factor"], 1.0)

    def test_execution_preserves_double_position_factor(self):
        self.assertEqual(_alpha_position_factor({"max_position_factor": 2.0}), 2.0)
        self.assertEqual(_alpha_position_factor({"max_position_factor": 3.0}), 2.0)

    def test_spread_at_hard_limit_is_blocked_without_cooldown(self):
        raw = _features(alpha_volume=3.2, futures_volume=2.1, oi4=0.01, oi24=0.0, trend=78)
        raw["depth"]["spread_pct"] = 0.35

        result = evaluate_alpha_volume_price(raw)

        self.assertFalse(result["allow_long"])
        self.assertEqual(result["action"], "observe")
        self.assertEqual(result["state"], "spread_too_wide")

    def test_medium_spread_does_not_create_suspicious_regime(self):
        raw = _features()
        raw["depth"]["spread_pct"] = 0.20
        result = classify_alpha_volume_regime(raw)
        self.assertNotEqual(result["regime"], "suspicious")

    def test_orderbook_imbalance_is_not_an_entry_parameter(self):
        raw = _features(alpha_volume=4.0, futures_volume=1.8)
        raw["depth"].update({"bid_depth": 100, "ask_depth": 140})

        result = evaluate_alpha_volume_price(raw)

        self.assertTrue(result["allow_long"])
        self.assertEqual(result["state"], "alpha_volume_impulse_entry")

    def test_live_orderbook_wide_spread_degrades_instead_of_rejecting(self):
        engine = ExecutionEngine.__new__(ExecutionEngine)
        engine.ex = FakeDepthExchange()
        engine.cfg = TRADING_CONFIG

        ok, reason, info = engine._check_live_orderbook("AKEUSDT", "LONG", {"template": "alpha_pre_breakout_volume_sync"})

        self.assertTrue(ok)
        self.assertIn("spread degraded", reason)
        self.assertTrue(info["spread_degraded"])
        self.assertLess(info["spread_size_multiplier"], 1.0)

    def test_live_orderbook_spread_at_hard_limit_is_rejected(self):
        engine = ExecutionEngine.__new__(ExecutionEngine)
        engine.ex = FakeDepthExchange("100.4")
        engine.cfg = TRADING_CONFIG

        ok, reason, info = engine._check_live_orderbook(
            "AKEUSDT", "LONG", {"template": "alpha_dual_market_normal_entry"},
        )

        self.assertFalse(ok)
        self.assertIn("spread hard blocked", reason)
        self.assertGreaterEqual(info["spread_pct"], 0.0035)

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

    def test_trend_score_is_not_an_entry_parameter(self):
        raw = _features(alpha_volume=4.2, futures_volume=1.8, oi4=0.01, oi24=0.0, trend=20)
        raw["alpha_trend"]["volume_regime"] = "impulse"
        result = evaluate_alpha_volume_price(raw)
        self.assertTrue(result["allow_long"])
        self.assertEqual(result["action"], "normal_review")
        self.assertEqual(result["state"], "alpha_volume_impulse_entry")

    def test_jct_impulse_snapshot_remains_direct_normal_entry(self):
        raw = _features(alpha_volume=4.3734, futures_volume=1.5454, oi4=-0.001733, oi24=-0.027692, trend=66.95)
        raw["alpha_trend"]["volume_regime"] = "impulse"
        raw["depth"]["spread_pct"] = 0.243257

        result = evaluate_alpha_volume_price(raw)

        self.assertTrue(result["allow_long"])
        self.assertEqual(result["state"], "alpha_volume_impulse_entry")

    def test_skyai_warmup_cannot_use_lower_trend_direct_path(self):
        raw = _features(alpha_volume=3.1035, futures_volume=2.1816, oi4=-0.001541, oi24=-0.013784, trend=71.4)
        raw["futures_sync"]["sync_score"] = 75
        raw["alpha_trend"]["volume_regime"] = "warmup"

        blocked = evaluate_alpha_volume_price(raw)

        self.assertFalse(blocked["allow_long"])
        self.assertEqual(blocked["state"], "alpha_entry_conditions_missing")

    def test_tradoor_oi_collapse_is_hard_blocked(self):
        raw = _features(alpha_volume=2.2075, futures_volume=1.8244, oi4=0.007407, oi24=-0.095524, trend=71.9)
        raw["alpha_trend"]["volume_regime"] = "warmup"
        raw["depth"]["spread_pct"] = 0.321746

        result = evaluate_alpha_volume_price(raw)

        self.assertFalse(result["allow_long"])
        self.assertEqual(result["state"], "alpha_oi_collapse")

    def test_warmup_does_not_open_even_with_high_trend_score(self):
        raw = _features(alpha_volume=3.1, futures_volume=2.1, oi4=0.01, oi24=0.0, trend=79.0)
        raw["alpha_trend"]["volume_regime"] = "warmup"
        raw["alpha_trend"]["trend_state"] = "trend_candidate"

        result = evaluate_alpha_volume_price(raw)

        self.assertFalse(result["allow_long"])
        self.assertEqual(result["state"], "alpha_entry_conditions_missing")

    def test_price_confirmation_blocks_falling_contract(self):
        raw_15m = _features(alpha_volume=4.0, futures_volume=1.8)
        raw_15m["returns"]["ret_15m"] = -1.01
        raw_1h = _features(alpha_volume=4.0, futures_volume=1.8)
        raw_1h["returns"]["ret_1h"] = -2.01

        self.assertFalse(evaluate_alpha_volume_price(raw_15m)["allow_long"])
        self.assertFalse(evaluate_alpha_volume_price(raw_1h)["allow_long"])

    def test_large_volume_alone_does_not_trigger_overheat(self):
        raw = _features(alpha_volume=12.0, futures_volume=2.0)

        result = evaluate_alpha_volume_price(raw)

        self.assertTrue(result["allow_long"])
        self.assertEqual(result["state"], "alpha_volume_impulse_entry")

    def test_discovery_score_78_to_80_uses_half_position(self):
        self.assertEqual(_alpha_discovery_position_factor(77.99), 0.0)
        self.assertEqual(_alpha_discovery_position_factor(78.0), 0.5)
        self.assertEqual(_alpha_discovery_position_factor(79.99), 0.5)
        self.assertEqual(_alpha_discovery_position_factor(80.0), 1.0)

    def test_normal_signal_ttl_is_seventy_five_minutes(self):
        self.assertEqual(TRADING_CONFIG["max_signal_age_minutes"], 75)


if __name__ == "__main__":
    unittest.main()
