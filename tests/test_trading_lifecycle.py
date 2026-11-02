import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import shared.db as db
from shared.trading_journal import TradingJournal, intent_id
from trader.action_coordinator import coordinate_actions
from trader.lifecycle_policy import evaluate_entry, evaluate_management, evaluate_add_risk
from trader.execution import ExecutionEngine, _position_r_state, _strongest_management_stop
from trader.roll_policy import evaluate_roll
from backtest.lifecycle_replay import replay_lifecycle


NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def opening(**kwargs):
    return {"action": "open", "symbol": "XUSDT", "side": "BUY", "position_side": "LONG",
            "scan_id": "scan1", "quantity": 10, "entry_price": 100, "stop_loss": 95,
            "position_id": "p1", "signal_time": NOW.isoformat(), **kwargs}


class CoordinationTest(unittest.TestCase):
    def test_stop_wins_regardless_of_input_order(self):
        stop = {**opening(), "action": "close", "side": "SELL", "is_stop": True}
        for actions in ([opening(), stop], [stop, opening()]):
            result = coordinate_actions(actions)
            self.assertEqual(result.accepted[0]["action"], "close")
            self.assertEqual(len(result.rejected), 1)

    def test_hedge_sides_and_accounts_are_independent(self):
        actions = [opening(account_id=1), opening(account_id=2), opening(account_id=1, position_side="SHORT", side="SELL")]
        self.assertEqual(len(coordinate_actions(actions).accepted), 3)

    def test_reduction_wins_over_add(self):
        result = coordinate_actions([opening(action="roll_add"), opening(action="partial_close", side="SELL")])
        self.assertEqual(result.accepted[0]["action"], "partial_close")

    def test_missing_direction_is_not_guessed(self):
        result = coordinate_actions([{"action": "open", "symbol": "XUSDT"}])
        self.assertEqual(result.accepted, [])

    def test_contradictory_direction_is_rejected(self):
        self.assertEqual(coordinate_actions([opening(position_side="SHORT")]).accepted, [])


