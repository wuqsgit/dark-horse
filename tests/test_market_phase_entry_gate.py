import unittest

from trader.execution import _market_phase_entry_decision


class MarketPhaseEntryGateTest(unittest.TestCase):
    def test_blocks_breakdown_and_data_insufficient_uncertain_entries(self):
        cases = (
            {"phase": "breakdown_risk"},
            {"phase": "uncertain", "confidence": 20, "position_style": "skip"},
        )
        for phase in cases:
            ok, mode, reason = _market_phase_entry_decision(phase, "pass")

            self.assertFalse(ok)
            self.assertEqual(mode, "blocked")
            self.assertIn("market_phase", reason)

    def test_breakout_pending_keeps_confirmed_entry_mode(self):
        ok, mode, reason = _market_phase_entry_decision(
            {"phase": "breakout_pending", "position_style": "probe"},
            "pass",
        )

        self.assertTrue(ok)
        self.assertEqual(mode, "pass")
        self.assertIn("breakout_pending", reason)

    def test_mixed_uncertain_soft_passes_when_data_exists(self):
        ok, mode, reason = _market_phase_entry_decision(
            {"phase": "uncertain", "confidence": 40, "position_style": "reduced"},
            "pass",
        )

        self.assertTrue(ok)
        self.assertEqual(mode, "pass")
        self.assertIn("uncertain", reason)

    def test_trend_up_keeps_confirmed_entry_mode(self):
        ok, mode, reason = _market_phase_entry_decision(
            {"phase": "trend_up", "position_style": "trend"},
            "pass",
        )

        self.assertTrue(ok)
        self.assertEqual(mode, "pass")
        self.assertEqual(reason, "market_phase_ok")


if __name__ == "__main__":
    unittest.main()
