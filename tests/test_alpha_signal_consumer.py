import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import shared.db as db
from alpha_engine.strategy.models import (
    ActionType,
    AlphaSignalState,
    TransitionResult,
)
from alpha_engine.strategy.repository import AlphaStrategyRepository
from trader.alpha_signal_consumer import AlphaSignalConsumer


class FakeExchange:
    def get_open_orders(self, symbol):
        return []

    def get_mark_price(self, symbol):
        return 1.0

    def get_atr(self, symbol):
        return 0.02

    def adjust_quantity(self, symbol, quantity):
        return round(float(quantity), 3)

    def get_symbol_info(self, symbol):
        return {"min_qty": 0.001, "min_notional": 5.0}

    def get_order_by_client_id(self, symbol, client_order_id):
        return None


class FakeEngine:
    def _get_trading_symbols(self):
        return {"AKEUSDT", "ARCUSDT"}

    def _check_live_orderbook(self, symbol, side, profile):
        return True, "OK", {"spread_pct": 0.05}


class AlphaSignalConsumerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "strategy.db")
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        self.readiness_patch = patch(
            "trader.alpha_signal_consumer.is_market_entry_ready",
            return_value=(True, None),
        )
        self.readiness_patch.start()
        self.breakout_patch = patch(
            "trader.execution._safe_alpha_futures_breakout_confirmation",
            return_value=(True, "confirmed", {"confirmation_volume_ratio": 1.8}),
        )
        self.breakout_patch.start()
        self.canonical_patch = patch("shared.db.fetch_latest_alpha_position_context", return_value={"volume_price_state": "explosive_breakout_pending", "candidate_time": datetime.now(timezone.utc).isoformat()})
        self.canonical_patch.start()
        db.init_db()
        self.account_token = db.set_account_context(7)
        self.repo = AlphaStrategyRepository()
        conn = db.get_conn()
        conn.execute(
            """INSERT INTO market_universe
               (pool_type, source_symbol, futures_symbol, selected,
                data_ready, updated_at)
               VALUES ('alpha','AKEALPHAUSDT','AKEUSDT',1,1,?)""",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute(
            """INSERT INTO market_universe
               (pool_type, source_symbol, futures_symbol, selected,
                data_ready, updated_at)
               VALUES ('alpha','ARCALPHAUSDT','ARCUSDT',1,1,?)""",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        db.reset_account_context(self.account_token)
        self.breakout_patch.stop()
        self.canonical_patch.stop()
        self.readiness_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def _event(
        self,
        symbol="AKEUSDT",
        alpha="AKEALPHAUSDT",
        mode="testnet_live",
        reasons=("trigger_confirmed",),
        max_position_factor=0.3,
        action_type=ActionType.PROBE_LONG,
    ):
        now = datetime.now(timezone.utc)
        short = action_type in {
            ActionType.PROBE_SHORT,
            ActionType.CONFIRM_SHORT,
            ActionType.RETEST_SHORT_ADD,
        }
        conn = db.get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO alpha_feature_snapshots
               (snapshot_id, market_env, alpha_symbol, futures_symbol,
                candle_close_time, feature_schema_version,
                data_quality_status, data_quality_json, features_json,
                created_at)
               VALUES (?, 'mainnet', ?, ?, ?, 4, 'ready', ?, ?, ?)""",
            (
                f"snap-{symbol}",
                alpha,
                symbol,
                now.isoformat().replace("+00:00", "Z"),
                json.dumps({"status": "ready"}),
                json.dumps({
                    "alpha_discovery_score": 88,
                    "spot_volume_ratio_15m": 2.0,
                    "futures_volume_ratio_15m": 2.1,
                    "volume_sync_score": 0.8,
                    "ret_15m": -1.2 if short else 1.2,
                    "ret_1h": -2.5 if short else 2.5,
                    "oi_change_1h": 0.01,
                    "oi_change_4h": 0.02,
                    "quote_volume_ratio_15m": 2.1,
                    "spot_taker_sell_quote_ratio": 0.68,
                    "spot_sell_pressure_bars_2h": 7,
                    "breakdown_distance_pct": -1.2,
                }),
                now.isoformat().replace("+00:00", "Z"),
            ),
        )
        conn.commit()
        conn.close()
        confirmed = action_type in {
            ActionType.CONFIRM_LONG,
            ActionType.CONFIRM_SHORT,
        }
        transition = TransitionResult(
            from_state=AlphaSignalState.ARMED,
            to_state=(
                AlphaSignalState.CONFIRMED
                if confirmed
                else AlphaSignalState.PROBE_READY
            ),
            action_type=action_type,
            changed=True,
            candle_close_time=now,
            snapshot_id=f"snap-{symbol}",
            setup_type="distribution_short" if short else "accumulation",
            setup_id=f"setup-{symbol}",
            started_at=now,
            expires_at=now + timedelta(hours=1),
            reference_price=1.0,
            base_low=0.95,
            base_high=1.01,
            breakout_level=1.01,
            invalidation_price=1.05 if short else 0.95,
            setup_probability=0.75,
            followthrough_probability=0.78,
            fakeout_probability=0.18,
            expected_r=0.5,
            max_position_factor=max_position_factor,
            reasons=reasons,
            model_versions={"trigger": "v1"},
            previous_version=0,
        )
        return self.repo.apply_transition(
            market_env="mainnet",
            futures_symbol=symbol,
            alpha_symbol=alpha,
            transition=transition,
            strategy_mode=mode,
        )

    @staticmethod
    def _account():
        return {
            "id": 7,
            "environment": "testnet",
            "alpha_trading_enabled": True,
            "max_positions": 5,
            "risk_per_trade_pct": 0.015,
            "max_capital_usage_pct": 0.8,
        }

    def test_confirmed_signal_builds_bounded_action_and_consumes_event_once(self):
        self._event(
            action_type=ActionType.CONFIRM_LONG,
            max_position_factor=0.7,
        )
        consumer = AlphaSignalConsumer(
            FakeExchange(),
            config={
                "risk_per_trade_pct": 0.015,
                "alpha_strategy_v2": {
                    "enabled": True,
                    "mode": "testnet_live",
                    "market_env": "mainnet",
                    "max_alpha_positions": 2,
                    "confirmed_stage_cap": 0.70,
                },
            },
            repository=self.repo,
        )

        actions = consumer.build_actions(
            account=self._account(),
            positions=[],
            balance=1000,
            engine=FakeEngine(),
            run_id="run-1",
        )
        duplicate = consumer.build_actions(
            account=self._account(),
            positions=[],
            balance=1000,
            engine=FakeEngine(),
            run_id="run-2",
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "open")
        self.assertEqual(actions[0]["strategy_source"], "alpha")
        self.assertEqual(actions[0]["market_data_env"], "mainnet")
        self.assertEqual(actions[0]["execution_env"], "testnet")
        self.assertLessEqual(actions[0]["alpha_suggested_position_pct"], 0.70)
        self.assertAlmostEqual(actions[0]["quantity"], 56.7)
        self.assertAlmostEqual(
            actions[0]["quantity"] * actions[0]["entry_price"]
            / actions[0]["leverage"],
            18.9,
        )
        self.assertTrue(actions[0]["client_order_id"].startswith("DH-A2-7-"))
        self.assertEqual(duplicate, [])

    def test_confirmed_short_builds_sell_action_with_stop_above_entry(self):
        self._event(
            action_type=ActionType.CONFIRM_SHORT,
            max_position_factor=0.6,
        )
        consumer = AlphaSignalConsumer(
            FakeExchange(),
            config={
                "risk_per_trade_pct": 0.015,
                "alpha_strategy_v2": {
                    "enabled": True,
                    "mode": "testnet_live",
                    "market_env": "mainnet",
                    "max_alpha_positions": 2,
                    "confirmed_short_stage_cap": 0.60,
                },
            },
            repository=self.repo,
        )

        actions = consumer.build_actions(
            account=self._account(),
            positions=[],
            balance=1000,
            engine=FakeEngine(),
            run_id="run-short",
        )

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["side"], "SELL")
        self.assertEqual(actions[0]["position_side"], "SHORT")
        self.assertGreater(actions[0]["stop_loss"], actions[0]["entry_price"])
        self.assertLess(actions[0]["tp1_price"], actions[0]["entry_price"])

    def test_signal_mode_records_without_building_order(self):
        self._event(
            symbol="ARCUSDT",
            alpha="ARCALPHAUSDT",
            mode="signal",
        )
        consumer = AlphaSignalConsumer(
            FakeExchange(),
            config={
                "alpha_strategy_v2": {
                    "enabled": True,
                    "mode": "signal",
                    "market_env": "mainnet",
                },
            },
            repository=self.repo,
        )

        actions = consumer.build_actions(
            account=self._account(),
            positions=[],
            balance=1000,
            engine=FakeEngine(),
            run_id="run-signal",
        )

        self.assertEqual(actions, [])
        conn = db.get_conn()
        try:
            status = conn.execute(
                """SELECT status FROM alpha_signal_consumptions
                   WHERE account_id=7"""
            ).fetchone()["status"]
        finally:
            conn.close()
        self.assertEqual(status, "SIGNAL_ONLY")

    def test_confirmed_signal_is_rejected_when_strict_snapshot_weakens(self):
        self._event(
            action_type=ActionType.CONFIRM_LONG,
            max_position_factor=0.7,
        )
        conn = db.get_conn()
        row = conn.execute(
            "SELECT features_json FROM alpha_feature_snapshots WHERE snapshot_id='snap-AKEUSDT'"
        ).fetchone()
        features = json.loads(row["features_json"])
        features["spot_volume_ratio_15m"] = 0.8
        conn.execute(
            "UPDATE alpha_feature_snapshots SET features_json=? WHERE snapshot_id='snap-AKEUSDT'",
            (json.dumps(features),),
        )
        conn.commit()
        conn.close()
        consumer = AlphaSignalConsumer(
            FakeExchange(),
            config={
                "alpha_strategy_v2": {
                    "enabled": True,
                    "mode": "testnet_live",
                    "market_env": "mainnet",
                },
            },
            repository=self.repo,
        )

        actions = consumer.build_actions(
            account=self._account(),
            positions=[],
            balance=1000,
            engine=FakeEngine(),
            run_id="run-weak",
        )

        self.assertEqual(actions, [])
        conn = db.get_conn()
        rejection = conn.execute(
            "SELECT rejection_reason FROM alpha_signal_consumptions WHERE account_id=7"
        ).fetchone()["rejection_reason"]
        conn.close()
        self.assertIn("spot_volume_expanded", rejection)

    def _live_consumer(self):
        return AlphaSignalConsumer(FakeExchange(), config={
            "alpha_strategy_v2": {"enabled": True, "mode": "testnet_live",
                                  "market_env": "mainnet"},
        }, repository=self.repo)

    def _build(self, consumer):
        return consumer.build_actions(account=self._account(), positions=[],
                                      balance=1000, engine=FakeEngine(), run_id="regression")

    def test_sync_score_uses_unit_interval_and_rejects_percent_scale(self):
        self._event(action_type=ActionType.CONFIRM_LONG)
        consumer = self._live_consumer()
        event = self.repo.fetch_account_events(account_id=7, market_env="mainnet",
                                             strategy_modes=("testnet_live",))[0]
        for score, expected in ((0.65, True), (0.64, False), (65, False), (1.01, False)):
            conn = db.get_conn()
            features = json.loads(conn.execute("SELECT features_json FROM alpha_feature_snapshots").fetchone()[0])
            features["volume_sync_score"] = score
            conn.execute("UPDATE alpha_feature_snapshots SET features_json=?", (json.dumps(features),))
            conn.commit()
            conn.close()
            ok, _, _ = consumer._strict_entry_confirmation(event=event, action_type="CONFIRM_LONG", symbol="AKEUSDT")
            self.assertEqual(ok, expected, score)

    def test_confirmed_v2_does_not_depend_on_legacy_candidate_state(self):
        self._event(action_type=ActionType.CONFIRM_LONG)
        with patch("shared.db.fetch_latest_alpha_position_context", return_value=None):
            self.assertEqual(len(self._build(self._live_consumer())), 1)

    def test_retest_without_position_opens_with_confirmation_size_cap(self):
        self._event(action_type=ActionType.RETEST_ADD, max_position_factor=1.0)
        actions = self._build(self._live_consumer())
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "open")
        self.assertLessEqual(actions[0]["alpha_suggested_position_pct"], 0.7)
        self.assertEqual(actions[0]["notification_event_type"], "explosive_breakout")

    def test_stale_data_waits_and_retries_once_after_cooldown(self):
        self._event(action_type=ActionType.CONFIRM_LONG)
        consumer = self._live_consumer()
        with patch("trader.alpha_signal_consumer.is_market_entry_ready", return_value=(False, "futures_15m_age")):
            self.assertEqual(self._build(consumer), [])
        conn = db.get_conn()
        row = conn.execute("SELECT status FROM alpha_signal_consumptions").fetchone()
        self.assertEqual(row[0], "WAITING_DATA")
        self.assertEqual(self._build(consumer), [])
        conn.execute("UPDATE alpha_signal_consumptions SET updated_at=datetime('now','-31 seconds')")
        conn.commit()
        conn.close()
        self.assertEqual(len(self._build(consumer)), 1)
        self.assertEqual(self._build(consumer), [])

    def test_waiting_signal_expiry_is_not_retried(self):
        self._event(action_type=ActionType.CONFIRM_LONG)
        consumer = self._live_consumer()
        with patch("trader.alpha_signal_consumer.is_market_entry_ready", return_value=(False, "futures_15m_age")):
            self._build(consumer)
        conn = db.get_conn()
        conn.execute("UPDATE alpha_signal_consumptions SET updated_at=datetime('now','-31 seconds')")
        conn.execute("UPDATE alpha_signal_events SET expires_at=datetime('now','-1 second')")
        conn.commit()
        conn.close()
        self.assertEqual(self._build(consumer), [])

    def test_execution_data_block_can_retry_without_replaying_submitted_order(self):
        self._event(action_type=ActionType.CONFIRM_LONG)
        consumer = self._live_consumer()
        actions = self._build(consumer)
        consumer.mark_submitted(7, actions)
        consumer.finalize(7, [{**actions[0], "status": "blocked",
                              "error": "market_data_not_ready", "data_error": "futures_15m_age"}])
        conn = db.get_conn()
        row = conn.execute("SELECT status,client_order_id FROM alpha_signal_consumptions").fetchone()
        self.assertEqual(tuple(row), ("WAITING_DATA", None))
        conn.close()

    def test_waiting_signal_cannot_retry_after_structure_invalidated(self):
        self._event(action_type=ActionType.CONFIRM_LONG)
        consumer = self._live_consumer()
        with patch("trader.alpha_signal_consumer.is_market_entry_ready", return_value=(False, "futures_15m_age")):
            self._build(consumer)
        conn = db.get_conn()
        conn.execute("UPDATE alpha_signal_consumptions SET updated_at=datetime('now','-31 seconds')")
        conn.execute("""INSERT INTO alpha_signal_events
            (event_id,market_env,strategy_mode,futures_symbol,setup_id,to_state,state_version,
             action_type,event_time,candle_close_time,reason_codes_json,created_at)
            SELECT 'invalidated-event',market_env,strategy_mode,futures_symbol,setup_id,'FAILED',99,
                   'NONE',strftime('%Y-%m-%dT%H:%M:%fZ','now','+1 second'),candle_close_time,'[]',created_at
            FROM alpha_signal_events LIMIT 1""")
        conn.commit()
        conn.close()
        self.assertEqual(self._build(consumer), [])

    def test_waiting_event_claim_is_atomic_and_never_reclaims_planned_order(self):
        applied = self._event(action_type=ActionType.CONFIRM_LONG)
        self.assertTrue(self.repo.claim_event(7, applied.event_id, ActionType.CONFIRM_LONG))
        self.repo.update_consumption(account_id=7, event_id=applied.event_id,
                                     action_type=ActionType.CONFIRM_LONG, status="WAITING_DATA")
        conn = db.get_conn()
        conn.execute("UPDATE alpha_signal_consumptions SET updated_at=datetime('now','-31 seconds')")
        conn.commit()
        conn.close()
        self.assertTrue(self.repo.claim_event(7, applied.event_id, ActionType.CONFIRM_LONG))
        self.assertFalse(self.repo.claim_event(7, applied.event_id, ActionType.CONFIRM_LONG))
        self.repo.update_consumption(account_id=7, event_id=applied.event_id,
                                     action_type=ActionType.CONFIRM_LONG, status="PLANNED", client_order_id="stable")
        self.assertFalse(self.repo.claim_event(7, applied.event_id, ActionType.CONFIRM_LONG))

    def test_probe_signal_is_observation_only(self):
        self._event(
            reasons=(
                "square_extreme_bearishness",
                "sentiment_reversal_probe_confirmed",
            ),
            max_position_factor=0.5,
        )
        consumer = AlphaSignalConsumer(
            FakeExchange(),
            config={
                "risk_per_trade_pct": 0.015,
                "alpha_strategy_v2": {
                    "enabled": True,
                    "mode": "testnet_live",
                    "market_env": "mainnet",
                    "max_alpha_positions": 2,
                    "probe_stage_cap": 0.30,
                    "sentiment_reversal_stage_cap": 0.50,
                },
            },
            repository=self.repo,
        )

        actions = consumer.build_actions(
            account=self._account(),
            positions=[],
            balance=1000,
            engine=FakeEngine(),
            run_id="run-square",
        )

        self.assertEqual(actions, [])
        conn = db.get_conn()
        try:
            row = conn.execute(
                """SELECT status, rejection_reason
                   FROM alpha_signal_consumptions
                   WHERE account_id=7"""
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "OBSERVED")
        self.assertEqual(row["rejection_reason"], "probe_observation_only")

    def test_restart_releases_unsubmitted_plan_for_safe_retry(self):
        applied = self._event()
        self.repo.claim_event(
            7,
            applied.event_id,
            ActionType.PROBE_LONG,
        )
        self.repo.update_consumption(
            account_id=7,
            event_id=applied.event_id,
            action_type=ActionType.PROBE_LONG,
            status="PLANNED",
            client_order_id="DH-A2-7-retry-P",
        )
        consumer = AlphaSignalConsumer(
            FakeExchange(),
            config={
                "alpha_strategy_v2": {
                    "enabled": True,
                    "mode": "testnet_live",
                    "market_env": "mainnet",
                },
            },
            repository=self.repo,
        )

        result = consumer.recover(self._account(), [])

        self.assertEqual(result["retryable"], 1)
        events = self.repo.fetch_account_events(
            account_id=7,
            market_env="mainnet",
            strategy_modes=("testnet_live",),
        )
        self.assertEqual(len(events), 1)

    def test_testnet_live_does_not_execute_for_mainnet_account(self):
        self._event()
        consumer = AlphaSignalConsumer(
            FakeExchange(),
            config={
                "alpha_strategy_v2": {
                    "enabled": True,
                    "mode": "testnet_live",
                    "market_env": "mainnet",
                },
            },
            repository=self.repo,
        )
        account = {**self._account(), "environment": "prod"}

        actions = consumer.build_actions(
            account=account,
            positions=[],
            balance=1000,
            engine=FakeEngine(),
            run_id="run-prod",
        )

        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
