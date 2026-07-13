import unittest

from trader.execution import _market_phase_entry_decision


class MarketPhaseEntryGateTest(unittest.TestCase):
    def test_blocks_breakdown_and_uncertain_entries(self):
        for phase in ("breakdown_risk", "uncertain"):
            ok, mode, reason = _market_phase_entry_decision({"phase": phase}, "pass")

            self.assertFalse(ok)
            self.assertEqual(mode, "blocked")
            self.assertIn(phase, reason)

    def test_breakout_pending_downgrades_to_probe(self):
        ok, mode, reason = _market_phase_entry_decision(
            {"phase": "breakout_pending", "position_style": "probe"},
            "pass",
        )

        self.assertTrue(ok)
        self.assertEqual(mode, "probe")
        self.assertIn("breakout_pending", reason)

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
