import os
import tempfile
import unittest

import shared.db as db


class UserTradeIncomeFallbackTest(unittest.TestCase):
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.temp.name, "trades.db")
        db.init_db()
        self.account_token = db.set_account_context(1)

    def tearDown(self):
        db.reset_account_context(self.account_token)
        db.DB_PATH = self.original_db_path
        self.temp.cleanup()

    def _store_closed_trade(self):
        db.upsert_exchange_fill({
            "id": "101",
            "symbol": "TAKEUSDT",
            "side": "BUY",
            "positionSide": "BOTH",
            "qty": "100",
            "price": "1.00",
            "realizedPnl": "0",
            "commission": "0.04",
            "commissionAsset": "USDT",
            "time": 1786810200000,
        })
        db.upsert_exchange_fill({
            "id": "102",
            "symbol": "TAKEUSDT",
            "side": "SELL",
            "positionSide": "BOTH",
            "qty": "100",
            "price": "1.02",
            "realizedPnl": "2.00",
            "commission": "0.04",
            "commissionAsset": "USDT",
            "time": 1786816260000,
        })

    def test_realized_pnl_from_user_trades_backfills_missing_income(self):
        self._store_closed_trade()

        self.assertEqual(db.backfill_income_ledger_from_fills(), 1)
        self.assertEqual(db.backfill_income_ledger_from_fills(), 0)

        db.upsert_exchange_income({
            "tradeId": "101",
            "symbol": "TAKEUSDT",
            "incomeType": "COMMISSION",
            "income": "-0.04",
            "asset": "USDT",
            "time": 1786810200000,
        })
        db.upsert_exchange_income({
            "tradeId": "102",
            "symbol": "TAKEUSDT",
            "incomeType": "COMMISSION",
            "income": "-0.04",
            "asset": "USDT",
            "time": 1786816260000,
        })

        conn = db.get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM exchange_income_ledger
                   WHERE income_type='REALIZED_PNL' AND symbol='TAKEUSDT'"""
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["income"], 2.0)
        self.assertEqual(row["trade_id"], "A1:102")
        self.assertEqual(row["source"], "binance_user_trades_fallback")

        db.rebuild_position_trades_from_income()
        trades = db.fetch_position_trade_groups(10)
        take = next(row for row in trades if row["symbol"] == "TAKEUSDT")
        self.assertAlmostEqual(take["pnl"], 1.92)
        self.assertEqual(take["side"], "LONG")
        self.assertEqual(take["entry_time"], "2026-08-15 16:10:00")

    def test_exchange_income_wins_over_user_trade_fallback(self):
        self._store_closed_trade()
        db.upsert_exchange_income({
            "tradeId": "102",
            "symbol": "TAKEUSDT",
            "incomeType": "REALIZED_PNL",
            "income": "2.00",
            "asset": "USDT",
            "time": 1786816260000,
        })

        self.assertEqual(db.backfill_income_ledger_from_fills(), 0)
        conn = db.get_conn()
        try:
            rows = conn.execute(
                """SELECT source FROM exchange_income_ledger
                   WHERE income_type='REALIZED_PNL' AND symbol='TAKEUSDT'"""
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([row["source"] for row in rows], ["binance_income"])


if __name__ == "__main__":
    unittest.main()
