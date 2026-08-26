import unittest

from shared.trade_history import deduplicate_fills, reconstruct_position_cycles


def fill(account_id, symbol, side, quantity, price, trade_id, created_at, position_side="BOTH"):
    return {
        "account_id": account_id,
        "symbol": symbol,
        "side": side,
        "position_side": position_side,
        "quantity": quantity,
        "price": price,
        "trade_id": trade_id,
        "created_at": created_at,
    }


class TradeHistorySummaryTest(unittest.TestCase):
    def test_deduplicate_fills_uses_normalized_trade_identity(self):
        fills = [
            fill(1, "akeusdt", "BUY", 100, 10, "A1:t1", "2026-08-22 01:00:00"),
            fill(1, "AKEUSDT", "BUY", 100, 10, "t1", "2026-08-22 01:00:00"),
            fill(1, "AKEUSDT", "SELL", 100, 11, "t2", "2026-08-22 02:00:00"),
        ]

        unique = deduplicate_fills(iter(fills))

        self.assertEqual(len(unique), 2)
        self.assertEqual(unique[0]["trade_id"], "A1:t1")
        self.assertEqual(unique[0]["symbol"], "akeusdt")

    def test_duplicate_trade_id_is_counted_once(self):
        fills = [
            fill(1, "AKEUSDT", "BUY", 100, 0.008, "t1", "2026-08-22 01:00:00"),
            fill(1, "AKEUSDT", "BUY", 100, 0.008, "A1:t1", "2026-08-22 01:00:00"),
            fill(1, "AKEUSDT", "SELL", 100, 0.009, "t2", "2026-08-22 02:00:00"),
        ]

        cycles = reconstruct_position_cycles(fills)

        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["entry_quantity"], 100)

    def test_additions_use_fill_weighted_entry_price(self):
        fills = [
            fill(1, "AKEUSDT", "BUY", 80, 10, "1", "2026-08-22 01:00:00"),
            fill(1, "AKEUSDT", "BUY", 20, 12, "2", "2026-08-22 01:01:00"),
            fill(1, "AKEUSDT", "SELL", 100, 13, "3", "2026-08-22 02:00:00"),
        ]

        cycle = reconstruct_position_cycles(fills)[0]

        self.assertAlmostEqual(cycle["entry_price"], (80 * 10 + 20 * 12) / 100)
        self.assertEqual(cycle["entry_quantity"], 100)

    def test_partial_exits_accumulate_until_the_position_is_flat(self):
        fills = [
            fill(1, "AKEUSDT", "BUY", 10, 10, "1", "2026-08-22 01:00:00"),
            fill(1, "AKEUSDT", "SELL", 4, 11, "2", "2026-08-22 02:00:00"),
            fill(1, "AKEUSDT", "SELL", 6, 12, "3", "2026-08-22 03:00:00"),
        ]

        cycle = reconstruct_position_cycles(fills)[0]

        self.assertEqual(cycle["direction"], "LONG")
        self.assertEqual(cycle["entry_quantity"], 10)
        self.assertEqual(cycle["exit_quantity"], 10)
        self.assertEqual([item["quantity"] for item in cycle["exit_fills"]], [4, 6])
        self.assertEqual(cycle["entry_time"], "2026-08-22 01:00:00")
        self.assertEqual(cycle["exit_time"], "2026-08-22 03:00:00")
        self.assertAlmostEqual(cycle["exit_price"], (4 * 11 + 6 * 12) / 10)
        self.assertTrue(cycle["complete"])

    def test_direct_reversal_splits_the_crossing_fill_into_two_cycles(self):
        fills = [
            fill(1, "AKEUSDT", "BUY", 10, 10, "1", "2026-08-22 01:00:00"),
            fill(1, "AKEUSDT", "SELL", 15, 9, "2", "2026-08-22 02:00:00"),
            fill(1, "AKEUSDT", "BUY", 5, 8, "3", "2026-08-22 03:00:00"),
        ]

        cycles = reconstruct_position_cycles(fills)

        self.assertEqual(len(cycles), 2)
        self.assertEqual(cycles[0]["direction"], "LONG")
        self.assertEqual(cycles[0]["exit_quantity"], 10)
        self.assertEqual(cycles[1]["direction"], "SHORT")
        self.assertEqual(cycles[1]["entry_quantity"], 5)
        self.assertEqual(cycles[1]["exit_quantity"], 5)
        self.assertEqual(cycles[1]["entry_fills"][0]["quantity"], 5)
        self.assertEqual(cycles[1]["exit_fills"][0]["quantity"], 5)

    def test_close_and_reopen_in_the_same_direction_creates_two_cycles(self):
        fills = [
            fill(1, "AKEUSDT", "BUY", 3, 10, "1", "2026-08-22 01:00:00"),
            fill(1, "AKEUSDT", "SELL", 3, 11, "2", "2026-08-22 02:00:00"),
            fill(1, "AKEUSDT", "BUY", 4, 12, "3", "2026-08-22 03:00:00"),
            fill(1, "AKEUSDT", "SELL", 4, 13, "4", "2026-08-22 04:00:00"),
        ]

        cycles = reconstruct_position_cycles(fills)

        self.assertEqual(len(cycles), 2)
        self.assertEqual([cycle["entry_quantity"] for cycle in cycles], [3, 4])
        self.assertEqual([cycle["exit_quantity"] for cycle in cycles], [3, 4])
        self.assertEqual([cycle["entry_price"] for cycle in cycles], [10, 12])

    def test_long_and_short_position_sides_are_separate_for_one_symbol(self):
        fills = [
            fill(1, "AKEUSDT", "BUY", 2, 10, "long-open", "2026-08-22 01:00:00", "LONG"),
            fill(1, "AKEUSDT", "SELL", 3, 20, "short-open", "2026-08-22 01:01:00", "SHORT"),
            fill(1, "AKEUSDT", "SELL", 2, 11, "long-close", "2026-08-22 02:00:00", "LONG"),
            fill(1, "AKEUSDT", "BUY", 3, 19, "short-close", "2026-08-22 02:01:00", "SHORT"),
        ]

        cycles = reconstruct_position_cycles(fills)

        self.assertEqual([cycle["direction"] for cycle in cycles], ["LONG", "SHORT"])
        self.assertEqual([cycle["entry_quantity"] for cycle in cycles], [2, 3])
        self.assertEqual([cycle["exit_quantity"] for cycle in cycles], [2, 3])

    def test_incomplete_final_cycle_is_excluded(self):
        fills = [
            fill(1, "AKEUSDT", "BUY", 3, 10, "1", "2026-08-22 01:00:00"),
            fill(1, "AKEUSDT", "SELL", 3, 11, "2", "2026-08-22 02:00:00"),
            fill(1, "AKEUSDT", "BUY", 4, 12, "3", "2026-08-22 03:00:00"),
        ]

        cycles = reconstruct_position_cycles(fills)

        self.assertEqual(len(cycles), 1)
        self.assertTrue(cycles[0]["complete"])
        self.assertEqual(cycles[0]["trade_ids"], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
