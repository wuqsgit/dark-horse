import unittest

from trader.execution import _alpha_probe_entry_decision


def _raw_alpha(*, synchronized=True, phase="trend_up"):
    return {
        "dual_market_volume": {
            "synchronized": synchronized,
            "alpha_spot_volume_ratio_6h": 2.2,
            "futures_volume_ratio_6h": 1.8,
        },
        "market_phase": {"phase": phase},
    }


class AlphaProbeEntryGateTest(unittest.TestCase):
    def test_not_confirmed_dual_market_volume_blocks_probe(self):
        allowed, reason = _alpha_probe_entry_decision(
            _raw_alpha(synchronized=False),
            entry_status="probe",
            breakout_confirmed=True,
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "alpha_probe_futures_volume_not_synchronized")

    def test_unconfirmed_price_structure_blocks_probe(self):
        allowed, reason = _alpha_probe_entry_decision(
            _raw_alpha(),
            entry_status="probe",
            breakout_confirmed=False,
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "alpha_probe_price_structure_not_confirmed")

    def test_breakdown_risk_blocks_probe(self):
        allowed, reason = _alpha_probe_entry_decision(
            _raw_alpha(phase="breakdown_risk"),
            entry_status="probe",
            breakout_confirmed=True,
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "alpha_probe_market_phase_breakdown_risk")

    def test_fully_confirmed_probe_is_allowed(self):
        allowed, reason = _alpha_probe_entry_decision(
            _raw_alpha(),
            entry_status="probe",
            breakout_confirmed=True,
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, "alpha_probe_confirmed")

    def test_not_confirmed_dual_market_volume_also_blocks_full_entry(self):
        allowed, reason = _alpha_probe_entry_decision(
            _raw_alpha(synchronized=False),
            entry_status="pass",
            breakout_confirmed=True,
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "alpha_futures_volume_not_synchronized")


if __name__ == "__main__":
    unittest.main()
