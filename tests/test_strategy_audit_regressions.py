import unittest
from unittest.mock import patch

from engine.breakout_detector import compute_breakout_metrics
from trader.execution import _score_layer_gate
from trader.portfolio_risk import check_portfolio_risk


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, *_args, **_kwargs):
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        return None


class StrategyAuditRegressionTest(unittest.TestCase):
    def test_breakout_price_and_volume_use_same_latest_closed_bar(self):
        rows = [
            {"time": "latest", "high": 111, "close": 110, "volume": 300},
            {"time": "prior-1", "high": 105, "close": 104, "volume": 100},
            {"time": "prior-2", "high": 104, "close": 103, "volume": 100},
        ]
        with patch(
            "engine.breakout_detector.get_conn",
            return_value=_Connection(rows),
        ):
            result = compute_breakout_metrics("TESTUSDT", period=2)

        self.assertTrue(result["breakout"])
        self.assertEqual(result["current_price"], 110)
        self.assertEqual(result["last_closed_time"], "latest")
        self.assertAlmostEqual(result["volume_ratio"], 3.0)

    def test_missing_score_layers_get_conservative_derived_gate(self):
        row = {
            "symbol": "TESTUSDT",
            "composite_score": 50,
            "entry_alpha": 50,
            "raw_features": {},
        }

        ok, reason, details = _score_layer_gate(row, {"status": "pass"}, "LONG")

        self.assertFalse(ok)
        self.assertTrue(details["derived_fallback"])
        self.assertIn("opportunity_score", reason)

    def test_first_position_still_obeys_single_exposure_limit(self):
        ok, reason = check_portfolio_risk([], 1000, "TESTUSDT", "unclassified", 350)

        self.assertFalse(ok)
        self.assertIn("单币", reason)


if __name__ == "__main__":
    unittest.main()
