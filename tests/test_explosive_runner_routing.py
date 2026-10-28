import unittest

from trader.runner import _filter_account_entry_actions, _filter_legacy_alpha_entries


class ExplosiveRunnerRoutingTest(unittest.TestCase):
    def test_v2_live_mode_removes_all_legacy_alpha_entries(self):
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

        self.assertEqual([action["symbol"] for action in filtered], ["ETHUSDT"])

    def test_shadow_mode_keeps_all_actions(self):
        actions = [{"action": "open", "symbol": "OTHERUSDT", "strategy_source": "alpha"}]

        self.assertEqual(
            _filter_legacy_alpha_entries(actions, {"enabled": True, "mode": "shadow"}),
            actions,
        )

    def test_disabled_account_blocks_every_alpha_entry(self):
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

        self.assertEqual(filtered, [])


if __name__ == "__main__":
    unittest.main()
