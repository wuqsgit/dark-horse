import unittest

from trader.execution import _explosive_reentry_allowed


class ExplosiveReentryTest(unittest.TestCase):
    def test_profitable_lock_exit_can_reenter_when_score_remains_strong(self):
        allowed = _explosive_reentry_allowed(
            {
                "cooldown_type": "post_close",
                "reason": "alpha execution close: pnl=1.02; alpha_profit_lock_exit",
            },
            score=90.5,
            min_score=88,
        )

        self.assertTrue(allowed)

    def test_stop_loss_and_second_reentry_are_not_overridden(self):
        self.assertFalse(_explosive_reentry_allowed(
            {"cooldown_type": "stop", "reason": "initial_stop_loss"},
            score=95,
            min_score=88,
        ))
        self.assertFalse(_explosive_reentry_allowed(
            {"cooldown_type": "explosive_reentry_used", "reason": "runner exited"},
            score=95,
            min_score=88,
        ))


if __name__ == "__main__":
    unittest.main()
