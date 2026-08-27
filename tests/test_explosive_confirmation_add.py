import unittest

from trader.execution import ExecutionEngine, _cap_explosive_add_quantity


class ExplosiveConfirmationAddTest(unittest.TestCase):
    def test_confirmation_add_cannot_exceed_two_times_initial_quantity(self):
        self.assertEqual(_cap_explosive_add_quantity(10, 10, 8), 8)
        self.assertEqual(_cap_explosive_add_quantity(10, 15, 10), 5)
        self.assertEqual(_cap_explosive_add_quantity(10, 20, 10), 0)

    def test_explosive_position_is_roll_enabled_for_one_confirmation_add(self):
        engine = ExecutionEngine.__new__(ExecutionEngine)
        engine.cfg = {"roll_trading": {}}

        allowed, reason = engine._roll_profile_allowed(
            {
                "strategy_source": "alpha",
                "entry_reason": "explosive_breakout alpha_volume_price",
                "alpha_profile": "high_risk_watch",
            },
            {},
            {},
        )

        self.assertTrue(allowed)
        self.assertIn("explosive", reason)


if __name__ == "__main__":
    unittest.main()
