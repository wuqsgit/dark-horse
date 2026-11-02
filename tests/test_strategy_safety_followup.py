import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

import shared.db as db
from trader import cooldown_manager as cooldown
from trader.risk import calculate_position
from trader.market_regime import adjust_strategy_for_regime
from trader.execution import ExecutionEngine


class Exchange:
    def get_atr(self, symbol):
        return 2.0


class SafetyFollowupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, "DB_PATH", str(Path(self.temp.name) / "test.db"))
        self.path_patch.start()
        self.token = db.set_account_context(1)
        db.set_position_context("", "")
        db.init_db()

    def tearDown(self):
        db.reset_account_context(self.token)
        db.set_position_context("", "")
        self.path_patch.stop()
        self.temp.cleanup()

    def test_zero_factor_disables_position(self):
        result = calculate_position(Exchange(), "ETHUSDT", 100, 1000, entry_mode="strong", size_multiplier=0)
        self.assertEqual(result["quantity"], 0)

    def test_small_factor_is_not_inflated(self):
        small = calculate_position(Exchange(), "ETHUSDT", 100, 1000, entry_mode="strong", size_multiplier=.01)
        larger = calculate_position(Exchange(), "ETHUSDT", 100, 1000, entry_mode="strong", size_multiplier=.1)
        self.assertAlmostEqual(small["target_margin"] * 10, larger["target_margin"])

    def test_bull_market_cannot_create_extra_slots(self):
        self.assertEqual(adjust_strategy_for_regime("Bull Market", 60, 0)[1], 0)

    def test_profit_reset_is_committed(self):
        cooldown.record_stop("TESTUSDT")
        cooldown.record_profit("TESTUSDT")
        conn = db.get_conn()
        row = conn.execute("SELECT consecutive_stops FROM trade_cooldown WHERE symbol='TESTUSDT'").fetchone()
        conn.close()
        self.assertEqual(row[0], 0)

    def test_cooldown_is_account_scoped(self):
        cooldown.record_stop("TESTUSDT")
        token = db.set_account_context(2)
        try:
            self.assertFalse(cooldown.is_in_cooldown("TESTUSDT")[0])
        finally:
            db.reset_account_context(token)

    def test_directional_history_preserves_both_sides(self):
        for side, qty in (("LONG", 1), ("SHORT", 2)):
            db.upsert_position_history("ETHUSDT", side, qty, 100, "test", 80, 110, 2)
        self.assertEqual(db.get_position_history("ETHUSDT", "LONG")["quantity"], 1)
        self.assertEqual(db.get_position_history("ETHUSDT", "SHORT")["quantity"], 2)
        db.set_position_context("ETHUSDT", "SHORT")
        db.update_position_management("ETHUSDT", quantity=3)
        db.delete_position_history("ETHUSDT")
        self.assertEqual(db.get_position_history("ETHUSDT", "LONG")["quantity"], 1)
        self.assertIsNone(db.get_position_history("ETHUSDT", "SHORT"))

    def test_quote_volume_sums_day_not_latest_hour(self):
        conn = db.get_conn()
        now = datetime.now(timezone.utc)
        for hours in (1, 2, 3):
            conn.execute("INSERT INTO candles_1h(time,symbol,quote_vol) VALUES(?,?,?)",
                         ((now-timedelta(hours=hours)).isoformat(), "TESTUSDT", 100))
        conn.commit()
        conn.close()
        self.assertEqual(db.fetch_24h_quote_volume("TESTUSDT"), 300)

    def test_roll_cannot_bypass_loss_circuit_breaker(self):
        class LiveExchange:
            account_id = 1

            def get_positions(self):
                return [{"symbol": "ETHUSDT", "side": "LONG", "quantity": 1,
                         "entry_price": 100, "mark_price": 110, "unrealized_pnl": 10,
                         "margin": 50}]

        engine = ExecutionEngine(LiveExchange())
        engine.get_balance = lambda: 1000
        engine._record_decision = lambda *a, **k: None
        action = {"action": "roll_add", "symbol": "ETHUSDT", "side": "BUY",
                  "position_side": "LONG", "quantity": .2, "entry_price": 110,
                  "leverage": 2, "strategy_source": "normal"}
        with patch("shared.accounts.account_allows_new_exposure", return_value=True), \
             patch("shared.db.is_market_entry_ready", return_value=(True, None)), \
             patch("trader.execution._account_loss_state", return_value=(-20, 3)):
            results = engine.execute([action])
        self.assertEqual(results[0]["status"], "blocked")
