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

    def test_range_market_phase_prevents_roll_even_after_tp1_and_r_trigger(self):
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

        self.assertEqual(actions, [])
        self.assertEqual(update.call_args.kwargs["roll_block_reason"], "market_phase_range")

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


if __name__ == "__main__":
    unittest.main()
