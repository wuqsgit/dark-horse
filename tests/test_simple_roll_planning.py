import os
import tempfile
import unittest
from unittest.mock import patch

import shared.db as db
from trader.execution import ExecutionEngine


class RollPlanningExchange:
    def get_symbol_info(self, symbol):
        return {"step_size": 0.1, "min_qty": 0.1, "min_notional": 5.0}


class OpenExchange:
    def set_leverage(self, symbol, leverage):
        pass

    def place_market_order(self, symbol, side, quantity):
        return {"orderId": "open-1"}

    def place_stop_order(self, symbol, side, quantity, stop_price):
        return {"orderId": "stop-1"}


class FailedStopOpenExchange(OpenExchange):
    def __init__(self):
        self.flattened = []

    def place_stop_order(self, symbol, side, quantity, stop_price):
        raise RuntimeError("stop rejected")

    def close_position_market(self, symbol, side, quantity):
        self.flattened.append((symbol, side, quantity))
        return {"orderId": "flatten-1", "executedQty": str(quantity)}


class RecoveryExchange:
    def get_atr(self, symbol):
        return 2.0


class SimpleRollPlanningTest(unittest.TestCase):
    def test_initial_quantity_is_written_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                db.upsert_position_history(
                    "BTCUSDT", "LONG", 10, 100, "entry", 80, 120, 2,
                    initial_stop_loss=95,
                )
                db.upsert_position_history(
                    "BTCUSDT", "LONG", 7, 100, "entry", 80, 120, 2,
                    initial_stop_loss=95,
                )
                row = db.get_position_history("BTCUSDT")

        self.assertEqual(row["quantity"], 7)
        self.assertEqual(row["initial_quantity"], 10)

    def test_new_open_fills_initial_quantity_on_stale_legacy_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                db.upsert_position_history(
                    "RAVEUSDT", "LONG", 100, 1, "old", 50, 2, 0.1,
                    initial_stop_loss=0.9,
                )
                conn = db.get_conn()
                conn.execute("UPDATE position_history SET initial_quantity=NULL WHERE symbol='RAVEUSDT'")
                conn.commit()
                conn.close()
                db.upsert_position_history(
                    "RAVEUSDT", "LONG", 368, 0.27, "new", 80, 0.4, 0.02,
                    position_id="new-position", initial_stop_loss=0.24,
                )
                row = db.get_position_history("RAVEUSDT")

        self.assertEqual(row["initial_quantity"], 368)
        self.assertEqual(row["position_id"], "new-position")

    def test_recovers_untracked_live_position_and_prior_partial_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                conn = db.get_conn()
                conn.execute(
                    """INSERT INTO orders
                       (account_id, symbol, side, order_type, quantity, price, reason,
                        position_id, strategy_source, signal_source, created_at)
                       VALUES (1, 'ETHUSDT', 'BUY', 'MARKET', 10, 100,
                               'bluechip_trend:probe', 'p1', 'normal',
                               'bluechip_trend', datetime('now', '-2 hours'))"""
                )
                conn.execute(
                    """INSERT INTO trades
                       (account_id, position_id, symbol, side, quantity, entry_price,
                        exit_price, pnl, pnl_pct, exit_reason, entry_time, exit_time)
                       VALUES (1, 'p1', 'ETHUSDT', 'LONG', 5, 100, 105, 25, 5,
                               'bluechip_TP1 r>=1',
                               datetime('now', '-2 hours'), datetime('now', '-1 hour'))"""
                )
                conn.commit()
                conn.close()

                engine = ExecutionEngine(RecoveryExchange())
                recovered = engine.recover_untracked_positions(
                    [{
                        "symbol": "ETHUSDT", "side": "LONG", "quantity": 5,
                        "entry_price": 100, "mark_price": 113,
                    }],
                    [{
                        "symbol": "ETHUSDT",
                        "raw_features": {"technical": {"atr": 2}},
                    }],
                )
                row = db.get_position_history("ETHUSDT")

        self.assertEqual(recovered, ["ETHUSDT"])
        self.assertEqual(row["initial_quantity"], 10)
        self.assertEqual(row["quantity"], 5)
        self.assertEqual(row["tp1_hit"], 1)
        self.assertEqual(row["tp2_hit"], 1)
        self.assertEqual(row["signal_source"], "bluechip_trend")
        self.assertAlmostEqual(row["initial_stop_loss"], 96)

    def test_recovery_does_not_reuse_a_fully_closed_old_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            with patch.object(db, "DB_PATH", db_path):
                db.init_db()
                conn = db.get_conn()
                conn.execute(
                    """INSERT INTO orders
                       (account_id, symbol, side, order_type, quantity, price, reason,
                        position_id, strategy_source, signal_source, created_at)
                       VALUES (1, 'ETHUSDT', 'BUY', 'MARKET', 10, 100,
                               'old_bluechip_entry', 'old-p1', 'normal',
                               'bluechip_trend', datetime('now', '-20 days'))"""
                )
                conn.execute(
                    """INSERT INTO trades
                       (account_id, position_id, symbol, side, quantity, entry_price,
                        exit_price, pnl, pnl_pct, exit_reason, entry_time, exit_time)
                       VALUES (1, 'old-p1', 'ETHUSDT', 'LONG', 10, 100, 101, 10, 1,
                               'old_full_close',
                               datetime('now', '-20 days'), datetime('now', '-19 days'))"""
                )
                conn.commit()
                conn.close()

                engine = ExecutionEngine(RecoveryExchange())
                recovered = engine.recover_untracked_positions(
                    [{
                        "symbol": "ETHUSDT", "side": "LONG", "quantity": 2,
                        "entry_price": 100, "mark_price": 113,
                    }],
                    [{
                        "symbol": "ETHUSDT",
                        "raw_features": {"technical": {"atr": 2}},
                    }],
                )
                row = db.get_position_history("ETHUSDT")

        self.assertEqual(recovered, ["ETHUSDT"])
        self.assertEqual(row["initial_quantity"], 2)
        self.assertEqual(row["tp1_hit"], 1)
        self.assertEqual(row["tp2_hit"], 1)
        self.assertIsNone(row["signal_source"])

    def test_builds_one_roll_from_twenty_five_percent_of_initial_quantity(self):
        engine = ExecutionEngine(RollPlanningExchange())
        engine._record_decision = lambda *args, **kwargs: None
        position = {
            "symbol": "BTCUSDT", "side": "LONG", "quantity": 6,
            "entry_price": 100, "mark_price": 110, "leverage": 3,
            "unrealized_pnl": 60,
        }
        state = {
            "position_id": "p1", "strategy_source": "normal",
            "initial_quantity": 10, "initial_stop_loss": 95, "atr_value": 2,
            "tp1_hit": 1, "roll_layer": 0,
        }
        scores = [{
            "symbol": "BTCUSDT", "composite_score": 80,
            "raw_features": {"technical": {"ema20": 105, "ema20_slope": 1.2}},
        }]

        with patch("shared.db.get_position_history", return_value=state), \
             patch("shared.db.update_position_management") as update:
            actions = engine._build_roll_actions(scores, [position], [], 5000, run_id="run1")

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["roll_layer"], 1)
        self.assertEqual(actions[0]["quantity"], 2.5)
        self.assertAlmostEqual(actions[0]["current_r"], 2.0)
        update.assert_called()

    def test_range_market_phase_allows_roll_when_trend_confirms(self):
        engine = ExecutionEngine(RollPlanningExchange())
        engine._record_decision = lambda *args, **kwargs: None
        position = {
            "symbol": "BTCUSDT", "side": "LONG", "quantity": 6,
            "entry_price": 100, "mark_price": 110, "leverage": 3,
            "unrealized_pnl": 60,
        }
        state = {
            "position_id": "p1", "strategy_source": "normal",
            "initial_quantity": 10, "initial_stop_loss": 95, "atr_value": 2,
            "tp1_hit": 1, "roll_layer": 0,
        }
        scores = [{
            "symbol": "BTCUSDT", "composite_score": 80,
            "raw_features": {
                "technical": {"ema20": 105, "ema20_slope": 1.2},
                "market_phase": {
                    "phase": "range",
                    "allow_roll": False,
                    "reason": "EMA flat",
                },
            },
        }]

        with patch("shared.db.get_position_history", return_value=state), \
             patch("shared.db.update_position_management") as update:
            actions = engine._build_roll_actions(scores, [position], [], 5000, run_id="run1")

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["roll_layer"], 1)
        update.assert_called()

    def test_builds_second_roll_after_armed_pullback_recovers(self):
        engine = ExecutionEngine(RollPlanningExchange())
        engine._record_decision = lambda *args, **kwargs: None
        position = {
            "symbol": "BTCUSDT", "side": "LONG", "quantity": 7,
            "entry_price": 100, "mark_price": 111.6, "leverage": 3,
            "unrealized_pnl": 81.2,
        }
        state = {
            "position_id": "p1", "strategy_source": "normal",
            "initial_quantity": 10, "initial_stop_loss": 95, "atr_value": 2,
            "tp1_hit": 1, "roll_layer": 1, "roll_price": 108,
            "roll_cycle_peak_price": 112, "roll_pullback_armed": 1,
        }
        scores = [{
            "symbol": "BTCUSDT", "composite_score": 80,
            "raw_features": {"technical": {"ema20": 108, "ema20_slope": 1.2}},
        }]

        with patch("shared.db.get_position_history", return_value=state), \
             patch("shared.db.update_position_management") as update:
            actions = engine._build_roll_actions(scores, [position], [], 5000, run_id="run1")

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["roll_layer"], 2)
        self.assertEqual(actions[0]["quantity"], 2.0)
        self.assertIn("trigger=pullback_recovery", actions[0]["reason"])
        update.assert_called()

    def test_builds_second_roll_for_any_normal_symbol_on_sustained_profit(self):
        engine = ExecutionEngine(RollPlanningExchange())
        engine._record_decision = lambda *args, **kwargs: None
        position = {
            "symbol": "MEMEUSDT", "side": "LONG", "quantity": 7,
            "entry_price": 100, "mark_price": 113, "leverage": 3,
            "unrealized_pnl": 91,
        }
        state = {
            "position_id": "p1", "strategy_source": "normal",
            "initial_quantity": 10, "initial_stop_loss": 95, "atr_value": 2,
            "tp1_hit": 1, "roll_layer": 1, "roll_price": 108,
            "roll_cycle_peak_price": 113, "roll_pullback_armed": 0,
        }
        scores = [{
            "symbol": "MEMEUSDT", "composite_score": 60,
            "raw_features": {"technical": {"ema20": 108, "ema20_slope": 1.2}},
        }]

        with patch("shared.db.get_position_history", return_value=state), \
             patch("shared.db.update_position_management"):
            actions = engine._build_roll_actions(scores, [position], [], 5000, run_id="run1")

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["roll_layer"], 2)
        self.assertEqual(actions[0]["quantity"], 2.0)
        self.assertIn("trigger=sustained_profit", actions[0]["reason"])

    def test_builds_sustained_profit_roll_for_synced_alpha_symbol(self):
        engine = ExecutionEngine(RollPlanningExchange())
        engine._record_decision = lambda *args, **kwargs: None
        position = {
            "symbol": "B2USDT", "side": "LONG", "quantity": 7,
            "entry_price": 100, "mark_price": 113, "leverage": 3,
            "unrealized_pnl": 91,
        }
        state = {
            "position_id": "p1", "strategy_source": "alpha",
            "alpha_profile": "futures_mapped",
            "initial_quantity": 10, "initial_stop_loss": 95, "atr_value": 2,
            "tp1_hit": 1, "roll_layer": 1, "roll_price": 108,
            "roll_cycle_peak_price": 113, "roll_pullback_armed": 0,
        }
        scores = [{
            "symbol": "B2USDT", "composite_score": 60,
            "raw_features": {
                "technical": {"ema20": 108, "ema20_slope": 1.2},
                "dual_market_volume": {"synchronized": True},
            },
        }]

        with patch("shared.db.get_position_history", return_value=state), \
             patch("shared.db.update_position_management"):
            actions = engine._build_roll_actions(scores, [position], [], 5000, run_id="run1")

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["strategy_source"], "alpha")
        self.assertEqual(actions[0]["roll_layer"], 2)
        self.assertIn("trigger=sustained_profit", actions[0]["reason"])

    def test_data_insufficient_uncertain_market_phase_still_prevents_roll(self):
        engine = ExecutionEngine(RollPlanningExchange())
        engine._record_decision = lambda *args, **kwargs: None
        position = {
            "symbol": "BTCUSDT", "side": "LONG", "quantity": 6,
            "entry_price": 100, "mark_price": 110, "leverage": 3,
            "unrealized_pnl": 60,
        }
        state = {
            "position_id": "p1", "strategy_source": "normal",
            "initial_quantity": 10, "initial_stop_loss": 95, "atr_value": 2,
            "tp1_hit": 1, "roll_layer": 0,
        }
        scores = [{
            "symbol": "BTCUSDT", "composite_score": 80,
            "raw_features": {
                "technical": {"ema20": 105, "ema20_slope": 1.2},
                "market_phase": {
                    "phase": "uncertain",
                    "confidence": 20,
                    "position_style": "skip",
                },
            },
        }]

        with patch("shared.db.get_position_history", return_value=state), \
             patch("shared.db.update_position_management") as update:
            actions = engine._build_roll_actions(scores, [position], [], 5000, run_id="run1")

        self.assertEqual(actions, [])
        self.assertEqual(update.call_args.kwargs["roll_block_reason"], "market_phase_uncertain")

    def test_planned_close_prevents_roll(self):
        engine = ExecutionEngine(RollPlanningExchange())
        actions = engine._build_roll_actions(
            [],
            [{"symbol": "BTCUSDT"}],
            [{"action": "partial_close", "symbol": "BTCUSDT"}],
            5000,
        )
        self.assertEqual(actions, [])

    def test_new_position_persists_complete_roll_state(self):
        engine = ExecutionEngine(OpenExchange())
        engine._record_decision = lambda *args, **kwargs: None
        act = {
            "action": "open", "symbol": "BTCUSDT", "side": "BUY",
            "position_side": "LONG", "quantity": 10, "entry_price": 100,
            "stop_loss": 95, "stop_model": "structure_atr", "stop_pct": 0.05,
            "trailing_atr_multiplier": 2, "atr_value": 2, "leverage": 3,
            "reason": "trend", "score": 80,
        }
        with patch("shared.db.new_position_id", return_value="p1"), \
             patch("shared.db.insert_order"), \
             patch("shared.db.upsert_position_history") as upsert, \
             patch("shared.db.record_entry_review_snapshot") as entry_snapshot, \
             patch("trader.execution.record_profit"):
            engine._execute_open(act, [])

        kwargs = upsert.call_args.kwargs
        self.assertEqual(kwargs["initial_stop_loss"], 95)
        self.assertEqual(kwargs["stop_model"], "structure_atr")
        self.assertEqual(kwargs["trailing_atr_multiplier"], 2)
        entry_snapshot.assert_called_once()

    def test_new_position_is_flattened_when_exchange_stop_is_rejected(self):
        exchange = FailedStopOpenExchange()
        engine = ExecutionEngine(exchange)
        engine._record_decision = lambda *args, **kwargs: None
        act = {
            "action": "open", "symbol": "BTCUSDT", "side": "BUY",
            "position_side": "LONG", "quantity": 10, "entry_price": 100,
            "stop_loss": 95, "stop_model": "structure_atr", "stop_pct": 0.05,
            "trailing_atr_multiplier": 2, "atr_value": 2, "leverage": 3,
            "reason": "trend", "score": 80,
        }

        with patch("shared.db.new_position_id", return_value="p1"), \
             patch("shared.db.insert_order"), \
             patch("trader.execution.record_profit"):
            with self.assertRaisesRegex(
                RuntimeError,
                "opened position flattened",
            ):
                engine._execute_open(act, [])

        self.assertEqual(exchange.flattened, [("BTCUSDT", "SELL", 10)])


if __name__ == "__main__":
    unittest.main()
