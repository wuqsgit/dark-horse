import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import shared.db as db
from trader.alpha_spot_short import evaluate_spot_distribution, publish_spot_short_events, MARKER
from trader.alpha_signal_consumer import AlphaSignalConsumer
from trader.execution import ExecutionEngine
from trader.lifecycle_policy import evaluate_management
from minute_pipeline.collectors import normalize_rest_kline
from tests.test_alpha_signal_consumer import FakeExchange, FakeEngine


class SpotSignalTest(unittest.TestCase):
    def test_missing_exchange_taker_field_is_never_labelled_real(self):
        row = [1786060800000, "100", "110", "90", "95", "10", 1786060859999, "1000", 5]
        normalized = normalize_rest_kline("AX", row, market_kind="alpha")
        self.assertIn("alpha-flow-estimated", normalized["source"])

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.ticker = {"percent_change_24h": -30, "last_seen": self.now.isoformat()}
        self.flow = [{"time": (self.now-timedelta(minutes=1)).isoformat(), "quote_vol": 100,
                      "taker_buy_quote_vol": 40, "source": "backfill"}]

    def test_only_drop_and_reported_selling_are_required(self):
        self.assertTrue(evaluate_spot_distribution(self.ticker, self.flow, now=self.now)[0])

    def test_drop_under_threshold_does_not_open(self):
        self.ticker["percent_change_24h"] = -29.99
        self.assertFalse(evaluate_spot_distribution(self.ticker, self.flow, now=self.now)[0])

    def test_no_selling_dominance_no_short(self):
        self.flow[0]["taker_buy_quote_vol"] = 50
        self.assertFalse(evaluate_spot_distribution(self.ticker, self.flow, now=self.now)[0])

    def test_estimated_unknown_missing_and_stale_flow_rejected(self):
        for source in ["backfill:alpha-flow-estimated", "", "synthetic"]:
            self.flow[0]["source"] = source
            self.assertFalse(evaluate_spot_distribution(self.ticker, self.flow, now=self.now)[0])
        self.flow[0]["source"] = "backfill"
        self.flow[0]["time"] = (self.now-timedelta(minutes=5)).isoformat()
        self.assertFalse(evaluate_spot_distribution(self.ticker, self.flow, now=self.now)[0])
        self.assertFalse(evaluate_spot_distribution(self.ticker, [], now=self.now)[0])


class DirectShortIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.tmp.name)/"test.db"))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init_db()
        self.account_token = db.set_account_context(7)
        self.addCleanup(db.reset_account_context, self.account_token)
        self.now = datetime.now(timezone.utc)
        with db.get_conn() as conn:
            conn.execute("INSERT INTO market_universe(pool_type,source_symbol,futures_symbol,selected,data_ready,updated_at) VALUES('alpha','AX','AKEUSDT',1,1,?)", (self.now.isoformat(),))
            conn.execute("INSERT INTO alpha_symbols(alpha_symbol,futures_symbol,percent_change_24h,last_seen) VALUES('AX','AKEUSDT',-35,?)", (self.now.isoformat(),))
            conn.execute("INSERT INTO alpha_candles_1m(time,alpha_symbol,open,high,low,close,volume,quote_vol,trades,taker_buy_quote_vol,source_env,is_closed,source,updated_at) VALUES(?,'AX',1,1,1,1,100,100,10,30,'mainnet',1,'backfill',?)", ((self.now-timedelta(minutes=1)).isoformat(),self.now.isoformat()))
        self.config = {"alpha_strategy_v2": {"enabled": True, "mode": "testnet_live", "market_env": "mainnet",
                                             "spot_distribution_short": {"enabled": True}}}

    def test_direct_short_needs_no_feature_snapshot_or_candle_confirmation(self):
        consumer = AlphaSignalConsumer(FakeExchange(), config=self.config)
        account = {"id": 7, "environment": "testnet", "alpha_trading_enabled": True}
        with patch("trader.alpha_signal_consumer.is_market_entry_ready", return_value=(True, None)), \
             patch("trader.execution._safe_alpha_futures_breakout_confirmation", side_effect=AssertionError("must not inspect pattern")):
            actions = consumer.build_actions(account=account, positions=[], balance=1000, engine=FakeEngine(), run_id="direct")
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["position_side"], "SHORT")
            self.assertEqual(actions[0]["reason"], MARKER)
            self.assertGreater(actions[0]["stop_loss"], actions[0]["entry_price"])
            self.assertEqual(consumer.build_actions(account=account, positions=[], balance=1000, engine=FakeEngine(), run_id="again"), [])

    def test_same_episode_only_publishes_one_event(self):
        publish_spot_short_events("testnet_live", now=self.now)
        publish_spot_short_events("testnet_live", now=self.now+timedelta(seconds=10))
        with db.get_conn() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM alpha_signal_events").fetchone()[0], 1)


