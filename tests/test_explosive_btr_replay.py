import json
import unittest
from pathlib import Path

from alpha_engine.volume_price import evaluate_alpha_volume_price
from trader.ai_client import apply_entry_quality_gate
from trader.execution import _evaluate_alpha_breakout_bars


class FakeExchange:
    def adjust_quantity(self, symbol, quantity):
        return quantity


class ExplosiveBtrReplayTest(unittest.TestCase):
    def test_btr_precursor_survives_rule_and_ai_planning_chain(self):
        fixture_path = Path(__file__).parent / "fixtures" / "btr_20260826_explosive.json"
        snapshot = json.loads(fixture_path.read_text(encoding="utf-8"))

        signal = evaluate_alpha_volume_price(
            snapshot["features"],
            market_price=snapshot["market_price"],
            alpha_score=snapshot["alpha_score"],
        )
        self.assertTrue(signal["allow_long"])
        self.assertEqual(signal["event_type"], "explosive_breakout")
        self.assertEqual(signal["metrics"]["explosive_quality_lane"], "structural_squeeze")
        breakout_ok, _, breakout = _evaluate_alpha_breakout_bars(
            snapshot["breakout_bars"],
            max_confirmation_distance_pct=8.0,
        )
        self.assertTrue(breakout_ok)
        self.assertGreaterEqual(breakout["confirmation_volume_ratio"], 1.5)

        action = {
            "action": "open",
            "symbol": snapshot["symbol"],
            "position_side": "LONG",
            "quantity": 1813.0,
            "entry_price": 0.03655,
            "leverage": 2,
            "strategy_source": "alpha",
            "event_type": signal["event_type"],
            "setup_id": "BTRUSDT:LONG:2026-08-26T02:30:00Z",
            "initial_position_factor": signal["initial_position_factor"],
            "max_total_position_factor": signal["max_total_position_factor"],
        }
        planned = apply_entry_quality_gate(
            [action],
            [{"symbol": snapshot["symbol"], "raw_features": snapshot["features"]}],
            balance=5000,
            exchange=FakeExchange(),
            account_id=1,
            evaluate=lambda candidate: {
                "status": "live",
                "decision": "reject",
                "quality_score": 42,
                "position_factor": 0,
                "applied": True,
            },
        )

        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0]["quantity"], 1813.0)
        self.assertEqual(planned[0]["ai_quality_decision"], "reject_advisory")

    def test_btr_hard_spread_limit_still_blocks(self):
        fixture_path = Path(__file__).parent / "fixtures" / "btr_20260826_explosive.json"
        snapshot = json.loads(fixture_path.read_text(encoding="utf-8"))
        snapshot["features"]["depth"]["spread_pct"] = 0.35

        signal = evaluate_alpha_volume_price(
            snapshot["features"],
            market_price=snapshot["market_price"],
            alpha_score=snapshot["alpha_score"],
        )

        self.assertFalse(signal["allow_long"])
        self.assertEqual(signal["state"], "spread_too_wide")

    def test_bus_volume_spike_is_rejected_when_price_and_oi_are_weak(self):
        snapshot = {
            "returns": {"ret_15m": -0.1512, "ret_1h": -0.3014, "ret_6h": 0.4556, "pct_24h": -0.33},
            "volume": {"alpha_volume_growth_6h": 6.9162},
            "depth": {"spread_pct": 0.091812},
            "risk": {},
            "futures_sync": {
                "available": True,
                "futures_volume_growth_6h": 1.8563,
                "oi_change_4h": -0.00281,
                "oi_change_24h": -0.003054,
                "sync_score": 65,
            },
        }

        signal = evaluate_alpha_volume_price(snapshot, alpha_score=83.02)

        self.assertFalse(signal["allow_long"])
        self.assertEqual(signal["state"], "explosive_volume_watch")
        self.assertFalse(signal["metrics"]["entry_conditions"]["price_strong_15m"])
        self.assertFalse(signal["metrics"]["entry_conditions"]["oi_expanded"])

    def test_jct_volume_spike_is_rejected_when_price_and_oi_are_weak(self):
        snapshot = {
            "returns": {"ret_15m": -0.1386, "ret_1h": -1.2768, "ret_6h": 0.0462, "pct_24h": -6.02},
            "volume": {"alpha_volume_growth_6h": 6.5058},
            "depth": {"spread_pct": 0.276465},
            "risk": {},
            "futures_sync": {
                "available": True,
                "futures_volume_growth_6h": 2.3143,
                "oi_change_4h": 0.00362,
                "oi_change_24h": -0.011644,
                "sync_score": 75,
            },
        }

        signal = evaluate_alpha_volume_price(snapshot, alpha_score=81.78)

        self.assertFalse(signal["allow_long"])
        self.assertEqual(signal["state"], "explosive_volume_watch")
        self.assertFalse(signal["metrics"]["entry_conditions"]["price_strong_1h"])
        self.assertFalse(signal["metrics"]["entry_conditions"]["oi_expanded"])


if __name__ == "__main__":
    unittest.main()
