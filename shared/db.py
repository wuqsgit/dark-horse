"""Database layer — SQLite backend (fast local dev, swap to PG later)"""
import os
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "alphadog.db")

_local = threading.local()


def get_conn():
    # 每个调用都获取新连接，避免线程安全问题
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    """创建所有表（幂等）—— 首次启动或新表迁移时调用"""
    conn = get_conn()
    conn.executescript("""
        -- 已有表：K线、期货、链上
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
        CREATE TABLE IF NOT EXISTS futures_data (
            time TEXT, symbol TEXT,
            open_interest REAL, funding_rate REAL, mark_price REAL,
            PRIMARY KEY (time, symbol)
        );
        CREATE TABLE IF NOT EXISTS onchain_flows (
            time TEXT, symbol TEXT, chain TEXT DEFAULT 'ethereum',
            cex_inflow_usd REAL DEFAULT 0, cex_outflow_usd REAL DEFAULT 0,
            cex_net_flow_usd REAL, cex_net_flow_14d_usd REAL,
            cex_net_outflow_ratio REAL, window_hours INTEGER DEFAULT 24,
            PRIMARY KEY (time, symbol, chain)
        );
        -- 评分表
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
        -- 交易表
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, side TEXT NOT NULL, position_side TEXT,
            quantity REAL, entry_price REAL, exit_price REAL,
            pnl REAL, pnl_pct REAL, exit_reason TEXT,
            entry_reason TEXT,  -- V3.0 开仓原因
            entry_time TEXT, exit_time TEXT,
            grade_at_entry TEXT, score_at_entry REAL,
            created_at TEXT DEFAULT (datetime('now')),
            source TEXT DEFAULT 'system',
            income_id TEXT,
            fill_ids TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time);
        -- V3.0 开仓记录表（记录开仓原因，重启不丢）
        CREATE TABLE IF NOT EXISTS position_history (
            symbol TEXT PRIMARY KEY,
            side TEXT, quantity REAL,
            entry_price REAL, entry_reason TEXT,
            entry_score REAL, entry_time TEXT,
            tp3_price REAL, atr_value REAL,
            update_time TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
        -- 回测表
        CREATE TABLE IF NOT EXISTS backtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, grade TEXT, grade_score REAL,
            grade_time TEXT, price_at_grade REAL,
            return_6h REAL, return_12h REAL, return_24h REAL, return_48h REAL,
            max_drawdown REAL, win_12h INTEGER, win_24h INTEGER,
            run_time TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_backtest_grade ON backtest_results(grade);
        CREATE INDEX IF NOT EXISTS idx_backtest_grade_time ON backtest_results(grade, grade_time DESC);
        CREATE INDEX IF NOT EXISTS idx_backtest_runtime ON backtest_results(run_time);
        CREATE INDEX IF NOT EXISTS idx_backtest_grade_score ON backtest_results(grade, grade_score);
        CREATE TABLE IF NOT EXISTS backtest_summary_cache (
            grade TEXT PRIMARY KEY,
            latest_run TEXT,
            count INTEGER,
            avg_return_12h REAL,
            avg_return_24h REAL,
            avg_return_48h REAL,
            win_rate_12h REAL,
            win_rate_24h REAL,
            avg_drawdown REAL,
            avg_score REAL,
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS backtest_review (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT DEFAULT (datetime('now')),
            review_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_backtest_review_run_time ON backtest_review(run_time DESC);
        -- ===== 新增表 =====
        -- 持仓快照（每轮循环记录）
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
        -- 因子归因
        CREATE TABLE IF NOT EXISTS factor_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time TEXT DEFAULT (datetime('now')),
            factor_name TEXT NOT NULL,
            bucket TEXT NOT NULL,
            samples INTEGER,
            win_rate REAL,
            avg_return REAL,
            avg_drawdown REAL,
            ev REAL,
            ic REAL,
            ir REAL
        );
        CREATE INDEX IF NOT EXISTS idx_factor_perf_run ON factor_performance(run_time);
        CREATE INDEX IF NOT EXISTS idx_factor_perf_name ON factor_performance(factor_name);
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
        -- 订单表（下单意图记录）
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT,
            quantity REAL,
            price REAL,
            status TEXT DEFAULT 'pending',
            reason TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        -- 成交记录表
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
        -- Alpha Score 训练样本表
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
        -- 交易对快照表（幸存者偏差修复）
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
        -- V3.0 交易冷却追踪表
        CREATE TABLE IF NOT EXISTS trade_cooldown (
            symbol TEXT PRIMARY KEY,
            last_stop_time TEXT,
            stop_count_24h INTEGER DEFAULT 0,
            consecutive_stops INTEGER DEFAULT 0,
            cooldown_until TEXT,
            reason TEXT,
            updated_at TEXT
        );
        -- V3.0 订单簿快照表
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
        -- V4.0 订单簿深度快照表（增强版，含大小单统计）
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
        "last_exit_reason": "TEXT",
        "roll_layer": "INTEGER DEFAULT 0",
        "last_roll_time": "TEXT",
        "roll_parent_trade_id": "TEXT",
        "protected_profit": "REAL DEFAULT 0",
        "max_floating_pnl": "REAL DEFAULT 0",
        "roll_enabled": "INTEGER DEFAULT 0",
        "roll_block_reason": "TEXT",
    }.items():
        _ensure_column(conn, "position_history", column, ddl)
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
    conn.execute(
        """INSERT OR IGNORE INTO trading_runtime_controls(key, value)
           VALUES ('normal_trading_enabled', 'true')"""
    )
    conn.execute(
        """INSERT OR IGNORE INTO trading_runtime_controls(key, value)
           VALUES ('alpha_trading_enabled', 'false')"""
    )
    conn.commit()


def _ensure_column(conn, table, column, ddl):
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS trading_runtime_controls (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        for key, enabled in defaults.items():
            conn.execute(
                """INSERT OR IGNORE INTO trading_runtime_controls(key, value)
                   VALUES (?, ?)""",
                (key, "true" if enabled else "false"),
            )
        rows = conn.execute("SELECT key, value, updated_at FROM trading_runtime_controls").fetchall()
        conn.commit()
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


def insert_alpha_candles(table, rows):
    if table not in {"alpha_candles_1h", "alpha_candles_15m", "alpha_candles_6h", "alpha_candles_24h"}:
        raise ValueError(f"unsupported alpha candle table: {table}")
    conn = get_conn()
    conn.executemany(
        f"""INSERT OR REPLACE INTO {table}
           (time, alpha_symbol, open, high, low, close, volume, quote_vol, trades)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
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
        """SELECT *
           FROM alpha_symbols
           WHERE status = 'TRADING'
             AND tradeability != 'inactive'
             AND COALESCE(volume_24h, 0) > 0
           ORDER BY
             CASE tradeability WHEN 'alpha_futures_mapped' THEN 0
                               WHEN 'alpha_tradeable' THEN 1
                               ELSE 2 END,
             volume_24h DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return rows


def fetch_alpha_candles(table, symbols, hours=None, days=None):
    if not symbols:
        return []
    if table not in {"alpha_candles_1h", "alpha_candles_15m", "alpha_candles_6h", "alpha_candles_24h"}:
        raise ValueError(f"unsupported alpha candle table: {table}")
    placeholders = ",".join("?" for _ in symbols)
    if days is not None:
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        cutoff = (datetime.utcnow() - timedelta(hours=hours or 72)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = get_conn()
    return conn.execute(
        f"""SELECT time, alpha_symbol, open, high, low, close, volume, quote_vol, trades
            FROM {table}
            WHERE alpha_symbol IN ({placeholders}) AND time > ?
            ORDER BY alpha_symbol, time""",
        symbols + [cutoff],
    ).fetchall()


def fetch_alpha_orderbook_depth(symbol, hours=6):
    conn = get_conn()
    return conn.execute(
        """SELECT *
           FROM alpha_orderbook_snapshots
           WHERE alpha_symbol = ? AND timestamp > datetime('now', ?, '+8 hours')
           ORDER BY timestamp DESC""",
        (symbol, f"-{hours} hours"),
    ).fetchall()


def fetch_klines_1h(symbols, hours=72):
    conn = get_conn()
    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"""SELECT time, symbol, open, high, low, close, volume, quote_vol
            FROM candles_1h
            WHERE symbol IN ({placeholders}) AND time > datetime('now', '-{hours} hours', '+8 hours')
            ORDER BY symbol, time""",
        symbols,
    ).fetchall()
    return rows


def fetch_klines_15m(symbols, hours=12):
    conn = get_conn()
    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"""SELECT time, symbol, open, high, low, close, volume, quote_vol
            FROM candles_15m
            WHERE symbol IN ({placeholders}) AND time > datetime('now', '-{hours} hours', '+8 hours')
            ORDER BY symbol, time""",
        symbols,
    ).fetchall()
    return rows


def fetch_klines_6h(symbols, days=14):
    conn = get_conn()
    placeholders = ",".join("?" for _ in symbols)
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        f"""SELECT time, symbol, open, high, low, close, volume, quote_vol
            FROM candles_6h
            WHERE symbol IN ({placeholders}) AND time > ?
            ORDER BY symbol, time""",
        symbols + [cutoff],
    ).fetchall()
    return rows


def fetch_klines_24h(symbols, days=35):
    conn = get_conn()
    placeholders = ",".join("?" for _ in symbols)
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        f"""SELECT time, symbol, open, high, low, close, volume, quote_vol
            FROM candles_24h
            WHERE symbol IN ({placeholders}) AND time > ?
            ORDER BY symbol, time""",
        symbols + [cutoff],
    ).fetchall()
    return rows


# ---- Futures ----

def insert_futures(rows):
    conn = get_conn()
    conn.executemany(
        """INSERT OR REPLACE INTO futures_data (time, symbol, open_interest, funding_rate, mark_price)
           VALUES (?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def fetch_futures(symbols, hours=72):
    conn = get_conn()
    placeholders = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"""SELECT time, symbol, open_interest, funding_rate, mark_price
            FROM futures_data
            WHERE symbol IN ({placeholders}) AND time > datetime('now', '-{hours} hours', '+8 hours')
            ORDER BY symbol, time""",
        symbols,
    ).fetchall()
    return rows


# ---- On-chain ----

def insert_onchain(rows):
    conn = get_conn()
    conn.executemany(
        """INSERT OR REPLACE INTO onchain_flows
           (time, symbol, chain, cex_inflow_usd, cex_outflow_usd, cex_net_flow_usd,
            cex_net_flow_14d_usd, cex_net_outflow_ratio, window_hours)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def fetch_onchain(symbols, hours=72):
    conn = get_conn()
    rows = conn.execute(
        f"""SELECT time, symbol, chain, cex_net_flow_usd, cex_net_flow_14d_usd,
                   cex_net_outflow_ratio
            FROM onchain_flows
            WHERE time > datetime('now', '-{hours} hours', '+8 hours')
            ORDER BY time""",
    ).fetchall()
    return rows


# ---- Trades ----

def new_position_id(symbol, side):
    clean_symbol = (symbol or "UNKNOWN").replace("/", "").upper()
    clean_side = (side or "SIDE").upper()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{clean_symbol}-{clean_side}-{stamp}-{uuid.uuid4().hex[:8]}"


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
):
    conn = get_conn()
    conn.execute(
        """INSERT INTO trades
           (position_id, symbol, side, quantity, entry_price, exit_price, pnl, pnl_pct,
            exit_reason, entry_reason, entry_time, exit_time, grade_at_entry, score_at_entry,
            strategy_source, signal_source, alpha_symbol, alpha_profile, alpha_entry_level,
            alpha_score, alpha_suggested_position_pct)
           VALUES (?,?,?,?,?,?,?,?,?,?, datetime('now'), datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
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
):
    """V3.0 记录/更新开仓信息，重启后可恢复"""
    conn = get_conn()
    existing = conn.execute("SELECT position_id FROM position_history WHERE symbol=?", (symbol,)).fetchone()
    position_id = position_id or (existing["position_id"] if existing and "position_id" in existing.keys() else None) or new_position_id(symbol, side)
    conn.execute(
        """INSERT INTO position_history
           (symbol, side, quantity, entry_price, entry_reason, entry_score, tp3_price, atr_value,
            highest_price, position_id, strategy_source, signal_source, alpha_symbol,
            alpha_profile, alpha_entry_level, alpha_score, alpha_suggested_position_pct, update_time)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(symbol) DO UPDATE SET
             side=excluded.side,
             quantity=excluded.quantity,
             entry_price=excluded.entry_price,
             entry_reason=excluded.entry_reason,
             entry_score=excluded.entry_score,
             tp3_price=excluded.tp3_price,
             atr_value=excluded.atr_value,
             position_id=COALESCE(position_history.position_id, excluded.position_id),
             strategy_source=excluded.strategy_source,
             signal_source=excluded.signal_source,
             alpha_symbol=excluded.alpha_symbol,
             alpha_profile=excluded.alpha_profile,
             alpha_entry_level=excluded.alpha_entry_level,
             alpha_score=excluded.alpha_score,
             alpha_suggested_position_pct=excluded.alpha_suggested_position_pct,
             highest_price=COALESCE(position_history.highest_price, excluded.highest_price),
             update_time=datetime('now')""",
        (
            symbol,
            side,
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
        )
    )
    conn.commit()
    conn.close()
    return position_id


def get_position_history(symbol):
    """V3.0 获取开仓信息"""
    conn = get_conn()
    row = conn.execute("SELECT * FROM position_history WHERE symbol=?", (symbol,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_position_history(symbol):
    """V3.0 删除开仓记录（平仓后）"""
    conn = get_conn()
    conn.execute("DELETE FROM position_history WHERE symbol=?", (symbol,))
    conn.commit()
    conn.close()


def update_position_management(symbol, **fields):
    """Update live position management state without resetting the entry record."""
    allowed = {
        "quantity",
        "entry_price",
        "highest_price",
        "tp1_hit",
        "tp2_hit",
        "last_exit_reason",
        "roll_layer",
        "last_roll_time",
        "protected_profit",
        "max_floating_pnl",
        "roll_enabled",
        "roll_block_reason",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    conn = get_conn()
    assignments = ", ".join([f"{k}=?" for k in updates])
    values = list(updates.values()) + [symbol]
    conn.execute(
        f"UPDATE position_history SET {assignments}, update_time=datetime('now') WHERE symbol=?",
        values,
    )
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
    conn.commit()
    conn.close()


def fetch_position_trade_groups(limit=100):
    conn = get_conn()
    rows = conn.execute(
        """SELECT
               COALESCE(position_id, symbol || '-' || side || '-' || entry_time) AS position_id,
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
           GROUP BY COALESCE(position_id, symbol || '-' || side || '-' || entry_time), symbol, side
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
    rows = conn.execute("SELECT symbol FROM symbols WHERE is_active = 1").fetchall()
    return [r["symbol"] for r in rows]


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
           ORDER BY s.alpha_score DESC""",
        (scan["scan_id"],),
    ).fetchall()
    return scan, rows


def fetch_alpha_symbol_detail(alpha_symbol):
    conn = get_conn()
    row = conn.execute(
        """SELECT s.*, a.alpha_name, a.tradeability, a.status, a.volume_24h,
                  a.liquidity, a.percent_change_24h, a.token_id, a.raw_json AS symbol_raw_json
           FROM alpha_scan_scores s
           LEFT JOIN alpha_symbols a ON a.alpha_symbol = s.alpha_symbol
           WHERE s.alpha_symbol = ?
           ORDER BY s.time DESC
           LIMIT 1""",
        (alpha_symbol,),
    ).fetchone()
    return row


def fetch_alpha_score_history(alpha_symbol, limit=100):
    conn = get_conn()
    rows = conn.execute(
        """SELECT time, alpha_score, grade, market_price
           FROM alpha_scan_scores
           WHERE alpha_symbol = ?
           ORDER BY time DESC LIMIT ?""",
        (alpha_symbol, limit),
    ).fetchall()
    return list(reversed(rows))


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
               ORDER BY time DESC, updated_at DESC, id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
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
            FROM candles_1h
            WHERE symbol IN ({placeholders})
              AND time > datetime('now', '-{hours_back} hours')
            ORDER BY symbol, time""",
        symbols,
    ).fetchall()
    return rows


def insert_backtest(rows):
    conn = get_conn()
    conn.executemany(
        """INSERT INTO backtest_results
           (symbol, grade, grade_score, grade_time, price_at_grade,
            return_6h, return_12h, return_24h, return_48h,
            max_drawdown, win_12h, win_24h)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.execute("DELETE FROM backtest_summary_cache")
    conn.commit()


def fetch_backtest_summary():
    conn = get_conn()
    latest = conn.execute("SELECT MAX(run_time) FROM backtest_results").fetchone()[0]
    cached_latest = conn.execute("SELECT MAX(latest_run) FROM backtest_summary_cache").fetchone()[0]
    if latest and cached_latest != latest:
        conn.execute("DELETE FROM backtest_summary_cache")
        conn.execute(
            """INSERT INTO backtest_summary_cache
               (grade, latest_run, count, avg_return_12h, avg_return_24h, avg_return_48h,
                win_rate_12h, win_rate_24h, avg_drawdown, avg_score, updated_at)
               SELECT grade,
                      ? AS latest_run,
                      COUNT(*) as count,
                      AVG(return_12h) as avg_return_12h,
                      AVG(return_24h) as avg_return_24h,
                      AVG(return_48h) as avg_return_48h,
                      AVG(CASE WHEN win_12h = 1 THEN 1.0 ELSE 0.0 END) as win_rate_12h,
                      AVG(CASE WHEN win_24h = 1 THEN 1.0 ELSE 0.0 END) as win_rate_24h,
                      AVG(max_drawdown) as avg_drawdown,
                      AVG(grade_score) as avg_score,
                      datetime('now')
               FROM backtest_results
               WHERE grade IN ('S1', 'S2', 'A1', 'A2', 'B', 'C', 'D')
               GROUP BY grade""",
            (latest,),
        )
        conn.commit()
    rows = conn.execute(
        """SELECT grade, count, avg_return_12h, avg_return_24h, avg_return_48h,
                  win_rate_12h, win_rate_24h, avg_drawdown, avg_score
           FROM backtest_summary_cache
           ORDER BY avg_score DESC"""
    ).fetchall()
    return rows, latest


def fetch_recent_signals(grade="S1", limit=50):
    conn = get_conn()
    if not grade or str(grade).lower() in ("all", "*"):
        rows = conn.execute(
            """SELECT symbol, grade_time, grade, grade_score, price_at_grade,
                      return_12h, return_24h, win_12h, win_24h
               FROM backtest_results
               ORDER BY grade_time DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT symbol, grade_time, grade, grade_score, price_at_grade,
                      return_12h, return_24h, win_12h, win_24h
               FROM backtest_results
               WHERE grade = ?
               ORDER BY grade_time DESC
               LIMIT ?""",
            (grade, limit),
        ).fetchall()
    return rows


# ---- Positions History ----

def insert_position_snapshot(rows):
    """批量插入持仓快照"""
    conn = get_conn()
    conn.executemany(
        """INSERT INTO positions_history
           (time, symbol, side, position_side, quantity, entry_price,
            mark_price, unrealized_pnl, leverage, stop_loss, take_profit)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
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
           (decision_id, run_id, time, scan_id, symbol, side, mode,
            decision_stage, decision_result, filter_reason, composite_score,
            grade, market_regime, price, quantity, entry_price,
            risk_params_json, features_json, reason_json)
           VALUES (?, ?, COALESCE(?, datetime('now')), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
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
           (decision_id, run_id, time, scan_id, symbol, side, mode,
            decision_stage, decision_result, filter_reason, composite_score,
            grade, market_regime, price, quantity, entry_price,
            risk_params_json, features_json, reason_json)
           VALUES (?, ?, COALESCE(?, datetime('now')), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                   FROM candles_1h
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
        conn.executemany(
            """INSERT INTO signal_outcomes
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
                updated_at=datetime('now')""",
            updates,
        )
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

def insert_factor_performance(rows):
    """批量插入因子归因数据"""
    conn = get_conn()
    conn.executemany(
        """INSERT INTO factor_performance
           (run_time, factor_name, bucket, samples, win_rate,
            avg_return, avg_drawdown, ev, ic, ir)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def fetch_factor_performance(limit=500):
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM factor_performance
           ORDER BY run_time DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return rows


def fetch_latest_factor_run():
    conn = get_conn()
    row = conn.execute(
        "SELECT DISTINCT run_time FROM factor_performance ORDER BY run_time DESC LIMIT 1"
    ).fetchone()
    return row["run_time"] if row else None


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
):
    conn = get_conn()
    conn.execute(
        """INSERT INTO orders
           (position_id, symbol, side, order_type, quantity, price, status, reason,
            strategy_source, signal_source, alpha_symbol, alpha_profile, alpha_entry_level,
            alpha_score, alpha_suggested_position_pct)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
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
        ),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_order_status(order_id, status):
    conn = get_conn()
    conn.execute(
        "UPDATE orders SET status = ? WHERE id = ?", (status, order_id)
    )
    conn.commit()


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
    conn.execute(
        """INSERT INTO fills
           (position_id, symbol, order_id, side, quantity, price, realized_pnl, fee, fee_asset, trade_id,
            strategy_source, signal_source, alpha_symbol, alpha_profile, alpha_entry_level,
            alpha_score, alpha_suggested_position_pct)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
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
    """批量写入 training_samples
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
    """回测后更新 training_samples 的未来收益字段
    updates: list of (return_6h, return_12h, return_24h, return_48h, max_drawdown, symbol, scan_id)
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
    """获取训练样本
    labeled_only=True 则只返回含 return_12h 标签的样本
    """
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


# ---- Symbol Snapshots（幸存者偏差修复）----

def insert_symbol_snapshot(rows):
    """批量写入或更新 symbol_snapshots
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
    """获取某日活跃的交易对列表"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT symbol FROM symbol_snapshots WHERE date = ? AND active = 1",
        (date_str,),
    ).fetchall()
    return {r["symbol"] for r in rows}


# ---- Order Book Depth (V4.0) ----

def insert_orderbook_snapshot(rows):
    """批量写入订单簿深度快照
    rows: list of (time, symbol, bid_depth, ask_depth, imbalance_ratio, top_bid_qty, top_ask_qty)
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
    """获取最近N小时的订单簿深度数据（用于计算大单因子）"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM orderbook_snapshots
           WHERE symbol = ? AND timestamp > datetime('now', ?, '+8 hours')
           ORDER BY timestamp DESC""",
        (symbol, f'-{hours} hours'),
    ).fetchall()
    return rows


def fetch_24h_quote_volume(symbol):
    """获取24h成交额（用于计算大单阈值）"""
    conn = get_conn()
    row = conn.execute(
        """SELECT quote_vol FROM candles_1h
           WHERE symbol = ? AND time > datetime('now', '-25 hours', '+8 hours')
           ORDER BY time DESC LIMIT 1""",
        (symbol,),
    ).fetchone()
    return float(row["quote_vol"]) if row else 0