class TrendRunnerTest(unittest.TestCase):
    def evaluate(self, mark, *, short=False, age=1, **extra):
        now = datetime.now(timezone.utc)
        state = {"initial_stop_loss": 105 if short else 95, "atr_value": 2, "initial_quantity": 10,
                 "entry_time": now-timedelta(hours=age), **extra}
        position = {"symbol": "AKEUSDT", "side": "SHORT" if short else "LONG", "entry_price": 100,
                    "mark_price": mark, "quantity": 10, "leverage": 1,
                    "unrealized_pnl": (100-mark if short else mark-100)*10}
        return evaluate_management(position, state, {"runner_enabled": True, "trailing_atr_multiplier": 2.5}, now=now)

    def test_one_r_only_moves_stop_both_sides(self):
        for short, mark, stop in [(False,105,100.2),(True,95,99.8)]:
            action, updates, _ = self.evaluate(mark, short=short)
            self.assertIsNone(action)
            self.assertAlmostEqual(updates["current_stop_loss"], stop)

    def test_two_r_takes_only_quarter_and_retains_runner(self):
        for short, mark in [(False,110),(True,90)]:
            action, updates, _ = self.evaluate(mark, short=short)
            self.assertEqual(action["close_pct"], 0.25)
            self.assertEqual(action["min_remaining_fraction"], 0.75)
            self.assertAlmostEqual(updates["current_stop_loss"], 95 if short else 105)
            action, _, _ = self.evaluate(mark, short=short, tp2_hit=1)
            self.assertIsNone(action)

    def test_armed_trend_ignores_time_stop_but_not_price_stop(self):
        action, _, _ = self.evaluate(101, age=48, highest_price=105)
        self.assertIsNone(action)
        action, _, _ = self.evaluate(99, age=48, highest_price=105, current_stop_loss=100.2)
        self.assertTrue(action["is_stop"])

    def test_previously_reduced_position_is_not_trimmed_again_below_floor(self):
        action, _, _ = self.evaluate(110, initial_quantity=20)
        self.assertIsNone(action)

    def test_legacy_reductions_are_bypassed_even_in_shadow_mode(self):
        class Exchange(FakeExchange):
            account_id = 7
        engine = ExecutionEngine(Exchange())
        history = {"initial_stop_loss": 95, "stop_pct": 0.05, "atr_value": 2,
                   "initial_quantity": 10, "entry_score": 100, "entry_time": datetime.now(timezone.utc).isoformat()}
        position = {"symbol": "AKEUSDT", "side": "LONG", "entry_price": 100, "mark_price": 105,
                    "quantity": 10, "leverage": 1, "unrealized_pnl": 50}
        with patch("shared.db.get_position_history", return_value=history), \
             patch("shared.db.update_position_management"), \
             patch("shared.trading_journal.TradingJournal.plan", return_value=None), \
             patch("shared.trading_journal.TradingJournal.event"):
            actions = engine._build_position_actions([{"symbol":"AKEUSDT","composite_score":1}], [position])
        self.assertEqual(actions, [])