class LifecyclePolicyTest(unittest.TestCase):
    def test_stop_reconciliation_never_prefers_stale_protection(self):
        self.assertEqual(_strongest_management_stop("LONG", 100,
                         {"initial_stop_loss": 95, "protected_stop": 101, "current_stop_loss": 108}), 108)
        self.assertEqual(_strongest_management_stop("SHORT", 100,
                         {"initial_stop_loss": 105, "protected_stop": 99, "current_stop_loss": 92}), 92)

    def test_stale_and_future_signals_rejected(self):
        for delta in (-76, 2):
            allowed, _, _ = evaluate_entry(opening(signal_time=NOW + timedelta(minutes=delta)), 100, {}, now=NOW)
            self.assertFalse(allowed)

    def test_chasing_is_measured_in_initial_risk_both_directions(self):
        for side, stop, mark in [("LONG", 95, 103), ("SHORT", 105, 97)]:
            allowed, reason, _ = evaluate_entry(opening(position_side=side, stop_loss=stop), mark, {}, now=NOW)
            self.assertFalse(allowed)
            self.assertEqual(reason, "entry_chased_wait_retest")

    def test_nan_prices_cannot_pass_gate(self):
        self.assertFalse(evaluate_entry(opening(), float("nan"), {}, now=NOW)[0])

    def test_r_does_not_drift_after_average_entry_changes(self):
        state = {"initial_stop_loss": 95, "stop_pct": 0.05, "risk_entry_price": 100, "risk_distance": 5}
        result = _position_r_state("LONG", 105, 110, state, 2)
        self.assertAlmostEqual(result["r_multiple"], 2)

    def test_losing_position_times_out(self):
        position = {"symbol": "XUSDT", "side": "LONG", "entry_price": 100, "mark_price": 99}
        state = {"initial_stop_loss": 95, "entry_time": NOW - timedelta(hours=13)}
        action, _, _ = evaluate_management(position, state, {}, now=NOW)
        self.assertEqual(action["reason"], "lifecycle_time_stop")

    def test_stop_has_priority_over_tp(self):
        position = {"symbol": "XUSDT", "side": "LONG", "entry_price": 100, "mark_price": 110}
        action, _, _ = evaluate_management(position, {"initial_stop_loss": 95, "current_stop_loss": 111}, {}, now=NOW)
        self.assertTrue(action["is_stop"])

    def test_add_cannot_spend_more_than_original_risk(self):
        position = {"quantity": 10, "entry_price": 100, "unrealized_pnl": 100}
        self.assertFalse(evaluate_add_risk(position, opening(quantity=20, entry_price=110, stop_loss=100), {"initial_risk": 50}, {})[0])

    def test_add_waits_for_pullback_even_when_price_keeps_rising(self):
        position = {"symbol": "XUSDT", "side": "LONG", "entry_price": 100, "mark_price": 110}
        state = {"initial_quantity": 10, "initial_stop_loss": 95, "atr_value": 2}
        decision = evaluate_roll(position, state, {"ema20": 105, "ema20_slope": 1}, True,
                                 {"require_pullback_recovery": True})
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.status, "waiting_healthy_pullback")


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(db, "DB_PATH", os.path.join(self.tmp.name, "test.db"))
        self.patch.start()
        db.init_db()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_claim_survives_restart_and_is_account_scoped(self):
        allowed, row = TradingJournal(1).claim(opening())
        self.assertTrue(allowed)
        self.assertFalse(TradingJournal(1).claim(opening())[0])
        self.assertTrue(TradingJournal(2).claim(opening())[0])
        self.assertEqual(row["intent_id"], intent_id(opening()))

    def test_unknown_order_blocks_new_exposure_not_emergency_exit(self):
        journal = TradingJournal(1)
        _, row = journal.claim(opening())
        journal.finish(row["intent_id"], "unknown")
        self.assertFalse(journal.claim(opening(scan_id="new"))[0])
        self.assertTrue(journal.claim(opening(action="close", management_stage="close"))[0])

    def test_initial_plan_is_immutable(self):
        journal = TradingJournal(1)
        journal.save_plan(opening())
        journal.save_plan(opening(entry_price=105))
        self.assertEqual(journal.plan("p1")["initial_entry"], 100)
        self.assertEqual(journal.plan("p1")["initial_risk"], 50)

    def test_trade_record_is_idempotent_per_account_and_intent(self):
        args = dict(symbol="XUSDT", side="LONG", qty=1, entry_price=100, exit_price=110,
                    pnl=10, pnl_pct=10, exit_reason="TP1", grade="A", score=80,
                    execution_intent_id="same-fill")
        self.assertTrue(db.record_trade(**args))
        self.assertFalse(db.record_trade(**args))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM trades").fetchone()[0], 1)

    def test_shadow_records_rejection_but_does_not_block(self):
        class Exchange:
            account_id = 1
            def get_mark_price(self, symbol): return 120
        engine = ExecutionEngine(Exchange())
        engine.cfg = {**engine.cfg, "strategy_lifecycle": {"mode": "shadow"}}
        self.assertTrue(engine._lifecycle_entry_gate(opening(signal_time=datetime.now(timezone.utc))))
        with db.get_conn() as conn:
            event = conn.execute("SELECT event_json FROM strategy_events ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertIn("entry_chased_wait_retest", event)
        engine.cfg = {**engine.cfg, "strategy_lifecycle": {"mode": "enforce"}}
        self.assertFalse(engine._lifecycle_entry_gate(opening(signal_time=datetime.now(timezone.utc))))

    def test_ack_without_fill_is_not_treated_as_completed(self):
        class Exchange:
            account_id = 1
            def get_open_orders(self, symbol): return []
            def place_market_order(self, *args, **kwargs): return {"status": "NEW", "orderId": "pending"}
        engine = ExecutionEngine(Exchange())
        engine.cfg = {**engine.cfg, "strategy_lifecycle": {"mode": "enforce"}}
        with self.assertRaises(RuntimeError): engine._place_market_action_order(opening())
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT status FROM execution_intents").fetchone()[0], "unknown")

    def test_uncertain_submit_is_not_sent_twice(self):
        class Exchange:
            account_id = 1
            calls = 0
            def get_open_orders(self, symbol): return []
            def place_market_order(self, *args, **kwargs):
                self.calls += 1
                raise TimeoutError("timeout")
            def get_order_by_client_id(self, symbol, client_id): return None
        ex = Exchange()
        engine = ExecutionEngine(ex)
        engine.cfg = {**engine.cfg, "strategy_lifecycle": {"mode": "enforce"}}
        for _ in range(2):
            with self.assertRaises((TimeoutError, RuntimeError)):
                engine._place_market_action_order(opening())
        self.assertEqual(ex.calls, 1)

    def test_internal_type_error_never_causes_second_submission(self):
        class Exchange:
            calls = 0
            def place_market_order(self, *args, **kwargs):
                self.calls += 1
                raise TypeError("response parser failed after submission")
        ex = Exchange()
        with self.assertRaises(TypeError):
            ExecutionEngine(ex)._place_market_action_order(opening())
        self.assertEqual(ex.calls, 1)

    def test_recovered_fill_returns_existing_order_without_resubmitting(self):
        class Exchange:
            account_id = 1
            calls = 0
            def get_open_orders(self, symbol): return []
            def place_market_order(self, *args, **kwargs):
                self.calls += 1
                raise TimeoutError("timeout")
            def get_order_by_client_id(self, symbol, client_id):
                return {"status": "FILLED", "orderId": "existing", "executedQty": "10"}
        ex = Exchange()
        engine = ExecutionEngine(ex)
        engine.cfg = {**engine.cfg, "strategy_lifecycle": {"mode": "enforce"}}
        with self.assertRaises(TimeoutError): engine._place_market_action_order(opening())
        recovered = engine._place_market_action_order(opening())
        self.assertEqual(recovered["orderId"], "existing")
        self.assertEqual(ex.calls, 1)


class ReplayTest(unittest.TestCase):
    def test_signal_cannot_fill_before_it_is_available(self):
        bars = [{"time": NOW + timedelta(minutes=15*i), "open": 100, "high": 101, "low": 99, "close": 100} for i in range(3)]
        result = replay_lifecycle(bars, [opening(signal_time=NOW + timedelta(minutes=15))], slippage=0)
        self.assertEqual(result["events"][0]["time"], str(NOW + timedelta(minutes=15)))
        self.assertGreater(result["fees"], 0)

    def test_stop_fill_uses_gap_price_not_unavailable_stop_price(self):
        bars = [{"time": NOW, "open": 100, "high": 101, "low": 99, "close": 100},
                {"time": NOW + timedelta(minutes=15), "open": 90, "high": 91, "low": 89, "close": 90}]
        result = replay_lifecycle(bars, [opening()], slippage=0)
        self.assertEqual(result["events"][-1]["price"], 90)
        self.assertEqual(result["gross_realized_pnl"], -100)

    def test_duplicate_bars_are_rejected(self):
        bar = {"time": NOW, "open": 100, "high": 101, "low": 99, "close": 100}
        with self.assertRaises(ValueError): replay_lifecycle([bar, bar], [])
