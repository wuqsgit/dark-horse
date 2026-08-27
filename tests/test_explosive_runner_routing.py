import unittest

from trader.runner import _filter_account_entry_actions, _filter_legacy_alpha_entries


class ExplosiveRunnerRoutingTest(unittest.TestCase):
    def test_v2_live_mode_keeps_explosive_legacy_signal_only(self):
        actions = [
            {
                "action": "open",
                "symbol": "BTRUSDT",
                "strategy_source": "alpha",
                "event_type": "explosive_breakout",
            },
            {
                "action": "open",
                "symbol": "OTHERUSDT",
                "strategy_source": "alpha",
            },
            {"action": "close", "symbol": "ETHUSDT", "strategy_source": "normal"},
        ]

        filtered = _filter_legacy_alpha_entries(
            actions,
            {"enabled": True, "mode": "mainnet_live", "legacy_alpha_entry_enabled": False},
        )

        self.assertEqual([action["symbol"] for action in filtered], ["BTRUSDT", "ETHUSDT"])

    def test_shadow_mode_keeps_all_actions(self):
        actions = [{"action": "open", "symbol": "OTHERUSDT", "strategy_source": "alpha"}]

        self.assertEqual(
            _filter_legacy_alpha_entries(actions, {"enabled": True, "mode": "shadow"}),
            actions,
        )

    def test_disabled_account_keeps_explosive_action_for_terminal_rejection(self):
        actions = [
            {
                "action": "open",
                "symbol": "BTRUSDT",
                "strategy_source": "alpha",
                "event_type": "explosive_breakout",
            },
            {"action": "open", "symbol": "OTHERUSDT", "strategy_source": "alpha"},
        ]

        filtered = _filter_account_entry_actions(
            actions,
            {"alpha_trading_enabled": False, "normal_trading_enabled": True},
        )

        self.assertEqual([action["symbol"] for action in filtered], ["BTRUSDT"])


if __name__ == "__main__":
    unittest.main()
