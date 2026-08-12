import unittest
from unittest.mock import patch

from trader.risk import is_adverse_funding_rate, meets_safety_filters


def _row(funding_rate):
    return {
        "symbol": "TESTUSDT",
        "composite_score": 80,
        "entry_alpha": 70,
        "raw_features": {"futures": {"funding_rate": funding_rate}},
    }


class FundingDirectionTest(unittest.TestCase):
    def test_negative_funding_is_allowed_for_long_and_blocked_for_short(self):
        self.assertFalse(is_adverse_funding_rate(-0.00537, 0.001, "LONG"))
        self.assertTrue(is_adverse_funding_rate(-0.00537, 0.001, "SHORT"))

    def test_positive_funding_is_blocked_for_long_and_allowed_for_short(self):
        self.assertTrue(is_adverse_funding_rate(0.00537, 0.001, "LONG"))
        self.assertFalse(is_adverse_funding_rate(0.00537, 0.001, "SHORT"))

    def test_safety_filter_uses_position_direction(self):
        with patch("trader.risk.get_symbol_threshold", return_value=60):
            long_ok, long_reason = meets_safety_filters(_row(-0.00537), side="LONG")
            short_ok, short_reason = meets_safety_filters(_row(-0.00537), side="SHORT")

        self.assertTrue(long_ok, long_reason)
        self.assertFalse(short_ok)
        self.assertIn("side=SHORT", short_reason)
        self.assertIn("rate=-0.00537", short_reason)


if __name__ == "__main__":
    unittest.main()
