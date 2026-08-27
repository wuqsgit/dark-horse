import json
import unittest
from pathlib import Path

from alpha_engine.volume_price import evaluate_alpha_volume_price
from trader.ai_client import apply_entry_quality_gate


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


if __name__ == "__main__":
    unittest.main()
