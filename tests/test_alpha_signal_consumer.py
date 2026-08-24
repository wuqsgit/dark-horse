import tempfile
import unittest
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
    ):
        now = datetime.now(timezone.utc)
        transition = TransitionResult(
            from_state=AlphaSignalState.ARMED,
            to_state=AlphaSignalState.PROBE_READY,
            action_type=ActionType.PROBE_LONG,
            changed=True,
            candle_close_time=now,
            snapshot_id=f"snap-{symbol}",
            setup_type="accumulation",
            setup_id=f"setup-{symbol}",
            started_at=now,
            expires_at=now + timedelta(hours=1),
            reference_price=1.0,
            base_low=0.95,
            base_high=1.01,
            breakout_level=1.01,
            invalidation_price=0.95,
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

    def test_builds_bounded_action_and_consumes_event_once(self):
        self._event()
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
        self.assertLessEqual(actions[0]["alpha_suggested_position_pct"], 0.30)
        self.assertLessEqual(
            actions[0]["quantity"]
            * (actions[0]["entry_price"] - actions[0]["stop_loss"]),
            3.0,
        )
        self.assertTrue(actions[0]["client_order_id"].startswith("DH-A2-7-"))
        self.assertEqual(duplicate, [])

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

    def test_sentiment_reversal_probe_uses_half_position_cap(self):
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

        self.assertEqual(len(actions), 1)
        self.assertLessEqual(
            actions[0]["alpha_suggested_position_pct"],
            0.50,
        )

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
