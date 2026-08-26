import base64
import json
import os
import tempfile
import unittest

import shared.db as db
from shared.trade_history import (
    deduplicate_fills,
    fetch_trade_history_summaries,
    reconstruct_position_cycles,
)


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
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.temp.name, "trade-history.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp.cleanup()

    def test_summary_aggregates_deduplicated_ake_long_cycles(self):
        fills = [
            ("A2:open-1", "BUY", 100000, 0.0085, "2026-08-22 01:00:00"),
            ("open-1", "BUY", 100000, 0.0085, "2026-08-22 01:00:00"),
            ("A2:open-2", "BUY", 30000, 0.009024709, "2026-08-22 01:01:00"),
            ("A2:close-1", "SELL", 50000, 0.0090, "2026-08-22 02:00:00"),
            ("A2:close-2", "SELL", 80000, 0.0092, "2026-08-22 03:00:00"),
            ("A2:open-3", "BUY", 39001, 0.0091, "2026-08-23 01:00:00"),
            ("A2:close-3", "SELL", 39001, 0.0095, "2026-08-23 02:00:00"),
        ]
        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO fills
                   (account_id, symbol, side, position_side, quantity, price, trade_id,
                    created_at, strategy_source)
                   VALUES (2, 'AKEUSDT', ?, 'LONG', ?, ?, ?, ?, 'ake')""",
                [(side, quantity, price, trade_id, created_at)
                 for trade_id, side, quantity, price, created_at in fills],
            )
            conn.executemany(
                """INSERT INTO exchange_income_ledger
                   (account_id, income_id, symbol, income_type, income, trade_id, income_time)
                   VALUES (2, ?, 'AKEUSDT', 'REALIZED_PNL', ?, ?, ?)""",
                [
                    ("income-1", 17.5, "close-1", "2026-08-22 02:00:00"),
                    ("income-2", 11.5, "A2:close-2", "2026-08-22 03:00:00"),
                    ("income-3", 3.0, "close-3", "2026-08-23 02:00:00"),
                    ("income-4", 1.0, "unmatched", "2026-08-22 02:30:00"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        result = fetch_trade_history_summaries(account_id=2, limit=20)

        self.assertEqual(len(result["items"]), 1)
        row = result["items"][0]
        self.assertEqual((row["symbol"], row["side"]), ("AKEUSDT", "LONG"))
        self.assertEqual(row["quantity"], 169001)
        self.assertAlmostEqual(row["entry_price"], 0.00873160732776729)
        self.assertAlmostEqual(row["exit_price"], 0.009210060887213685)
        self.assertAlmostEqual(row["pnl"], 33.0)
        self.assertEqual(row["position_count"], 2)
        self.assertEqual(row["close_count"], 3)
        self.assertEqual(row["strategy_sources"], ["ake"])
        self.assertEqual(result["stats"]["total_cycles"], 2)
        self.assertEqual(result["stats"]["win_count"], 2)
        self.assertAlmostEqual(result["stats"]["total_pnl"], 33.0)

    def test_summary_filters_cycles_by_source_before_grouping_and_stats(self):
        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO fills
                   (account_id, symbol, side, position_side, quantity, price, trade_id,
                    created_at, strategy_source)
                   VALUES (2, 'AKEUSDT', ?, 'LONG', 10, ?, ?, ?, ?)""",
                [
                    ("BUY", 10, "alpha-open", "2026-08-22 01:00:00", "alpha"),
                    ("SELL", 11, "alpha-close", "2026-08-22 02:00:00", "alpha"),
                    ("BUY", 20, "normal-open", "2026-08-23 01:00:00", "normal"),
                    ("SELL", 19, "normal-close", "2026-08-23 02:00:00", "normal"),
                ],
            )
            conn.executemany(
                """INSERT INTO exchange_income_ledger
                   (account_id, income_id, symbol, income_type, income, trade_id, income_time)
                   VALUES (2, ?, 'AKEUSDT', 'REALIZED_PNL', ?, ?, ?)""",
                [
                    ("alpha-income", 1.0, "alpha-close", "2026-08-22 02:00:00"),
                    ("normal-income", -1.0, "normal-close", "2026-08-23 02:00:00"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        all_sources = fetch_trade_history_summaries(account_id=2)
        result = fetch_trade_history_summaries(account_id=2, source="alpha")

        self.assertEqual(len(all_sources["items"]), 1)
        self.assertEqual(
            all_sources["items"][0]["strategy_sources"],
            ["alpha", "normal"],
        )
        self.assertEqual(all_sources["stats"]["total_cycles"], 2)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["quantity"], 10)
        self.assertAlmostEqual(result["items"][0]["pnl"], 1.0)
        self.assertEqual(result["items"][0]["strategy_sources"], ["alpha"])
        self.assertEqual(result["stats"]["total_cycles"], 1)
        self.assertEqual(result["stats"]["win_count"], 1)
        self.assertEqual(result["stats"]["loss_count"], 0)
        self.assertAlmostEqual(result["stats"]["win_rate"], 100.0)

    def test_summary_validates_cursor_and_falls_back_to_position_trades(self):
        conn = db.get_conn()
        try:
            conn.execute(
                """INSERT INTO position_trades
                   (account_id, position_trade_id, symbol, side, strategy_source, entry_time,
                    exit_time, entry_price, exit_price, quantity, net_pnl, income_count)
                   VALUES (2, 'fallback-1', 'BTCUSDT', 'SHORT', 'alpha',
                           '2026-08-24 01:00:00', '2026-08-24 02:00:00',
                           100, 90, 2, 20, 3)"""
            )
            conn.commit()
        finally:
            conn.close()

        result = fetch_trade_history_summaries(
            account_id=2, symbol="BTCUSDT", source="alpha"
        )

        self.assertEqual(result["reconcile_status"], "incomplete")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["side"], "SHORT")
        self.assertEqual(result["items"][0]["position_count"], 1)
        self.assertEqual(result["items"][0]["close_count"], 3)
        self.assertEqual(result["items"][0]["strategy_sources"], ["alpha"])
        self.assertEqual(result["stats"]["total_cycles"], 1)
        for invalid_cursor in ("not-a-cursor", "a"):
            with self.subTest(cursor=invalid_cursor):
                with self.assertRaisesRegex(ValueError, "^invalid_history_cursor$"):
                    fetch_trade_history_summaries(
                        account_id=2, cursor=invalid_cursor
                    )

    def test_fallback_deduplicates_22_ake_rows_without_inventing_values(self):
        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO position_trades
                   (account_id, position_trade_id, symbol, side, strategy_source,
                    entry_time, exit_time, entry_price, exit_price, quantity,
                    net_pnl, income_count, source)
                   VALUES (2, ?, 'AKEUSDT', 'LONG', 'alpha',
                           '2026-08-22 01:00:00', '2026-08-22 02:00:00',
                           NULL, NULL, NULL, 3.25, 2, 'exchange_income')""",
                [(f"ake-duplicate-{index}",) for index in range(22)],
            )
            conn.commit()
        finally:
            conn.close()

        result = fetch_trade_history_summaries(
            account_id=2, symbol="AKEUSDT", source="alpha"
        )

        self.assertEqual(result["reconcile_status"], "incomplete")
        self.assertEqual(len(result["items"]), 1)
        row = result["items"][0]
        self.assertIsNone(row["quantity"])
        self.assertIsNone(row["entry_price"])
        self.assertIsNone(row["exit_price"])
        self.assertAlmostEqual(row["pnl"], 3.25)
        self.assertEqual(row["position_count"], 1)
        self.assertEqual(row["close_count"], 2)
        self.assertEqual(result["stats"]["total_cycles"], 1)
        self.assertAlmostEqual(result["stats"]["total_pnl"], 3.25)

    def test_income_uses_stable_identity_and_reconciles_execution_value(self):
        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO fills
                   (account_id, symbol, side, position_side, quantity, price,
                    trade_id, created_at, strategy_source)
                   VALUES (2, 'SOLUSDT', ?, 'LONG', 2, ?, ?, ?, 'alpha')""",
                [
                    ("BUY", 10, "sol-open", "2026-08-22 01:00:00"),
                    ("SELL", 12, "sol-close", "2026-08-22 02:00:00"),
                ],
            )
            conn.executemany(
                """INSERT INTO exchange_income_ledger
                   (account_id, income_id, symbol, income_type, income,
                    trade_id, income_time)
                   VALUES (2, ?, 'SOLUSDT', ?, ?, 'sol-close',
                           '2026-08-22 02:00:00')""",
                [
                    ("realized-copy-1", "REALIZED_PNL", 4.0),
                    ("realized-copy-2", "REALIZED_PNL", 4.0),
                    ("commission-copy-1", "COMMISSION", -0.2),
                    ("commission-copy-2", "COMMISSION", -0.2),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        result = fetch_trade_history_summaries(account_id=2)

        self.assertEqual(result["reconcile_status"], "ok")
        self.assertAlmostEqual(result["items"][0]["pnl"], 3.8)
        self.assertAlmostEqual(result["stats"]["total_pnl"], 3.8)

    def test_missing_income_is_incomplete_and_does_not_report_zero_pnl(self):
        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO fills
                   (account_id, symbol, side, position_side, quantity, price,
                    trade_id, created_at, strategy_source)
                   VALUES (2, 'ETHUSDT', ?, 'LONG', 1, ?, ?, ?, 'normal')""",
                [
                    ("BUY", 100, "eth-open", "2026-08-22 01:00:00"),
                    ("SELL", 105, "eth-close", "2026-08-22 02:00:00"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        result = fetch_trade_history_summaries(account_id=2)

        self.assertEqual(result["reconcile_status"], "incomplete")
        self.assertIsNone(result["items"][0]["pnl"])
        self.assertIsNone(result["stats"]["total_pnl"])

    def test_income_value_mismatch_is_reported_without_replacing_ledger_pnl(self):
        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO fills
                   (account_id, symbol, side, position_side, quantity, price,
                    trade_id, created_at, strategy_source)
                   VALUES (2, 'ETHUSDT', ?, 'LONG', 1, ?, ?, ?, 'normal')""",
                [
                    ("BUY", 100, "eth-open", "2026-08-22 01:00:00"),
                    ("SELL", 105, "eth-close", "2026-08-22 02:00:00"),
                ],
            )
            conn.execute(
                """INSERT INTO exchange_income_ledger
                   (account_id, income_id, symbol, income_type, income,
                    trade_id, income_time)
                   VALUES (2, 'eth-income', 'ETHUSDT', 'REALIZED_PNL', 4.5,
                           'eth-close', '2026-08-22 02:00:00')"""
            )
            conn.commit()
        finally:
            conn.close()

        result = fetch_trade_history_summaries(account_id=2)

        self.assertEqual(result["reconcile_status"], "mismatch")
        self.assertAlmostEqual(result["items"][0]["pnl"], 4.5)

    def test_fill_and_fallback_cycles_merge_without_double_counting_coverage(self):
        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO fills
                   (account_id, symbol, side, position_side, quantity, price,
                    trade_id, created_at, strategy_source)
                   VALUES (2, 'BTCUSDT', ?, 'LONG', 1, ?, ?, ?, 'alpha')""",
                [
                    ("BUY", 100, "btc-open", "2026-08-23 01:00:00"),
                    ("SELL", 110, "btc-close", "2026-08-23 02:00:00"),
                ],
            )
            conn.execute(
                """INSERT INTO exchange_income_ledger
                   (account_id, income_id, symbol, income_type, income,
                    trade_id, income_time)
                   VALUES (2, 'btc-income', 'BTCUSDT', 'REALIZED_PNL', 10,
                           'btc-close', '2026-08-23 02:00:00')"""
            )
            conn.executemany(
                """INSERT INTO position_trades
                   (account_id, position_trade_id, symbol, side, strategy_source,
                    entry_time, exit_time, entry_price, exit_price, quantity,
                    net_pnl, income_count, source)
                   VALUES (2, ?, ?, ?, 'alpha', ?, ?, ?, ?, ?, ?, 1,
                           'exchange_income')""",
                [
                    (
                        "btc-covered", "BTCUSDT", "LONG",
                        "2026-08-23 01:00:00", "2026-08-23 02:00:00",
                        100, 110, 1, 10,
                    ),
                    (
                        "btc-legacy", "BTCUSDT", "LONG",
                        "2026-08-21 01:00:00", "2026-08-21 02:00:00",
                        50, 55, 2, 10,
                    ),
                    (
                        "eth-legacy", "ETHUSDT", "SHORT",
                        "2026-08-20 01:00:00", "2026-08-20 02:00:00",
                        40, 35, 1, 5,
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        result = fetch_trade_history_summaries(account_id=2)
        rows = {row["symbol"]: row for row in result["items"]}

        self.assertEqual(result["reconcile_status"], "incomplete")
        self.assertEqual(set(rows), {"BTCUSDT", "ETHUSDT"})
        self.assertEqual(rows["BTCUSDT"]["quantity"], 3)
        self.assertAlmostEqual(rows["BTCUSDT"]["pnl"], 20)
        self.assertEqual(rows["BTCUSDT"]["position_count"], 2)
        self.assertEqual(rows["BTCUSDT"]["close_count"], 2)
        self.assertEqual(rows["ETHUSDT"]["position_count"], 1)
        self.assertEqual(result["stats"]["total_cycles"], 3)

    def test_binance_sync_fills_recover_strategy_from_local_cycle_context(self):
        conn = db.get_conn()
        try:
            conn.execute(
                """INSERT INTO orders
                   (account_id, symbol, side, order_type, status, reason,
                    strategy_source, created_at)
                   VALUES (2, 'AKEUSDT', 'BUY', 'MARKET', 'filled',
                           'normal_entry', 'alpha', '2026-08-22 00:59:30')"""
            )
            conn.execute(
                """INSERT INTO position_trades
                   (account_id, position_trade_id, symbol, side, strategy_source,
                    entry_time, exit_time, entry_price, exit_price, quantity,
                    net_pnl, income_count, source)
                   VALUES (2, 'btc-local-cycle', 'BTCUSDT', 'SHORT', 'normal',
                           '2026-08-23 01:00:00', '2026-08-23 02:00:00',
                           20, 18, 1, 2, 1, 'exchange_income')"""
            )
            conn.commit()
        finally:
            conn.close()

        token = db.set_account_context(2)
        try:
            for item in [
                {
                    "id": 101, "orderId": 9001, "symbol": "AKEUSDT",
                    "side": "BUY", "positionSide": "BOTH", "qty": 2,
                    "price": 10, "realizedPnl": 0,
                    "time": "2026-08-22 01:00:00",
                },
                {
                    "id": 102, "orderId": 9002, "symbol": "AKEUSDT",
                    "side": "SELL", "positionSide": "BOTH", "qty": 2,
                    "price": 12, "realizedPnl": 4,
                    "time": "2026-08-22 02:00:00",
                },
                {
                    "id": 201, "orderId": 9101, "symbol": "BTCUSDT",
                    "side": "SELL", "positionSide": "BOTH", "qty": 1,
                    "price": 20, "realizedPnl": 0,
                    "time": "2026-08-23 01:00:00",
                },
                {
                    "id": 202, "orderId": 9102, "symbol": "BTCUSDT",
                    "side": "BUY", "positionSide": "BOTH", "qty": 1,
                    "price": 18, "realizedPnl": 2,
                    "time": "2026-08-23 02:00:00",
                },
            ]:
                db.upsert_exchange_fill(item)
            db.upsert_exchange_income(
                {
                    "tranId": 1002, "tradeId": 102, "symbol": "AKEUSDT",
                    "incomeType": "REALIZED_PNL", "income": 4,
                    "time": "2026-08-22 02:00:00",
                }
            )
            db.upsert_exchange_income(
                {
                    "tranId": 2002, "tradeId": 202, "symbol": "BTCUSDT",
                    "incomeType": "REALIZED_PNL", "income": 2,
                    "time": "2026-08-23 02:00:00",
                }
            )
        finally:
            db.reset_account_context(token)

        alpha = fetch_trade_history_summaries(account_id=2, source="alpha")
        normal = fetch_trade_history_summaries(account_id=2, source="normal")

        self.assertEqual([row["symbol"] for row in alpha["items"]], ["AKEUSDT"])
        self.assertEqual(alpha["items"][0]["strategy_sources"], ["alpha"])
        self.assertEqual([row["symbol"] for row in normal["items"]], ["BTCUSDT"])
        self.assertEqual(normal["items"][0]["strategy_sources"], ["normal"])

    def test_summary_paginates_lifetime_rows_by_exit_time_and_date_filter(self):
        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO fills
                   (account_id, symbol, side, position_side, quantity, price, trade_id,
                    created_at, strategy_source)
                   VALUES (2, ?, ?, 'LONG', 1, ?, ?, ?, 'alpha')""",
                [
                    ("ETHUSDT", "BUY", 100, "eth-open", "2026-08-22 01:00:00"),
                    ("ETHUSDT", "SELL", 101, "eth-close", "2026-08-22 02:00:00"),
                    ("BTCUSDT", "BUY", 200, "btc-open", "2026-08-23 01:00:00"),
                    ("BTCUSDT", "SELL", 201, "btc-close", "2026-08-23 02:00:00"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        first_page = fetch_trade_history_summaries(account_id=2, limit=1)
        second_page = fetch_trade_history_summaries(
            account_id=2, limit=1, cursor=first_page["next_cursor"]
        )
        filtered = fetch_trade_history_summaries(
            account_id=2, from_time="2026-08-23 00:00:00"
        )

        self.assertEqual([row["symbol"] for row in first_page["items"]], ["BTCUSDT"])
        self.assertIsNotNone(first_page["next_cursor"])
        self.assertEqual([row["symbol"] for row in second_page["items"]], ["ETHUSDT"])
        self.assertIsNone(second_page["next_cursor"])
        self.assertEqual([row["symbol"] for row in filtered["items"]], ["BTCUSDT"])

    def test_cursor_freezes_as_of_watermark_when_new_cycle_arrives(self):
        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO fills
                   (account_id, symbol, side, position_side, quantity, price,
                    trade_id, created_at, strategy_source)
                   VALUES (2, ?, ?, 'LONG', 1, ?, ?, ?, 'alpha')""",
                [
                    ("ETHUSDT", "BUY", 100, "eth-old-open", "2026-08-21 01:00:00"),
                    ("ETHUSDT", "SELL", 101, "eth-old-close", "2026-08-21 02:00:00"),
                    ("BTCUSDT", "BUY", 200, "btc-open", "2026-08-23 01:00:00"),
                    ("BTCUSDT", "SELL", 201, "btc-close", "2026-08-23 02:00:00"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        first_page = fetch_trade_history_summaries(account_id=2, limit=1)

        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO fills
                   (account_id, symbol, side, position_side, quantity, price,
                    trade_id, created_at, strategy_source)
                   VALUES (2, 'ETHUSDT', ?, 'LONG', 2, ?, ?, ?, 'alpha')""",
                [
                    ("BUY", 110, "eth-new-open", "2026-08-24 01:00:00"),
                    ("SELL", 112, "eth-new-close", "2026-08-24 02:00:00"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        second_page = fetch_trade_history_summaries(
            account_id=2, limit=1, cursor=first_page["next_cursor"]
        )
        fresh_page = fetch_trade_history_summaries(account_id=2, limit=1)

        self.assertEqual([row["symbol"] for row in first_page["items"]], ["BTCUSDT"])
        self.assertEqual([row["symbol"] for row in second_page["items"]], ["ETHUSDT"])
        self.assertEqual(second_page["items"][0]["quantity"], 1)
        self.assertEqual(second_page["items"][0]["exit_time"], "2026-08-21 02:00:00")
        self.assertEqual([row["symbol"] for row in fresh_page["items"]], ["ETHUSDT"])
        self.assertEqual(fresh_page["items"][0]["quantity"], 3)

        legacy_cursor = base64.urlsafe_b64encode(
            json.dumps(["2026-08-23 02:00:00", "BTCUSDT", "LONG"]).encode()
        ).decode().rstrip("=")
        with self.assertRaisesRegex(ValueError, "^invalid_history_cursor$"):
            fetch_trade_history_summaries(account_id=2, cursor=legacy_cursor)

    def test_same_second_fills_use_numeric_id_order(self):
        first = fill(1, "AKEUSDT", "BUY", 1, 10, "t2", "2026-08-22 01:00:00")
        second = fill(1, "AKEUSDT", "SELL", 1, 12, "t10", "2026-08-22 01:00:00")
        first["id"] = 2
        second["id"] = 10

        cycle = reconstruct_position_cycles([first, second])[0]

        self.assertEqual(cycle["direction"], "LONG")
        self.assertAlmostEqual(cycle["entry_price"], 10)
        self.assertAlmostEqual(cycle["exit_price"], 12)

    def test_non_execution_sides_are_ignored_before_cycle_accounting(self):
        fills = [
            fill(1, "AKEUSDT", "REALIZED_PNL", 1, 100, "income", "2026-08-22 01:00:00"),
            fill(1, "AKEUSDT", "BUY", 1, 10, "buy", "2026-08-22 02:00:00"),
            fill(1, "AKEUSDT", "SELL", 1, 12, "sell", "2026-08-22 03:00:00"),
        ]

        unique = deduplicate_fills(fills)
        cycles = reconstruct_position_cycles(fills)

        self.assertEqual([row["side"] for row in unique], ["BUY", "SELL"])
        self.assertEqual(len(cycles), 1)
        self.assertEqual(cycles[0]["direction"], "LONG")
        self.assertEqual(cycles[0]["entry_fills"][0]["trade_id"], "buy")
        self.assertEqual(cycles[0]["exit_fills"][0]["trade_id"], "sell")

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
