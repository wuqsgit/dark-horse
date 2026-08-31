import unittest
from unittest.mock import patch

from alpha_engine.volume_price import evaluate_alpha_volume_price
from alpha_engine.volume_regime import classify_alpha_volume_regime
from trader.config import TRADING_CONFIG
from trader.execution import (
    ExecutionEngine,
    _alpha_discovery_position_factor,
    _alpha_position_factor,
    _evaluate_alpha_breakout_bars,
    _safe_alpha_futures_breakout_confirmation,
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
    def test_btr_breakout_snapshot_is_classified_as_explosive_event(self):
        raw = _features(
            alpha_volume=3.5119,
            futures_volume=2.7481,
            oi4=0.034194,
            oi24=0.104198,
            trend=64.25,
        )
        raw["returns"] = {
            "ret_15m": 1.9042,
            "ret_1h": 9.1482,
            "ret_6h": 9.8069,
            "pct_24h": 22.16,
        }
        raw["depth"]["spread_pct"] = 0.0
        raw["futures_sync"]["sync_score"] = 100
        raw["alpha_trend"]["volume_regime"] = "suspicious"

        result = evaluate_alpha_volume_price(
            raw,
            market_price=0.03639,
            alpha_score=91.44,
        )

        self.assertTrue(result["allow_long"])
        self.assertEqual(result["event_type"], "explosive_breakout")
        self.assertEqual(result["initial_position_factor"], 1.0)
        self.assertEqual(result["max_total_position_factor"], 2.0)

    def test_ub_snapshot_is_blocked_by_dual_volume_gate(self):
        result = evaluate_alpha_volume_price(_features(1.8026, 1.396, -0.004428, -0.005824, 77.15))
        self.assertFalse(result["allow_long"])
        self.assertEqual(result["state"], "alpha_entry_conditions_missing")

    def test_oi_collapse_is_hard_blocked_and_smaller_decline_stays_watch_only(self):
        blocked = evaluate_alpha_volume_price(_features(4.0, 1.8, -0.004, -0.05, 78))
        allowed = evaluate_alpha_volume_price(_features(4.0, 1.8, -0.004, -0.049, 78))
        self.assertFalse(blocked["allow_long"])
        self.assertEqual(blocked["state"], "alpha_oi_collapse")
        self.assertFalse(allowed["allow_long"])
        self.assertEqual(allowed["state"], "alpha_entry_conditions_missing")

    def test_weak_dual_market_sync_stays_watch_with_degraded_spread(self):
        raw = _features(alpha_volume=4.05, futures_volume=1.85, oi4=0.012, oi24=0.015, trend=61.25)
        raw["alpha_trend"]["volume_regime"] = "impulse"
        raw["depth"]["spread_pct"] = 0.20
        raw["returns"] = {"ret_15m": 0.8, "ret_1h": 2.6, "ret_6h": 4.2, "pct_24h": 6.27}
        raw["futures_sync"]["sync_score"] = 75

        result = evaluate_alpha_volume_price(raw, alpha_score=85)

        self.assertFalse(result["allow_long"])
        self.assertEqual(result["action"], "observe")
        self.assertEqual(result["state"], "explosive_volume_watch")

    def test_weak_dual_market_sync_stays_watch_at_tight_spread(self):
        raw = _features(alpha_volume=4.05, futures_volume=1.85, oi4=0.012, oi24=0.015, trend=61.25)
        raw["alpha_trend"]["volume_regime"] = "impulse"
        raw["returns"]["ret_1h"] = 2.6
        raw["futures_sync"]["sync_score"] = 75

        result = evaluate_alpha_volume_price(raw, alpha_score=85)

        self.assertFalse(result["allow_long"])
        self.assertEqual(result["state"], "explosive_volume_watch")

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
        raw = _features(alpha_volume=4.0, futures_volume=2.0, oi4=0.035, oi24=0.04, trend=76)
        raw["depth"].update({"bid_depth": 100, "ask_depth": 140})
        raw["returns"]["ret_1h"] = 2.5
        raw["futures_sync"]["sync_score"] = 88
        raw["alpha_trend"]["volume_regime"] = "impulse"

        result = evaluate_alpha_volume_price(raw, alpha_score=85)

        self.assertTrue(result["allow_long"])
        self.assertEqual(result["state"], "explosive_breakout_pending")

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
            {"time": f"2026-08-26T0{1 + (i // 4)}:{(i % 4) * 15:02d}:00Z", "high": high, "low": low, "close": close, "quote_vol": volume}
            for i, (high, low, close, volume) in enumerate([
                (10.0, 9.7, 9.8, 100), (10.2, 9.8, 10.0, 110), (10.1, 9.7, 9.9, 90), (10.3, 9.9, 10.1, 100),
                (10.8, 10.2, 10.5, 150), (10.7, 10.3, 10.4, 160),
            ])
        ]
        ok, _, _ = _evaluate_alpha_breakout_bars(bars)
        self.assertTrue(ok)
        bars[-1]["close"] = 10.2
        ok, _, _ = _evaluate_alpha_breakout_bars(bars)
        self.assertFalse(ok)

    def test_breakout_rejects_weak_confirmation_volume(self):
        bars = [
            {"time": f"2026-08-26T0{1 + (i // 4)}:{(i % 4) * 15:02d}:00Z", "high": high, "low": low, "close": close, "quote_vol": volume}
            for i, (high, low, close, volume) in enumerate([
                (10.0, 9.7, 9.8, 100), (10.2, 9.8, 10.0, 100), (10.1, 9.7, 9.9, 100), (10.3, 9.9, 10.1, 100),
                (10.8, 10.2, 10.5, 180), (10.7, 10.3, 10.4, 149),
            ])
        ]

        ok, reason, _ = _evaluate_alpha_breakout_bars(bars)

        self.assertFalse(ok)
        self.assertIn("1.50x", reason)

    def test_breakout_rejects_confirmation_wick_below_level(self):
        bars = [
            {"time": f"2026-08-26T0{1 + (i // 4)}:{(i % 4) * 15:02d}:00Z", "high": high, "low": low, "close": close, "quote_vol": volume}
            for i, (high, low, close, volume) in enumerate([
                (10.0, 9.7, 9.8, 100), (10.2, 9.8, 10.0, 100), (10.1, 9.7, 9.9, 100), (10.3, 9.9, 10.1, 100),
                (10.8, 10.2, 10.5, 180), (10.7, 10.0, 10.4, 160),
            ])
        ]

        ok, reason, _ = _evaluate_alpha_breakout_bars(bars)

        self.assertFalse(ok)
        self.assertIn("low broke level", reason)

    def test_continuation_breakout_rejects_more_than_four_percent_chase(self):
        bars = [
            {"time": f"2026-08-26T0{1 + (i // 4)}:{(i % 4) * 15:02d}:00Z", "high": high, "low": low, "close": close, "quote_vol": volume}
            for i, (high, low, close, volume) in enumerate([
                (10.0, 9.7, 9.8, 100), (10.2, 9.8, 10.0, 100),
                (10.1, 9.7, 9.9, 100), (10.3, 9.9, 10.1, 100),
                (10.7, 10.2, 10.6, 180), (10.9, 10.4, 10.8, 200),
            ])
        ]

        ok, reason, details = _evaluate_alpha_breakout_bars(bars)

        self.assertFalse(ok)
        self.assertGreater(details["confirmation_distance_pct"], 4.0)
        self.assertIn("4.00%", reason)

    def test_structural_squeeze_can_use_eight_percent_chase_ceiling(self):
        bars = [
            {"time": f"2026-08-26T0{1 + (i // 4)}:{(i % 4) * 15:02d}:00Z", "high": high, "low": low, "close": close, "quote_vol": volume}
            for i, (high, low, close, volume) in enumerate([
                (10.0, 9.7, 9.8, 100), (10.2, 9.8, 10.0, 100),
                (10.1, 9.7, 9.9, 100), (10.3, 9.9, 10.1, 100),
                (10.7, 10.2, 10.6, 180), (11.0, 10.4, 10.75, 200),
            ])
        ]

        ok, _, details = _evaluate_alpha_breakout_bars(
            bars,
            max_confirmation_distance_pct=8.0,
        )

        self.assertTrue(ok)
        self.assertLessEqual(details["confirmation_distance_pct"], 8.0)

    def test_breakout_requires_the_expected_latest_closed_candle(self):
        bars = [
            {"time": f"2026-08-26T0{hour}:{minute:02d}:00Z", "high": high, "low": low, "close": close, "quote_vol": volume}
            for (hour, minute), high, low, close, volume in [
                ((1, 15), 10.0, 9.7, 9.8, 100), ((1, 30), 10.2, 9.8, 10.0, 100),
                ((1, 45), 10.1, 9.7, 9.9, 100), ((2, 0), 10.3, 9.9, 10.1, 100),
                ((2, 15), 10.8, 10.2, 10.5, 180), ((2, 30), 10.7, 10.3, 10.4, 160),
            ]
        ]

        ok, reason, _ = _evaluate_alpha_breakout_bars(
            bars,
            expected_confirmation_time="2026-08-26T02:45:00Z",
        )

        self.assertFalse(ok)
        self.assertIn("stale", reason)

    def test_breakout_and_confirmation_must_be_consecutive_15m_candles(self):
        bars = [
            {"time": time, "high": high, "low": low, "close": close, "quote_vol": volume}
            for time, high, low, close, volume in [
                ("2026-08-26T01:15:00Z", 10.0, 9.7, 9.8, 100),
                ("2026-08-26T01:30:00Z", 10.2, 9.8, 10.0, 100),
                ("2026-08-26T01:45:00Z", 10.1, 9.7, 9.9, 100),
                ("2026-08-26T02:00:00Z", 10.3, 9.9, 10.1, 100),
                ("2026-08-26T02:15:00Z", 10.8, 10.2, 10.5, 180),
                ("2026-08-26T02:45:00Z", 10.7, 10.3, 10.4, 160),
            ]
        ]

        ok, reason, _ = _evaluate_alpha_breakout_bars(bars)

        self.assertFalse(ok)
        self.assertIn("not consecutive", reason)

    def test_breakout_lookup_failure_becomes_safe_rejection(self):
        with patch(
            "trader.execution._check_alpha_futures_breakout_confirmation",
            side_effect=RuntimeError("database unavailable"),
        ):
            ok, reason, info = _safe_alpha_futures_breakout_confirmation("AKEUSDT")

        self.assertFalse(ok)
        self.assertIn("database unavailable", reason)
        self.assertEqual(info["error"], "database unavailable")

    def test_low_trend_score_requires_structural_squeeze_confirmation(self):
        raw = _features(alpha_volume=4.2, futures_volume=1.8, oi4=0.012, oi24=0.01, trend=20)
        raw["alpha_trend"]["volume_regime"] = "impulse"
        raw["returns"]["ret_1h"] = 2.5
        raw["futures_sync"]["sync_score"] = 75
        weak = evaluate_alpha_volume_price(raw, alpha_score=85)
        self.assertFalse(weak["allow_long"])

        raw["alpha_trend"]["volume_regime"] = "suspicious"
        raw["futures_sync"].update({
            "futures_volume_growth_6h": 2.6,
            "oi_change_4h": 0.035,
            "sync_score": 92,
        })
        strong = evaluate_alpha_volume_price(raw, alpha_score=89)
        self.assertTrue(strong["allow_long"])
        self.assertEqual(strong["metrics"]["explosive_quality_lane"], "structural_squeeze")

    def test_high_score_with_weak_oi_and_sync_stays_watch_only(self):
        raw = _features(
            alpha_volume=4.1,
            futures_volume=2.1,
            oi4=0.0134,
            oi24=0.006,
            trend=82,
        )
        raw["alpha_trend"]["volume_regime"] = "impulse"
        raw["returns"].update({"ret_15m": 2.0, "ret_1h": 9.4, "ret_6h": 8.9})
        raw["futures_sync"]["sync_score"] = 75

        result = evaluate_alpha_volume_price(raw, alpha_score=91.4)

        self.assertFalse(result["allow_long"])
        self.assertEqual(result["state"], "explosive_volume_watch")
        self.assertFalse(result["metrics"]["entry_conditions"]["explosive_quality"])

    def test_overheated_climax_is_rejected_even_with_strong_oi(self):
        raw = _features(
            alpha_volume=8.8,
            futures_volume=8.8,
            oi4=0.161,
            oi24=0.117,
            trend=68.5,
        )
        raw["alpha_trend"].update({"volume_regime": "overheated", "trend_state": "probe"})
        raw["returns"].update({"ret_15m": 3.9, "ret_1h": 7.5, "ret_6h": 18.0})
        raw["futures_sync"]["sync_score"] = 100

        result = evaluate_alpha_volume_price(raw, alpha_score=91.5)

        self.assertFalse(result["allow_long"])
        self.assertEqual(result["state"], "overheated_climax")

    def test_jct_impulse_snapshot_stays_watch_only_without_price_and_oi_confirmation(self):
        raw = _features(alpha_volume=4.3734, futures_volume=1.5454, oi4=-0.001733, oi24=-0.027692, trend=66.95)
        raw["alpha_trend"]["volume_regime"] = "impulse"
        raw["depth"]["spread_pct"] = 0.243257

        result = evaluate_alpha_volume_price(raw, alpha_score=81.78)

        self.assertFalse(result["allow_long"])
        self.assertEqual(result["state"], "explosive_volume_watch")

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

    def test_large_volume_alone_stays_watch_only_without_strict_confirmation(self):
        raw = _features(alpha_volume=12.0, futures_volume=2.0)

        result = evaluate_alpha_volume_price(raw, alpha_score=85)

        self.assertFalse(result["allow_long"])
        self.assertEqual(result["state"], "explosive_volume_watch")

    def test_discovery_score_78_to_80_uses_half_position(self):
        self.assertEqual(_alpha_discovery_position_factor(77.99), 0.0)
        self.assertEqual(_alpha_discovery_position_factor(78.0), 0.5)
        self.assertEqual(_alpha_discovery_position_factor(79.99), 0.5)
        self.assertEqual(_alpha_discovery_position_factor(80.0), 1.0)

    def test_normal_signal_ttl_is_seventy_five_minutes(self):
        self.assertEqual(TRADING_CONFIG["max_signal_age_minutes"], 75)


if __name__ == "__main__":
    unittest.main()
