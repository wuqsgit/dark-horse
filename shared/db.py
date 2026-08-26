"""Database layer 鈥?SQLite backend (fast local dev, swap to PG later)"""
import os
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from functools import wraps

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "alphadog.db")
RETENTION_DAYS = 4
STRATEGY_RETENTION_DAYS = max(
    RETENTION_DAYS,
    int(os.getenv("ALPHA_STRATEGY_RETENTION_DAYS", "90")),
)
STRATEGY_RETENTION_TABLES = {
    "alpha_candles_15m",
    "alpha_candles_1h",
    "alpha_candles_6h",
    "alpha_candles_24h",
    "futures_candles_15m",
    "futures_candles_1h",
    "futures_candles_6h",
    "futures_candles_24h",
    "futures_data",
    "alpha_orderbook_snapshots",
}

OPERATIONAL_RETENTION_TABLES = {
    "candles_15m": "time",
    "candles_1h": "time",
    "candles_6h": "time",
    "candles_24h": "time",
    "alpha_candles_15m": "time",
    "alpha_candles_1h": "time",
    "alpha_candles_6h": "time",
    "alpha_candles_24h": "time",
    "futures_candles_15m": "time",
    "futures_candles_1h": "time",
    "futures_candles_6h": "time",
    "futures_candles_24h": "time",
    "futures_data": "time",
    "onchain_flows": "time",
    "orderbook_depth": "time",
    "orderbook_snapshots": "timestamp",
    "alpha_orderbook_snapshots": "timestamp",
    "symbol_snapshots": "date",
    "alpha_scores": "time",
    "training_samples": "timestamp",
    "alpha_scan_scores": "time",
    "alpha_square_posts": "published_at",
    "alpha_square_sentiment_snapshots": "time",
    "alpha_trade_candidates": "time",
    "strategy_decisions": "time",
    "decision_actions": "time",
    "decision_outcomes": "signal_time",
    "signal_outcomes": "signal_time",
    "policy_reviews": "run_time",
    "exit_review_summaries": "run_time",
    "trade_exit_reviews": "exit_time",
    "factor_effectiveness": "run_time",
    "shadow_decisions": "created_at",
}

_local = threading.local()
_account_context = ContextVar("darkhorse_account_id", default=1)
_init_lock = threading.RLock()
_database_write_lock = threading.RLock()
_initialized_databases = set()
_SCHEMA_VERSION = 6


def _serialized_write(function):
    """Serialize SQLite write transactions inside one service process.

    WAL permits concurrent readers but SQLite still has a single writer.
    Minute collection uses several worker threads, so without this guard the
    process can spend the entire busy timeout competing with itself.
    """
    @wraps(function)
    def wrapper(*args, **kwargs):
        with _database_write_lock:
            return function(*args, **kwargs)

    return wrapper


class _AutoClosingConnection:
    """Close SQLite deterministically when legacy callers omit close()."""

    __slots__ = ("_connection", "_closed")

    def __init__(self, connection):
        self._connection = connection
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        if not self._closed:
            self._closed = True
            self._connection.close()

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            return self._connection.__exit__(exc_type, exc, traceback)
        finally:
            self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


@contextmanager
def _database_init_file_lock():
    """Serialize schema migration across all DarkHorse processes."""
    lock_path = os.path.abspath(DB_PATH) + ".init.lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_file = open(lock_path, "a+b")
    try:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows uses process startup order
            fcntl = None
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _serialized_database_init(function):
    def schema_is_current():
        if not os.path.exists(DB_PATH):
            return False
        try:
            connection = sqlite3.connect(DB_PATH, timeout=5.0)
            try:
                version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                pk_rows = connection.execute(
                    "PRAGMA table_info(futures_candles_15m)"
                ).fetchall()
                primary_key = [
                    row[1]
                    for row in sorted(pk_rows, key=lambda row: int(row[5] or 0))
                    if int(row[5] or 0) > 0
                ]
                return (
                    version == _SCHEMA_VERSION
                    and primary_key == ["time", "symbol", "source_env"]
                )
            finally:
                connection.close()
        except sqlite3.Error:
            return False

    @wraps(function)
    def wrapper(*args, **kwargs):
        database_key = (os.getpid(), os.path.abspath(DB_PATH))
        if database_key in _initialized_databases and schema_is_current():
            return None
        with _init_lock:
            if database_key in _initialized_databases and schema_is_current():
                return None
            with _database_init_file_lock():
                result = function(*args, **kwargs)
                _initialized_databases.add(database_key)
                return result
    return wrapper


def current_account_id() -> int:
    return int(_account_context.get() or 1)


def set_account_context(account_id: int):
    return _account_context.set(int(account_id))


def reset_account_context(token):
    _account_context.reset(token)


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return _AutoClosingConnection(conn)


