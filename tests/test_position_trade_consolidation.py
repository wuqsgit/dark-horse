import unittest

import shared.db as db


class PositionTradeConsolidationTest(unittest.TestCase):
    def test_same_price_with_different_entry_cycles_stays_separate(self):
        rows = [
            {
                "position_trade_id": "B2USDT-INCOME-1",
                "symbol": "B2USDT",
                "side": "LONG",
                "entry_price": 0.5172,
                "entry_time": "2026-07-09 10:00:00",
                "exit_time": "2026-07-09 11:00:00",
            },
            {
                "position_trade_id": "B2USDT-INCOME-2",
                "symbol": "B2USDT",
                "side": "LONG",
                "entry_price": 0.5172,
                "entry_time": "2026-07-10 10:00:00",
                "exit_time": "2026-07-10 11:00:00",
            },
        ]

        partition = getattr(db, "_partition_entry_key_rows", lambda items, has_open_between: [items])
        groups = partition(rows, lambda symbol, start, end: False)

        self.assertEqual(len(groups), 2)

    def test_intervening_open_splits_generated_income_rows(self):
        rows = [
            {
                "position_trade_id": "B2USDT-INCOME-1",
                "symbol": "B2USDT",
                "side": "LONG",
                "entry_price": 0.5172,
                "entry_time": None,
                "exit_time": "2026-07-09 11:00:00",
            },
            {
                "position_trade_id": "B2USDT-INCOME-2",
                "symbol": "B2USDT",
                "side": "LONG",
                "entry_price": 0.5172,
                "entry_time": None,
                "exit_time": "2026-07-09 11:10:00",
            },
        ]

        partition = getattr(db, "_partition_entry_key_rows", lambda items, has_open_between: [items])
        groups = partition(rows, lambda symbol, start, end: True)

        self.assertEqual(len(groups), 2)

    def test_same_price_positions_reopened_within_five_minutes_stay_separate(self):
        rows = [
            {
                "position_trade_id": "B2USDT-INCOME-1",
                "symbol": "B2USDT",
                "side": "LONG",
                "entry_price": 0.5172,
                "entry_time": "2026-07-10 10:00:00",
                "exit_time": "2026-07-10 10:01:00",
            },
            {
                "position_trade_id": "B2USDT-INCOME-2",
                "symbol": "B2USDT",
                "side": "LONG",
                "entry_price": 0.5172,
                "entry_time": "2026-07-10 10:02:00",
                "exit_time": "2026-07-10 10:03:00",
            },
        ]

        groups = db._partition_entry_key_rows(rows, lambda symbol, start, end: False)

        self.assertEqual(len(groups), 2)

    def test_income_row_must_match_a_local_close_from_the_same_position(self):
        local_trades = [
            {"side": "LONG", "entry_price": 3.578, "exit_time": "2026-07-08 08:55:09"}
        ]
        matching = {
            "side": "LONG",
            "entry_price": 3.578,
            "exit_time": "2026-07-08 08:55:19",
        }
        next_position = {
            "side": "LONG",
            "entry_price": 3.578,
            "exit_time": "2026-07-08 09:33:55",
        }
        matcher = getattr(db, "_income_row_matches_local_trades", lambda row, trades: True)

        self.assertTrue(matcher(matching, local_trades))
        self.assertFalse(matcher(next_position, local_trades))

    def test_roll_add_order_is_not_treated_as_new_position_open(self):
        classifier = getattr(db, "_is_position_open_order", lambda row: True)

        self.assertTrue(classifier({"order_type": "MARKET", "reason": "alpha trend probe"}))
        self.assertFalse(classifier({"order_type": "MARKET", "reason": "roll_add layer=1"}))


if __name__ == "__main__":
    unittest.main()