@_serialized_database_init
def init_db():
    """鍒涘缓鎵€鏈夎〃锛堝箓绛夛級鈥斺€?棣栨鍚姩鎴栨柊琛ㄨ縼绉绘椂璋冪敤"""
    conn = get_conn()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        -- Core market data tables.
        CREATE TABLE IF NOT EXISTS symbols (
            symbol TEXT PRIMARY KEY,
            is_active INTEGER DEFAULT 1,
            first_seen TEXT DEFAULT (datetime('now')),
            last_seen TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS candles_1h (
            time TEXT, symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, symbol)
        );
        CREATE TABLE IF NOT EXISTS candles_15m (
            time TEXT, symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, symbol)
        );
        CREATE TABLE IF NOT EXISTS candles_6h (
            time TEXT, symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, symbol)
        );
        CREATE TABLE IF NOT EXISTS candles_24h (
            time TEXT, symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, symbol)
        );
        CREATE TABLE IF NOT EXISTS candles_1m (
            time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL DEFAULT 0,
            quote_vol REAL NOT NULL DEFAULT 0,
            trades INTEGER NOT NULL DEFAULT 0,
            taker_buy_quote_vol REAL NOT NULL DEFAULT 0,
            source_env TEXT NOT NULL DEFAULT 'mainnet',
            is_closed INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'stream',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (time, symbol, source_env)
        );
        CREATE TABLE IF NOT EXISTS futures_candles_1m (
            time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL DEFAULT 0,
            quote_vol REAL NOT NULL DEFAULT 0,
            trades INTEGER NOT NULL DEFAULT 0,
            taker_buy_quote_vol REAL NOT NULL DEFAULT 0,
            source_env TEXT NOT NULL DEFAULT 'mainnet',
            is_closed INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'stream',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (time, symbol, source_env)
        );
        CREATE TABLE IF NOT EXISTS futures_candles_1h (
            time TEXT, symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            taker_buy_quote_vol REAL,
            source_env TEXT NOT NULL DEFAULT 'mainnet',
            is_closed INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (time, symbol, source_env)
        );
        CREATE TABLE IF NOT EXISTS futures_candles_15m (
            time TEXT, symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            taker_buy_quote_vol REAL,
            source_env TEXT NOT NULL DEFAULT 'mainnet',
            is_closed INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (time, symbol, source_env)
        );
        CREATE TABLE IF NOT EXISTS futures_candles_6h (
            time TEXT, symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            taker_buy_quote_vol REAL,
            source_env TEXT NOT NULL DEFAULT 'mainnet',
            is_closed INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (time, symbol, source_env)
        );
        CREATE TABLE IF NOT EXISTS futures_candles_24h (
            time TEXT, symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            taker_buy_quote_vol REAL,
            source_env TEXT NOT NULL DEFAULT 'mainnet',
            is_closed INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (time, symbol, source_env)
        );
        CREATE TABLE IF NOT EXISTS market_universe (
            pool_type TEXT NOT NULL,
            source_symbol TEXT NOT NULL,
            spot_symbol TEXT,
            futures_symbol TEXT NOT NULL,
            spot_quote_volume_24h REAL NOT NULL DEFAULT 0,
            futures_quote_volume_24h REAL NOT NULL DEFAULT 0,
            effective_quote_volume_24h REAL NOT NULL DEFAULT 0,
            universe_rank INTEGER,
            selected INTEGER NOT NULL DEFAULT 0,
            forced_position INTEGER NOT NULL DEFAULT 0,
            data_ready INTEGER NOT NULL DEFAULT 0,
            data_error TEXT,
            data_checked_at TEXT,
            selection_reason TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (pool_type, source_symbol)
        );
        CREATE TABLE IF NOT EXISTS futures_data (
            time TEXT, symbol TEXT,
            open_interest REAL, funding_rate REAL, mark_price REAL,
            source_env TEXT NOT NULL DEFAULT 'mainnet',
            PRIMARY KEY (time, symbol, source_env)
        );
        CREATE TABLE IF NOT EXISTS onchain_flows (
            time TEXT, symbol TEXT, chain TEXT DEFAULT 'ethereum',
            cex_inflow_usd REAL DEFAULT 0, cex_outflow_usd REAL DEFAULT 0,
            cex_net_flow_usd REAL, cex_net_flow_14d_usd REAL,
            cex_net_outflow_ratio REAL, window_hours INTEGER DEFAULT 24,
            PRIMARY KEY (time, symbol, chain)
        );
        -- Score table.
        CREATE TABLE IF NOT EXISTS alpha_scores (
            time TEXT, symbol TEXT,
            composite_score REAL, composite_summary TEXT,
            risk_label TEXT, chip_phase TEXT, trend_state TEXT, trend_direction TEXT,
            volatility_level TEXT, price_position TEXT,
            relative_strength REAL, market_price REAL,
            raw_features TEXT, scan_id TEXT,
            entry_alpha REAL, hold_alpha REAL,  -- V3.0
            PRIMARY KEY (time, symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_scores_scan ON alpha_scores(scan_id);
        CREATE INDEX IF NOT EXISTS idx_alpha_scores_time ON alpha_scores(time);
        -- Trade table.
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, side TEXT NOT NULL, position_side TEXT,
            quantity REAL, entry_price REAL, exit_price REAL,
            pnl REAL, pnl_pct REAL, exit_reason TEXT,
            entry_reason TEXT,
            entry_time TEXT, exit_time TEXT,
            grade_at_entry TEXT, score_at_entry REAL,
            created_at TEXT DEFAULT (datetime('now')),
            source TEXT DEFAULT 'system',
            income_id TEXT,
            fill_ids TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time);
        CREATE TABLE IF NOT EXISTS trading_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            environment TEXT NOT NULL DEFAULT 'testnet' CHECK(environment IN ('testnet','prod')),
            api_key_encrypted TEXT,
            api_secret_encrypted TEXT,
            initial_capital REAL NOT NULL DEFAULT 0,
            initial_capital_time TEXT,
            max_positions INTEGER NOT NULL DEFAULT 5,
            max_capital_usage_pct REAL NOT NULL DEFAULT 0.40,
            risk_per_trade_pct REAL NOT NULL DEFAULT 0.015,
            normal_trading_enabled INTEGER NOT NULL DEFAULT 1,
            alpha_trading_enabled INTEGER NOT NULL DEFAULT 1,
            auto_trading_enabled INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            is_default INTEGER NOT NULL DEFAULT 0,
            last_sync_time TEXT,
            last_sync_status TEXT,
            last_sync_error TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS account_capital_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES trading_accounts(id),
            adjustment_type TEXT NOT NULL,
            amount REAL NOT NULL,
            effective_time TEXT NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_account_adjustments ON account_capital_adjustments(account_id, effective_time DESC);
        -- Live position entry state.
        CREATE TABLE IF NOT EXISTS position_history (
            symbol TEXT PRIMARY KEY,
            side TEXT, quantity REAL,
            entry_price REAL, entry_reason TEXT,
            entry_score REAL, entry_time TEXT,
            tp3_price REAL, atr_value REAL,
            update_time TEXT DEFAULT (datetime('now'))
        );
        -- Legacy backtest tables removed; policy-loop tables below are the source of truth.
        -- 鎸佷粨蹇収锛堟瘡杞惊鐜褰曪級
        CREATE TABLE IF NOT EXISTS positions_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            side TEXT,
            position_side TEXT,
            quantity REAL,
            entry_price REAL,
            mark_price REAL,
            unrealized_pnl REAL,
            leverage INTEGER DEFAULT 1,
            stop_loss REAL,
            take_profit REAL
        );
        CREATE INDEX IF NOT EXISTS idx_positions_time ON positions_history(time);
        CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions_history(symbol);
        CREATE TABLE IF NOT EXISTS strategy_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT UNIQUE,
            run_id TEXT,
            time TEXT DEFAULT (datetime('now')),
            scan_id TEXT,
            symbol TEXT NOT NULL,
            side TEXT,
            mode TEXT DEFAULT 'live',
            decision_stage TEXT,
            decision_result TEXT,
            filter_reason TEXT,
            composite_score REAL,
            grade TEXT,
            market_regime TEXT,
            price REAL,
            quantity REAL,
            entry_price REAL,
            risk_params_json TEXT,
            features_json TEXT,
            reason_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_decisions_time ON strategy_decisions(time);
        CREATE INDEX IF NOT EXISTS idx_strategy_decisions_symbol ON strategy_decisions(symbol);
        CREATE INDEX IF NOT EXISTS idx_strategy_decisions_run ON strategy_decisions(run_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_decisions_scan ON strategy_decisions(scan_id);
        CREATE INDEX IF NOT EXISTS idx_strategy_decisions_run_stage ON strategy_decisions(run_id, decision_stage);
        CREATE INDEX IF NOT EXISTS idx_strategy_decisions_run_result ON strategy_decisions(run_id, decision_result);
        CREATE INDEX IF NOT EXISTS idx_strategy_decisions_run_filter ON strategy_decisions(run_id, filter_reason);
        CREATE INDEX IF NOT EXISTS idx_strategy_decisions_time_id ON strategy_decisions(time DESC, id DESC);
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id TEXT UNIQUE,
            strategy_decision_id INTEGER,
            run_id TEXT,
            scan_id TEXT,
            symbol TEXT NOT NULL,
            signal_time TEXT NOT NULL,
            entry_price REAL,
            side TEXT,
            return_1h REAL,
            return_4h REAL,
            return_12h REAL,
            return_24h REAL,
            max_favorable_return REAL,
            max_adverse_return REAL,
            best_side TEXT,
            direction_correct INTEGER,
            hit_tp INTEGER,
            hit_sl INTEGER,
            bars_observed INTEGER DEFAULT 0,
            is_complete INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_signal_outcomes_symbol ON signal_outcomes(symbol);
        CREATE INDEX IF NOT EXISTS idx_signal_outcomes_run ON signal_outcomes(run_id);
        CREATE INDEX IF NOT EXISTS idx_signal_outcomes_complete ON signal_outcomes(is_complete);
        CREATE INDEX IF NOT EXISTS idx_signal_outcomes_run_complete ON signal_outcomes(run_id, is_complete);
        CREATE INDEX IF NOT EXISTS idx_signal_outcomes_run_side ON signal_outcomes(run_id, best_side);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_outcomes_decision ON signal_outcomes(decision_id);
        CREATE TABLE IF NOT EXISTS decision_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id TEXT UNIQUE,
            source_decision_id TEXT,
            source_trade_id INTEGER,
            run_id TEXT,
            time TEXT DEFAULT (datetime('now')),
            symbol TEXT NOT NULL,
            category TEXT,
            strategy_source TEXT DEFAULT 'normal',
            action_type TEXT,
            action_result TEXT,
            side TEXT,
            price REAL,
            score REAL,
            entry_alpha REAL,
            hold_alpha REAL,
            grade TEXT,
            reason_code TEXT,
            reason_text TEXT,
            reason_json TEXT,
            features_json TEXT,
            risk_params_json TEXT,
            position_params_json TEXT,
            policy_version TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_decision_actions_time ON decision_actions(time DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_actions_symbol ON decision_actions(symbol, time DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_actions_category ON decision_actions(category, time DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_actions_type ON decision_actions(action_type, action_result, time DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_actions_source_decision ON decision_actions(source_decision_id);
        CREATE INDEX IF NOT EXISTS idx_decision_actions_source_trade ON decision_actions(source_trade_id);
        CREATE TABLE IF NOT EXISTS decision_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action_id TEXT UNIQUE,
            symbol TEXT NOT NULL,
            category TEXT,
            action_type TEXT,
            action_result TEXT,
            signal_time TEXT,
            entry_price REAL,
            side TEXT,
            return_1h REAL,
            return_4h REAL,
            return_12h REAL,
            return_24h REAL,
            return_48h REAL,
            return_72h REAL,
            max_favorable_return REAL,
            max_adverse_return REAL,
            max_favorable_time TEXT,
            max_adverse_time TEXT,
            atr_at_signal REAL,
            mfe_atr_multiple REAL,
            mae_atr_multiple REAL,
            missed_big_move INTEGER DEFAULT 0,
            early_exit INTEGER DEFAULT 0,
            good_block INTEGER DEFAULT 0,
            bad_block INTEGER DEFAULT 0,
            small_profit_exit INTEGER DEFAULT 0,
            churn_trade INTEGER DEFAULT 0,
            probe_failed INTEGER DEFAULT 0,
            weak_after_entry INTEGER DEFAULT 0,
            holding_minutes REAL,
            trend_capture_ratio REAL,
            bars_observed INTEGER DEFAULT 0,
            is_complete INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_decision_outcomes_symbol ON decision_outcomes(symbol, signal_time DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_outcomes_category ON decision_outcomes(category, signal_time DESC);
        CREATE INDEX IF NOT EXISTS idx_decision_outcomes_flags ON decision_outcomes(missed_big_move, early_exit, bad_block);
        CREATE TABLE IF NOT EXISTS policy_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id TEXT UNIQUE,
            run_time TEXT DEFAULT (datetime('now')),
            category TEXT,
            strategy_source TEXT,
            target_type TEXT,
            target_name TEXT,
            sample_size INTEGER DEFAULT 0,
            avg_return REAL,
            median_return REAL,
            total_return REAL,
            avg_mfe REAL,
            avg_mae REAL,
            trend_capture_ratio REAL,
            missed_big_move_count INTEGER DEFAULT 0,
            early_exit_count INTEGER DEFAULT 0,
            small_profit_exit_count INTEGER DEFAULT 0,
            churn_trade_count INTEGER DEFAULT 0,
            probe_failed_count INTEGER DEFAULT 0,
            weak_after_entry_count INTEGER DEFAULT 0,
            bad_block_count INTEGER DEFAULT 0,
            good_block_count INTEGER DEFAULT 0,
            diagnosis TEXT,
            recommendation_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_policy_reviews_run ON policy_reviews(run_time DESC);
        CREATE INDEX IF NOT EXISTS idx_policy_reviews_category ON policy_reviews(category, target_type, run_time DESC);
        CREATE TABLE IF NOT EXISTS policy_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id TEXT UNIQUE,
            created_at TEXT DEFAULT (datetime('now')),
            category TEXT,
            strategy_source TEXT,
            target_type TEXT,
            policy_json TEXT,
            source_candidate_id INTEGER,
            status TEXT DEFAULT 'active',
            activated_at TEXT,
            replaced_version_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_policy_versions_status ON policy_versions(status, category, target_type);
        CREATE TABLE IF NOT EXISTS policy_experiments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            experiment_id TEXT UNIQUE,
            policy_version TEXT,
            category TEXT,
            start_time TEXT,
            end_time TEXT,
            sample_size INTEGER DEFAULT 0,
            before_return REAL,
            after_return REAL,
            before_trend_capture REAL,
            after_trend_capture REAL,
            before_early_exit_rate REAL,
            after_early_exit_rate REAL,
            before_missed_big_move_rate REAL,
            after_missed_big_move_rate REAL,
            result TEXT,
            rollback_triggered INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_policy_experiments_version ON policy_experiments(policy_version, created_at DESC);
        CREATE TABLE IF NOT EXISTS strategy_policy_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            source_type TEXT,
            source_run_time TEXT,
            target TEXT NOT NULL,
            action TEXT NOT NULL,
            title TEXT,
            summary TEXT,
            condition_json TEXT,
            change_json TEXT,
            confidence REAL DEFAULT 0,
            sample_size INTEGER DEFAULT 0,
            expected_delta REAL DEFAULT 0,
            risk_note TEXT,
            status TEXT DEFAULT 'proposed',
            activated_at TEXT,
            rollback_condition_json TEXT,
            dedupe_key TEXT,
            UNIQUE(source_type, source_run_time, target, action, title)
        );
        CREATE INDEX IF NOT EXISTS idx_policy_candidates_status ON strategy_policy_candidates(status);
        CREATE INDEX IF NOT EXISTS idx_policy_candidates_created ON strategy_policy_candidates(created_at DESC);
        CREATE TABLE IF NOT EXISTS shadow_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            candidate_id INTEGER,
            run_id TEXT,
            scan_id TEXT,
            symbol TEXT,
            side TEXT,
            live_result TEXT,
            shadow_result TEXT,
            conflict INTEGER DEFAULT 0,
            price REAL,
            outcome_json TEXT,
            FOREIGN KEY(candidate_id) REFERENCES strategy_policy_candidates(id)
        );
        CREATE INDEX IF NOT EXISTS idx_shadow_candidate ON shadow_decisions(candidate_id);
        CREATE INDEX IF NOT EXISTS idx_shadow_created ON shadow_decisions(created_at DESC);
        CREATE TABLE IF NOT EXISTS strategy_policy_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            candidate_id INTEGER,
            action TEXT,
            old_status TEXT,
            new_status TEXT,
            detail_json TEXT,
            FOREIGN KEY(candidate_id) REFERENCES strategy_policy_candidates(id)
        );
        CREATE INDEX IF NOT EXISTS idx_policy_audit_candidate ON strategy_policy_audit(candidate_id);
        CREATE TABLE IF NOT EXISTS factor_effectiveness (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT,
            factor_name TEXT,
            layer TEXT,
            profile TEXT,
            bucket TEXT,
            samples INTEGER,
            win_rate_6h REAL,
            win_rate_24h REAL,
            avg_return_6h REAL,
            avg_return_24h REAL,
            avg_drawdown REAL,
            ev REAL,
            ic REAL
        );
        CREATE INDEX IF NOT EXISTS idx_factor_effectiveness_run ON factor_effectiveness(run_time DESC);
        CREATE INDEX IF NOT EXISTS idx_factor_effectiveness_factor ON factor_effectiveness(factor_name, layer, profile);
        -- Order intent table.
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT,
            quantity REAL,
            price REAL,
            status TEXT DEFAULT 'pending',
            reason TEXT,
            client_order_id TEXT,
            exchange_order_id TEXT,
            signal_event_id TEXT,
            setup_id TEXT,
            alpha_stage TEXT,
            ai_model_versions_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        -- Fill table.
        CREATE TABLE IF NOT EXISTS fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            order_id INTEGER REFERENCES orders(id),
            side TEXT NOT NULL,
            quantity REAL,
            price REAL,
            realized_pnl REAL,
            fee REAL,
            fee_asset TEXT DEFAULT 'USDT',
            trade_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_trade_id ON fills(trade_id);
        CREATE TABLE IF NOT EXISTS exchange_income_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            income_id TEXT UNIQUE,
            symbol TEXT,
            income_type TEXT NOT NULL,
            income REAL DEFAULT 0,
            asset TEXT DEFAULT 'USDT',
            income_time TEXT,
            trade_id TEXT,
            order_id TEXT,
            position_side TEXT,
            raw_json TEXT,
            source TEXT DEFAULT 'binance_income',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_income_ledger_time ON exchange_income_ledger(income_time DESC);
        CREATE INDEX IF NOT EXISTS idx_income_ledger_symbol ON exchange_income_ledger(symbol, income_time DESC);
        CREATE INDEX IF NOT EXISTS idx_income_ledger_type ON exchange_income_ledger(income_type, income_time DESC);
        CREATE TABLE IF NOT EXISTS position_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_trade_id TEXT UNIQUE,
            symbol TEXT NOT NULL,
            side TEXT,
            strategy_source TEXT DEFAULT 'unknown',
            signal_source TEXT,
            alpha_symbol TEXT,
            entry_time TEXT,
            exit_time TEXT,
            entry_price REAL,
            exit_price REAL,
            quantity REAL,
            realized_pnl REAL DEFAULT 0,
            commission REAL DEFAULT 0,
            funding_fee REAL DEFAULT 0,
            adjustment REAL DEFAULT 0,
            net_pnl REAL DEFAULT 0,
            pnl_pct REAL,
            income_count INTEGER DEFAULT 0,
            entry_reason TEXT,
            exit_reason TEXT,
            grade_at_entry TEXT,
            score_at_entry REAL,
            source TEXT DEFAULT 'reconstructed',
            reconcile_status TEXT DEFAULT 'ok',
            raw_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_position_trades_exit ON position_trades(exit_time DESC);
        CREATE INDEX IF NOT EXISTS idx_position_trades_symbol ON position_trades(symbol, exit_time DESC);
        CREATE INDEX IF NOT EXISTS idx_position_trades_source ON position_trades(source, exit_time DESC);
        CREATE TABLE IF NOT EXISTS trade_history_page_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL,
            query_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            cursor_secret TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_trade_history_snapshots_expiry
            ON trade_history_page_snapshots(expires_at);
        CREATE TABLE IF NOT EXISTS trade_exit_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_trade_id TEXT UNIQUE,
            symbol TEXT NOT NULL,
            strategy_source TEXT,
            alpha_symbol TEXT,
            side TEXT,
            category TEXT,
            entry_time TEXT,
            exit_time TEXT,
            exit_reason TEXT,
            net_pnl REAL DEFAULT 0,
            pnl_pct REAL,
            holding_minutes REAL,
            return_1h REAL,
            return_4h REAL,
            return_12h REAL,
            return_24h REAL,
            return_72h REAL,
            max_favorable_return REAL,
            max_adverse_return REAL,
            max_favorable_time TEXT,
            max_adverse_time TEXT,
            review_label TEXT,
            review_summary TEXT,
            evidence_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_trade_exit_reviews_exit ON trade_exit_reviews(exit_time DESC);
        CREATE INDEX IF NOT EXISTS idx_trade_exit_reviews_reason ON trade_exit_reviews(exit_reason, exit_time DESC);
        CREATE INDEX IF NOT EXISTS idx_trade_exit_reviews_label ON trade_exit_reviews(review_label, exit_time DESC);
        CREATE TABLE IF NOT EXISTS trade_entry_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_trade_id TEXT UNIQUE NOT NULL,
            source_decision_id TEXT,
            symbol TEXT NOT NULL,
            alpha_symbol TEXT,
            side TEXT,
            strategy_source TEXT DEFAULT 'unknown',
            category TEXT,
            entry_template TEXT,
            market_regime TEXT,
            entry_time TEXT,
            entry_price REAL,
            quantity REAL,
            leverage REAL,
            margin REAL,
            notional REAL,
            stop_loss REAL,
            stop_pct REAL,
            take_profit_1 REAL,
            take_profit_2 REAL,
            risk_reward_ratio REAL,
            atr_pct REAL,
            total_score REAL,
            grade TEXT,
            score_items_json TEXT,
            trend_score REAL,
            breakout_state TEXT,
            spot_volume_ratio REAL,
            futures_volume_ratio REAL,
            volume_sync_state TEXT,
            spread_pct REAL,
            orderbook_state TEXT,
            passed_conditions_json TEXT,
            relaxed_conditions_json TEXT,
            features_json TEXT,
            risk_params_json TEXT,
            reason_json TEXT,
            entry_snapshot_json TEXT,
            entry_reason_text TEXT,
            snapshot_source TEXT DEFAULT 'live_execution',
            position_status TEXT DEFAULT 'historical',
            exit_time TEXT,
            exit_price REAL,
            net_pnl REAL,
            pnl_pct REAL,
            return_now REAL,
            max_favorable_return REAL,
            max_adverse_return REAL,
            first_hit_1r_time TEXT,
            first_hit_minus_075r_time TEXT,
            bars_observed INTEGER DEFAULT 0,
            review_label TEXT DEFAULT 'pending',
            review_reason TEXT,
            reviewed_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_trade_entry_reviews_time ON trade_entry_reviews(entry_time DESC);
        CREATE INDEX IF NOT EXISTS idx_trade_entry_reviews_group ON trade_entry_reviews(strategy_source, category, entry_template, review_label);
        CREATE INDEX IF NOT EXISTS idx_trade_entry_reviews_status ON trade_entry_reviews(position_status, reviewed_at);
        CREATE TABLE IF NOT EXISTS exit_review_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary_id TEXT UNIQUE,
            run_time TEXT,
            window_days INTEGER,
            category TEXT,
            strategy_source TEXT,
            exit_reason TEXT,
            sample_size INTEGER DEFAULT 0,
            win_count INTEGER DEFAULT 0,
            loss_count INTEGER DEFAULT 0,
            avg_pnl REAL DEFAULT 0,
            total_pnl REAL DEFAULT 0,
            avg_mfe_after_exit REAL DEFAULT 0,
            avg_mae_after_exit REAL DEFAULT 0,
            good_exit_count INTEGER DEFAULT 0,
            early_exit_count INTEGER DEFAULT 0,
            noise_loss_exit_count INTEGER DEFAULT 0,
            small_profit_exit_count INTEGER DEFAULT 0,
            late_exit_count INTEGER DEFAULT 0,
            conclusion TEXT,
            action_type TEXT,
            summary_text TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_exit_review_summaries_run ON exit_review_summaries(run_time DESC);
        CREATE INDEX IF NOT EXISTS idx_exit_review_summaries_reason ON exit_review_summaries(exit_reason, run_time DESC);
        -- Training samples table.
        CREATE TABLE IF NOT EXISTS training_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            feature_json TEXT,
            composite_score REAL,
            market_regime TEXT,
            return_6h REAL,
            return_12h REAL,
            return_24h REAL,
            return_48h REAL,
            max_drawdown REAL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_train_sym_time ON training_samples(symbol, timestamp);
        CREATE INDEX IF NOT EXISTS idx_train_scan ON training_samples(scan_id);
        -- 浜ゆ槗瀵瑰揩鐓ц〃锛堝垢瀛樿€呭亸宸慨澶嶏級
        CREATE TABLE IF NOT EXISTS symbol_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            status TEXT,
            quote_volume REAL,
            price_change_24h REAL,
            active BOOLEAN DEFAULT 1,
            UNIQUE(date, symbol)
        );
        -- Trade cooldown table.
        CREATE TABLE IF NOT EXISTS trade_cooldown (
            symbol TEXT PRIMARY KEY,
            last_stop_time TEXT,
            stop_count_24h INTEGER DEFAULT 0,
            consecutive_stops INTEGER DEFAULT 0,
            cooldown_until TEXT,
            reason TEXT,
            updated_at TEXT
        );
        -- V3.0 璁㈠崟绨垮揩鐓ц〃
        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            bid_depth REAL,
            ask_depth REAL,
            imbalance_ratio REAL,
            top_bid_qty REAL,
            top_ask_qty REAL
        );
        CREATE INDEX IF NOT EXISTS idx_ob_timestamp ON orderbook_snapshots(timestamp);
        CREATE INDEX IF NOT EXISTS idx_ob_symbol ON orderbook_snapshots(symbol);
        -- V4.0 璁㈠崟绨挎繁搴﹀揩鐓ц〃锛堝寮虹増锛屽惈澶у皬鍗曠粺璁★級
        CREATE TABLE IF NOT EXISTS orderbook_depth (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            bid_depth REAL,
            ask_depth REAL,
            imbalance_ratio REAL,
            top_bid_qty REAL,
            top_ask_qty REAL,
            big_bid_cnt INTEGER DEFAULT 0,
            big_ask_cnt INTEGER DEFAULT 0,
            big_bid_vol REAL DEFAULT 0,
            big_ask_vol REAL DEFAULT 0,
            total_bid_20 REAL DEFAULT 0,
            total_ask_20 REAL DEFAULT 0,
            quote_volume_24h REAL DEFAULT 0,
            UNIQUE(time, symbol)
        );
        CREATE TABLE IF NOT EXISTS alpha_symbols (
            alpha_symbol TEXT PRIMARY KEY,
            base_asset TEXT,
            token_id TEXT,
            alpha_name TEXT,
            status TEXT,
            alpha_trade_symbol TEXT,
            futures_symbol TEXT,
            tradeability TEXT,
            price REAL,
            percent_change_24h REAL,
            volume_24h REAL,
            liquidity REAL,
            market_cap REAL,
            first_seen TEXT DEFAULT (datetime('now')),
            last_seen TEXT DEFAULT (datetime('now')),
            raw_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_symbols_tradeability ON alpha_symbols(tradeability);
        CREATE INDEX IF NOT EXISTS idx_alpha_symbols_volume ON alpha_symbols(volume_24h DESC);
        CREATE TABLE IF NOT EXISTS alpha_square_posts (
            post_id TEXT PRIMARY KEY,
            base_asset TEXT NOT NULL,
            published_at TEXT NOT NULL,
            author_id TEXT,
            author_name TEXT,
            content TEXT,
            sentiment TEXT NOT NULL,
            sentiment_confidence REAL DEFAULT 0,
            substantive_risk INTEGER DEFAULT 0,
            engagement REAL DEFAULT 0,
            source_url TEXT,
            raw_json TEXT,
            collected_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_square_posts_asset_time
            ON alpha_square_posts(base_asset, published_at DESC);
        CREATE TABLE IF NOT EXISTS alpha_square_sentiment_snapshots (
            time TEXT NOT NULL,
            base_asset TEXT NOT NULL,
            window_minutes INTEGER NOT NULL DEFAULT 30,
            effective_post_count INTEGER NOT NULL DEFAULT 0,
            unique_authors INTEGER NOT NULL DEFAULT 0,
            bearish_ratio REAL NOT NULL DEFAULT 0,
            baseline_bearish_ratio_24h REAL NOT NULL DEFAULT 0,
            top3_author_share REAL NOT NULL DEFAULT 1,
            substantive_risk_count INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT,
            PRIMARY KEY (time, base_asset, window_minutes)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_square_sentiment_asset_time
            ON alpha_square_sentiment_snapshots(base_asset, time DESC);
        CREATE TABLE IF NOT EXISTS alpha_candles_1h (
            time TEXT, alpha_symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, alpha_symbol)
        );
        CREATE TABLE IF NOT EXISTS alpha_candles_15m (
            time TEXT, alpha_symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, alpha_symbol)
        );
        CREATE TABLE IF NOT EXISTS alpha_candles_6h (
            time TEXT, alpha_symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, alpha_symbol)
        );
        CREATE TABLE IF NOT EXISTS alpha_candles_24h (
            time TEXT, alpha_symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, alpha_symbol)
        );
        CREATE TABLE IF NOT EXISTS alpha_candles_1m (
            time TEXT NOT NULL,
            alpha_symbol TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL DEFAULT 0,
            quote_vol REAL NOT NULL DEFAULT 0,
            trades INTEGER NOT NULL DEFAULT 0,
            taker_buy_quote_vol REAL NOT NULL DEFAULT 0,
            source_env TEXT NOT NULL DEFAULT 'mainnet',
            is_closed INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'rest',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (time, alpha_symbol, source_env)
        );
        CREATE TABLE IF NOT EXISTS aggregated_candles (
            market_kind TEXT NOT NULL,
            source_env TEXT NOT NULL DEFAULT 'mainnet',
            symbol TEXT NOT NULL,
            interval TEXT NOT NULL,
            time TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL DEFAULT 0,
            quote_vol REAL NOT NULL DEFAULT 0,
            trades INTEGER NOT NULL DEFAULT 0,
            taker_buy_quote_vol REAL NOT NULL DEFAULT 0,
            minute_count INTEGER NOT NULL,
            expected_count INTEGER NOT NULL,
            is_complete INTEGER NOT NULL DEFAULT 0,
            comparison_status TEXT NOT NULL DEFAULT 'pending',
            comparison_details_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (market_kind, source_env, symbol, interval, time)
        );
        CREATE TABLE IF NOT EXISTS candle_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_kind TEXT NOT NULL,
            source_env TEXT NOT NULL DEFAULT 'mainnet',
            symbol TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            detected_at TEXT NOT NULL,
            resolved_at TEXT,
            updated_at TEXT NOT NULL,
            UNIQUE(market_kind, source_env, symbol, start_time, end_time)
        );
        CREATE TABLE IF NOT EXISTS candle_sync_runtime (
            collector_id TEXT PRIMARY KEY,
            market_kind TEXT NOT NULL,
            source_env TEXT NOT NULL DEFAULT 'mainnet',
            status TEXT NOT NULL DEFAULT 'starting',
            connection_state TEXT NOT NULL DEFAULT 'disconnected',
            heartbeat_at TEXT NOT NULL,
            last_event_at TEXT,
            last_closed_time TEXT,
            queue_depth INTEGER NOT NULL DEFAULT 0,
            lag_seconds REAL,
            error_count INTEGER NOT NULL DEFAULT 0,
            reconnect_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS alpha_orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            alpha_symbol TEXT,
            bid_depth REAL,
            ask_depth REAL,
            imbalance_ratio REAL,
            spread_pct REAL,
            top_bid_qty REAL,
            top_ask_qty REAL
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_ob_symbol ON alpha_orderbook_snapshots(alpha_symbol);
        CREATE INDEX IF NOT EXISTS idx_alpha_ob_time ON alpha_orderbook_snapshots(timestamp DESC);
        CREATE TABLE IF NOT EXISTS alpha_scan_scores (
            time TEXT,
            scan_id TEXT,
            alpha_symbol TEXT,
            base_asset TEXT,
            futures_symbol TEXT,
            alpha_score REAL,
            discovery_score REAL,
            momentum_score REAL,
            liquidity_score REAL,
            risk_score REAL,
            tradeability_score REAL,
            grade TEXT,
            decision TEXT,
            market_price REAL,
            raw_features TEXT,
            alpha_profile TEXT,
            entry_level TEXT,
            suggested_position_pct REAL DEFAULT 0,
            block_reasons TEXT,
            profile_thresholds TEXT,
            PRIMARY KEY (scan_id, alpha_symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_scan_scores_scan ON alpha_scan_scores(scan_id);
        CREATE INDEX IF NOT EXISTS idx_alpha_scan_scores_symbol ON alpha_scan_scores(alpha_symbol);
        CREATE INDEX IF NOT EXISTS idx_alpha_scan_scores_time ON alpha_scan_scores(time DESC);
        CREATE TABLE IF NOT EXISTS trading_runtime_controls (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS alpha_trade_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT,
            time TEXT,
            alpha_symbol TEXT NOT NULL,
            futures_symbol TEXT,
            base_asset TEXT,
            alpha_discovery_score REAL,
            alpha_profile TEXT,
            alpha_reason TEXT,
            raw_alpha_json TEXT,
            normal_score REAL,
            normal_grade TEXT,
            normal_side TEXT,
            entry_profile TEXT,
            entry_status TEXT,
            block_reason TEXT,
            adapter_quality REAL,
            missing_fields_json TEXT,
            volume_price_state TEXT,
            volume_price_action TEXT,
            volume_price_reasons_json TEXT,
            volume_price_metrics_json TEXT,
            volume_price_max_position_factor REAL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(scan_id, alpha_symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_trade_candidates_time ON alpha_trade_candidates(time DESC);
        CREATE INDEX IF NOT EXISTS idx_alpha_trade_candidates_symbol ON alpha_trade_candidates(alpha_symbol);
        CREATE INDEX IF NOT EXISTS idx_alpha_trade_candidates_futures ON alpha_trade_candidates(futures_symbol);
        CREATE TABLE IF NOT EXISTS alpha_cooldowns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT DEFAULT 'alpha',
            symbol TEXT,
            cooldown_type TEXT,
            reason TEXT,
            cooldown_until TEXT,
            loss_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(source, symbol, cooldown_type)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_cooldowns_until ON alpha_cooldowns(cooldown_until);
        CREATE INDEX IF NOT EXISTS idx_alpha_cooldowns_symbol ON alpha_cooldowns(symbol);
        CREATE TABLE IF NOT EXISTS alpha_feature_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            market_env TEXT NOT NULL CHECK(market_env='mainnet'),
            alpha_symbol TEXT,
            futures_symbol TEXT NOT NULL,
            candle_close_time TEXT NOT NULL,
            feature_schema_version INTEGER NOT NULL,
            data_quality_status TEXT NOT NULL,
            data_quality_json TEXT NOT NULL,
            features_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (
                market_env, futures_symbol, candle_close_time,
                feature_schema_version
            )
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_feature_snapshots_symbol_time
            ON alpha_feature_snapshots(market_env, futures_symbol, candle_close_time DESC);
        CREATE TABLE IF NOT EXISTS alpha_signal_states (
            market_env TEXT NOT NULL CHECK(market_env='mainnet'),
            futures_symbol TEXT NOT NULL,
            alpha_symbol TEXT,
            state TEXT NOT NULL,
            setup_type TEXT,
            setup_id TEXT,
            state_version INTEGER NOT NULL DEFAULT 1,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT,
            last_candle_close_time TEXT,
            snapshot_id TEXT,
            reference_price REAL,
            base_low REAL,
            base_high REAL,
            breakout_level REAL,
            invalidation_price REAL,
            p_setup_success REAL,
            p_followthrough REAL,
            p_fakeout REAL,
            expected_r REAL,
            model_versions_json TEXT,
            reason_codes_json TEXT,
            metrics_json TEXT,
            PRIMARY KEY (market_env, futures_symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_signal_states_state
            ON alpha_signal_states(market_env, state, updated_at DESC);
        CREATE TABLE IF NOT EXISTS alpha_signal_events (
            event_id TEXT PRIMARY KEY,
            market_env TEXT NOT NULL CHECK(market_env='mainnet'),
            strategy_mode TEXT NOT NULL DEFAULT 'signal',
            futures_symbol TEXT NOT NULL,
            alpha_symbol TEXT,
            setup_id TEXT,
            from_state TEXT,
            to_state TEXT NOT NULL,
            state_version INTEGER NOT NULL,
            action_type TEXT,
            event_time TEXT NOT NULL,
            candle_close_time TEXT NOT NULL,
            snapshot_id TEXT,
            reference_price REAL,
            invalidation_price REAL,
            max_position_factor REAL,
            expires_at TEXT,
            reason_codes_json TEXT NOT NULL,
            ai_decision_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_signal_events_action
            ON alpha_signal_events(market_env, action_type, event_time DESC);
        CREATE INDEX IF NOT EXISTS idx_alpha_signal_events_symbol
            ON alpha_signal_events(market_env, futures_symbol, event_time DESC);
        CREATE TABLE IF NOT EXISTS alpha_signal_consumptions (
            account_id INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            action_type TEXT NOT NULL,
            status TEXT NOT NULL,
            rejection_reason TEXT,
            client_order_id TEXT,
            position_id TEXT,
            quantity REAL,
            order_id TEXT,
            consumed_at TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account_id, event_id, action_type)
        );
        CREATE INDEX IF NOT EXISTS idx_alpha_signal_consumptions_status
            ON alpha_signal_consumptions(account_id, status, updated_at DESC);
        CREATE TABLE IF NOT EXISTS alpha_strategy_runtime (
            market_env TEXT PRIMARY KEY CHECK(market_env='mainnet'),
            strategy_mode TEXT NOT NULL,
            worker_id TEXT,
            heartbeat_at TEXT NOT NULL,
            last_candle_close_time TEXT,
            processed_count INTEGER NOT NULL DEFAULT 0,
            transition_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS service_runtime_status (
            service_name TEXT NOT NULL,
            account_id INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'starting',
            heartbeat_at TEXT NOT NULL,
            last_success_at TEXT,
            last_error_at TEXT,
            error_code TEXT,
            last_error TEXT,
            detail_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (service_name, account_id)
        );
        CREATE INDEX IF NOT EXISTS idx_service_runtime_health
            ON service_runtime_status(status, heartbeat_at DESC);
        CREATE TABLE IF NOT EXISTS position_roll_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id TEXT,
            symbol TEXT NOT NULL,
            position_side TEXT,
            strategy_source TEXT DEFAULT 'normal',
            roll_layer INTEGER,
            roll_qty REAL,
            roll_price REAL,
            roll_reason TEXT,
            risk_before_json TEXT,
            risk_after_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_position_roll_events_symbol ON position_roll_events(symbol, created_at DESC);
    """)
    for table in (
        "candles_1m", "futures_candles_1m", "alpha_candles_1m",
        "futures_candles_15m", "futures_candles_1h",
        "futures_candles_6h", "futures_candles_24h",
        "alpha_candles_15m", "alpha_candles_1h",
        "alpha_candles_6h", "alpha_candles_24h",
    ):
        _ensure_column(conn, table, "taker_buy_quote_vol", "REAL")
        _ensure_column(conn, table, "source_env", "TEXT NOT NULL DEFAULT 'mainnet'")
        _ensure_column(conn, table, "is_closed", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(
        conn,
        "futures_data",
        "source_env",
        "TEXT NOT NULL DEFAULT 'mainnet'",
    )
    _ensure_environment_scoped_futures_tables(conn)
    _ensure_column(
        conn,
        "alpha_signal_events",
        "strategy_mode",
        "TEXT NOT NULL DEFAULT 'signal'",
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_alpha_signal_events_mode_action
           ON alpha_signal_events(
               market_env, strategy_mode, action_type, event_time DESC
           )"""
    )
    _ensure_column(conn, "positions_history", "position_side", "TEXT")
    _ensure_column(conn, "positions_history", "mark_price", "REAL")
    _ensure_column(conn, "positions_history", "leverage", "INTEGER DEFAULT 1")
    for table in ("trades", "orders", "fills", "position_history"):
        _ensure_column(conn, table, "position_id", "TEXT")
        _ensure_column(conn, table, "strategy_source", "TEXT DEFAULT 'normal'")
        _ensure_column(conn, table, "signal_source", "TEXT")
        _ensure_column(conn, table, "alpha_symbol", "TEXT")
        _ensure_column(conn, table, "alpha_profile", "TEXT")
        _ensure_column(conn, table, "alpha_entry_level", "TEXT")
        _ensure_column(conn, table, "alpha_score", "REAL")
        _ensure_column(conn, table, "alpha_suggested_position_pct", "REAL")
    _ensure_column(conn, "fills", "position_side", "TEXT")
    _ensure_column(conn, "fills", "exchange_order_id", "TEXT")
    for column, ddl in {
        "client_order_id": "TEXT",
        "exchange_order_id": "TEXT",
        "signal_event_id": "TEXT",
        "setup_id": "TEXT",
        "alpha_stage": "TEXT",
        "ai_model_versions_json": "TEXT",
    }.items():
        _ensure_column(conn, "orders", column, ddl)
    for column, ddl in {
        "signal_event_id": "TEXT",
        "setup_id": "TEXT",
        "alpha_stage": "TEXT",
        "ai_model_versions_json": "TEXT",
    }.items():
        _ensure_column(conn, "position_roll_events", column, ddl)
    _ensure_column(conn, "trades", "position_side", "TEXT")
    for table in (
        "trades", "orders", "fills", "positions_history", "strategy_decisions",
        "decision_actions", "exchange_income_ledger", "position_trades",
        "trade_entry_reviews", "trade_exit_reviews", "position_roll_events",
    ):
        _ensure_column(conn, table, "account_id", "INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_account_client_id
           ON orders(account_id, client_order_id)
           WHERE client_order_id IS NOT NULL"""
    )
    _ensure_column(conn, "position_trades", "grade_at_entry", "TEXT")
    _ensure_column(conn, "position_trades", "score_at_entry", "REAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS account_position_history AS SELECT 1 AS account_id, * FROM position_history"
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_account_position_history_key ON account_position_history(account_id, symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_account_time ON orders(account_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_account_time ON fills(account_id, created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_account_symbol_time ON fills(account_id, symbol, created_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_income_account_symbol_time ON exchange_income_ledger(account_id, symbol, income_time, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_positions_history_account_time ON positions_history(account_id, time DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_position_trades_account_exit ON position_trades(account_id, exit_time DESC)")
    for column, ddl in {
        "volume_price_state": "TEXT",
        "volume_price_action": "TEXT",
        "volume_price_reasons_json": "TEXT",
        "volume_price_metrics_json": "TEXT",
        "volume_price_max_position_factor": "REAL",
    }.items():
        _ensure_column(conn, "alpha_trade_candidates", column, ddl)
    for column, ddl in {
        "alpha_profile": "TEXT",
        "entry_level": "TEXT",
        "suggested_position_pct": "REAL DEFAULT 0",
        "block_reasons": "TEXT",
        "profile_thresholds": "TEXT",
    }.items():
        _ensure_column(conn, "alpha_scan_scores", column, ddl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_trades_position_id ON trades(position_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_position_id ON fills(position_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_position_id ON orders(position_id)")
    for column, ddl in {
        "tp1_hit": "INTEGER DEFAULT 0",
        "tp2_hit": "INTEGER DEFAULT 0",
        "highest_price": "REAL",
        "lowest_price": "REAL",
        "last_exit_reason": "TEXT",
        "roll_layer": "INTEGER DEFAULT 0",
        "last_roll_time": "TEXT",
        "roll_parent_trade_id": "TEXT",
        "protected_profit": "REAL DEFAULT 0",
        "max_floating_pnl": "REAL DEFAULT 0",
        "max_floating_roi": "REAL DEFAULT 0",
        "roll_enabled": "INTEGER DEFAULT 0",
        "roll_block_reason": "TEXT",
        "stop_model": "TEXT",
        "initial_stop_loss": "REAL",
        "stop_pct": "REAL",
        "current_stop_loss": "REAL",
        "trailing_stop_price": "REAL",
        "trailing_enabled": "INTEGER DEFAULT 0",
        "trailing_atr_multiplier": "REAL",
        "r_multiple": "REAL DEFAULT 0",
        "initial_quantity": "REAL",
        "roll_price": "REAL",
        "protected_stop": "REAL",
        "roll_cycle_peak_price": "REAL",
        "roll_pullback_armed": "INTEGER DEFAULT 0",
        "alpha_volume_protect_regime": "TEXT",
        "alpha_volume_protect_time": "TEXT",
        "alpha_profit_lock_stage": "INTEGER DEFAULT 0",
        "alpha_locked_roi": "REAL DEFAULT 0",
        "alpha_stall_protect_price": "REAL",
        "alpha_stall_protect_time": "TEXT",
    }.items():
        _ensure_column(conn, "position_history", column, ddl)
        _ensure_column(conn, "account_position_history", column, ddl)
    for column, ddl in {
        "decision_id": "TEXT",
        "run_id": "TEXT",
        "scan_id": "TEXT",
        "side": "TEXT",
        "mode": "TEXT DEFAULT 'live'",
        "decision_stage": "TEXT",
        "decision_result": "TEXT",
        "filter_reason": "TEXT",
        "composite_score": "REAL",
        "grade": "TEXT",
        "market_regime": "TEXT",
        "price": "REAL",
        "quantity": "REAL",
        "entry_price": "REAL",
        "risk_params_json": "TEXT",
        "features_json": "TEXT",
        "reason_json": "TEXT",
    }.items():
        _ensure_column(conn, "strategy_decisions", column, ddl)
    for column, ddl in {
        "decision_id": "TEXT",
        "strategy_decision_id": "INTEGER",
        "run_id": "TEXT",
        "scan_id": "TEXT",
        "symbol": "TEXT",
        "signal_time": "TEXT",
        "entry_price": "REAL",
        "side": "TEXT",
        "return_1h": "REAL",
        "return_4h": "REAL",
        "return_12h": "REAL",
        "return_24h": "REAL",
        "max_favorable_return": "REAL",
        "max_adverse_return": "REAL",
        "best_side": "TEXT",
        "direction_correct": "INTEGER",
        "hit_tp": "INTEGER",
        "hit_sl": "INTEGER",
        "bars_observed": "INTEGER DEFAULT 0",
        "is_complete": "INTEGER DEFAULT 0",
        "updated_at": "TEXT",
    }.items():
        _ensure_column(conn, "signal_outcomes", column, ddl)
    for column, ddl in {
        "churn_trade": "INTEGER DEFAULT 0",
        "probe_failed": "INTEGER DEFAULT 0",
        "weak_after_entry": "INTEGER DEFAULT 0",
        "holding_minutes": "REAL",
    }.items():
        _ensure_column(conn, "decision_outcomes", column, ddl)
    for column, ddl in {
        "churn_trade_count": "INTEGER DEFAULT 0",
        "probe_failed_count": "INTEGER DEFAULT 0",
        "weak_after_entry_count": "INTEGER DEFAULT 0",
    }.items():
        _ensure_column(conn, "policy_reviews", column, ddl)
    for column, ddl in {
        "source_type": "TEXT",
        "source_run_time": "TEXT",
        "target": "TEXT",
        "action": "TEXT",
        "title": "TEXT",
        "summary": "TEXT",
        "condition_json": "TEXT",
        "change_json": "TEXT",
        "confidence": "REAL DEFAULT 0",
        "sample_size": "INTEGER DEFAULT 0",
        "expected_delta": "REAL DEFAULT 0",
        "risk_note": "TEXT",
        "status": "TEXT DEFAULT 'proposed'",
        "activated_at": "TEXT",
        "rollback_condition_json": "TEXT",
        "dedupe_key": "TEXT",
    }.items():
        _ensure_column(conn, "strategy_policy_candidates", column, ddl)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_policy_candidates_dedupe "
        "ON strategy_policy_candidates(dedupe_key) WHERE dedupe_key IS NOT NULL"
    )
    _ensure_performance_indexes(conn)
    conn.execute(
        """INSERT OR IGNORE INTO trading_runtime_controls(key, value)
           VALUES ('normal_trading_enabled', 'true')"""
    )
    conn.execute(
        """INSERT OR IGNORE INTO trading_runtime_controls(key, value)
           VALUES ('alpha_trading_enabled', 'false')"""
    )
    conn.execute(
        """DELETE FROM alpha_trade_candidates
           WHERE futures_symbol IS NULL OR futures_symbol = ''"""
    )
    conn.execute(
        """DELETE FROM alpha_scan_scores
           WHERE futures_symbol IS NULL OR futures_symbol = ''"""
    )
    conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
    conn.commit()
    conn.close()


def upsert_service_runtime_status(
    service_name,
    *,
    status="ok",
    account_id=0,
    error_code=None,
    last_error=None,
    details=None,
    heartbeat_at=None,
):
    """Persist a service heartbeat without exposing credentials or payload secrets."""
    now = heartbeat_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    normalized_status = str(status or "unknown").strip().lower()
    normalized_error = str(last_error).strip()[:2000] if last_error else None
    success_at = now if normalized_status in {"ok", "idle"} else None
    error_at = now if normalized_error else None
    payload = json.dumps(details or {}, ensure_ascii=False, default=str, separators=(",", ":"))
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO service_runtime_status
               (service_name, account_id, status, heartbeat_at, last_success_at,
                last_error_at, error_code, last_error, detail_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(service_name, account_id) DO UPDATE SET
                 status=excluded.status,
                 heartbeat_at=excluded.heartbeat_at,
                 last_success_at=COALESCE(excluded.last_success_at, service_runtime_status.last_success_at),
                 last_error_at=COALESCE(excluded.last_error_at, service_runtime_status.last_error_at),
                 error_code=excluded.error_code,
                 last_error=excluded.last_error,
                 detail_json=excluded.detail_json,
                 updated_at=excluded.updated_at""",
            (
                str(service_name).strip(), int(account_id or 0), normalized_status,
                now, success_at, error_at, error_code, normalized_error, payload, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_service_runtime_status(service_name=None, account_id=None):
    clauses = []
    params = []
    if service_name is not None:
        clauses.append("service_name = ?")
        params.append(str(service_name))
    if account_id is not None:
        clauses.append("account_id = ?")
        params.append(int(account_id))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT * FROM service_runtime_status {where}
                ORDER BY service_name, account_id""",
            params,
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("detail_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                item["details"] = {}
            result.append(item)
        return result
    finally:
        conn.close()


def _ensure_column(conn, table, column, ddl):
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _primary_key_columns(conn, table):
    return [
        row["name"]
        for row in sorted(
            conn.execute(f"PRAGMA table_info({table})").fetchall(),
            key=lambda row: int(row["pk"] or 0),
        )
        if int(row["pk"] or 0) > 0
    ]


def _unique_index_columns(conn, table):
    result = []
    for index in conn.execute(f"PRAGMA index_list({table})").fetchall():
        if not int(index["unique"] or 0):
            continue
        result.append(
            [
                row["name"]
                for row in conn.execute(
                    f"PRAGMA index_info({index['name']})"
                ).fetchall()
            ]
        )
    return result


def _table_sql(conn, table):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return str(row["sql"] or "") if row else ""


def _ensure_environment_scoped_futures_tables(conn):
    """Migrate legacy market tables so mainnet/testnet rows can coexist."""
    expected_key = ["time", "symbol", "source_env"]
    for table in _FUTURES_CANDLE_TABLES:
        table_sql = _table_sql(conn, table)
        if (
            _primary_key_columns(conn, table) == expected_key
            and not table_sql.startswith('CREATE TABLE "')
        ):
            continue
        legacy = f"{table}__legacy_env"
        conn.execute(f"DROP TABLE IF EXISTS {legacy}")
        conn.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
        conn.execute(
            f"""CREATE TABLE {table} (
                time TEXT,
                symbol TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                quote_vol REAL,
                trades INTEGER,
                taker_buy_quote_vol REAL,
                source_env TEXT NOT NULL DEFAULT 'mainnet',
                is_closed INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (time, symbol, source_env)
            )"""
        )
        conn.execute(
            f"""INSERT OR REPLACE INTO {table}
                (time, symbol, open, high, low, close, volume, quote_vol,
                 trades, taker_buy_quote_vol, source_env, is_closed)
                SELECT time, symbol, open, high, low, close, volume, quote_vol,
                       trades, taker_buy_quote_vol,
                       COALESCE(source_env, 'mainnet'), COALESCE(is_closed, 1)
                FROM {legacy}"""
        )
        conn.execute(f"DROP TABLE {legacy}")

    futures_data_sql = " ".join(_table_sql(conn, "futures_data").split())
    if (
        expected_key in _unique_index_columns(conn, "futures_data")
        and "UNIQUE(time, symbol, source_env)" in futures_data_sql
    ):
        return
    legacy = "futures_data__legacy_env"
    conn.execute(f"DROP TABLE IF EXISTS {legacy}")
    conn.execute(f"ALTER TABLE futures_data RENAME TO {legacy}")
    conn.execute(
        """CREATE TABLE futures_data (
            time TEXT NOT NULL,
            symbol TEXT NOT NULL,
            open_interest REAL,
            funding_rate REAL,
            mark_price REAL,
            source_env TEXT NOT NULL DEFAULT 'mainnet',
            UNIQUE(time, symbol, source_env)
        )"""
    )
    conn.execute(
        """INSERT OR REPLACE INTO futures_data
            (time, symbol, open_interest, funding_rate, mark_price, source_env)
            SELECT time, symbol, open_interest, funding_rate, mark_price,
                   COALESCE(source_env, 'mainnet')
            FROM futures_data__legacy_env"""
    )
    conn.execute("DROP TABLE futures_data__legacy_env")


def _ensure_performance_indexes(conn):
    """Add query-path indexes for the scanner, dashboard, trader, and reviews."""
    indexes = [
        # Market data: most reads are latest-N by symbol or latest scan windows.
        ("idx_c1h_time_symbol", "candles_1h", "time DESC, symbol"),
        ("idx_c15m_time_symbol", "candles_15m", "time DESC, symbol"),
        ("idx_c6h_sym_time", "candles_6h", "symbol, time DESC"),
        ("idx_c6h_time_symbol", "candles_6h", "time DESC, symbol"),
        ("idx_c24h_sym_time", "candles_24h", "symbol, time DESC"),
        ("idx_c24h_time_symbol", "candles_24h", "time DESC, symbol"),
        ("idx_c1m_env_sym_time", "candles_1m", "source_env, symbol, time DESC"),
        ("idx_fc1m_env_sym_time", "futures_candles_1m", "source_env, symbol, time DESC"),
        ("idx_ac1m_env_sym_time", "alpha_candles_1m", "source_env, alpha_symbol, time DESC"),
        ("idx_agg_candles_lookup", "aggregated_candles", "market_kind, source_env, symbol, interval, time DESC"),
        ("idx_agg_candles_complete", "aggregated_candles", "interval, is_complete, time DESC"),
        ("idx_candle_gaps_status", "candle_gaps", "status, detected_at"),
        ("idx_candle_runtime_market", "candle_sync_runtime", "market_kind, source_env"),
        ("idx_fc1h_sym_time", "futures_candles_1h", "symbol, time DESC"),
        ("idx_fc15m_sym_time", "futures_candles_15m", "symbol, time DESC"),
        ("idx_fc6h_sym_time", "futures_candles_6h", "symbol, time DESC"),
        ("idx_fc24h_sym_time", "futures_candles_24h", "symbol, time DESC"),
        ("idx_fc1h_env_sym_time", "futures_candles_1h", "source_env, symbol, time DESC"),
        ("idx_fc15m_env_sym_time", "futures_candles_15m", "source_env, symbol, time DESC"),
        ("idx_fc6h_env_sym_time", "futures_candles_6h", "source_env, symbol, time DESC"),
        ("idx_fc24h_env_sym_time", "futures_candles_24h", "source_env, symbol, time DESC"),
        ("idx_market_universe_pool_ready", "market_universe", "pool_type, selected, data_ready, universe_rank"),
        ("idx_market_universe_futures", "market_universe", "futures_symbol"),
        ("idx_fut_time_symbol", "futures_data", "time DESC, symbol"),
        ("idx_fut_sym", "futures_data", "symbol, time"),
        ("idx_fut_env_symbol_time", "futures_data", "source_env, symbol, time DESC"),
        ("idx_oc_time_symbol", "onchain_flows", "time DESC, symbol"),
        ("idx_oc_chain_time", "onchain_flows", "chain, time DESC"),
        ("idx_orderbook_depth_symbol_time", "orderbook_depth", "symbol, time DESC"),
        ("idx_orderbook_depth_time_symbol", "orderbook_depth", "time DESC, symbol"),
        ("idx_ob_symbol_timestamp", "orderbook_snapshots", "symbol, timestamp DESC"),
        # Normal scoring and live decisions.
        ("idx_alpha_scores_symbol_time_desc", "alpha_scores", "symbol, time DESC"),
        ("idx_alpha_scores_time_score", "alpha_scores", "time DESC, composite_score DESC"),
        ("idx_alpha_scores_scan_score", "alpha_scores", "scan_id, composite_score DESC"),
        ("idx_alpha_scores_grade_time", "alpha_scores", "composite_summary, time DESC"),
        ("idx_strategy_decisions_symbol_time", "strategy_decisions", "symbol, time DESC"),
        ("idx_strategy_decisions_stage_time", "strategy_decisions", "decision_stage, time DESC"),
        ("idx_strategy_decisions_result_time", "strategy_decisions", "decision_result, time DESC"),
        ("idx_strategy_decisions_run_symbol", "strategy_decisions", "run_id, symbol"),
        ("idx_strategy_decisions_created", "strategy_decisions", "created_at DESC"),
        ("idx_strategy_decisions_account_time", "strategy_decisions", "account_id, time DESC, id DESC"),
        ("idx_strategy_decisions_account_run_id", "strategy_decisions", "account_id, run_id, id DESC"),
        ("idx_strategy_decisions_account_run_filter", "strategy_decisions", "account_id, run_id, filter_reason"),
        ("idx_signal_outcomes_symbol_time", "signal_outcomes", "symbol, signal_time DESC"),
        ("idx_signal_outcomes_complete_time", "signal_outcomes", "is_complete, signal_time DESC"),
        ("idx_signal_outcomes_scan", "signal_outcomes", "scan_id"),
        ("idx_decision_actions_source_decision", "decision_actions", "source_decision_id"),
        ("idx_decision_actions_source_trade", "decision_actions", "source_trade_id"),
        ("idx_decision_outcomes_complete_time", "decision_outcomes", "is_complete, signal_time"),
        # Backtests, factors, and learning pages.
        ("idx_factor_effectiveness_bucket", "factor_effectiveness", "bucket, run_time DESC"),
        ("idx_policy_candidates_target_status", "strategy_policy_candidates", "target, status"),
        ("idx_policy_audit_created", "strategy_policy_audit", "created_at DESC"),
        ("idx_shadow_symbol_created", "shadow_decisions", "symbol, created_at DESC"),
        # Orders, fills, trades, and position management.
        ("idx_trades_symbol_created", "trades", "symbol, created_at DESC"),
        ("idx_trades_created", "trades", "created_at DESC"),
        ("idx_trades_source_created", "trades", "source, created_at DESC"),
        ("idx_trades_strategy_created", "trades", "strategy_source, created_at DESC"),
        ("idx_trades_alpha_symbol", "trades", "alpha_symbol, created_at DESC"),
        ("idx_orders_status_created", "orders", "status, created_at DESC"),
        ("idx_orders_symbol_created", "orders", "symbol, created_at DESC"),
        ("idx_orders_alpha_symbol", "orders", "alpha_symbol, created_at DESC"),
        ("idx_fills_symbol_created", "fills", "symbol, created_at DESC"),
        ("idx_fills_order_id", "fills", "order_id"),
        ("idx_fills_alpha_symbol", "fills", "alpha_symbol, created_at DESC"),
        ("idx_position_history_update", "position_history", "update_time DESC"),
        ("idx_position_history_strategy", "position_history", "strategy_source"),
        ("idx_position_history_alpha", "position_history", "alpha_symbol"),
        ("idx_positions_symbol_time", "positions_history", "symbol, time DESC"),
        ("idx_position_roll_events_position", "position_roll_events", "position_id, created_at DESC"),
        ("idx_position_roll_events_created", "position_roll_events", "created_at DESC"),
        ("idx_trade_cooldown_until_symbol", "trade_cooldown", "cooldown_until, symbol"),
        # Symbol universes and snapshots.
        ("idx_symbols_active_last_seen", "symbols", "is_active, last_seen DESC"),
        ("idx_symbol_snapshots_symbol_date", "symbol_snapshots", "symbol, date DESC"),
        ("idx_symbol_snapshots_active_volume", "symbol_snapshots", "active, quote_volume DESC"),
        # Alpha market data and execution adapters.
        ("idx_alpha_symbols_base", "alpha_symbols", "base_asset"),
        ("idx_alpha_symbols_futures", "alpha_symbols", "futures_symbol"),
        ("idx_alpha_symbols_last_seen", "alpha_symbols", "last_seen DESC"),
        ("idx_alpha_c1h_sym_time", "alpha_candles_1h", "alpha_symbol, time DESC"),
        ("idx_alpha_c1h_time_symbol", "alpha_candles_1h", "time DESC, alpha_symbol"),
        ("idx_alpha_c15m_sym_time", "alpha_candles_15m", "alpha_symbol, time DESC"),
        ("idx_alpha_c15m_time_symbol", "alpha_candles_15m", "time DESC, alpha_symbol"),
        ("idx_alpha_c6h_sym_time", "alpha_candles_6h", "alpha_symbol, time DESC"),
        ("idx_alpha_c6h_time_symbol", "alpha_candles_6h", "time DESC, alpha_symbol"),
        ("idx_alpha_c24h_sym_time", "alpha_candles_24h", "alpha_symbol, time DESC"),
        ("idx_alpha_c24h_time_symbol", "alpha_candles_24h", "time DESC, alpha_symbol"),
        ("idx_alpha_ob_symbol_time", "alpha_orderbook_snapshots", "alpha_symbol, timestamp DESC"),
        ("idx_alpha_scan_scores_scan_score", "alpha_scan_scores", "scan_id, discovery_score DESC"),
        ("idx_alpha_scan_scores_futures", "alpha_scan_scores", "futures_symbol"),
        ("idx_alpha_scan_scores_symbol_time", "alpha_scan_scores", "alpha_symbol, time DESC"),
        ("idx_alpha_scan_scores_profile_time", "alpha_scan_scores", "alpha_profile, time DESC"),
        ("idx_alpha_scan_scores_entry_time", "alpha_scan_scores", "entry_level, time DESC"),
        ("idx_alpha_trade_candidates_scan_score", "alpha_trade_candidates", "scan_id, alpha_discovery_score DESC"),
        ("idx_alpha_trade_candidates_symbol_time", "alpha_trade_candidates", "alpha_symbol, time DESC, updated_at DESC, id DESC"),
        ("idx_alpha_trade_candidates_status_time", "alpha_trade_candidates", "entry_status, time DESC"),
        ("idx_alpha_trade_candidates_updated", "alpha_trade_candidates", "updated_at DESC"),
        ("idx_alpha_trade_candidates_profile", "alpha_trade_candidates", "alpha_profile, time DESC"),
        ("idx_alpha_trade_candidates_vp_state", "alpha_trade_candidates", "volume_price_state, time DESC"),
        ("idx_alpha_cooldowns_source_until", "alpha_cooldowns", "source, cooldown_until"),
        # Miscellaneous small tables still benefit in admin/dashboard lookups.
        ("idx_trading_runtime_updated", "trading_runtime_controls", "updated_at DESC"),
        ("idx_user_favorites_symbol", "user_favorites", "symbol"),
    ]
    for name, table, columns in indexes:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not exists:
            continue
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})")


def close_conn():
    if hasattr(_local, "conn") and _local.conn:
        _local.conn.close()
        _local.conn = None


def get_trading_runtime_controls():
    defaults = {
        "normal_trading_enabled": True,
        "alpha_trading_enabled": False,
    }
    conn = get_conn()
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trading_runtime_controls'"
        ).fetchone()
        if not exists:
            rows = []
        else:
            rows = conn.execute("SELECT key, value, updated_at FROM trading_runtime_controls").fetchall()
    except sqlite3.OperationalError as e:
        if "locked" not in str(e).lower():
            raise
        rows = []
    finally:
        conn.close()

    controls = defaults.copy()
    updated_at = {}
    for row in rows:
        key = row["key"]
        if key in controls:
            controls[key] = str(row["value"]).lower() in ("1", "true", "yes", "on")
            updated_at[key] = row["updated_at"]
    controls["updated_at"] = updated_at
    return controls


def set_trading_runtime_control(key, enabled):
    if key not in {"normal_trading_enabled", "alpha_trading_enabled"}:
        raise ValueError(f"unsupported trading control: {key}")
    conn = get_conn()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS trading_runtime_controls (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        conn.execute(
            """INSERT INTO trading_runtime_controls(key, value, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value,
                 updated_at=datetime('now')""",
            (key, "true" if enabled else "false"),
        )
        conn.commit()
    finally:
        conn.close()
    return get_trading_runtime_controls()


# ---- Candles ----

def insert_candles_1h(rows):
    conn = get_conn()
    conn.executemany(
        """INSERT OR REPLACE INTO candles_1h (time, symbol, open, high, low, close, volume, quote_vol, trades)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def insert_candles_15m(rows):
    conn = get_conn()
    conn.executemany(
        """INSERT OR REPLACE INTO candles_15m (time, symbol, open, high, low, close, volume, quote_vol, trades)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def insert_candles_6h(rows):
    conn = get_conn()
    conn.executemany(
        """INSERT OR REPLACE INTO candles_6h (time, symbol, open, high, low, close, volume, quote_vol, trades)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def insert_candles_24h(rows):
    conn = get_conn()
    conn.executemany(
        """INSERT OR REPLACE INTO candles_24h (time, symbol, open, high, low, close, volume, quote_vol, trades)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


_FUTURES_CANDLE_TABLES = {
    "futures_candles_1h",
    "futures_candles_15m",
    "futures_candles_6h",
    "futures_candles_24h",
}


def _normalize_extended_candle_rows(rows, default_env="mainnet"):
    normalized = []
    for row in rows or []:
        values = tuple(row)
        if len(values) == 9:
            values = (*values, None, default_env, 1)
        if len(values) != 12:
            raise ValueError(
                "candle row must have 9 legacy fields or 12 extended fields"
            )
        normalized.append(values)
    return normalized


def insert_futures_candles(table, rows):
    if table not in _FUTURES_CANDLE_TABLES:
        raise ValueError(f"unsupported futures candle table: {table}")
    if not rows:
        return
    normalized = _normalize_extended_candle_rows(rows)
    conn = get_conn()
    try:
        conn.executemany(
            f"""INSERT OR REPLACE INTO {table}
               (time, symbol, open, high, low, close, volume, quote_vol, trades,
                taker_buy_quote_vol, source_env, is_closed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            normalized,
        )
        conn.commit()
    finally:
        conn.close()


def fetch_futures_candles(
    table,
    symbols,
    hours=None,
    days=None,
    source_env=None,
    closed_only=False,
):
    if table not in _FUTURES_CANDLE_TABLES:
        raise ValueError(f"unsupported futures candle table: {table}")
    if not symbols:
        return []
    cutoff = datetime.now(timezone.utc) - (
        timedelta(days=days)
        if days is not None
        else timedelta(hours=hours or 72)
    )
    placeholders = ",".join("?" for _ in symbols)
    clauses = [f"symbol IN ({placeholders})", "time > ?"]
    params = list(symbols) + [cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")]
    if source_env:
        clauses.append("source_env = ?")
        params.append(str(source_env).lower())
    if closed_only:
        clauses.append("is_closed = 1")
    conn = get_conn()
    try:
        return conn.execute(
            f"""SELECT time, symbol, open, high, low, close, volume, quote_vol, trades,
                       taker_buy_quote_vol, source_env, is_closed
                FROM {table}
                WHERE {' AND '.join(clauses)}
                ORDER BY symbol, time""",
            params,
        ).fetchall()
    finally:
        conn.close()


_MINUTE_CANDLE_TABLES = {
    "spot": ("candles_1m", "symbol"),
    "futures": ("futures_candles_1m", "symbol"),
    "alpha": ("alpha_candles_1m", "alpha_symbol"),
}


def _minute_table(market_kind):
    try:
        return _MINUTE_CANDLE_TABLES[str(market_kind).strip().lower()]
    except KeyError as exc:
        raise ValueError(
            f"unsupported minute candle market: {market_kind}"
        ) from exc


@_serialized_write
def upsert_minute_candles(market_kind, rows):
    """Batch upsert fully normalized closed 1m candles."""
    if not rows:
        return 0
    table, symbol_column = _minute_table(market_kind)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    values = []
    for raw in rows:
        row = dict(raw)
        values.append(
            (
                row["time"],
                row.get("symbol") or row.get("alpha_symbol"),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row.get("volume") or 0),
                float(row.get("quote_vol") or 0),
                int(row.get("trades") or 0),
                float(row.get("taker_buy_quote_vol") or 0),
                str(row.get("source_env") or "mainnet").lower(),
                int(bool(row.get("is_closed", True))),
                str(row.get("source") or "stream"),
                str(row.get("updated_at") or now),
            )
        )
    conn = get_conn()
    try:
        conn.executemany(
            f"""INSERT INTO {table}
                (time, {symbol_column}, open, high, low, close, volume,
                 quote_vol, trades, taker_buy_quote_vol, source_env,
                 is_closed, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(time, {symbol_column}, source_env) DO UPDATE SET
                  open=excluded.open,
                  high=excluded.high,
                  low=excluded.low,
                  close=excluded.close,
                  volume=excluded.volume,
                  quote_vol=excluded.quote_vol,
                  trades=excluded.trades,
                  taker_buy_quote_vol=excluded.taker_buy_quote_vol,
                  is_closed=excluded.is_closed,
                  source=excluded.source,
                  updated_at=excluded.updated_at""",
            values,
        )
        conn.commit()
        return len(values)
    finally:
        conn.close()


def fetch_minute_candles(
    market_kind,
    symbol,
    start_time,
    end_time,
    source_env="mainnet",
):
    table, symbol_column = _minute_table(market_kind)
    conn = get_conn()
    try:
        return conn.execute(
            f"""SELECT time, {symbol_column} AS symbol, open, high, low,
                       close, volume, quote_vol, trades,
                       taker_buy_quote_vol, source_env, is_closed, source
                FROM {table}
                WHERE {symbol_column}=? AND source_env=?
                  AND time>=? AND time<=? AND is_closed=1
                ORDER BY time""",
            (
                str(symbol),
                str(source_env).lower(),
                str(start_time),
                str(end_time),
            ),
        ).fetchall()
    finally:
        conn.close()


def fetch_latest_minute_time(market_kind, symbol, source_env="mainnet"):
    table, symbol_column = _minute_table(market_kind)
    conn = get_conn()
    try:
        row = conn.execute(
            f"""SELECT MAX(time) AS latest
                FROM {table}
                WHERE {symbol_column}=? AND source_env=? AND is_closed=1""",
            (str(symbol), str(source_env).lower()),
        ).fetchone()
        return row["latest"] if row else None
    finally:
        conn.close()


@_serialized_write
def upsert_aggregated_candles(rows):
    if not rows:
        return 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    values = []
    for raw in rows:
        row = dict(raw)
        values.append(
            (
                row["market_kind"],
                str(row.get("source_env") or "mainnet").lower(),
                row["symbol"],
                row["interval"],
                row["time"],
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row.get("volume") or 0),
                float(row.get("quote_vol") or 0),
                int(row.get("trades") or 0),
                float(row.get("taker_buy_quote_vol") or 0),
                int(row["minute_count"]),
                int(row["expected_count"]),
                int(bool(row.get("is_complete"))),
                str(row.get("comparison_status") or "pending"),
                json.dumps(
                    row.get("comparison_details") or {},
                    ensure_ascii=False,
                ),
                str(row.get("updated_at") or now),
            )
        )
    conn = get_conn()
    try:
        conn.executemany(
            """INSERT INTO aggregated_candles
               (market_kind, source_env, symbol, interval, time,
                open, high, low, close, volume, quote_vol, trades,
                taker_buy_quote_vol, minute_count, expected_count,
                is_complete, comparison_status, comparison_details_json,
                updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?)
               ON CONFLICT(market_kind, source_env, symbol, interval, time)
               DO UPDATE SET
                 open=excluded.open,
                 high=excluded.high,
                 low=excluded.low,
                 close=excluded.close,
                 volume=excluded.volume,
                 quote_vol=excluded.quote_vol,
                 trades=excluded.trades,
                 taker_buy_quote_vol=excluded.taker_buy_quote_vol,
                 minute_count=excluded.minute_count,
                 expected_count=excluded.expected_count,
                 is_complete=excluded.is_complete,
                 comparison_status=excluded.comparison_status,
                 comparison_details_json=excluded.comparison_details_json,
                 updated_at=excluded.updated_at""",
            values,
        )
        conn.commit()
        return len(values)
    finally:
        conn.close()


_AGGREGATE_LEGACY_TABLES = {
    ("spot", "15m"): ("candles_15m", "symbol", False),
    ("spot", "1h"): ("candles_1h", "symbol", False),
    ("spot", "6h"): ("candles_6h", "symbol", False),
    ("spot", "1d"): ("candles_24h", "symbol", False),
    ("futures", "15m"): (
        "futures_candles_15m",
        "symbol",
        True,
    ),
    ("futures", "1h"): ("futures_candles_1h", "symbol", True),
    ("futures", "6h"): ("futures_candles_6h", "symbol", True),
    ("futures", "1d"): ("futures_candles_24h", "symbol", True),
    ("alpha", "15m"): ("alpha_candles_15m", "alpha_symbol", True),
    ("alpha", "1h"): ("alpha_candles_1h", "alpha_symbol", True),
    ("alpha", "6h"): ("alpha_candles_6h", "alpha_symbol", True),
    ("alpha", "1d"): ("alpha_candles_24h", "alpha_symbol", True),
}


@_serialized_write
def materialize_aggregated_candles(rows):
    """Publish complete unified aggregates to legacy strategy tables."""
    grouped = {}
    for raw in rows or []:
        row = dict(raw)
        if not bool(row.get("is_complete")):
            continue
        mapping = _AGGREGATE_LEGACY_TABLES.get(
            (row.get("market_kind"), row.get("interval"))
        )
        if not mapping:
            continue
        grouped.setdefault(mapping, []).append(row)
    if not grouped:
        return 0
    conn = get_conn()
    written = 0
    try:
        for (table, symbol_column, extended), items in grouped.items():
            if extended:
                values = [
                    (
                        row["time"],
                        row["symbol"],
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row.get("volume") or 0),
                        float(row.get("quote_vol") or 0),
                        int(row.get("trades") or 0),
                        float(row.get("taker_buy_quote_vol") or 0),
                        str(row.get("source_env") or "mainnet").lower(),
                        1,
                    )
                    for row in items
                ]
                conn.executemany(
                    f"""INSERT OR REPLACE INTO {table}
                       (time, {symbol_column}, open, high, low, close,
                        volume, quote_vol, trades, taker_buy_quote_vol,
                        source_env, is_closed)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
            else:
                values = [
                    (
                        row["time"],
                        row["symbol"],
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row.get("volume") or 0),
                        float(row.get("quote_vol") or 0),
                        int(row.get("trades") or 0),
                    )
                    for row in items
                ]
                conn.executemany(
                    f"""INSERT OR REPLACE INTO {table}
                       (time, {symbol_column}, open, high, low, close,
                        volume, quote_vol, trades)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    values,
                )
            written += len(values)
        conn.commit()
        return written
    finally:
        conn.close()


def materialize_stored_aggregates(since_hours=None):
    clauses = ["is_complete=1"]
    params = []
    if since_hours is not None:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(hours=max(1, int(since_hours)))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        clauses.append("time>=?")
        params.append(cutoff)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT * FROM aggregated_candles
                WHERE {' AND '.join(clauses)}
                ORDER BY time""",
            params,
        ).fetchall()
    finally:
        conn.close()
    return materialize_aggregated_candles(rows)


@_serialized_write
def upsert_candle_gap(
    market_kind,
    symbol,
    start_time,
    end_time,
    *,
    source_env="mainnet",
    status="pending",
    error=None,
):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO candle_gaps
               (market_kind, source_env, symbol, start_time, end_time,
                status, attempt_count, last_error, detected_at, resolved_at,
                updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, ?)
               ON CONFLICT(
                 market_kind, source_env, symbol, start_time, end_time
               ) DO UPDATE SET
                 status=excluded.status,
                 last_error=excluded.last_error,
                 resolved_at=NULL,
                 updated_at=excluded.updated_at""",
            (
                market_kind,
                str(source_env).lower(),
                symbol,
                start_time,
                end_time,
                status,
                error,
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


@_serialized_write
def update_candle_gap(gap_id, status, error=None):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    resolved_at = now if status == "resolved" else None
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE candle_gaps
               SET status=?,
                   attempt_count=attempt_count
                     + CASE WHEN ?='repairing' THEN 1 ELSE 0 END,
                   last_error=?,
                   resolved_at=?, updated_at=?
               WHERE id=?""",
            (status, status, error, resolved_at, now, int(gap_id)),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_candle_gaps(status="pending", limit=200):
    conn = get_conn()
    try:
        if status:
            rows = conn.execute(
                """SELECT * FROM candle_gaps WHERE status=?
                   ORDER BY detected_at LIMIT ?""",
                (status, int(limit)),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM candle_gaps
                   ORDER BY detected_at DESC LIMIT ?""",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@_serialized_write
def reset_stale_candle_repairs():
    conn = get_conn()
    try:
        cursor = conn.execute(
            """UPDATE candle_gaps
               SET status='pending', updated_at=datetime('now')
               WHERE status='repairing'"""
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


@_serialized_write
def upsert_candle_sync_runtime(collector_id, market_kind, **fields):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "source_env": "mainnet",
        "status": "running",
        "connection_state": "connected",
        "heartbeat_at": now,
        "last_event_at": None,
        "last_closed_time": None,
        "queue_depth": 0,
        "lag_seconds": None,
        "error_count": 0,
        "reconnect_count": 0,
        "last_error": None,
        "metrics": {},
        **fields,
    }
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO candle_sync_runtime
               (collector_id, market_kind, source_env, status,
                connection_state, heartbeat_at, last_event_at,
                last_closed_time, queue_depth, lag_seconds, error_count,
                reconnect_count, last_error, metrics_json, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(collector_id) DO UPDATE SET
                 market_kind=excluded.market_kind,
                 source_env=excluded.source_env,
                 status=excluded.status,
                 connection_state=excluded.connection_state,
                 heartbeat_at=excluded.heartbeat_at,
                 last_event_at=COALESCE(
                   excluded.last_event_at,
                   candle_sync_runtime.last_event_at
                 ),
                 last_closed_time=COALESCE(
                   excluded.last_closed_time,
                   candle_sync_runtime.last_closed_time
                 ),
                 queue_depth=excluded.queue_depth,
                 lag_seconds=excluded.lag_seconds,
                 error_count=excluded.error_count,
                 reconnect_count=excluded.reconnect_count,
                 last_error=excluded.last_error,
                 metrics_json=excluded.metrics_json,
                 updated_at=excluded.updated_at""",
            (
                collector_id,
                market_kind,
                str(payload["source_env"]).lower(),
                payload["status"],
                payload["connection_state"],
                payload["heartbeat_at"],
                payload["last_event_at"],
                payload["last_closed_time"],
                int(payload["queue_depth"] or 0),
                payload["lag_seconds"],
                int(payload["error_count"] or 0),
                int(payload["reconnect_count"] or 0),
                payload["last_error"],
                json.dumps(payload["metrics"], ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_candle_sync_status():
    conn = get_conn()
    try:
        runtime = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM candle_sync_runtime
                   ORDER BY market_kind, collector_id"""
            ).fetchall()
        ]
        pending = conn.execute(
            """SELECT market_kind, source_env, COUNT(*) AS count,
                      MIN(start_time) AS oldest_start
               FROM candle_gaps
               WHERE status != 'resolved'
               GROUP BY market_kind, source_env"""
        ).fetchall()
        aggregates = conn.execute(
            """SELECT market_kind, source_env, interval,
                      MAX(time) AS latest_time,
                      SUM(CASE WHEN is_complete=1 THEN 1 ELSE 0 END)
                        AS complete_count,
                      SUM(CASE WHEN comparison_status='matched' THEN 1 ELSE 0 END)
                        AS matched_count,
                      SUM(CASE WHEN comparison_status='mismatch' THEN 1 ELSE 0 END)
                        AS mismatch_count
               FROM aggregated_candles
               GROUP BY market_kind, source_env, interval"""
        ).fetchall()
        return {
            "runtime": runtime,
            "gaps": [dict(row) for row in pending],
            "aggregates": [dict(row) for row in aggregates],
        }
    finally:
        conn.close()


@_serialized_write
def purge_minute_candle_data(days=4):
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_conn()
    try:
        deleted = {}
        for table in (
            "candles_1m",
            "futures_candles_1m",
            "alpha_candles_1m",
        ):
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE time < ?",
                (cutoff,),
            )
            deleted[table] = cursor.rowcount
        conn.execute(
            """DELETE FROM candle_gaps
               WHERE status='resolved' AND updated_at < ?""",
            (cutoff,),
        )
        conn.commit()
        return deleted
    finally:
        conn.close()


def upsert_market_universe(rows, *, conn=None):
    if not rows:
        return
    columns = (
        "pool_type", "source_symbol", "spot_symbol", "futures_symbol",
        "spot_quote_volume_24h", "futures_quote_volume_24h", "effective_quote_volume_24h",
        "universe_rank", "selected", "forced_position", "data_ready", "data_error",
        "data_checked_at", "selection_reason", "updated_at",
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    values = []
    for row in rows:
        item = dict(row)
        item.setdefault("spot_quote_volume_24h", 0)
        item.setdefault("futures_quote_volume_24h", 0)
        item.setdefault("effective_quote_volume_24h", 0)
        item.setdefault("universe_rank", None)
        item.setdefault("selected", False)
        item.setdefault("forced_position", False)
        item.setdefault("data_ready", False)
        item.setdefault("data_error", None if item.get("data_ready") else "not_checked")
        item.setdefault("data_checked_at", None)
        item.setdefault("selection_reason", None)
        item.setdefault("updated_at", now)
        values.append(tuple(item.get(column) for column in columns))
    readiness_columns = {"data_ready", "data_error", "data_checked_at"}
    assignments = ", ".join(
        f"{column}=excluded.{column}"
        for column in columns[2:]
        if column not in readiness_columns
    )
    owns_connection = conn is None
    conn = conn or get_conn()
    try:
        conn.executemany(
            f"""INSERT INTO market_universe ({', '.join(columns)})
                VALUES ({', '.join('?' for _ in columns)})
                ON CONFLICT(pool_type, source_symbol) DO UPDATE SET {assignments}""",
            values,
        )
        if owns_connection:
            conn.commit()
    finally:
        if owns_connection:
            conn.close()


def replace_market_universe(pool_type, rows):
    conn = get_conn()
    try:
        # Publish the new selection atomically. Scoring workers must never
        # observe the short window where the old pool has been deselected but
        # the refreshed rows have not yet been upserted.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """UPDATE market_universe
               SET selected = 0, forced_position = 0,
                   selection_reason = 'not_in_current_pool', updated_at = ?
               WHERE pool_type = ?""",
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), pool_type),
        )
        upsert_market_universe(rows, conn=conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_tracked_position_symbols():
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT symbol FROM position_history
               WHERE COALESCE(quantity, 0) != 0 AND symbol IS NOT NULL AND symbol != ''"""
        ).fetchall()
        return {str(row["symbol"]).upper() for row in rows}
    finally:
        conn.close()


def fetch_tracked_alpha_positions():
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT alpha_symbol, symbol AS futures_symbol
               FROM position_history
               WHERE COALESCE(quantity, 0) != 0
                 AND alpha_symbol IS NOT NULL AND alpha_symbol != ''"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def futures_candles_current(
    symbol,
    max_age_minutes=20,
    source_env=None,
    table="futures_candles_15m",
    closed_only=False,
):
    if table not in _FUTURES_CANDLE_TABLES:
        raise ValueError(f"unsupported futures candle table: {table}")
    conn = get_conn()
    try:
        closed_clause = " AND is_closed = 1" if closed_only else ""
        if source_env:
            row = conn.execute(
                f"""SELECT MAX(time) AS latest
                    FROM {table}
                    WHERE symbol = ? AND source_env = ?{closed_clause}""",
                (symbol, source_env),
            ).fetchone()
        else:
            row = conn.execute(
                f"""SELECT MAX(time) AS latest FROM {table}
                    WHERE symbol = ?{closed_clause}""",
                (symbol,),
            ).fetchone()
    finally:
        conn.close()
    if not row or not row["latest"]:
        return False
    latest = datetime.fromisoformat(str(row["latest"]).replace("Z", "+00:00"))
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - latest <= timedelta(minutes=max_age_minutes)


def fetch_market_universe(pool_type=None, selected_only=False, ready_only=False):
    clauses = []
    params = []
    if pool_type:
        clauses.append("pool_type = ?")
        params.append(pool_type)
    if selected_only:
        clauses.append("selected = 1")
    if ready_only:
        clauses.append("data_ready = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = get_conn()
    try:
        return conn.execute(
            f"SELECT * FROM market_universe {where} ORDER BY pool_type, universe_rank, source_symbol",
            params,
        ).fetchall()
    finally:
        conn.close()


@_serialized_write
def update_market_readiness(pool_type, source_symbol, ready, error=None, checked_at=None):
    checked_at = checked_at or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE market_universe
               SET data_ready = ?, data_error = ?, data_checked_at = ?
               WHERE pool_type = ? AND source_symbol = ?""",
            (int(bool(ready)), error, checked_at, pool_type, source_symbol),
        )
        conn.commit()
    finally:
        conn.close()


@_serialized_write
def update_market_readiness_batch(pool_type, results, checked_at=None):
    """Publish one pool's readiness in a single short write transaction.

    The minute pipeline used to open and commit one SQLite connection for
    every symbol.  A normal+Alpha refresh therefore created hundreds of
    competing write transactions while candle batches were being stored.
    Keeping the whole refresh in one executemany materially shortens the
    lock window and makes the readiness snapshot atomic.
    """
    if not results:
        return 0
    checked_at = checked_at or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    values = []
    for source_symbol, result in results.items():
        if isinstance(result, dict):
            ready = result.get("ready")
            error = result.get("error")
        else:
            ready = getattr(result, "ready")
            error = getattr(result, "error", None)
        values.append(
            (
                int(bool(ready)),
                error,
                checked_at,
                str(pool_type),
                str(source_symbol),
            )
        )
    conn = get_conn()
    try:
        conn.executemany(
            """UPDATE market_universe
               SET data_ready = ?, data_error = ?, data_checked_at = ?
               WHERE pool_type = ? AND source_symbol = ?""",
            values,
        )
        conn.commit()
        return len(values)
    finally:
        conn.close()


def fetch_market_data_health():
    conn = get_conn()
    try:
        output = {}
        for pool_type, limit in (("normal", 150), ("alpha", 80)):
            summary = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN selected = 1 THEN 1 ELSE 0 END) AS selected,
                          SUM(CASE WHEN selected = 1 AND data_ready = 1 THEN 1 ELSE 0 END) AS ready,
                          SUM(CASE WHEN selected = 1 AND data_ready = 0 THEN 1 ELSE 0 END) AS unready,
                          SUM(CASE WHEN forced_position = 1 THEN 1 ELSE 0 END) AS forced
                   FROM market_universe WHERE pool_type = ?""",
                (pool_type,),
            ).fetchone()
            spot_table = "alpha_candles_15m" if pool_type == "alpha" else "candles_15m"
            spot_column = "alpha_symbol" if pool_type == "alpha" else "symbol"
            latest_spot = conn.execute(
                f"""SELECT MAX(c.time) AS latest FROM {spot_table} c
                    JOIN market_universe u ON u.pool_type = ?
                      AND u.source_symbol = c.{spot_column}
                    WHERE u.selected = 1""",
                (pool_type,),
            ).fetchone()["latest"]
            latest_futures = conn.execute(
                """SELECT MAX(c.time) AS latest FROM futures_candles_15m c
                   JOIN market_universe u ON u.pool_type = ?
                     AND u.futures_symbol = c.symbol
                   WHERE u.selected = 1""",
                (pool_type,),
            ).fetchone()["latest"]
            errors = [dict(row) for row in conn.execute(
                """SELECT source_symbol, futures_symbol, universe_rank, data_error, data_checked_at
                   FROM market_universe
                   WHERE pool_type = ? AND selected = 1 AND data_ready = 0
                   ORDER BY universe_rank LIMIT 50""",
                (pool_type,),
            ).fetchall()]
            output[pool_type] = {
                "limit": limit,
                "total": int(summary["total"] or 0),
                "selected": int(summary["selected"] or 0),
                "ready": int(summary["ready"] or 0),
                "unready": int(summary["unready"] or 0),
                "forced": int(summary["forced"] or 0),
                "latest_spot_15m": latest_spot,
                "latest_futures_15m": latest_futures,
                "errors": errors,
            }
        return output
    finally:
        conn.close()


def purge_old_kline_data(days=RETENTION_DAYS):
    now = datetime.now(timezone.utc)
    tables = (
        "candles_1h",
        "candles_15m",
        "candles_6h",
        "candles_24h",
        "alpha_candles_1h",
        "alpha_candles_15m",
        "alpha_candles_6h",
        "alpha_candles_24h",
        "futures_candles_1h",
        "futures_candles_15m",
        "futures_candles_6h",
        "futures_candles_24h",
    )
    conn = get_conn()
    try:
        deleted = {}
        for table in tables:
            table_days = (
                max(int(days), STRATEGY_RETENTION_DAYS)
                if table in STRATEGY_RETENTION_TABLES
                else int(days)
            )
            cutoff = (now - timedelta(days=table_days)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            cur = conn.execute(
                f"DELETE FROM {table} WHERE time < ?",
                (cutoff,),
            )
            deleted[table] = cur.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def upsert_alpha_symbols(rows):
    conn = get_conn()
    conn.executemany(
        """INSERT INTO alpha_symbols
           (alpha_symbol, base_asset, token_id, alpha_name, status, alpha_trade_symbol,
            futures_symbol, tradeability, price, percent_change_24h, volume_24h,
            liquidity, market_cap, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(alpha_symbol) DO UPDATE SET
             base_asset=excluded.base_asset,
             token_id=excluded.token_id,
             alpha_name=excluded.alpha_name,
             status=excluded.status,
             alpha_trade_symbol=excluded.alpha_trade_symbol,
             futures_symbol=excluded.futures_symbol,
             tradeability=excluded.tradeability,
             price=excluded.price,
             percent_change_24h=excluded.percent_change_24h,
             volume_24h=excluded.volume_24h,
             liquidity=excluded.liquidity,
             market_cap=excluded.market_cap,
             last_seen=datetime('now'),
             raw_json=excluded.raw_json""",
        rows,
    )
    conn.commit()


def upsert_alpha_square_posts(rows):
    if not rows:
        return 0
    conn = get_conn()
    try:
        conn.executemany(
            """INSERT INTO alpha_square_posts
               (post_id, base_asset, published_at, author_id, author_name,
                content, sentiment, sentiment_confidence, substantive_risk,
                engagement, source_url, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(post_id) DO UPDATE SET
                 base_asset=excluded.base_asset,
                 published_at=excluded.published_at,
                 author_id=excluded.author_id,
                 author_name=excluded.author_name,
                 content=excluded.content,
                 sentiment=excluded.sentiment,
                 sentiment_confidence=excluded.sentiment_confidence,
                 substantive_risk=excluded.substantive_risk,
                 engagement=excluded.engagement,
                 source_url=excluded.source_url,
                 raw_json=excluded.raw_json,
                 collected_at=datetime('now')""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def insert_alpha_square_sentiment_snapshot(row):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO alpha_square_sentiment_snapshots
               (time, base_asset, window_minutes, effective_post_count,
                unique_authors, bearish_ratio, baseline_bearish_ratio_24h,
                top3_author_share, substantive_risk_count, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            row,
        )
        conn.commit()
    finally:
        conn.close()


def fetch_latest_alpha_square_sentiment(base_asset, max_age_minutes=20):
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT *,
                      (julianday('now') - julianday(time)) * 1440 AS age_minutes
               FROM alpha_square_sentiment_snapshots
               WHERE base_asset=?
                 AND datetime(time) >= datetime('now', ?)
               ORDER BY datetime(time) DESC
               LIMIT 1""",
            (
                str(base_asset or "").upper(),
                f"-{float(max_age_minutes):g} minutes",
            ),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fetch_alpha_square_posts(base_asset, hours=24):
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT *
               FROM alpha_square_posts
               WHERE base_asset=?
                 AND datetime(published_at) >= datetime('now', ?)
               ORDER BY datetime(published_at)""",
            (
                str(base_asset or "").upper(),
                f"-{float(hours):g} hours",
            ),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def insert_alpha_candles(table, rows):
    if table not in {"alpha_candles_1h", "alpha_candles_15m", "alpha_candles_6h", "alpha_candles_24h"}:
        raise ValueError(f"unsupported alpha candle table: {table}")
    normalized = _normalize_extended_candle_rows(rows)
    conn = get_conn()
    conn.executemany(
        f"""INSERT OR REPLACE INTO {table}
           (time, alpha_symbol, open, high, low, close, volume, quote_vol, trades,
            taker_buy_quote_vol, source_env, is_closed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        normalized,
    )
    conn.commit()


def insert_alpha_orderbook_snapshot(rows):
    conn = get_conn()
    conn.executemany(
        """INSERT INTO alpha_orderbook_snapshots
           (timestamp, alpha_symbol, bid_depth, ask_depth, imbalance_ratio,
            spread_pct, top_bid_qty, top_ask_qty)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def fetch_active_alpha_symbols(limit=200):
    conn = get_conn()
    rows = conn.execute(
        """SELECT a.*
           FROM alpha_symbols a
           JOIN market_universe u
             ON u.pool_type = 'alpha' AND u.source_symbol = a.alpha_symbol
           WHERE a.status = 'TRADING'
             AND a.tradeability = 'alpha_futures_mapped'
             AND u.selected = 1 AND u.data_ready = 1
           ORDER BY u.universe_rank
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return rows


def fetch_alpha_candles(
    table,
    symbols,
    hours=None,
    days=None,
    source_env=None,
    closed_only=False,
):
    if not symbols:
        return []
    if table not in {"alpha_candles_1h", "alpha_candles_15m", "alpha_candles_6h", "alpha_candles_24h"}:
        raise ValueError(f"unsupported alpha candle table: {table}")
    placeholders = ",".join("?" for _ in symbols)
    if days is not None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours or 72)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    clauses = [f"alpha_symbol IN ({placeholders})", "time > ?"]
    params = list(symbols) + [cutoff]
    if source_env:
        clauses.append("source_env = ?")
        params.append(str(source_env).strip().lower())
    if closed_only:
        clauses.append("is_closed = 1")
    conn = get_conn()
    try:
        return conn.execute(
            f"""SELECT time, alpha_symbol, open, high, low, close, volume, quote_vol, trades,
                       taker_buy_quote_vol, source_env, is_closed
                FROM {table}
                WHERE {' AND '.join(clauses)}
                ORDER BY alpha_symbol, time""",
            params,
        ).fetchall()
    finally:
        conn.close()


def fetch_alpha_orderbook_depth(symbol, hours=6):
    conn = get_conn()
    try:
        return conn.execute(
            """SELECT *
               FROM alpha_orderbook_snapshots
               WHERE alpha_symbol = ?
                 AND julianday(timestamp) > julianday('now', ?)
               ORDER BY julianday(timestamp) DESC""",
            (symbol, f"-{hours} hours"),
        ).fetchall()
    finally:
        conn.close()


def fetch_klines_1h(symbols, hours=72):
    return fetch_futures_candles("futures_candles_1h", symbols, hours=hours)


def fetch_klines_15m(symbols, hours=12):
    return fetch_futures_candles("futures_candles_15m", symbols, hours=hours)


def fetch_klines_6h(symbols, days=14):
    return fetch_futures_candles("futures_candles_6h", symbols, days=days)


def fetch_klines_24h(symbols, days=35):
    return fetch_futures_candles("futures_candles_24h", symbols, days=days)


def fetch_spot_klines_1h(symbols, hours=72):
    if not symbols:
        return []
    placeholders = ",".join("?" for _ in symbols)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_conn()
    try:
        return conn.execute(
            f"""SELECT time, symbol, open, high, low, close, volume, quote_vol
                FROM candles_1h WHERE symbol IN ({placeholders}) AND time > ?
                ORDER BY symbol, time""",
            list(symbols) + [cutoff],
        ).fetchall()
    finally:
        conn.close()


def cleanup_old_operational_data(retention_days=RETENTION_DAYS, now=None, batch_size=5000):
    """Delete expired regenerable data without touching accounting/current-state tables."""
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    deleted = {}

    for table, time_column in OPERATIONAL_RETENTION_TABLES.items():
        table_days = (
            max(int(retention_days), STRATEGY_RETENTION_DAYS)
            if table in STRATEGY_RETENTION_TABLES
            else int(retention_days)
        )
        cutoff = (
            now.astimezone(timezone.utc) - timedelta(days=table_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = get_conn()
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            total = 0
            while True:
                cursor = conn.execute(
                    f"""DELETE FROM {table}
                        WHERE rowid IN (
                            SELECT rowid FROM {table}
                            WHERE {time_column} IS NOT NULL
                              AND julianday({time_column}) < julianday(?)
                            LIMIT ?
                        )""",
                    (cutoff, batch_size),
                )
                count = max(0, int(cursor.rowcount or 0))
                conn.commit()
                total += count
                if count < batch_size:
                    break
            deleted[table] = total
        finally:
            conn.close()

    checkpoint = get_conn()
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        checkpoint.close()
    return deleted


# ---- Futures ----

def insert_futures(rows):
    normalized = []
    for row in rows or []:
        values = tuple(row)
        if len(values) == 5:
            values = (*values, "mainnet")
        if len(values) != 6:
            raise ValueError(
                "futures row must have 5 legacy fields or 6 environment-scoped fields"
            )
        normalized.append(values)
    if not normalized:
        return
    conn = get_conn()
    try:
        conn.executemany(
            """INSERT OR REPLACE INTO futures_data
               (time, symbol, open_interest, funding_rate, mark_price, source_env)
               VALUES (?, ?, ?, ?, ?, ?)""",
            normalized,
        )
        conn.commit()
    finally:
        conn.close()


def fetch_futures(symbols, hours=72, source_env=None):
    if not symbols:
        return []
    conn = get_conn()
    placeholders = ",".join("?" for _ in symbols)
    clauses = [
        f"symbol IN ({placeholders})",
        f"julianday(time) > julianday('now', '-{int(hours)} hours')",
    ]
    params = list(symbols)
    if source_env:
        clauses.append("source_env = ?")
        params.append(str(source_env).strip().lower())
    try:
        return conn.execute(
            f"""SELECT time, symbol, open_interest, funding_rate, mark_price,
                       source_env
                FROM futures_data
                WHERE {' AND '.join(clauses)}
                ORDER BY symbol, time""",
            params,
        ).fetchall()
    finally:
        conn.close()


# ---- On-chain ----

def insert_onchain(rows):
    conn = get_conn()
    try:
        conn.executemany(
            """INSERT OR REPLACE INTO onchain_flows
               (time, symbol, chain, cex_inflow_usd, cex_outflow_usd, cex_net_flow_usd,
                cex_net_flow_14d_usd, cex_net_outflow_ratio, window_hours)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def fetch_onchain(symbols, hours=72):
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT time, symbol, chain, cex_net_flow_usd, cex_net_flow_14d_usd,
                       cex_net_outflow_ratio
                FROM onchain_flows
                WHERE julianday(time) > julianday('now', '-{int(hours)} hours')
                ORDER BY julianday(time)""",
        ).fetchall()
        return rows
    finally:
        conn.close()


# ---- Trades ----

def new_position_id(symbol, side):
    clean_symbol = (symbol or "UNKNOWN").replace("/", "").upper()
    clean_side = (side or "SIDE").upper()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"A{current_account_id()}-{clean_symbol}-{clean_side}-{stamp}-{uuid.uuid4().hex[:8]}"


def record_trade(
    symbol,
    side,
    qty,
    entry_price,
    exit_price,
    pnl,
    pnl_pct,
    exit_reason,
    grade,
    score,
    entry_reason=None,
    position_id=None,
    strategy_source="normal",
    signal_source=None,
    alpha_symbol=None,
    alpha_profile=None,
    alpha_entry_level=None,
    alpha_score=None,
    alpha_suggested_position_pct=None,
    stop_model=None,
    initial_stop_loss=None,
    stop_pct=None,
    trailing_atr_multiplier=None,
):
    conn = get_conn()
    conn.execute(
        """INSERT INTO trades
           (account_id, position_id, symbol, side, quantity, entry_price, exit_price, pnl, pnl_pct,
            exit_reason, entry_reason, entry_time, exit_time, grade_at_entry, score_at_entry,
            strategy_source, signal_source, alpha_symbol, alpha_profile, alpha_entry_level,
            alpha_score, alpha_suggested_position_pct)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            current_account_id(),
            position_id,
            symbol,
            side,
            qty,
            entry_price,
            exit_price,
            pnl,
            pnl_pct,
            exit_reason,
            entry_reason,
            grade,
            score,
            strategy_source,
            signal_source,
            alpha_symbol,
            alpha_profile,
            alpha_entry_level,
            alpha_score,
            alpha_suggested_position_pct,
        )
    )
    conn.commit()


def _mirror_default_position_history(conn, symbol):
    """Keep the legacy table readable while account 1 uses the scoped table."""
    legacy = [row["name"] for row in conn.execute("PRAGMA table_info(position_history)")]
    scoped = {row["name"] for row in conn.execute("PRAGMA table_info(account_position_history)")}
    columns = [name for name in legacy if name in scoped]
    quoted = ", ".join(f'"{name}"' for name in columns)
    conn.execute(
        f"INSERT OR REPLACE INTO position_history ({quoted}) SELECT {quoted} FROM account_position_history WHERE account_id=1 AND symbol=?",
        (symbol,),
    )


def upsert_position_history(
    symbol,
    side,
    quantity,
    entry_price,
    entry_reason,
    entry_score,
    tp3_price,
    atr_value,
    position_id=None,
    strategy_source="normal",
    signal_source=None,
    alpha_symbol=None,
    alpha_profile=None,
    alpha_entry_level=None,
    alpha_score=None,
    alpha_suggested_position_pct=None,
    stop_model=None,
    initial_stop_loss=None,
    stop_pct=None,
    trailing_atr_multiplier=None,
):
    """V3.0 璁板綍/鏇存柊寮€浠撲俊鎭紝閲嶅惎鍚庡彲鎭㈠"""
    conn = get_conn()
    account_id = current_account_id()
    existing = conn.execute("SELECT position_id FROM account_position_history WHERE account_id=? AND symbol=?", (account_id, symbol)).fetchone()
    if (
        existing
        and position_id
        and existing["position_id"]
        and str(existing["position_id"]) != str(position_id)
    ):
        conn.execute("DELETE FROM account_position_history WHERE account_id=? AND symbol=?", (account_id, symbol))
        existing = None
    position_id = position_id or (existing["position_id"] if existing and "position_id" in existing.keys() else None) or new_position_id(symbol, side)
    conn.execute(
        """INSERT INTO account_position_history
           (account_id, symbol, side, quantity, initial_quantity, entry_price, entry_reason, entry_score, entry_time, tp3_price, atr_value,
            highest_price, position_id, strategy_source, signal_source, alpha_symbol,
            alpha_profile, alpha_entry_level, alpha_score, alpha_suggested_position_pct,
            lowest_price, stop_model, initial_stop_loss, stop_pct, current_stop_loss,
            trailing_atr_multiplier, update_time)
           VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(account_id, symbol) DO UPDATE SET
             side=excluded.side,
             quantity=excluded.quantity,
             entry_price=excluded.entry_price,
             entry_reason=excluded.entry_reason,
             entry_score=excluded.entry_score,
             entry_time=COALESCE(account_position_history.entry_time, excluded.entry_time),
             tp3_price=excluded.tp3_price,
             atr_value=excluded.atr_value,
             position_id=COALESCE(account_position_history.position_id, excluded.position_id),
             strategy_source=excluded.strategy_source,
             signal_source=excluded.signal_source,
             alpha_symbol=excluded.alpha_symbol,
             alpha_profile=excluded.alpha_profile,
             alpha_entry_level=excluded.alpha_entry_level,
             alpha_score=excluded.alpha_score,
             alpha_suggested_position_pct=excluded.alpha_suggested_position_pct,
             highest_price=COALESCE(account_position_history.highest_price, excluded.highest_price),
             lowest_price=COALESCE(account_position_history.lowest_price, excluded.lowest_price),
             stop_model=excluded.stop_model,
             initial_stop_loss=excluded.initial_stop_loss,
             stop_pct=excluded.stop_pct,
             current_stop_loss=COALESCE(account_position_history.current_stop_loss, excluded.current_stop_loss),
             trailing_atr_multiplier=excluded.trailing_atr_multiplier,
             update_time=datetime('now')""",
        (
            account_id,
            symbol,
            side,
            quantity,
            quantity,
            entry_price,
            entry_reason,
            entry_score,
            tp3_price,
            atr_value,
            entry_price,
            position_id,
            strategy_source,
            signal_source,
            alpha_symbol,
            alpha_profile,
            alpha_entry_level,
            alpha_score,
            alpha_suggested_position_pct,
            entry_price,
            stop_model,
            initial_stop_loss,
            stop_pct,
            initial_stop_loss,
            trailing_atr_multiplier,
        )
    )
    if account_id == 1:
        _mirror_default_position_history(conn, symbol)
    conn.commit()
    conn.close()
    return position_id


def get_position_history(symbol):
    """Fetch persisted live position entry state."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM account_position_history WHERE account_id=? AND symbol=?", (current_account_id(), symbol)).fetchone()
    conn.close()
    return dict(row) if row else None


def fetch_position_recovery_evidence(symbol, side):
    """Find local entry/partial-close evidence for a live position missing state."""
    account_id = current_account_id()
    open_side = "BUY" if str(side or "").upper() == "LONG" else "SELL"
    conn = get_conn()
    try:
        entry = conn.execute(
            """SELECT *
               FROM orders
               WHERE account_id=?
                 AND symbol=?
                 AND side=?
                 AND order_type='MARKET'
                 AND COALESCE(reason, '') NOT LIKE 'roll_add%'
               ORDER BY id DESC
               LIMIT 1""",
            (account_id, symbol, open_side),
        ).fetchone()
        if not entry:
            return {
                "entry": None,
                "closed_quantity": 0.0,
                "partial_close_count": 0,
                "tp1_hit": False,
                "tp2_hit": False,
                "last_exit_reason": None,
            }

        trades = conn.execute(
            """SELECT quantity, exit_reason, exit_time
               FROM trades
               WHERE account_id=?
                 AND symbol=?
                 AND datetime(exit_time) >= datetime(?)
               ORDER BY id ASC""",
            (account_id, symbol, entry["created_at"]),
        ).fetchall()
        reasons = [str(row["exit_reason"] or "") for row in trades]
        closed_quantity = sum(float(row["quantity"] or 0) for row in trades)
        entry_quantity = float(entry["quantity"] or 0)
        # A fully closed local lifecycle is stale evidence for a position that is
        # still live on the exchange (for example, a later manual/restarted entry).
        if entry_quantity > 0 and closed_quantity >= entry_quantity * 0.999:
            return {
                "entry": None,
                "closed_quantity": 0.0,
                "partial_close_count": 0,
                "tp1_hit": False,
                "tp2_hit": False,
                "last_exit_reason": None,
            }
        return {
            "entry": dict(entry),
            "closed_quantity": closed_quantity,
            "partial_close_count": len(trades),
            "tp1_hit": bool(trades),
            "tp2_hit": any("TP2" in reason for reason in reasons) or len(trades) >= 2,
            "last_exit_reason": reasons[-1] if reasons else None,
        }
    finally:
        conn.close()


def delete_position_history(symbol):
    """Delete persisted live position entry state after close."""
    conn = get_conn()
    conn.execute("DELETE FROM account_position_history WHERE account_id=? AND symbol=?", (current_account_id(), symbol))
    if current_account_id() == 1:
        conn.execute("DELETE FROM position_history WHERE symbol=?", (symbol,))
    conn.commit()
    conn.close()


def update_position_management(symbol, **fields):
    """Update live position management state without resetting the entry record."""
    allowed = {
        "quantity",
        "initial_quantity",
        "entry_price",
        "highest_price",
        "lowest_price",
        "tp1_hit",
        "tp2_hit",
        "last_exit_reason",
        "roll_layer",
        "last_roll_time",
        "protected_profit",
        "max_floating_pnl",
        "max_floating_roi",
        "roll_enabled",
        "roll_block_reason",
        "stop_model",
        "initial_stop_loss",
        "stop_pct",
        "current_stop_loss",
        "trailing_stop_price",
        "trailing_enabled",
        "trailing_atr_multiplier",
        "r_multiple",
        "roll_price",
        "protected_stop",
        "roll_cycle_peak_price",
        "roll_pullback_armed",
        "alpha_volume_protect_regime",
        "alpha_volume_protect_time",
        "alpha_profit_lock_stage",
        "alpha_locked_roi",
        "alpha_stall_protect_price",
        "alpha_stall_protect_time",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    conn = get_conn()
    assignments = ", ".join([f"{k}=?" for k in updates])
    values = list(updates.values()) + [current_account_id(), symbol]
    conn.execute(
        f"UPDATE account_position_history SET {assignments}, update_time=datetime('now') WHERE account_id=? AND symbol=?",
        values,
    )
    if current_account_id() == 1:
        _mirror_default_position_history(conn, symbol)
    conn.commit()
    conn.close()


def record_position_roll_event(
    symbol,
    position_side,
    strategy_source,
    roll_layer,
    roll_qty,
    roll_price,
    roll_reason,
    position_id=None,
    risk_before=None,
    risk_after=None,
    signal_event_id=None,
    setup_id=None,
    alpha_stage=None,
    ai_model_versions=None,
):
    conn = get_conn()
    conn.execute(
        """INSERT INTO position_roll_events
           (position_id, symbol, position_side, strategy_source, roll_layer, roll_qty,
            roll_price, roll_reason, risk_before_json, risk_after_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            position_id,
            symbol,
            position_side,
            strategy_source,
            roll_layer,
            roll_qty,
            roll_price,
            roll_reason,
            json.dumps(risk_before or {}, ensure_ascii=False),
            json.dumps(risk_after or {}, ensure_ascii=False),
        ),
    )
    if signal_event_id or setup_id or alpha_stage or ai_model_versions:
        conn.execute(
            """UPDATE position_roll_events
               SET signal_event_id=?, setup_id=?, alpha_stage=?,
                   ai_model_versions_json=?
               WHERE id=last_insert_rowid()""",
            (
                signal_event_id,
                setup_id,
                alpha_stage,
                json.dumps(ai_model_versions or {}, ensure_ascii=False),
            ),
        )
    conn.commit()
    conn.close()


def _parse_db_time(value):
    if not value:
        return None
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        try:
            return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None


def _format_db_time(dt):
    if not dt:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _partition_entry_key_rows(items, has_open_between):
    """Split same-price fallback rows into continuous position lifecycles."""
    rows = [dict(row) for row in items]
    rows.sort(key=lambda row: _parse_db_time(row.get("exit_time")) or datetime.min.replace(tzinfo=timezone.utc))
    groups = []
    current = []
    for row in rows:
        split = False
        if current:
            previous = current[-1]
            previous_id = str(previous.get("position_trade_id") or "")
            current_id = str(row.get("position_trade_id") or "")
            both_generated = "-INCOME-" in previous_id and "-INCOME-" in current_id
            if previous_id and current_id and previous_id != current_id and not both_generated:
                split = True

            previous_entry = _parse_db_time(previous.get("entry_time"))
            current_entry = _parse_db_time(row.get("entry_time"))
            if previous_entry and current_entry:
                if abs((current_entry - previous_entry).total_seconds()) > 1:
                    split = True
            elif bool(previous_entry) != bool(current_entry):
                split = True

            previous_exit = _parse_db_time(previous.get("exit_time"))
            current_exit = _parse_db_time(row.get("exit_time"))
            symbol = row.get("symbol") or previous.get("symbol")
            if previous_exit and current_exit and has_open_between(symbol, previous_exit, current_exit):
                split = True
            if not previous_entry and not current_entry and previous_exit and current_exit:
                if current_exit - previous_exit > timedelta(hours=6):
                    split = True

        if split and current:
            groups.append(current)
            current = []
        current.append(row)
    if current:
        groups.append(current)
    return groups


def _income_row_matches_local_trades(income_row, local_trades, max_seconds=900):
    income = dict(income_row)
    income_exit = _parse_db_time(income.get("exit_time"))
    for local_row in local_trades:
        local = dict(local_row)
        local_exit = _parse_db_time(local.get("exit_time"))
        if not income_exit or not local_exit:
            continue
        if abs((income_exit - local_exit).total_seconds()) > max_seconds:
            continue
        income_side = str(income.get("side") or "").upper()
        local_side = str(local.get("side") or "").upper()
        if income_side and local_side and income_side != local_side:
            continue
        try:
            income_entry = float(income.get("entry_price") or 0)
            local_entry = float(local.get("entry_price") or 0)
        except Exception:
            income_entry = local_entry = 0
        if income_entry and local_entry:
            tolerance = max(1e-10, abs(local_entry) * 1e-6)
            if abs(income_entry - local_entry) > tolerance:
                continue
        return True
    return False


def _is_position_open_order(row):
    item = dict(row)
    if str(item.get("order_type") or "").upper() != "MARKET":
        return False
    reason = str(item.get("reason") or "").lower()
    return "roll" not in reason and "reduce" not in reason and "close" not in reason


def _income_transaction_id(item):
    for field in ("tranId", "tran_id", "incomeId", "income_id"):
        value = item.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _income_id_from_payload(item):
    transaction_id = _income_transaction_id(item)
    if transaction_id:
        return f"transaction:{transaction_id}"
    identity = {
        "income_type": str(
            item.get("incomeType") or item.get("income_type") or "UNKNOWN"
        ).strip().upper(),
        "symbol": str(item.get("symbol") or "").strip().upper(),
        "trade_id": str(item.get("tradeId") or item.get("trade_id") or "").strip(),
        "order_id": str(item.get("orderId") or item.get("order_id") or "").strip(),
        "income_time": str(item.get("time") or item.get("income_time") or "").strip(),
        "asset": str(item.get("asset") or "USDT").strip().upper(),
        "position_side": str(
            item.get("positionSide") or item.get("position_side") or ""
        ).strip().upper(),
    }
    stable_uuid = uuid.uuid5(
        uuid.NAMESPACE_URL,
        json.dumps(identity, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
    )
    return f"fallback:{stable_uuid.hex}"


def upsert_exchange_income(item, source="binance_income"):
    conn = get_conn()
    try:
        income_type = str(item.get("incomeType") or item.get("income_type") or "").strip() or "UNKNOWN"
        symbol = str(item.get("symbol") or "").strip().upper()
        income = float(item.get("income") or 0)
        asset = str(item.get("asset") or "USDT")
        raw_time = item.get("time") or item.get("income_time")
        if isinstance(raw_time, (int, float)):
            income_time = datetime.fromtimestamp(float(raw_time) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        else:
            income_time = str(raw_time) if raw_time else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        raw_trade_id = str(item.get("tradeId") or item.get("trade_id") or "") or None
        trade_id = f"A{current_account_id()}:{raw_trade_id}" if raw_trade_id else None
        order_id = str(item.get("orderId") or item.get("order_id") or "") or None
        position_side = str(item.get("positionSide") or item.get("position_side") or "") or None
        account_id = current_account_id()
        income_id = f"A{account_id}:{_income_id_from_payload(item)}"
        if _income_transaction_id(item) is None:
            replay = conn.execute(
                """SELECT income_id
                   FROM exchange_income_ledger
                   WHERE account_id=?
                     AND symbol=?
                     AND income_type=?
                     AND COALESCE(trade_id, '')=COALESCE(?, '')
                     AND COALESCE(order_id, '')=COALESCE(?, '')
                     AND COALESCE(income_time, '')=COALESCE(?, '')
                     AND COALESCE(asset, '')=COALESCE(?, '')
                     AND COALESCE(position_side, '')=COALESCE(?, '')
                   ORDER BY id ASC
                   LIMIT 1""",
                (
                    account_id,
                    symbol,
                    income_type,
                    trade_id,
                    order_id,
                    income_time,
                    asset,
                    position_side,
                ),
            ).fetchone()
            if replay:
                income_id = replay["income_id"]
        conn.execute(
            """INSERT INTO exchange_income_ledger
               (account_id, income_id, symbol, income_type, income, asset, income_time, trade_id,
                order_id, position_side, raw_json, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(income_id) DO UPDATE SET
                 symbol=excluded.symbol,
                 income_type=excluded.income_type,
                 income=excluded.income,
                 asset=excluded.asset,
                 income_time=excluded.income_time,
                 trade_id=excluded.trade_id,
                 order_id=excluded.order_id,
                 position_side=excluded.position_side,
                 raw_json=excluded.raw_json,
                 source=excluded.source""",
            (
                account_id, income_id,
                symbol,
                income_type,
                income,
                asset,
                income_time,
                trade_id,
                order_id,
                position_side,
                json.dumps(item, ensure_ascii=False),
                source,
            ),
        )
        conn.commit()
        return income_id
    finally:
        conn.close()


def backfill_income_ledger_from_fills():
    conn = get_conn()
    try:
        account_id = current_account_id()
        rows = conn.execute(
            """SELECT *
               FROM fills
               WHERE account_id=?
                 AND trade_id IS NOT NULL
                 AND trade_id != ''
                 AND (
                   side='REALIZED_PNL'
                   OR (
                     strategy_source='binance_user_trades'
                     AND ABS(COALESCE(realized_pnl, 0)) > 0.000000000001
                   )
                 )""",
            (account_id,),
        ).fetchall()
        count = 0
        for r in rows:
            raw_trade_id = str(r["trade_id"] or "")
            account_prefix = f"A{account_id}:"
            if raw_trade_id.startswith(account_prefix):
                raw_trade_id = raw_trade_id[len(account_prefix):]
            stored_trade_id = f"A{account_id}:{raw_trade_id}"
            income_id = f"A{account_id}:fill:{raw_trade_id}"
            exists = conn.execute(
                """SELECT 1
                   FROM exchange_income_ledger
                   WHERE account_id=? AND (
                     income_id=? OR (income_type='REALIZED_PNL' AND trade_id IN (?, ?) AND trade_id IS NOT NULL AND trade_id!='')
                   )""",
                (account_id, income_id, raw_trade_id, stored_trade_id),
            ).fetchone()
            if exists:
                continue
            raw = dict(r)
            source = (
                "binance_user_trades_fallback"
                if r["strategy_source"] == "binance_user_trades"
                else "legacy_fills"
            )
            conn.execute(
                """INSERT INTO exchange_income_ledger
                   (account_id, income_id, symbol, income_type, income, asset, income_time,
                    trade_id, position_side, raw_json, source)
                   VALUES (?, ?, ?, 'REALIZED_PNL', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account_id,
                    income_id,
                    r["symbol"],
                    float(r["realized_pnl"] or 0),
                    r["fee_asset"] or "USDT",
                    r["created_at"],
                    stored_trade_id,
                    r["position_side"],
                    json.dumps(raw, ensure_ascii=False),
                    source,
                ),
            )
            count += 1
        conn.commit()
        return count
    finally:
        conn.close()


def rebuild_position_trades_from_income(group_gap_minutes=12, account_pnl=None, unrealized_pnl=0):
    """Rebuild the position-level ledger from exchange income rows.

    Binance income is the factual ledger, but it arrives as many small rows.
    Consecutive realized PnL rows for the same symbol are grouped into a
    position-level record. Fees/funding and any final account reconciliation
    difference are kept as non-win-rate rows.
    """
    backfill_income_ledger_from_fills()
    conn = get_conn()
    try:
        account_id = current_account_id()
        rows = conn.execute(
            """SELECT *
               FROM exchange_income_ledger
               WHERE account_id=?
               ORDER BY income_time ASC, id ASC""",
            (account_id,),
        ).fetchall()
        deduped_rows = []
        seen_income = set()
        for r in rows:
            trade_id = str(r["trade_id"] or "")
            if trade_id:
                prefix = f"A{account_id}:"
                normalized_trade_id = trade_id[len(prefix):] if trade_id.startswith(prefix) else trade_id
                key = (r["income_type"], (r["symbol"] or "").upper(), normalized_trade_id)
            else:
                key = (r["income_type"], (r["symbol"] or "").upper(), r["income_time"], round(float(r["income"] or 0), 8))
            if key in seen_income:
                continue
            seen_income.add(key)
            deduped_rows.append(r)
        rows = deduped_rows
        open_times_by_symbol = {}
        for r in conn.execute(
            """SELECT *
               FROM orders
               WHERE account_id=? AND order_type='MARKET'
               ORDER BY created_at ASC""",
            (account_id,),
        ).fetchall():
            if not _is_position_open_order(r):
                continue
            dt = _parse_db_time(r["created_at"])
            if dt:
                open_times_by_symbol.setdefault((r["symbol"] or "").upper(), []).append(dt)

        def has_open_between(symbol, start_dt, end_dt):
            if not start_dt or not end_dt:
                return False
            lo, hi = (start_dt, end_dt) if start_dt <= end_dt else (end_dt, start_dt)
            for open_dt in open_times_by_symbol.get((symbol or "").upper(), []):
                if lo < open_dt <= hi:
                    return True
            return False

        def order_side_to_position_side(order_side):
            side_text = (order_side or "").upper()
            if side_text == "BUY":
                return "LONG"
            if side_text == "SELL":
                return "SHORT"
            return None

        def latest_open_order_before(symbol, dt):
            if not dt:
                return None
            return conn.execute(
                """SELECT *
                   FROM orders
                   WHERE symbol=?
                     AND order_type='MARKET'
                     AND datetime(created_at) <= datetime(?)
                   ORDER BY datetime(created_at) DESC, id DESC
                   LIMIT 1""",
                (symbol, _format_db_time(dt)),
            ).fetchone()

        def latest_position_snapshot_before(symbol, dt):
            if not dt:
                return None
            return conn.execute(
                """SELECT *
                   FROM positions_history
                   WHERE symbol=?
                     AND datetime(time) <= datetime(?)
                   ORDER BY datetime(time) DESC, id DESC
                   LIMIT 1""",
                (symbol, _format_db_time(dt)),
            ).fetchone()

        def row_time(row, column):
            if row is None:
                return None
            try:
                return _parse_db_time(row[column])
            except Exception:
                return None

        def infer_exit_price(side, entry_price, qty, realized_pnl):
            try:
                entry = float(entry_price or 0)
                amount = float(qty or 0)
                pnl_value = float(realized_pnl or 0)
            except Exception:
                return None
            if entry <= 0 or amount <= 0:
                return None
            delta = pnl_value / amount
            if (side or "").upper() == "SHORT":
                return entry - delta
            return entry + delta

        def fill_metadata_for_group(symbol, trade_ids, first_dt, last_dt):
            """Resolve a complete flat-to-flat position cycle from user trades."""
            if not trade_ids or not last_dt:
                return None
            normalized_ids = {
                str(value).split(":")[-1]
                for value in trade_ids
                if value is not None and str(value).strip()
            }
            fill_rows = conn.execute(
                """SELECT * FROM fills
                   WHERE account_id=? AND symbol=?
                     AND side IN ('BUY', 'SELL')
                     AND COALESCE(quantity, 0) > 0
                     AND COALESCE(price, 0) > 0
                     AND datetime(created_at) <= datetime(?, '+5 minutes')
                     AND datetime(created_at) >= datetime(?, '-7 days')
                   ORDER BY datetime(created_at) ASC, id ASC""",
                (
                    account_id,
                    symbol,
                    _format_db_time(last_dt),
                    _format_db_time(first_dt or last_dt),
                ),
            ).fetchall()
            cycles = []
            current = []
            position = 0.0
            tolerance = 1e-10
            for row in fill_rows:
                qty = float(row["quantity"] or 0)
                signed_qty = qty if str(row["side"] or "").upper() == "BUY" else -qty
                if not current:
                    position = 0.0
                current.append(row)
                position += signed_qty
                if abs(position) <= tolerance:
                    cycles.append(current)
                    current = []
                    position = 0.0
            if current:
                cycles.append(current)

            matching = None
            for cycle in cycles:
                cycle_ids = {str(row["trade_id"] or "").split(":")[-1] for row in cycle}
                if cycle_ids & normalized_ids:
                    matching = cycle
            if not matching:
                return None

            first_side = str(matching[0]["side"] or "").upper()
            side = "LONG" if first_side == "BUY" else "SHORT"
            entry_side = "BUY" if side == "LONG" else "SELL"
            exit_side = "SELL" if side == "LONG" else "BUY"
            entries = [row for row in matching if str(row["side"] or "").upper() == entry_side]
            exits = [row for row in matching if str(row["side"] or "").upper() == exit_side]
            entry_qty = sum(float(row["quantity"] or 0) for row in entries)
            exit_qty = sum(float(row["quantity"] or 0) for row in exits)
            if entry_qty <= 0 or exit_qty <= 0:
                return None
            entry_notional = sum(float(row["quantity"] or 0) * float(row["price"] or 0) for row in entries)
            exit_notional = sum(float(row["quantity"] or 0) * float(row["price"] or 0) for row in exits)
            return {
                "side": side,
                "quantity": min(entry_qty, exit_qty),
                "entry_price": entry_notional / entry_qty,
                "exit_price": exit_notional / exit_qty,
                "entry_time": entries[0]["created_at"],
                "exit_time": exits[-1]["created_at"],
                "trade_ids": {
                    str(row["trade_id"] or "")
                    for row in matching
                    if row["trade_id"]
                },
            }

        def consolidate_by_local_position_id():
            position_rows = conn.execute(
                """SELECT
                       position_id,
                       symbol,
                       MAX(side) AS side,
                       MIN(entry_time) AS entry_time,
                       MIN(exit_time) AS first_exit_time,
                       MAX(exit_time) AS last_exit_time,
                       SUM(COALESCE(quantity, 0)) AS quantity,
                       SUM(COALESCE(quantity, 0) * COALESCE(entry_price, 0))
                         / NULLIF(SUM(COALESCE(quantity, 0)), 0) AS entry_price,
                       SUM(COALESCE(quantity, 0) * COALESCE(exit_price, 0))
                         / NULLIF(SUM(COALESCE(quantity, 0)), 0) AS exit_price,
                       GROUP_CONCAT(DISTINCT exit_reason) AS exit_reason,
                       MAX(entry_reason) AS entry_reason,
                       MAX(strategy_source) AS strategy_source,
                       MAX(signal_source) AS signal_source,
                       MAX(alpha_symbol) AS alpha_symbol,
                       MAX(alpha_profile) AS alpha_profile,
                       MAX(grade_at_entry) AS grade_at_entry,
                       MAX(score_at_entry) AS score_at_entry
                   FROM trades
                   WHERE position_id IS NOT NULL
                     AND position_id != ''
                     AND exit_time IS NOT NULL
                     AND exit_time != 'N/A'
                   GROUP BY position_id, symbol
                   HAVING COUNT(*) > 1"""
            ).fetchall()
            for pos in position_rows:
                local_trades = conn.execute(
                    """SELECT side, entry_price, exit_time
                       FROM trades
                       WHERE position_id=?
                         AND symbol=?
                         AND exit_time IS NOT NULL
                         AND exit_time != 'N/A'
                       ORDER BY datetime(exit_time) ASC, id ASC""",
                    (pos["position_id"], pos["symbol"]),
                ).fetchall()
                first_exit = _parse_db_time(pos["first_exit_time"])
                last_exit = _parse_db_time(pos["last_exit_time"])
                if not first_exit or not last_exit:
                    continue
                income_rows = conn.execute(
                    """SELECT *
                       FROM position_trades
                       WHERE symbol=?
                         AND source='exchange_income'
                         AND datetime(exit_time) >= datetime(?, '-15 minutes')
                         AND datetime(exit_time) <= datetime(?, '+15 minutes')
                       ORDER BY datetime(exit_time) ASC, id ASC""",
                    (pos["symbol"], _format_db_time(first_exit), _format_db_time(last_exit)),
                ).fetchall()
                income_rows = [
                    row
                    for row in income_rows
                    if _income_row_matches_local_trades(row, local_trades)
                ]
                if len(income_rows) <= 1:
                    continue

                raw_rows = []
                realized_pnl = commission = funding_fee = adjustment = net_pnl = 0.0
                income_count = 0
                for row in income_rows:
                    realized_pnl += float(row["realized_pnl"] or 0)
                    commission += float(row["commission"] or 0)
                    funding_fee += float(row["funding_fee"] or 0)
                    adjustment += float(row["adjustment"] or 0)
                    net_pnl += float(row["net_pnl"] or 0)
                    income_count += int(row["income_count"] or 0)
                    try:
                        raw = json.loads(row["raw_json"] or "[]")
                        raw_rows.extend(raw if isinstance(raw, list) else [raw])
                    except Exception:
                        pass

                quantity = pos["quantity"]
                entry_price = pos["entry_price"]
                exit_price = pos["exit_price"]
                leverage_row = conn.execute(
                    """SELECT leverage
                       FROM positions_history
                       WHERE symbol=?
                         AND datetime(time) <= datetime(?)
                       ORDER BY datetime(time) DESC, id DESC
                       LIMIT 1""",
                    (pos["symbol"], _format_db_time(last_exit)),
                ).fetchone()
                leverage = leverage_row["leverage"] if leverage_row and "leverage" in leverage_row.keys() else 3
                notional = float(entry_price or 0) * float(quantity or 0)
                margin = notional / max(float(leverage or 3), 1) if notional else 0
                pnl_pct = (net_pnl / margin * 100) if margin else None
                conn.executemany(
                    "DELETE FROM position_trades WHERE id=?",
                    [(row["id"],) for row in income_rows],
                )
                conn.execute(
                    """INSERT OR REPLACE INTO position_trades
                       (account_id, position_trade_id, symbol, side, strategy_source, signal_source,
                       alpha_symbol, entry_time, exit_time, entry_price, exit_price,
                       quantity, realized_pnl, commission, funding_fee, adjustment,
                       net_pnl, pnl_pct, income_count,
                       entry_reason, exit_reason, grade_at_entry, score_at_entry,
                       source, reconcile_status, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        account_id,
                        f"A{account_id}:{pos['position_id']}",
                        pos["symbol"],
                        pos["side"],
                        pos["strategy_source"] or "unknown",
                        pos["signal_source"],
                        pos["alpha_symbol"],
                        pos["entry_time"],
                        pos["last_exit_time"],
                        entry_price,
                        exit_price,
                        quantity,
                        realized_pnl,
                        commission,
                        funding_fee,
                        adjustment,
                        net_pnl,
                        pnl_pct,
                        income_count,
                        pos["entry_reason"],
                        pos["exit_reason"],
                        pos["grade_at_entry"],
                        pos["score_at_entry"],
                        "exchange_income",
                        "ok",
                        json.dumps(raw_rows, ensure_ascii=False),
                    ),
                )

        def consolidate_by_entry_key():
            rows = conn.execute(
                """SELECT *
                   FROM position_trades
                   WHERE symbol != 'ACCOUNT'
                     AND source='exchange_income'
                     AND side IS NOT NULL
                     AND entry_price IS NOT NULL
                   ORDER BY datetime(exit_time) ASC, id ASC"""
            ).fetchall()
            grouped = {}
            for row in rows:
                try:
                    entry_key = round(float(row["entry_price"]), 10)
                except Exception:
                    continue
                key = ((row["symbol"] or "").upper(), (row["side"] or "").upper(), entry_key)
                grouped.setdefault(key, []).append(row)

            safe_grouped = {}
            for key, candidate_rows in grouped.items():
                for partition_index, partition in enumerate(
                    _partition_entry_key_rows(candidate_rows, has_open_between)
                ):
                    safe_grouped[(*key, partition_index)] = partition

            for (symbol, side, entry_key, _partition_index), items in safe_grouped.items():
                if len(items) <= 1:
                    continue
                raw_rows = []
                realized_pnl = commission = funding_fee = adjustment = net_pnl = 0.0
                income_count = 0
                quantities = []
                exit_weight_sum = 0.0
                exit_notional_sum = 0.0
                reasons = []
                for row in items:
                    realized_pnl += float(row["realized_pnl"] or 0)
                    commission += float(row["commission"] or 0)
                    funding_fee += float(row["funding_fee"] or 0)
                    adjustment += float(row["adjustment"] or 0)
                    net_pnl += float(row["net_pnl"] or 0)
                    income_count += int(row["income_count"] or 0)
                    qty = float(row["quantity"] or 0)
                    if qty > 0:
                        quantities.append(qty)
                    if qty > 0 and row["exit_price"] is not None:
                        exit_weight_sum += qty
                        exit_notional_sum += qty * float(row["exit_price"] or 0)
                    if row["exit_reason"]:
                        reasons.extend([x for x in str(row["exit_reason"]).split(",") if x])
                    try:
                        raw = json.loads(row["raw_json"] or "[]")
                        raw_rows.extend(raw if isinstance(raw, list) else [raw])
                    except Exception:
                        pass

                distinct_quantities = {round(q, 10) for q in quantities}
                if len(distinct_quantities) == 1:
                    quantity = max(quantities) if quantities else None
                else:
                    quantity = sum(quantities) if quantities else None
                entry_price = float(items[0]["entry_price"])
                exit_price = (exit_notional_sum / exit_weight_sum) if exit_weight_sum else None
                entry_time_rows = [(row["entry_time"], _parse_db_time(row["entry_time"])) for row in items if row["entry_time"]]
                exit_time_rows = [(row["exit_time"], _parse_db_time(row["exit_time"])) for row in items if row["exit_time"]]
                entry_time = (
                    min(entry_time_rows, key=lambda x: x[1] or datetime.max.replace(tzinfo=timezone.utc))[0]
                    if entry_time_rows
                    else None
                )
                exit_time = (
                    max(exit_time_rows, key=lambda x: x[1] or datetime.min.replace(tzinfo=timezone.utc))[0]
                    if exit_time_rows
                    else None
                )
                leverage_row = conn.execute(
                    """SELECT leverage
                       FROM positions_history
                       WHERE symbol=?
                         AND datetime(time) <= datetime(?)
                       ORDER BY datetime(time) DESC, id DESC
                       LIMIT 1""",
                    (symbol, exit_time),
                ).fetchone()
                leverage = leverage_row["leverage"] if leverage_row and "leverage" in leverage_row.keys() else 3
                notional = float(entry_price or 0) * float(quantity or 0)
                margin = notional / max(float(leverage or 3), 1) if notional else 0
                pnl_pct = (net_pnl / margin * 100) if margin else None
                preferred = next((row for row in items if not str(row["position_trade_id"] or "").startswith(f"{symbol}-INCOME-")), items[-1])
                position_trade_id = preferred["position_trade_id"] or f"{symbol}-{side}-{entry_key}-MERGED"

                conn.executemany(
                    "DELETE FROM position_trades WHERE id=?",
                    [(row["id"],) for row in items],
                )
                conn.execute(
                    """INSERT OR REPLACE INTO position_trades
                       (account_id, position_trade_id, symbol, side, strategy_source, signal_source,
                       alpha_symbol, entry_time, exit_time, entry_price, exit_price,
                       quantity, realized_pnl, commission, funding_fee, adjustment,
                       net_pnl, pnl_pct, income_count,
                       entry_reason, exit_reason, grade_at_entry, score_at_entry,
                       source, reconcile_status, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        account_id,
                        f"A{account_id}:{position_trade_id}" if not str(position_trade_id).startswith(f"A{account_id}:") else position_trade_id,
                        symbol,
                        side,
                        preferred["strategy_source"] or "unknown",
                        preferred["signal_source"],
                        preferred["alpha_symbol"],
                        entry_time,
                        exit_time,
                        entry_price,
                        exit_price,
                        quantity,
                        realized_pnl,
                        commission,
                        funding_fee,
                        adjustment,
                        net_pnl,
                        pnl_pct,
                        income_count,
                        preferred["entry_reason"],
                        ",".join(dict.fromkeys(reasons)) if reasons else preferred["exit_reason"],
                        preferred["grade_at_entry"] if "grade_at_entry" in preferred.keys() else None,
                        preferred["score_at_entry"] if "score_at_entry" in preferred.keys() else None,
                        "exchange_income",
                        "ok",
                        json.dumps(raw_rows, ensure_ascii=False),
                    ),
                )

        conn.execute("DELETE FROM position_trades WHERE account_id=?", (account_id,))
        groups = []
        real_by_symbol = {}
        side_income_rows = []
        gap = timedelta(minutes=group_gap_minutes)

        for r in rows:
            income_type = r["income_type"]
            symbol = (r["symbol"] or "ACCOUNT").upper()
            income = float(r["income"] or 0)
            dt = _parse_db_time(r["income_time"])
            if income_type != "REALIZED_PNL" or not symbol or symbol == "ACCOUNT":
                side_income_rows.append((dt, dict(r), income))
                continue
            raw = dict(r)
            real_by_symbol.setdefault(symbol, []).append((dt, raw, income))

        for symbol, items in real_by_symbol.items():
            current = None
            for dt, raw, income in sorted(items, key=lambda x: ((x[0] or datetime.min.replace(tzinfo=timezone.utc)), x[1].get("id") or 0)):
                if (
                    current
                    and dt
                    and current["last_dt"]
                    and dt - current["last_dt"] <= gap
                    and not has_open_between(symbol, current["last_dt"], dt)
                ):
                    current["rows"].append(raw)
                    current["pnl"] += income
                    current["last_dt"] = dt
                    if raw.get("trade_id"):
                        current["trade_ids"].add(str(raw.get("trade_id")))
                else:
                    if current:
                        groups.append(current)
                    current = {
                        "symbol": symbol,
                        "first_dt": dt,
                        "last_dt": dt,
                        "pnl": income,
                        "commission": 0.0,
                        "funding_fee": 0.0,
                        "adjustment": 0.0,
                        "trade_ids": {str(raw.get("trade_id"))} if raw.get("trade_id") else set(),
                        "rows": [raw],
                    }
            if (
                current
                and current["first_dt"] is None
                and current["last_dt"] is None
            ):
                current["first_dt"] = current["last_dt"] = datetime.now(timezone.utc)
            if current:
                groups.append(current)

        # The Income API may expose only the closing trade id. Expand each
        # realized-PnL group with its complete flat-to-flat user-trade cycle so
        # opening commissions and the factual entry time/price stay attached.
        for group in groups:
            fill_meta = fill_metadata_for_group(
                group["symbol"],
                group.get("trade_ids") or set(),
                group.get("first_dt"),
                group.get("last_dt"),
            )
            if fill_meta:
                group["fill_meta"] = fill_meta
                group["trade_ids"].update(fill_meta.get("trade_ids") or set())

        unmatched_side_income = 0.0
        side_attach_gap = timedelta(minutes=max(group_gap_minutes * 2, 30))
        for dt, raw, income in side_income_rows:
            income_type = raw.get("income_type")
            symbol = (raw.get("symbol") or "ACCOUNT").upper()
            trade_id = str(raw.get("trade_id") or "")
            best = None
            best_distance = None
            for g in groups:
                if g["symbol"] != symbol:
                    continue
                if trade_id and trade_id in g.get("trade_ids", set()):
                    best = g
                    best_distance = 0
                    break
                if not dt or not g.get("first_dt") or not g.get("last_dt"):
                    continue
                if dt > g["last_dt"] and has_open_between(symbol, g["last_dt"], dt):
                    continue
                if dt < g["first_dt"] and has_open_between(symbol, dt, g["first_dt"]):
                    continue
                if g["first_dt"] <= dt <= g["last_dt"]:
                    distance = 0
                else:
                    distance = min(abs((dt - g["first_dt"]).total_seconds()), abs((dt - g["last_dt"]).total_seconds()))
                if distance <= side_attach_gap.total_seconds() and (best_distance is None or distance < best_distance):
                    best = g
                    best_distance = distance
            if best is None:
                unmatched_side_income += income
                continue
            if income_type == "COMMISSION":
                best["commission"] += income
            elif income_type == "FUNDING_FEE":
                best["funding_fee"] += income
            else:
                best["adjustment"] += income
            best["rows"].append(raw)
            if dt:
                best["first_dt"] = min(best["first_dt"], dt) if best["first_dt"] else dt
                best["last_dt"] = max(best["last_dt"], dt) if best["last_dt"] else dt

        for i, g in enumerate(groups, start=1):
            symbol = g["symbol"]
            first_dt = g["first_dt"]
            last_dt = g["last_dt"]
            pid = f"A{account_id}:{symbol}-INCOME-{_format_db_time(first_dt) or i}-{i}".replace(" ", "T")
            meta = conn.execute(
                """SELECT *
                   FROM trades
                   WHERE symbol=?
                     AND exit_time IS NOT NULL
                     AND exit_time != 'N/A'
                     AND datetime(exit_time) <= datetime(?, '+5 minutes')
                     AND ABS(strftime('%s', exit_time) - strftime('%s', ?)) <= 3600
                   ORDER BY ABS(strftime('%s', exit_time) - strftime('%s', ?))
                   LIMIT 1""",
                (symbol, _format_db_time(last_dt), _format_db_time(last_dt), _format_db_time(last_dt)),
            ).fetchone()
            side = meta["side"] if meta and "side" in meta.keys() else None
            strategy_source = meta["strategy_source"] if meta and "strategy_source" in meta.keys() else "unknown"
            signal_source = meta["signal_source"] if meta and "signal_source" in meta.keys() else None
            alpha_symbol = meta["alpha_symbol"] if meta and "alpha_symbol" in meta.keys() else None
            entry_price = meta["entry_price"] if meta and "entry_price" in meta.keys() else None
            exit_price = meta["exit_price"] if meta and "exit_price" in meta.keys() else None
            qty = meta["quantity"] if meta and "quantity" in meta.keys() else None
            entry_reason = meta["entry_reason"] if meta and "entry_reason" in meta.keys() else None
            exit_reason = meta["exit_reason"] if meta and "exit_reason" in meta.keys() else "REALIZED_PNL"
            entry_time = meta["entry_time"] if meta and "entry_time" in meta.keys() else None
            fill_meta = g.get("fill_meta") or fill_metadata_for_group(
                symbol,
                g.get("trade_ids") or set(),
                first_dt,
                last_dt,
            )
            if fill_meta:
                side = fill_meta["side"]
                entry_price = fill_meta["entry_price"]
                exit_price = fill_meta["exit_price"]
                qty = fill_meta["quantity"]
                entry_time = fill_meta["entry_time"]
                last_dt = _parse_db_time(fill_meta["exit_time"]) or last_dt
            open_order = latest_open_order_before(symbol, first_dt or last_dt)
            snapshot = latest_position_snapshot_before(symbol, last_dt or first_dt)
            open_dt = row_time(open_order, "created_at")
            snapshot_dt = row_time(snapshot, "time")
            prefer_snapshot = snapshot_dt and (not open_dt or snapshot_dt >= open_dt)
            if prefer_snapshot and snapshot:
                side = side or snapshot["side"]
                entry_price = entry_price or snapshot["entry_price"]
                qty = qty or snapshot["quantity"]
                entry_time = entry_time or snapshot["time"]
            if open_order and not prefer_snapshot:
                side = side or order_side_to_position_side(open_order["side"])
                strategy_source = strategy_source if strategy_source != "unknown" else (open_order["strategy_source"] or "unknown")
                signal_source = signal_source or open_order["signal_source"]
                alpha_symbol = alpha_symbol or open_order["alpha_symbol"]
                entry_price = entry_price or open_order["price"]
                qty = qty or open_order["quantity"]
                entry_reason = entry_reason or open_order["reason"]
                entry_time = entry_time or open_order["created_at"]
            if snapshot:
                side = side or snapshot["side"]
                entry_price = entry_price or snapshot["entry_price"]
                qty = qty or snapshot["quantity"]
                entry_time = entry_time or snapshot["time"]
            entry_time = entry_time or _format_db_time(first_dt)
            if not exit_price:
                exit_price = infer_exit_price(side, entry_price, qty, g["pnl"])
            notional = float(entry_price or 0) * float(qty or 0)
            leverage = snapshot["leverage"] if snapshot and "leverage" in snapshot.keys() else 3
            margin = notional / max(float(leverage or 3), 1) if notional else 0
            net_pnl = g["pnl"] + g.get("commission", 0.0) + g.get("funding_fee", 0.0) + g.get("adjustment", 0.0)
            pnl_pct = (net_pnl / margin * 100) if margin else None
            conn.execute(
                """INSERT OR REPLACE INTO position_trades
                   (account_id, position_trade_id, symbol, side, strategy_source, signal_source,
                    alpha_symbol, entry_time, exit_time, entry_price, exit_price,
                    quantity, realized_pnl, commission, funding_fee, adjustment,
                    net_pnl, pnl_pct, income_count,
                    entry_reason, exit_reason, source, reconcile_status, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account_id,
                    pid,
                    symbol,
                    side,
                    strategy_source,
                    signal_source,
                    alpha_symbol,
                    entry_time,
                    _format_db_time(last_dt),
                    entry_price,
                    exit_price,
                    qty,
                    g["pnl"],
                    g.get("commission", 0.0),
                    g.get("funding_fee", 0.0),
                    g.get("adjustment", 0.0),
                    net_pnl,
                    pnl_pct,
                    len(g["rows"]),
                    entry_reason,
                    exit_reason,
                    "exchange_income",
                    "ok",
                    json.dumps(g["rows"], ensure_ascii=False),
                ),
            )

        consolidate_by_local_position_id()
        consolidate_by_entry_key()

        if abs(unmatched_side_income) >= 0.00000001:
            now_text = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                """INSERT OR REPLACE INTO position_trades
                   (account_id, position_trade_id, symbol, side, entry_time, exit_time,
                    adjustment, net_pnl, income_count, exit_reason, source,
                    reconcile_status, raw_json)
                   VALUES (?, ?, 'ACCOUNT', NULL, ?, ?, ?, ?, 1,
                           'UNMATCHED_EXCHANGE_INCOME', 'exchange_income',
                           'unmatched', ?)""",
                (
                    account_id,
                    f"A{account_id}:ACCOUNT-UNMATCHED-INCOME",
                    now_text,
                    now_text,
                    unmatched_side_income,
                    unmatched_side_income,
                    json.dumps({"unmatched_side_income": unmatched_side_income}, ensure_ascii=False),
                ),
            )

        if account_pnl is not None:
            net = conn.execute(
                "SELECT COALESCE(SUM(net_pnl),0) FROM position_trades WHERE account_id=?",
                (account_id,),
            ).fetchone()[0] or 0
            diff = float(account_pnl or 0) - float(unrealized_pnl or 0) - float(net or 0)
            if abs(diff) >= 0.01:
                now_text = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """INSERT OR REPLACE INTO position_trades
                       (account_id, position_trade_id, symbol, side, entry_time, exit_time,
                        adjustment, net_pnl, income_count, exit_reason, source,
                        reconcile_status, raw_json)
                       VALUES (?, ?, 'ACCOUNT', NULL, ?, ?, ?, ?, 1,
                               'ACCOUNT_RECONCILE_DIFF', 'reconcile_adjustment',
                               'unmatched', ?)""",
                    (
                        account_id,
                        f"A{account_id}:ACCOUNT-RECONCILE-DIFF",
                        now_text,
                        now_text,
                        diff,
                        diff,
                        json.dumps(
                            {
                                "account_pnl": account_pnl,
                                "unrealized_pnl": unrealized_pnl,
                                "position_net_before_adjustment": net,
                                "diff": diff,
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )

        conn.commit()
        return conn.execute(
            "SELECT COUNT(*) FROM position_trades WHERE account_id=?", (account_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _fetch_history_rows(table, time_column, account_id, symbol=None, from_time=None, to_time=None):
    conditions = ["account_id=?"]
    params = [int(account_id)]
    if symbol:
        conditions.append("symbol=?")
        params.append(str(symbol).upper())
    if from_time:
        conditions.append(f"{time_column}>=?")
        params.append(str(from_time))
    if to_time:
        conditions.append(f"{time_column}<=?")
        params.append(str(to_time))
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} "
            f"ORDER BY {time_column} ASC, id ASC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _trade_history_watermarks(conn, account_id):
    row = conn.execute(
        """SELECT
               COALESCE((SELECT MAX(id) FROM fills WHERE account_id=?), 0) AS fills,
               COALESCE((SELECT MAX(id) FROM exchange_income_ledger
                         WHERE account_id=?), 0) AS income,
               COALESCE((SELECT MAX(id) FROM position_trades
                         WHERE account_id=?), 0) AS position_trades,
               COALESCE((SELECT MAX(id) FROM orders WHERE account_id=?), 0) AS orders""",
        (int(account_id),) * 4,
    ).fetchone()
    return {name: int(row[name] or 0) for name in row.keys()}


def fetch_trade_history_snapshot(account_id, *, symbol=None, watermarks=None):
    account_id = int(account_id)
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        if watermarks is None:
            watermarks = _trade_history_watermarks(conn, account_id)

        def fetch(table, time_column, watermark_name):
            conditions = ["account_id=?", "id<=?"]
            params = [account_id, int(watermarks[watermark_name])]
            if symbol:
                conditions.append("symbol=?")
                params.append(str(symbol).upper())
            rows = conn.execute(
                f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} "
                f"ORDER BY {time_column} ASC, id ASC",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

        return {
            "watermarks": dict(watermarks),
            "fills": fetch("fills", "created_at", "fills"),
            "income": fetch(
                "exchange_income_ledger", "income_time", "income"
            ),
            "position_trades": fetch(
                "position_trades", "exit_time", "position_trades"
            ),
            "orders": fetch("orders", "created_at", "orders"),
        }
    finally:
        conn.close()


def save_trade_history_page_snapshot(
    account_id, query_hash, payload, *, ttl_seconds=1800, max_per_account=32
):
    snapshot_id = uuid.uuid4().hex
    cursor_secret = uuid.uuid4().hex + uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    expires_at = _format_db_time(now + timedelta(seconds=ttl_seconds))
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM trade_history_page_snapshots WHERE expires_at < ?",
            (_format_db_time(now),),
        )
        conn.execute(
            """INSERT INTO trade_history_page_snapshots
               (snapshot_id, account_id, query_hash, payload_json,
                cursor_secret, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                int(account_id),
                str(query_hash),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                cursor_secret,
                expires_at,
            ),
        )
        conn.execute(
            """DELETE FROM trade_history_page_snapshots
               WHERE snapshot_id IN (
                   SELECT snapshot_id
                   FROM trade_history_page_snapshots
                   WHERE account_id=?
                   ORDER BY datetime(created_at) DESC, rowid DESC
                   LIMIT -1 OFFSET ?
               )""",
            (int(account_id), max(1, int(max_per_account))),
        )
        conn.commit()
        return {"snapshot_id": snapshot_id, "cursor_secret": cursor_secret}
    finally:
        conn.close()


def fetch_trade_history_page_snapshot(account_id, snapshot_id, query_hash):
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT payload_json, cursor_secret
               FROM trade_history_page_snapshots
               WHERE snapshot_id=? AND account_id=? AND query_hash=?
                 AND expires_at >= ?""",
            (
                str(snapshot_id),
                int(account_id),
                str(query_hash),
                _format_db_time(datetime.now(timezone.utc)),
            ),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return {"payload": payload, "cursor_secret": row["cursor_secret"]}
    finally:
        conn.close()


def fetch_trade_history_fills(account_id, *, symbol=None, from_time=None, to_time=None):
    return _fetch_history_rows(
        "fills", "created_at", account_id, symbol, from_time, to_time
    )


def fetch_trade_history_income(account_id, *, symbol=None, from_time=None, to_time=None):
    return _fetch_history_rows(
        "exchange_income_ledger", "income_time", account_id, symbol, from_time, to_time
    )


def fetch_trade_history_position_trades(
    account_id, *, symbol=None, from_time=None, to_time=None
):
    return _fetch_history_rows(
        "position_trades", "exit_time", account_id, symbol, from_time, to_time
    )


def fetch_position_trade_groups(limit=100, account_id=None):
    account_id = int(account_id or current_account_id())
    conn = get_conn()
    has_position_trades = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='position_trades'"
    ).fetchone()[0]
    if has_position_trades:
        rows = conn.execute(
            """SELECT
                   position_trade_id AS position_id,
                   symbol,
                   side,
                   entry_time,
                   exit_time,
                   quantity,
                   entry_price,
                   exit_price,
                   net_pnl AS pnl,
                   pnl_pct,
                   exit_reason,
                   income_count AS close_count,
                   entry_reason,
                   source,
                   strategy_source,
                   signal_source,
                   alpha_symbol,
                   alpha_symbol AS alpha_profile,
                   NULL AS alpha_entry_level,
                   NULL AS alpha_score,
                   NULL AS alpha_suggested_position_pct,
                   commission,
                   funding_fee,
                   adjustment,
                   reconcile_status,
                   grade_at_entry,
                   score_at_entry
               FROM position_trades
               WHERE account_id=?
                 AND symbol != 'ACCOUNT'
               ORDER BY COALESCE(exit_time, updated_at, created_at) DESC""",
            (account_id,),
        ).fetchall()
        grouped = {}
        for r in rows:
            d = dict(r)
            entry_price = d.get("entry_price")
            try:
                entry_key = round(float(entry_price), 10)
            except Exception:
                entry_key = entry_price
            key = (
                str(d.get("symbol") or "").upper(),
                str(d.get("side") or "").upper(),
                entry_key,
                str(d.get("entry_time") or ""),
            )
            grouped.setdefault(key, []).append(d)
        result = []
        for items in grouped.values():
            items.sort(key=lambda row: _parse_db_time(row.get("exit_time")) or datetime.min.replace(tzinfo=timezone.utc))
            first = items[0]
            quantity = sum(float(row.get("quantity") or 0) for row in items)
            entry_weight = sum(float(row.get("quantity") or 0) for row in items if row.get("entry_price") is not None)
            exit_weight = sum(float(row.get("quantity") or 0) for row in items if row.get("exit_price") is not None)
            entry_notional = sum(float(row.get("quantity") or 0) * float(row.get("entry_price") or 0) for row in items)
            exit_notional = sum(float(row.get("quantity") or 0) * float(row.get("exit_price") or 0) for row in items)
            net_pnl = sum(float(row.get("pnl") or 0) for row in items)
            commission = sum(float(row.get("commission") or 0) for row in items)
            funding_fee = sum(float(row.get("funding_fee") or 0) for row in items)
            adjustment = sum(float(row.get("adjustment") or 0) for row in items)
            income_count = sum(int(row.get("close_count") or 0) for row in items) or len(items)
            exit_time = max(
                (row.get("exit_time") for row in items if row.get("exit_time")),
                default=first.get("exit_time"),
            )
            leverage_row = conn.execute(
                """SELECT leverage
                   FROM positions_history
                   WHERE account_id=? AND symbol=?
                     AND datetime(time) <= datetime(?)
                   ORDER BY datetime(time) DESC, id DESC
                   LIMIT 1""",
                (account_id, first.get("symbol"), exit_time),
            ).fetchone()
            leverage = leverage_row["leverage"] if leverage_row and "leverage" in leverage_row.keys() else 3
            margin = entry_notional / max(float(leverage or 3), 1) if entry_notional else 0
            pnl_pct = (net_pnl / margin * 100) if margin else None
            score_values = [row.get("score_at_entry") for row in items if row.get("score_at_entry") is not None]
            grade_values = [row.get("grade_at_entry") for row in items if row.get("grade_at_entry")]
            if not score_values and first.get("symbol"):
                fallback_score = conn.execute(
                    """SELECT MAX(grade_at_entry) AS grade_at_entry,
                              MAX(score_at_entry) AS score_at_entry
                       FROM trades
                       WHERE account_id=?
                         AND symbol=?
                         AND (side=? OR position_side=?)
                         AND score_at_entry IS NOT NULL
                         AND (
                           entry_time=?
                           OR ABS(COALESCE(entry_price, 0) - COALESCE(?, 0)) <= MAX(0.00000001, ABS(COALESCE(?, 0)) * 0.000001)
                         )""",
                    (
                        account_id,
                        first.get("symbol"),
                        first.get("side"),
                        first.get("side"),
                        first.get("entry_time"),
                        first.get("entry_price"),
                        first.get("entry_price"),
                    ),
                ).fetchone()
                if fallback_score and fallback_score["score_at_entry"] is not None:
                    score_values.append(fallback_score["score_at_entry"])
                    if fallback_score["grade_at_entry"]:
                        grade_values.append(fallback_score["grade_at_entry"])
            d = {
                **first,
                "position_id": first.get("position_id") or f"{first.get('symbol')}-{first.get('side')}-{first.get('entry_time')}",
                "entry_time": min((row.get("entry_time") for row in items if row.get("entry_time")), default=first.get("entry_time")),
                "exit_time": exit_time,
                "quantity": quantity,
                "qty": round(quantity, 6) if quantity else None,
                "entry_price": round(entry_notional / entry_weight, 8) if entry_weight else None,
                "exit_price": round(exit_notional / exit_weight, 8) if exit_weight else None,
                "pnl": round(net_pnl, 2),
                "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
                "commission": round(commission, 8),
                "funding_fee": round(funding_fee, 8),
                "adjustment": round(adjustment, 8),
                "close_count": income_count,
                "grade_at_entry": grade_values[0] if grade_values else None,
                "score_at_entry": max(float(v) for v in score_values) if score_values else None,
                "is_grouped": True,
                "is_adjustment": first.get("source") == "reconcile_adjustment",
            }
            result.append(d)
        result.sort(key=lambda row: _parse_db_time(row.get("exit_time")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        conn.close()
        return result[:limit]

    rows = conn.execute(
        """SELECT
               COALESCE(NULLIF(position_id, ''), symbol || '-' || side || '-' || entry_time) AS position_id,
               symbol,
               side,
               MIN(entry_time) AS entry_time,
               MAX(exit_time) AS exit_time,
               SUM(COALESCE(quantity, 0)) AS quantity,
               SUM(COALESCE(quantity, 0) * COALESCE(entry_price, 0)) / NULLIF(SUM(COALESCE(quantity, 0)), 0) AS entry_price,
               SUM(COALESCE(quantity, 0) * COALESCE(exit_price, 0)) / NULLIF(SUM(COALESCE(quantity, 0)), 0) AS exit_price,
               SUM(COALESCE(pnl, 0)) AS pnl,
               SUM(COALESCE(entry_price, 0) * COALESCE(quantity, 0)) AS notional,
               GROUP_CONCAT(DISTINCT exit_reason) AS exit_reasons,
               COUNT(*) AS close_count,
               MAX(grade_at_entry) AS grade_at_entry,
               MAX(score_at_entry) AS score_at_entry,
               MAX(entry_reason) AS entry_reason,
               MAX(source) AS source,
               MAX(strategy_source) AS strategy_source,
               MAX(signal_source) AS signal_source,
               MAX(alpha_symbol) AS alpha_symbol,
               MAX(alpha_profile) AS alpha_profile,
               MAX(alpha_entry_level) AS alpha_entry_level,
               MAX(alpha_score) AS alpha_score,
               MAX(alpha_suggested_position_pct) AS alpha_suggested_position_pct
           FROM trades
           WHERE source='system'
             AND exit_time IS NOT NULL
             AND exit_time != 'N/A'
           GROUP BY COALESCE(NULLIF(position_id, ''), symbol || '-' || side || '-' || entry_time), symbol, side
           ORDER BY MAX(COALESCE(exit_time, created_at)) DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        pnl = float(d.get("pnl") or 0)
        notional = float(d.get("notional") or 0)
        # Assume max leverage for historical grouped records when exact leverage is not stored in trades.
        margin = notional / 3 if notional else 0
        d["pnl"] = round(pnl, 2)
        d["pnl_pct"] = round(pnl / margin * 100, 2) if margin else 0
        d["qty"] = round(float(d.get("quantity") or 0), 6)
        d["entry_price"] = round(float(d.get("entry_price") or 0), 8)
        d["exit_price"] = round(float(d.get("exit_price") or 0), 8)
        d["exit_reason"] = d.get("exit_reasons")
        d["is_grouped"] = True
        result.append(d)
    conn.close()
    return result


def clear_trade_history():
    conn = get_conn()
    for table in ("trades", "fills", "orders"):
        conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
    conn.commit()
    conn.close()


# ---- Symbols ----

def get_symbols():
    conn = get_conn()
    rows = conn.execute("SELECT symbol FROM symbols WHERE is_active = 1").fetchall()
    return [r["symbol"] for r in rows]


def upsert_symbol(symbol):
    conn = get_conn()
    conn.execute(
        """INSERT INTO symbols (symbol) VALUES (?)
           ON CONFLICT(symbol) DO UPDATE SET last_seen = datetime('now')""",
        (symbol,),
    )
    conn.commit()


def fetch_active_symbols():
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT futures_symbol AS symbol FROM market_universe
               WHERE pool_type = 'normal' AND selected = 1 AND data_ready = 1
               ORDER BY universe_rank"""
        ).fetchall()
        return [r["symbol"] for r in rows]
    finally:
        conn.close()


def is_market_entry_ready(symbol, strategy_source="normal", alpha_symbol=None):
    pool_type = "alpha" if str(strategy_source).lower() == "alpha" else "normal"
    conn = get_conn()
    try:
        if pool_type == "alpha":
            row = conn.execute(
                """SELECT data_ready, data_error FROM market_universe
                   WHERE pool_type = 'alpha' AND source_symbol = ?
                     AND futures_symbol = ? AND selected = 1""",
                (alpha_symbol or "", symbol),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT data_ready, data_error FROM market_universe
                   WHERE pool_type = 'normal' AND futures_symbol = ? AND selected = 1""",
                (symbol,),
            ).fetchone()
        if not row:
            return False, "not_in_current_universe"
        if not bool(row["data_ready"]):
            return False, row["data_error"]
        if pool_type == "alpha":
            source_symbol = alpha_symbol or ""
        else:
            source_symbol = symbol

        def candle_state(market_kind, market_symbol):
            legacy = {
                "spot": ("candles_15m", "candles_1h", "symbol", False),
                "futures": (
                    "futures_candles_15m",
                    "futures_candles_1h",
                    "symbol",
                    True,
                ),
                "alpha": (
                    "alpha_candles_15m",
                    "alpha_candles_1h",
                    "alpha_symbol",
                    True,
                ),
            }[market_kind]

            def interval_state(interval, legacy_table):
                latest = conn.execute(
                    """SELECT MAX(time) latest, COUNT(*) count
                       FROM aggregated_candles
                       WHERE market_kind=? AND source_env='mainnet'
                         AND symbol=? AND interval=? AND is_complete=1""",
                    (market_kind, market_symbol, interval),
                ).fetchone()
                env_sql = " AND source_env='mainnet'" if legacy[3] else ""
                count = conn.execute(
                    f"""SELECT COUNT(*) count FROM {legacy_table}
                        WHERE {legacy[2]}=?{env_sql}""",
                    (market_symbol,),
                ).fetchone()
                return latest["latest"], count["count"]

            latest_15m, count_15m = interval_state("15m", legacy[0])
            latest_1h, count_1h = interval_state("1h", legacy[1])
            from shared.market_universe import CandleState

            def parse(value):
                if not value:
                    return None
                parsed = datetime.fromisoformat(
                    str(value).replace("Z", "+00:00")
                )
                return (
                    parsed.replace(tzinfo=timezone.utc)
                    if parsed.tzinfo is None
                    else parsed.astimezone(timezone.utc)
                )

            return CandleState(
                parse(latest_15m),
                parse(latest_1h),
                int(count_15m or 0),
                int(count_1h or 0),
            )

        spot_state = candle_state(
            "alpha" if pool_type == "alpha" else "spot",
            source_symbol,
        )
        futures_state = candle_state("futures", symbol)
        from shared.market_universe import assess_dual_market_readiness

        readiness = assess_dual_market_readiness(
            datetime.now(timezone.utc),
            spot_state,
            futures_state,
        )
        return readiness.ready, readiness.error
    finally:
        conn.close()


def upsert_exchange_fill(item, source="binance_user_trades"):
    """Persist a Binance user trade, replacing income-only placeholder fills."""
    account_id = current_account_id()
    raw_trade_id = str(item.get("id") or item.get("tradeId") or "").strip()
    if not raw_trade_id:
        return None
    trade_id = f"A{account_id}:{raw_trade_id}"
    raw_time = item.get("time") or item.get("created_at")
    if isinstance(raw_time, (int, float)):
        created_at = datetime.fromtimestamp(
            float(raw_time) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
    else:
        created_at = str(raw_time or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO fills
               (account_id, symbol, order_id, exchange_order_id, side, position_side, quantity, price,
                realized_pnl, fee, fee_asset, trade_id, created_at, strategy_source)
               VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(trade_id) DO UPDATE SET
                 account_id=excluded.account_id,
                 symbol=excluded.symbol,
                 exchange_order_id=excluded.exchange_order_id,
                 side=excluded.side,
                 position_side=excluded.position_side,
                 quantity=excluded.quantity,
                 price=excluded.price,
                 realized_pnl=excluded.realized_pnl,
                 fee=excluded.fee,
                 fee_asset=excluded.fee_asset,
                 created_at=excluded.created_at""",
            (
                account_id,
                str(item.get("symbol") or "").upper(),
                str(item.get("orderId") or "") or None,
                str(item.get("side") or "").upper(),
                str(item.get("positionSide") or "BOTH").upper(),
                float(item.get("qty") or item.get("quantity") or 0),
                float(item.get("price") or 0),
                float(item.get("realizedPnl") or item.get("realized_pnl") or 0),
                -abs(float(item.get("commission") or item.get("fee") or 0)),
                str(item.get("commissionAsset") or item.get("fee_asset") or "USDT"),
                trade_id,
                created_at,
                source,
            ),
        )
        conn.commit()
        return trade_id
    finally:
        conn.close()


# ---- Scores ----

def insert_scores(rows):
    conn = get_conn()
    conn.executemany(
        """INSERT OR REPLACE INTO alpha_scores
           (time, symbol, composite_score, composite_summary,
            risk_label, chip_phase, trend_state, trend_direction,
            volatility_level, price_position, relative_strength,
            market_price, raw_features, scan_id,
            entry_alpha, hold_alpha)  -- V3.0
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def fetch_latest_scan():
    conn = get_conn()
    scan = conn.execute(
        "SELECT scan_id, time FROM alpha_scores ORDER BY time DESC LIMIT 1"
    ).fetchone()
    if not scan:
        return None, []

    scan_id = scan["scan_id"]
    rows = conn.execute(
        """SELECT DISTINCT symbol, time, composite_score, composite_summary,
                  risk_label, chip_phase, trend_state, trend_direction,
                  volatility_level, price_position, relative_strength, market_price,
                  raw_features, scan_id, entry_alpha, hold_alpha
           FROM alpha_scores
           WHERE scan_id = ?
           ORDER BY composite_score DESC""",
        (scan_id,),
    ).fetchall()
    return scan, rows


def fetch_latest_scan_meta():
    conn = get_conn()
    return conn.execute(
        "SELECT scan_id, time FROM alpha_scores ORDER BY time DESC LIMIT 1"
    ).fetchone()


def fetch_symbol_detail(symbol):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM alpha_scores WHERE symbol = ? ORDER BY time DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    return row


def fetch_score_history(symbol, limit=100):
    conn = get_conn()
    rows = conn.execute(
        """SELECT time, composite_score, composite_summary, market_price
           FROM alpha_scores
           WHERE symbol = ?
           ORDER BY time DESC LIMIT ?""",
        (symbol, limit),
    ).fetchall()
    return list(reversed(rows))


def insert_alpha_scan_scores(rows):
    conn = get_conn()
    conn.executemany(
        """INSERT OR REPLACE INTO alpha_scan_scores
           (time, scan_id, alpha_symbol, base_asset, futures_symbol, alpha_score,
            discovery_score, momentum_score, liquidity_score, risk_score,
            tradeability_score, grade, decision, market_price, raw_features,
            alpha_profile, entry_level, suggested_position_pct, block_reasons,
            profile_thresholds)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def fetch_latest_alpha_scan():
    conn = get_conn()
    scan = conn.execute(
        "SELECT scan_id, time FROM alpha_scan_scores ORDER BY time DESC LIMIT 1"
    ).fetchone()
    if not scan:
        return None, []
    rows = conn.execute(
        """SELECT s.*, a.alpha_name, a.tradeability, a.status, a.volume_24h,
                  a.liquidity, a.percent_change_24h, a.token_id
           FROM alpha_scan_scores s
           LEFT JOIN alpha_symbols a ON a.alpha_symbol = s.alpha_symbol
           WHERE s.scan_id = ?
             AND a.tradeability = 'alpha_futures_mapped'
             AND s.futures_symbol IS NOT NULL
             AND s.futures_symbol != ''
           ORDER BY s.alpha_score DESC""",
        (scan["scan_id"],),
    ).fetchall()
    return scan, rows


def fetch_alpha_symbol_detail(alpha_symbol):
    conn = get_conn()
    try:
        return conn.execute(
            """SELECT s.*, a.alpha_name, a.tradeability, a.status, a.volume_24h,
                      a.liquidity, a.percent_change_24h, a.token_id, a.raw_json AS symbol_raw_json
               FROM alpha_scan_scores s
               LEFT JOIN alpha_symbols a ON a.alpha_symbol = s.alpha_symbol
               WHERE s.alpha_symbol = ?
               ORDER BY s.time DESC
               LIMIT 1""",
            (alpha_symbol,),
        ).fetchone()
    finally:
        conn.close()


def fetch_alpha_score_history(alpha_symbol, limit=100):
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT time, alpha_score, grade, market_price
               FROM alpha_scan_scores
               WHERE alpha_symbol = ?
               ORDER BY time DESC LIMIT ?""",
            (alpha_symbol, limit),
        ).fetchall()
        return list(reversed(rows))
    finally:
        conn.close()


def fetch_latest_score_for_symbol(symbol):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM alpha_scores WHERE symbol = ? ORDER BY time DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_alpha_trade_candidate(
    scan_id,
    time,
    alpha_symbol,
    futures_symbol=None,
    base_asset=None,
    alpha_discovery_score=0,
    alpha_profile=None,
    alpha_reason=None,
    raw_alpha=None,
    normal_score=None,
    normal_grade=None,
    normal_side=None,
    entry_profile=None,
    entry_status=None,
    block_reason=None,
    adapter_quality=0,
    missing_fields=None,
    volume_price=None,
):
    vp = volume_price or {}
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO alpha_trade_candidates
               (scan_id, time, alpha_symbol, futures_symbol, base_asset,
                alpha_discovery_score, alpha_profile, alpha_reason, raw_alpha_json,
                normal_score, normal_grade, normal_side, entry_profile, entry_status,
                block_reason, adapter_quality, missing_fields_json,
                volume_price_state, volume_price_action, volume_price_reasons_json,
                volume_price_metrics_json, volume_price_max_position_factor, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
               ON CONFLICT(scan_id, alpha_symbol) DO UPDATE SET
                 time=excluded.time,
                 futures_symbol=excluded.futures_symbol,
                 base_asset=excluded.base_asset,
                 alpha_discovery_score=excluded.alpha_discovery_score,
                 alpha_profile=excluded.alpha_profile,
                 alpha_reason=excluded.alpha_reason,
                 raw_alpha_json=excluded.raw_alpha_json,
                 normal_score=excluded.normal_score,
                 normal_grade=excluded.normal_grade,
                 normal_side=excluded.normal_side,
                 entry_profile=excluded.entry_profile,
                 entry_status=excluded.entry_status,
                 block_reason=excluded.block_reason,
                 adapter_quality=excluded.adapter_quality,
                 missing_fields_json=excluded.missing_fields_json,
                 volume_price_state=excluded.volume_price_state,
                 volume_price_action=excluded.volume_price_action,
                 volume_price_reasons_json=excluded.volume_price_reasons_json,
                 volume_price_metrics_json=excluded.volume_price_metrics_json,
                 volume_price_max_position_factor=excluded.volume_price_max_position_factor,
                 updated_at=datetime('now')""",
            (
                scan_id,
                time,
                alpha_symbol,
                futures_symbol,
                base_asset,
                alpha_discovery_score,
                alpha_profile,
                alpha_reason,
                json.dumps(raw_alpha or {}, ensure_ascii=False),
                normal_score,
                normal_grade,
                normal_side,
                json.dumps(entry_profile or {}, ensure_ascii=False) if isinstance(entry_profile, (dict, list)) else entry_profile,
                entry_status,
                block_reason,
                adapter_quality,
                json.dumps(missing_fields or [], ensure_ascii=False),
                vp.get("state"),
                vp.get("action"),
                json.dumps(vp.get("reasons") or [], ensure_ascii=False),
                json.dumps(vp.get("metrics") or {}, ensure_ascii=False),
                vp.get("max_position_factor"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_latest_alpha_trade_candidates(limit=200):
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT *
               FROM alpha_trade_candidates
               WHERE futures_symbol IS NOT NULL
                 AND futures_symbol != ''
               ORDER BY time DESC, updated_at DESC, id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def fetch_latest_alpha_trade_candidate(alpha_symbol):
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT *
               FROM alpha_trade_candidates
               WHERE alpha_symbol = ?
                 AND futures_symbol IS NOT NULL
                 AND futures_symbol != ''
               ORDER BY time DESC, updated_at DESC, id DESC
               LIMIT 1""",
            (alpha_symbol,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fetch_latest_alpha_position_context(symbol=None, alpha_symbol=None):
    """Return the freshest Alpha score and volume-price review for a live position."""
    conn = get_conn()
    try:
        params = []
        where = []
        if symbol:
            where.append("futures_symbol = ?")
            params.append(symbol)
        if alpha_symbol:
            where.append("alpha_symbol = ?")
            params.append(alpha_symbol)
        if not where:
            return None

        candidate = conn.execute(
            f"""SELECT *
                FROM alpha_trade_candidates
                WHERE {' OR '.join(where)}
                ORDER BY time DESC, updated_at DESC, id DESC
                LIMIT 1""",
            params,
        ).fetchone()

        scan = conn.execute(
            f"""SELECT *
                FROM alpha_scan_scores
                WHERE {' OR '.join(where)}
                ORDER BY time DESC
                LIMIT 1""",
            params,
        ).fetchone()

        if not candidate and not scan:
            return None

        data = {}
        if scan:
            data.update(dict(scan))
        if candidate:
            c = dict(candidate)
            data.update({
                "candidate_id": c.get("id"),
                "candidate_time": c.get("time"),
                "candidate_status": c.get("entry_status"),
                "candidate_block_reason": c.get("block_reason"),
                "volume_price_state": c.get("volume_price_state"),
                "volume_price_action": c.get("volume_price_action"),
                "volume_price_reasons_json": c.get("volume_price_reasons_json"),
                "volume_price_metrics_json": c.get("volume_price_metrics_json"),
                "volume_price_max_position_factor": c.get("volume_price_max_position_factor"),
                "raw_alpha_json": c.get("raw_alpha_json"),
            })
            if c.get("alpha_discovery_score") is not None:
                data["alpha_score"] = c.get("alpha_discovery_score")
        return data
    finally:
        conn.close()


def get_alpha_cooldown(symbol=None, cooldown_type=None, source="alpha"):
    conn = get_conn()
    try:
        where = ["source = ?", "cooldown_until > datetime('now')"]
        params = [source]
        if symbol is not None:
            where.append("(symbol = ? OR symbol = '*')")
            params.append(symbol)
        if cooldown_type is not None:
            where.append("cooldown_type = ?")
            params.append(cooldown_type)
        row = conn.execute(
            f"""SELECT *
                FROM alpha_cooldowns
                WHERE {' AND '.join(where)}
                ORDER BY cooldown_until DESC
                LIMIT 1""",
            params,
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_alpha_cooldown(symbol, cooldown_type, reason, minutes, source="alpha", loss_count=0):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO alpha_cooldowns
               (source, symbol, cooldown_type, reason, cooldown_until, loss_count, updated_at)
               VALUES (?, ?, ?, ?, datetime('now', ?), ?, datetime('now'))
               ON CONFLICT(source, symbol, cooldown_type) DO UPDATE SET
                 reason=excluded.reason,
                 cooldown_until=excluded.cooldown_until,
                 loss_count=excluded.loss_count,
                 updated_at=datetime('now')""",
            (source, symbol or "*", cooldown_type, reason, f"+{int(minutes)} minutes", int(loss_count or 0)),
        )
        conn.commit()
    finally:
        conn.close()


def fetch_active_alpha_cooldowns(limit=100):
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT *
               FROM alpha_cooldowns
               WHERE cooldown_until > datetime('now')
               ORDER BY cooldown_until DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---- Backtest ----

def fetch_historical_scores(hours_back=720):
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT time, symbol, composite_score, composite_summary, market_price, raw_features
            FROM alpha_scores
            WHERE time > datetime('now', '-{hours_back} hours')
            ORDER BY symbol, time"""
    ).fetchall()
    return rows


def fetch_price_history(symbols, hours_back=720):
    conn = get_conn()
    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"""SELECT time as time_bucket, symbol, close
            FROM futures_candles_1h
            WHERE symbol IN ({placeholders})
              AND time > datetime('now', '-{hours_back} hours')
            ORDER BY symbol, time""",
        symbols,
    ).fetchall()
    return rows


# ---- Positions History ----

def insert_position_snapshot(rows):
    """鎵归噺鎻掑叆鎸佷粨蹇収"""
    conn = get_conn()
    account_id = current_account_id()
    conn.executemany(
        """INSERT INTO positions_history
           (account_id, time, symbol, side, position_side, quantity, entry_price,
            mark_price, unrealized_pnl, leverage, stop_loss, take_profit)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(account_id, *row) for row in rows],
    )
    conn.commit()


def fetch_positions_history(symbol=None, limit=200):
    conn = get_conn()
    if symbol:
        rows = conn.execute(
            """SELECT * FROM positions_history
               WHERE symbol = ?
               ORDER BY time DESC LIMIT ?""",
            (symbol, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM positions_history
               ORDER BY time DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return list(reversed(rows))


# ---- Strategy Decisions (learning loop V1) ----

def _json_dumps(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def record_strategy_decision(
    symbol,
    side=None,
    mode="live",
    decision_stage=None,
    decision_result=None,
    filter_reason=None,
    composite_score=None,
    grade=None,
    market_regime=None,
    price=None,
    quantity=None,
    entry_price=None,
    risk_params=None,
    features=None,
    reason=None,
    scan_id=None,
    run_id=None,
    decision_id=None,
    time=None,
):
    conn = get_conn()
    conn.execute(
        """INSERT OR IGNORE INTO strategy_decisions
           (account_id, decision_id, run_id, time, scan_id, symbol, side, mode,
            decision_stage, decision_result, filter_reason, composite_score,
            grade, market_regime, price, quantity, entry_price,
            risk_params_json, features_json, reason_json)
           VALUES (?, ?, ?, COALESCE(?, datetime('now')), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            current_account_id(),
            decision_id,
            run_id,
            time,
            scan_id,
            symbol,
            side,
            mode,
            decision_stage,
            decision_result,
            filter_reason,
            composite_score,
            grade,
            market_regime,
            price,
            quantity,
            entry_price,
            _json_dumps(risk_params),
            _json_dumps(features),
            _json_dumps(reason),
        ),
    )
    conn.commit()


def record_strategy_decisions(rows):
    if not rows:
        return
    conn = get_conn()
    payload = []
    for row in rows:
        payload.append(
            (
                row.get("account_id", current_account_id()),
                row.get("decision_id"),
                row.get("run_id"),
                row.get("time"),
                row.get("scan_id"),
                row.get("symbol"),
                row.get("side"),
                row.get("mode", "live"),
                row.get("decision_stage"),
                row.get("decision_result"),
                row.get("filter_reason"),
                row.get("composite_score"),
                row.get("grade"),
                row.get("market_regime"),
                row.get("price"),
                row.get("quantity"),
                row.get("entry_price"),
                _json_dumps(row.get("risk_params")),
                _json_dumps(row.get("features")),
                _json_dumps(row.get("reason")),
            )
        )
    conn.executemany(
        """INSERT OR IGNORE INTO strategy_decisions
           (account_id, decision_id, run_id, time, scan_id, symbol, side, mode,
            decision_stage, decision_result, filter_reason, composite_score,
            grade, market_regime, price, quantity, entry_price,
            risk_params_json, features_json, reason_json)
           VALUES (?, ?, ?, COALESCE(?, datetime('now')), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        payload,
    )
    conn.commit()


def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso_z(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window_return(candles, target_dt, entry_price):
    eligible = [c for c in candles if c["_time"] <= target_dt]
    if not eligible or not entry_price:
        return None
    return (float(eligible[-1]["close"]) - entry_price) / entry_price


def label_signal_outcomes(max_rows=1000, min_age_minutes=30):
    """V2: label strategy decisions with future 1h/4h/12h/24h outcomes.

    Labels are incremental: if only 1h future data exists, the 1h fields are
    written and the same row is updated later when 4h/12h/24h data arrives.
    """
    conn = get_conn()
    min_time = _iso_z(datetime.now(timezone.utc) - timedelta(minutes=min_age_minutes))
    rows = conn.execute(
        """SELECT d.*
           FROM strategy_decisions d
           LEFT JOIN signal_outcomes o ON o.decision_id = d.decision_id
           WHERE d.decision_id IS NOT NULL
             AND d.price IS NOT NULL
             AND d.price > 0
             AND d.time <= ?
             AND d.decision_stage IN ('scan', 'candidate_filter', 'side_decision', 'open_decision')
             AND (o.id IS NULL OR o.is_complete = 0)
           ORDER BY d.time ASC, d.id ASC
           LIMIT ?""",
        (min_time, max_rows),
    ).fetchall()
    updates = []
    for d in rows:
        try:
            signal_dt = _parse_dt(d["time"])
            if not signal_dt:
                continue
            end_dt = signal_dt + timedelta(hours=25)
            candles = conn.execute(
                """SELECT time, close, high, low
                   FROM futures_candles_1h
                   WHERE symbol = ?
                     AND time > ?
                     AND time <= ?
                   ORDER BY time ASC""",
                (d["symbol"], _iso_z(signal_dt), _iso_z(end_dt)),
            ).fetchall()
            candles = [dict(c) for c in candles]
            for c in candles:
                c["_time"] = _parse_dt(c["time"])
            if not candles:
                continue

            entry = float(d["entry_price"] or d["price"])
            highs = [float(c["high"]) for c in candles if c.get("high") is not None]
            lows = [float(c["low"]) for c in candles if c.get("low") is not None]
            if not highs or not lows or entry <= 0:
                continue

            max_up = (max(highs) - entry) / entry
            max_down = (min(lows) - entry) / entry
            best_side = "LONG" if max_up > abs(max_down) and max_up > 0 else "SHORT" if abs(max_down) > 0 else "NONE"
            side = (d["side"] or "").upper()
            ret_1h = _window_return(candles, signal_dt + timedelta(hours=1), entry)
            ret_4h = _window_return(candles, signal_dt + timedelta(hours=4), entry)
            ret_12h = _window_return(candles, signal_dt + timedelta(hours=12), entry)
            ret_24h = _window_return(candles, signal_dt + timedelta(hours=24), entry)
            direction_correct = None
            if side == "LONG" and ret_24h is not None:
                direction_correct = 1 if ret_24h > 0 else 0
            elif side == "SHORT" and ret_24h is not None:
                direction_correct = 1 if ret_24h < 0 else 0
            latest_dt = max(c["_time"] for c in candles)
            is_complete = 1 if latest_dt >= signal_dt + timedelta(hours=24) else 0
            hit_tp = 1 if max_up >= 0.05 else 0
            hit_sl = 1 if max_down <= -0.05 else 0
            updates.append((
                d["decision_id"],
                d["id"],
                d["run_id"],
                d["scan_id"],
                d["symbol"],
                _iso_z(signal_dt),
                entry,
                side,
                ret_1h,
                ret_4h,
                ret_12h,
                ret_24h,
                max_up,
                max_down,
                best_side,
                direction_correct,
                hit_tp,
                hit_sl,
                len(candles),
                is_complete,
            ))
        except Exception:
            continue

    if updates:
        sql = """INSERT INTO signal_outcomes
               (decision_id, strategy_decision_id, run_id, scan_id, symbol,
                signal_time, entry_price, side, return_1h, return_4h,
                return_12h, return_24h, max_favorable_return,
                max_adverse_return, best_side, direction_correct, hit_tp,
                hit_sl, bars_observed, is_complete, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(decision_id) DO UPDATE SET
                return_1h=excluded.return_1h,
                return_4h=excluded.return_4h,
                return_12h=excluded.return_12h,
                return_24h=excluded.return_24h,
                max_favorable_return=excluded.max_favorable_return,
                max_adverse_return=excluded.max_adverse_return,
                best_side=excluded.best_side,
                direction_correct=excluded.direction_correct,
                hit_tp=excluded.hit_tp,
                hit_sl=excluded.hit_sl,
                bars_observed=excluded.bars_observed,
                is_complete=excluded.is_complete,
                updated_at=datetime('now')"""
        # SQLite only has one writer.  Keep each write transaction short so
        # live candle ingestion can acquire the writer slot between chunks.
        for offset in range(0, len(updates), 100):
            conn.executemany(sql, updates[offset:offset + 100])
            conn.commit()
    return len(updates)


def fetch_signal_outcome_summary(run_id=None):
    conn = get_conn()
    where = "WHERE run_id = ?" if run_id else ""
    params = (run_id,) if run_id else ()
    row = conn.execute(
        f"""SELECT COUNT(*) AS total,
                  SUM(CASE WHEN is_complete = 1 THEN 1 ELSE 0 END) AS complete,
                  AVG(return_1h) AS avg_return_1h,
                  AVG(return_4h) AS avg_return_4h,
                  AVG(return_12h) AS avg_return_12h,
                  AVG(return_24h) AS avg_return_24h,
                  AVG(max_favorable_return) AS avg_mfe,
                  AVG(max_adverse_return) AS avg_mae,
                  AVG(direction_correct) AS direction_accuracy
           FROM signal_outcomes {where}""",
        params,
    ).fetchone()
    by_side = conn.execute(
        f"""SELECT best_side, COUNT(*) AS count,
                  AVG(return_24h) AS avg_return_24h,
                  AVG(max_favorable_return) AS avg_mfe,
                  AVG(max_adverse_return) AS avg_mae
           FROM signal_outcomes {where}
           GROUP BY best_side
           ORDER BY count DESC""",
        params,
    ).fetchall()
    return dict(row) if row else {}, [dict(r) for r in by_side]


# ---- Factor Performance ----

# ---- Orders ----

def insert_order(
    symbol,
    side,
    order_type,
    quantity,
    price,
    status="pending",
    reason=None,
    position_id=None,
    strategy_source="normal",
    signal_source=None,
    alpha_symbol=None,
    alpha_profile=None,
    alpha_entry_level=None,
    alpha_score=None,
    alpha_suggested_position_pct=None,
    client_order_id=None,
    exchange_order_id=None,
    signal_event_id=None,
    setup_id=None,
    alpha_stage=None,
    ai_model_versions=None,
):
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO orders
               (account_id, position_id, symbol, side, order_type, quantity, price, status, reason,
                strategy_source, signal_source, alpha_symbol, alpha_profile, alpha_entry_level,
                alpha_score, alpha_suggested_position_pct, client_order_id,
                exchange_order_id, signal_event_id, setup_id, alpha_stage,
                ai_model_versions_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                current_account_id(),
                position_id,
                symbol,
                side,
                order_type,
                quantity,
                price,
                status,
                reason,
                strategy_source,
                signal_source,
                alpha_symbol,
                alpha_profile,
                alpha_entry_level,
                alpha_score,
                alpha_suggested_position_pct,
                client_order_id,
                exchange_order_id,
                signal_event_id,
                setup_id,
                alpha_stage,
                json.dumps(ai_model_versions or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()


def update_order_status(order_id, status):
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE orders SET status = ? WHERE id = ?", (status, order_id)
        )
        conn.commit()
    finally:
        conn.close()


def insert_fill(
    symbol,
    order_id,
    side,
    quantity,
    price,
    realized_pnl,
    fee,
    fee_asset,
    trade_id,
    position_id=None,
    strategy_source="normal",
    signal_source=None,
    alpha_symbol=None,
    alpha_profile=None,
    alpha_entry_level=None,
    alpha_score=None,
    alpha_suggested_position_pct=None,
):
    conn = get_conn()
    if trade_id and not str(trade_id).startswith("A"):
        trade_id = f"A{current_account_id()}:{trade_id}"
    conn.execute(
        """INSERT INTO fills
           (account_id, position_id, symbol, order_id, side, quantity, price, realized_pnl, fee, fee_asset, trade_id,
            strategy_source, signal_source, alpha_symbol, alpha_profile, alpha_entry_level,
            alpha_score, alpha_suggested_position_pct)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            current_account_id(),
            position_id,
            symbol,
            order_id,
            side,
            quantity,
            price,
            realized_pnl,
            fee,
            fee_asset,
            trade_id,
            strategy_source,
            signal_source,
            alpha_symbol,
            alpha_profile,
            alpha_entry_level,
            alpha_score,
            alpha_suggested_position_pct,
        ),
    )
    conn.commit()


def get_trade_ids_from_fills():
    conn = get_conn()
    rows = conn.execute("SELECT trade_id FROM fills WHERE trade_id IS NOT NULL").fetchall()
    return {r["trade_id"] for r in rows if r["trade_id"]}


# ---- Alpha Score Training Samples ----

def insert_training_samples(rows):
    """鎵归噺鍐欏叆 training_samples
    rows: list of (scan_id, symbol, timestamp, feature_json, composite_score, market_regime)
    """
    conn = get_conn()
    conn.executemany(
        """INSERT INTO training_samples
           (scan_id, symbol, timestamp, feature_json, composite_score, market_regime)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def update_training_sample_returns(scan_id, updates):
    """鍥炴祴鍚庢洿鏂?training_samples 鐨勬湭鏉ユ敹鐩婂瓧娈?    updates: list of (return_6h, return_12h, return_24h, return_48h, max_drawdown, symbol, scan_id)
    """
    conn = get_conn()
    conn.executemany(
        """UPDATE training_samples
           SET return_6h = ?, return_12h = ?, return_24h = ?, return_48h = ?, max_drawdown = ?
           WHERE symbol = ? AND scan_id = ?""",
        updates,
    )
    conn.commit()


def fetch_training_samples(hours_back=720, labeled_only=True):
    """鑾峰彇璁粌鏍锋湰
    labeled_only=True 鍒欏彧杩斿洖鍚?return_12h 鏍囩鐨勬牱鏈?    """
    conn = get_conn()
    if labeled_only:
        rows = conn.execute(
            """SELECT * FROM training_samples
               WHERE return_12h IS NOT NULL
               AND timestamp > datetime('now', ?)
               ORDER BY timestamp DESC""",
            (f'-{hours_back} hours',),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM training_samples
               WHERE timestamp > datetime('now', ?)
               ORDER BY timestamp DESC""",
            (f'-{hours_back} hours',),
        ).fetchall()
    return rows


# ---- Symbol Snapshots锛堝垢瀛樿€呭亸宸慨澶嶏級----

def insert_symbol_snapshot(rows):
    """鎵归噺鍐欏叆鎴栨洿鏂?symbol_snapshots
    rows: list of (date, symbol, status, quote_volume, price_change_24h, active)
    """
    conn = get_conn()
    conn.executemany(
        """INSERT OR REPLACE INTO symbol_snapshots
           (date, symbol, status, quote_volume, price_change_24h, active)
           VALUES (?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def fetch_symbol_snapshots(date_str):
    """鑾峰彇鏌愭棩娲昏穬鐨勪氦鏄撳鍒楄〃"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT symbol FROM symbol_snapshots WHERE date = ? AND active = 1",
        (date_str,),
    ).fetchall()
    return {r["symbol"] for r in rows}


# ---- Order Book Depth (V4.0) ----

def insert_orderbook_snapshot(rows):
    """鎵归噺鍐欏叆璁㈠崟绨挎繁搴﹀揩鐓?    rows: list of (time, symbol, bid_depth, ask_depth, imbalance_ratio, top_bid_qty, top_ask_qty)
    """
    conn = get_conn()
    conn.executemany(
        """INSERT OR REPLACE INTO orderbook_snapshots
           (timestamp, symbol, bid_depth, ask_depth, imbalance_ratio, top_bid_qty, top_ask_qty)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def fetch_orderbook_depth(symbol, hours=6):
    """鑾峰彇鏈€杩慛灏忔椂鐨勮鍗曠翱娣卞害鏁版嵁锛堢敤浜庤绠楀ぇ鍗曞洜瀛愶級"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM orderbook_snapshots
           WHERE symbol = ?
             AND julianday(timestamp) > julianday('now', ?)
           ORDER BY julianday(timestamp) DESC""",
        (symbol, f'-{hours} hours'),
    ).fetchall()
    return rows


def fetch_24h_quote_volume(symbol):
    """鑾峰彇24h鎴愪氦棰濓紙鐢ㄤ簬璁＄畻澶у崟闃堝€硷級"""
    conn = get_conn()
    row = conn.execute(
        """SELECT quote_vol FROM candles_1h
           WHERE symbol = ?
             AND julianday(time) > julianday('now', '-25 hours')
           ORDER BY julianday(time) DESC LIMIT 1""",
        (symbol,),
    ).fetchone()
    return float(row["quote_vol"]) if row else 0


ENTRY_REVIEW_SNAPSHOT_COLUMNS = (
    "position_trade_id", "source_decision_id", "symbol", "alpha_symbol", "side",
    "strategy_source", "category", "entry_template", "market_regime", "entry_time",
    "entry_price", "quantity", "leverage", "margin", "notional", "stop_loss",
    "stop_pct", "take_profit_1", "take_profit_2", "risk_reward_ratio", "atr_pct",
    "total_score", "grade", "score_items_json", "trend_score", "breakout_state",
    "spot_volume_ratio", "futures_volume_ratio", "volume_sync_state", "spread_pct",
    "orderbook_state", "passed_conditions_json", "relaxed_conditions_json",
    "features_json", "risk_params_json", "reason_json", "entry_snapshot_json",
    "entry_reason_text", "snapshot_source", "position_status",
)


def record_entry_review_snapshot(snapshot, conn=None):
    """Insert immutable entry evidence; retries never overwrite the first snapshot."""
    owns_connection = conn is None
    if owns_connection:
        init_db()
    payload = dict(snapshot or {})
    for key in (
        "score_items_json", "passed_conditions_json", "relaxed_conditions_json",
        "features_json", "risk_params_json", "reason_json", "entry_snapshot_json",
    ):
        if key in payload and payload[key] is not None and not isinstance(payload[key], str):
            payload[key] = json.dumps(payload[key], ensure_ascii=False, separators=(",", ":"))
    if not payload.get("entry_snapshot_json"):
        payload["entry_snapshot_json"] = json.dumps(snapshot or {}, ensure_ascii=False, default=str, separators=(",", ":"))
    columns = [name for name in ENTRY_REVIEW_SNAPSHOT_COLUMNS if name in payload]
    if not payload.get("position_trade_id") or not payload.get("symbol"):
        raise ValueError("position_trade_id and symbol are required")
    conn = conn or get_conn()
    try:
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO trade_entry_reviews ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(payload.get(name) for name in columns),
        )
        if owns_connection:
            conn.commit()
        return cursor.rowcount == 1
    finally:
        if owns_connection:
            conn.close()


def fetch_entry_reviews(limit=100):
    init_db()
    conn = get_conn()
    try:
        return [dict(row) for row in conn.execute(
            """SELECT id, position_trade_id, source_decision_id, symbol, alpha_symbol, side,
                      strategy_source, category, entry_template, market_regime, entry_time,
                      entry_price, quantity, leverage, margin, notional, stop_loss, stop_pct,
                      take_profit_1, take_profit_2, risk_reward_ratio, atr_pct, total_score,
                      grade, trend_score, breakout_state, spot_volume_ratio,
                      futures_volume_ratio, volume_sync_state, spread_pct, orderbook_state,
                      entry_reason_text, snapshot_source, position_status, exit_time,
                      exit_price, net_pnl, pnl_pct, return_now, max_favorable_return,
                      max_adverse_return, bars_observed, review_label, review_reason,
                      reviewed_at
               FROM trade_entry_reviews ORDER BY datetime(entry_time) DESC, id DESC LIMIT ?""",
            (int(limit),),
        ).fetchall()]
    finally:
        conn.close()
