-- Auto-generated SQLite schema snapshot for DarkHorse.
-- Generated from alphadog.db sqlite_master.

CREATE TABLE account_capital_adjustments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL REFERENCES trading_accounts(id),
            adjustment_type TEXT NOT NULL,
            amount REAL NOT NULL,
            effective_time TEXT NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        );

CREATE TABLE account_position_history(
  account_id,
  symbol TEXT,
  side TEXT,
  quantity REAL,
  entry_price REAL,
  entry_reason TEXT,
  entry_score REAL,
  entry_time TEXT,
  tp3_price REAL,
  atr_value REAL,
  update_time TEXT,
  tp1_hit INT,
  tp2_hit INT,
  highest_price REAL,
  last_exit_reason TEXT,
  position_id TEXT,
  strategy_source TEXT,
  signal_source TEXT,
  alpha_symbol TEXT,
  alpha_profile TEXT,
  alpha_entry_level TEXT,
  alpha_score REAL,
  alpha_suggested_position_pct REAL,
  roll_layer INT,
  last_roll_time TEXT,
  roll_parent_trade_id TEXT,
  protected_profit REAL,
  max_floating_pnl REAL,
  roll_enabled INT,
  roll_block_reason TEXT,
  lowest_price REAL,
  stop_model TEXT,
  initial_stop_loss REAL,
  stop_pct REAL,
  current_stop_loss REAL,
  trailing_stop_price REAL,
  trailing_enabled INT,
  trailing_atr_multiplier REAL,
  r_multiple REAL,
  alpha_volume_protect_regime TEXT,
  alpha_volume_protect_time TEXT,
  initial_quantity REAL,
  roll_price REAL,
  protected_stop REAL
);

CREATE TABLE alpha_candles_15m (
            time TEXT, alpha_symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, alpha_symbol)
        );

CREATE TABLE alpha_candles_1h (
            time TEXT, alpha_symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, alpha_symbol)
        );

CREATE TABLE alpha_candles_24h (
            time TEXT, alpha_symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, alpha_symbol)
        );

CREATE TABLE alpha_candles_6h (
            time TEXT, alpha_symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, alpha_symbol)
        );

CREATE TABLE alpha_cooldowns (
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

CREATE TABLE alpha_orderbook_snapshots (
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

CREATE TABLE alpha_scan_scores (
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
            raw_features TEXT, alpha_profile TEXT, entry_level TEXT, suggested_position_pct REAL DEFAULT 0, block_reasons TEXT, profile_thresholds TEXT,
            PRIMARY KEY (scan_id, alpha_symbol)
        );

CREATE TABLE alpha_scores (
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    composite_score REAL,
    composite_summary TEXT,
    risk_label TEXT,
    chip_phase TEXT,
    trend_state TEXT,
    trend_direction TEXT,
    volatility_level TEXT,
    price_position TEXT,
    relative_strength REAL,
    market_price REAL,
    raw_features TEXT,
    scan_id TEXT, entry_alpha REAL, hold_alpha REAL,
    UNIQUE(time, symbol)
);

CREATE TABLE alpha_symbols (
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

CREATE TABLE alpha_trade_candidates (
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
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')), volume_price_state TEXT, volume_price_action TEXT, volume_price_reasons_json TEXT, volume_price_metrics_json TEXT, volume_price_max_position_factor REAL,
            UNIQUE(scan_id, alpha_symbol)
        );

CREATE TABLE candles_15m (
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, quote_vol REAL, trades INTEGER,
    UNIQUE(time, symbol)
);

CREATE TABLE candles_1h (
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, quote_vol REAL, trades INTEGER,
    UNIQUE(time, symbol)
);

CREATE TABLE candles_24h (
    time TEXT, symbol TEXT,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, quote_vol REAL, trades INTEGER,
    PRIMARY KEY (time, symbol)
);

CREATE TABLE candles_6h (
    time TEXT, symbol TEXT,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, quote_vol REAL, trades INTEGER,
    PRIMARY KEY (time, symbol)
);

CREATE TABLE decision_actions (
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
        , account_id INTEGER NOT NULL DEFAULT 1);

CREATE TABLE decision_outcomes (
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
            trend_capture_ratio REAL,
            bars_observed INTEGER DEFAULT 0,
            is_complete INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT (datetime('now'))
        , churn_trade INTEGER DEFAULT 0, probe_failed INTEGER DEFAULT 0, weak_after_entry INTEGER DEFAULT 0, holding_minutes REAL);

CREATE TABLE exchange_income_ledger (
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
        , account_id INTEGER NOT NULL DEFAULT 1);

CREATE TABLE exit_review_summaries (
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

CREATE TABLE factor_effectiveness (
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

CREATE TABLE fills (
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
        , position_id TEXT, strategy_source TEXT DEFAULT 'normal', signal_source TEXT, alpha_symbol TEXT, alpha_profile TEXT, alpha_entry_level TEXT, alpha_score REAL, alpha_suggested_position_pct REAL, account_id INTEGER NOT NULL DEFAULT 1);

CREATE TABLE futures_candles_15m (
            time TEXT, symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, symbol)
        );

CREATE TABLE futures_candles_1h (
            time TEXT, symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, symbol)
        );

CREATE TABLE futures_candles_24h (
            time TEXT, symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, symbol)
        );

CREATE TABLE futures_candles_6h (
            time TEXT, symbol TEXT,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, quote_vol REAL, trades INTEGER,
            PRIMARY KEY (time, symbol)
        );

CREATE TABLE futures_data (
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open_interest REAL,
    funding_rate REAL,
    mark_price REAL,
    UNIQUE(time, symbol)
);

CREATE TABLE market_universe (
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

CREATE TABLE onchain_flows (
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    chain TEXT,
    cex_inflow_usd REAL,
    cex_outflow_usd REAL,
    cex_net_flow_usd REAL,
    cex_net_flow_14d_usd REAL,
    cex_net_outflow_ratio REAL,
    window_hours INTEGER DEFAULT 24,
    UNIQUE(time, symbol)
);

CREATE TABLE orderbook_depth (
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

CREATE TABLE orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            symbol TEXT,
            bid_depth REAL,
            ask_depth REAL,
            imbalance_ratio REAL,
            top_bid_qty REAL,
            top_ask_qty REAL
        );

CREATE TABLE orders (
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
        , source TEXT DEFAULT 'system', position_id TEXT, strategy_source TEXT DEFAULT 'normal', signal_source TEXT, alpha_symbol TEXT, alpha_profile TEXT, alpha_entry_level TEXT, alpha_score REAL, alpha_suggested_position_pct REAL, account_id INTEGER NOT NULL DEFAULT 1);

CREATE TABLE policy_experiments (
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

CREATE TABLE policy_reviews (
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
            bad_block_count INTEGER DEFAULT 0,
            good_block_count INTEGER DEFAULT 0,
            diagnosis TEXT,
            recommendation_json TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        , churn_trade_count INTEGER DEFAULT 0, probe_failed_count INTEGER DEFAULT 0, weak_after_entry_count INTEGER DEFAULT 0);

CREATE TABLE policy_versions (
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

CREATE TABLE position_history (
    symbol TEXT PRIMARY KEY,
    side TEXT, quantity REAL,
    entry_price REAL, entry_reason TEXT,
    entry_score REAL, entry_time TEXT,
    tp3_price REAL, atr_value REAL,
    update_time TEXT DEFAULT (datetime('now'))
, tp1_hit INTEGER DEFAULT 0, tp2_hit INTEGER DEFAULT 0, highest_price REAL, last_exit_reason TEXT, position_id TEXT, strategy_source TEXT DEFAULT 'normal', signal_source TEXT, alpha_symbol TEXT, alpha_profile TEXT, alpha_entry_level TEXT, alpha_score REAL, alpha_suggested_position_pct REAL, roll_layer INTEGER DEFAULT 0, last_roll_time TEXT, roll_parent_trade_id TEXT, protected_profit REAL DEFAULT 0, max_floating_pnl REAL DEFAULT 0, roll_enabled INTEGER DEFAULT 0, roll_block_reason TEXT, lowest_price REAL, stop_model TEXT, initial_stop_loss REAL, stop_pct REAL, current_stop_loss REAL, trailing_stop_price REAL, trailing_enabled INTEGER DEFAULT 0, trailing_atr_multiplier REAL, r_multiple REAL DEFAULT 0, alpha_volume_protect_regime TEXT, alpha_volume_protect_time TEXT, initial_quantity REAL, roll_price REAL, protected_stop REAL);

CREATE TABLE position_roll_events (
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
        , account_id INTEGER NOT NULL DEFAULT 1);

CREATE TABLE position_trades (
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
            source TEXT DEFAULT 'reconstructed',
            reconcile_status TEXT DEFAULT 'ok',
            raw_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        , account_id INTEGER NOT NULL DEFAULT 1, grade_at_entry TEXT, score_at_entry REAL);

CREATE TABLE positions_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL,
    entry_price REAL,
    current_price REAL,
    unrealized_pnl REAL,
    stop_loss REAL,
    take_profit REAL
, position_side TEXT, mark_price REAL, leverage INTEGER DEFAULT 1, account_id INTEGER NOT NULL DEFAULT 1);

CREATE TABLE shadow_decisions (
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

CREATE TABLE signal_outcomes (
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

CREATE TABLE strategy_decisions (
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
        , account_id INTEGER NOT NULL DEFAULT 1);

CREATE TABLE strategy_policy_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT (datetime('now')),
            candidate_id INTEGER,
            action TEXT,
            old_status TEXT,
            new_status TEXT,
            detail_json TEXT,
            FOREIGN KEY(candidate_id) REFERENCES strategy_policy_candidates(id)
        );

CREATE TABLE strategy_policy_candidates (
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
            rollback_condition_json TEXT, dedupe_key TEXT,
            UNIQUE(source_type, source_run_time, target, action, title)
        );

CREATE TABLE symbol_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            status TEXT,
            quote_volume REAL,
            price_change_24h REAL,
            active BOOLEAN DEFAULT 1,
            UNIQUE(date, symbol)
        );

CREATE TABLE symbols (
    symbol TEXT PRIMARY KEY,
    base_asset TEXT,
    is_active INTEGER DEFAULT 1,
    first_seen TEXT DEFAULT (datetime('now')),
    last_seen TEXT DEFAULT (datetime('now'))
);

CREATE TABLE trade_cooldown (
            symbol TEXT PRIMARY KEY,
            last_stop_time TEXT,
            stop_count_24h INTEGER DEFAULT 0,
            consecutive_stops INTEGER DEFAULT 0,
            cooldown_until TEXT,
            reason TEXT,
            updated_at TEXT
        , created_at TEXT);

CREATE TABLE trade_entry_reviews (
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
        , account_id INTEGER NOT NULL DEFAULT 1);

CREATE TABLE trade_exit_reviews (
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
        , account_id INTEGER NOT NULL DEFAULT 1);

CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL,
    entry_price REAL,
    exit_price REAL,
    pnl REAL,
    pnl_pct REAL,
    exit_reason TEXT,
    entry_time TEXT,
    exit_time TEXT,
    grade_at_entry TEXT,
    score_at_entry REAL,
    created_at TEXT DEFAULT (datetime('now'))
, source TEXT DEFAULT 'system', income_id TEXT, entry_reason TEXT, position_id TEXT, strategy_source TEXT DEFAULT 'normal', signal_source TEXT, alpha_symbol TEXT, alpha_profile TEXT, alpha_entry_level TEXT, alpha_score REAL, alpha_suggested_position_pct REAL, account_id INTEGER NOT NULL DEFAULT 1, position_side TEXT);

CREATE TABLE trades_paginated (
    page INTEGER,
    page_row INTEGER,
    trade_id INTEGER,
    symbol TEXT,
    side TEXT,
    quantity REAL,
    entry_price REAL,
    exit_price REAL,
    pnl REAL,
    pnl_pct REAL,
    exit_reason TEXT,
    entry_time TEXT,
    exit_time TEXT,
    grade_at_entry TEXT,
    score_at_entry REAL,
    source TEXT
);

CREATE TABLE trading_accounts (
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

CREATE TABLE trading_runtime_controls (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT (datetime('now'))
        );

CREATE TABLE training_samples (
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

CREATE TABLE user_favorites (
    user_id INTEGER REFERENCES users(id),
    symbol TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, symbol)
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'viewer',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX idx_account_adjustments ON account_capital_adjustments(account_id, effective_time DESC);

CREATE UNIQUE INDEX idx_account_position_history_key ON account_position_history(account_id, symbol);

CREATE INDEX idx_alpha_c15m_sym_time ON alpha_candles_15m(alpha_symbol, time DESC);

CREATE INDEX idx_alpha_c15m_time_symbol ON alpha_candles_15m(time DESC, alpha_symbol);

CREATE INDEX idx_alpha_c1h_sym_time ON alpha_candles_1h(alpha_symbol, time DESC);

CREATE INDEX idx_alpha_c1h_time_symbol ON alpha_candles_1h(time DESC, alpha_symbol);

CREATE INDEX idx_alpha_c24h_sym_time ON alpha_candles_24h(alpha_symbol, time DESC);

CREATE INDEX idx_alpha_c24h_time_symbol ON alpha_candles_24h(time DESC, alpha_symbol);

CREATE INDEX idx_alpha_c6h_sym_time ON alpha_candles_6h(alpha_symbol, time DESC);

CREATE INDEX idx_alpha_c6h_time_symbol ON alpha_candles_6h(time DESC, alpha_symbol);

CREATE INDEX idx_alpha_cooldowns_source_until ON alpha_cooldowns(source, cooldown_until);

CREATE INDEX idx_alpha_cooldowns_symbol ON alpha_cooldowns(symbol);

CREATE INDEX idx_alpha_cooldowns_until ON alpha_cooldowns(cooldown_until);

CREATE INDEX idx_alpha_ob_symbol ON alpha_orderbook_snapshots(alpha_symbol);

CREATE INDEX idx_alpha_ob_symbol_time ON alpha_orderbook_snapshots(alpha_symbol, timestamp DESC);

CREATE INDEX idx_alpha_ob_time ON alpha_orderbook_snapshots(timestamp DESC);

CREATE INDEX idx_alpha_scan_scores_entry_time ON alpha_scan_scores(entry_level, time DESC);

CREATE INDEX idx_alpha_scan_scores_futures ON alpha_scan_scores(futures_symbol);

CREATE INDEX idx_alpha_scan_scores_profile_time ON alpha_scan_scores(alpha_profile, time DESC);

CREATE INDEX idx_alpha_scan_scores_scan ON alpha_scan_scores(scan_id);

CREATE INDEX idx_alpha_scan_scores_scan_score ON alpha_scan_scores(scan_id, discovery_score DESC);

CREATE INDEX idx_alpha_scan_scores_symbol ON alpha_scan_scores(alpha_symbol);

CREATE INDEX idx_alpha_scan_scores_symbol_time ON alpha_scan_scores(alpha_symbol, time DESC);

CREATE INDEX idx_alpha_scan_scores_time ON alpha_scan_scores(time DESC);

CREATE INDEX idx_alpha_scores_grade_time ON alpha_scores(composite_summary, time DESC);

CREATE INDEX idx_alpha_scores_scan ON alpha_scores(scan_id);

CREATE INDEX idx_alpha_scores_scan_score ON alpha_scores(scan_id, composite_score DESC);

CREATE INDEX idx_alpha_scores_symbol_time_desc ON alpha_scores(symbol, time DESC);

CREATE INDEX idx_alpha_scores_time ON alpha_scores(time);

CREATE INDEX idx_alpha_scores_time_score ON alpha_scores(time DESC, composite_score DESC);

CREATE INDEX idx_alpha_symbols_base ON alpha_symbols(base_asset);

CREATE INDEX idx_alpha_symbols_futures ON alpha_symbols(futures_symbol);

CREATE INDEX idx_alpha_symbols_last_seen ON alpha_symbols(last_seen DESC);

CREATE INDEX idx_alpha_symbols_tradeability ON alpha_symbols(tradeability);

CREATE INDEX idx_alpha_symbols_volume ON alpha_symbols(volume_24h DESC);

CREATE INDEX idx_alpha_trade_candidates_futures ON alpha_trade_candidates(futures_symbol);

CREATE INDEX idx_alpha_trade_candidates_profile ON alpha_trade_candidates(alpha_profile, time DESC);

CREATE INDEX idx_alpha_trade_candidates_scan_score ON alpha_trade_candidates(scan_id, alpha_discovery_score DESC);

CREATE INDEX idx_alpha_trade_candidates_status_time ON alpha_trade_candidates(entry_status, time DESC);

CREATE INDEX idx_alpha_trade_candidates_symbol ON alpha_trade_candidates(alpha_symbol);

CREATE INDEX idx_alpha_trade_candidates_symbol_time ON alpha_trade_candidates(alpha_symbol, time DESC, updated_at DESC, id DESC);

CREATE INDEX idx_alpha_trade_candidates_time ON alpha_trade_candidates(time DESC);

CREATE INDEX idx_alpha_trade_candidates_updated ON alpha_trade_candidates(updated_at DESC);

CREATE INDEX idx_alpha_trade_candidates_vp_state ON alpha_trade_candidates(volume_price_state, time DESC);

CREATE INDEX idx_as_scan ON alpha_scores(scan_id);

CREATE INDEX idx_as_score ON alpha_scores(composite_score DESC);

CREATE INDEX idx_as_sym ON alpha_scores(symbol, time);

CREATE INDEX idx_c15m_sym ON candles_15m(symbol, time);

CREATE INDEX idx_c15m_time_symbol ON candles_15m(time DESC, symbol);

CREATE INDEX idx_c1h_sym ON candles_1h(symbol, time);

CREATE INDEX idx_c1h_time_symbol ON candles_1h(time DESC, symbol);

CREATE INDEX idx_c24h_sym_time ON candles_24h(symbol, time DESC);

CREATE INDEX idx_c24h_time_symbol ON candles_24h(time DESC, symbol);

CREATE INDEX idx_c6h_sym_time ON candles_6h(symbol, time DESC);

CREATE INDEX idx_c6h_time_symbol ON candles_6h(time DESC, symbol);

CREATE INDEX idx_decision_actions_category ON decision_actions(category, time DESC);

CREATE INDEX idx_decision_actions_symbol ON decision_actions(symbol, time DESC);

CREATE INDEX idx_decision_actions_time ON decision_actions(time DESC);

CREATE INDEX idx_decision_actions_type ON decision_actions(action_type, action_result, time DESC);

CREATE INDEX idx_decision_outcomes_category ON decision_outcomes(category, signal_time DESC);

CREATE INDEX idx_decision_outcomes_flags ON decision_outcomes(missed_big_move, early_exit, bad_block);

CREATE INDEX idx_decision_outcomes_symbol ON decision_outcomes(symbol, signal_time DESC);

CREATE INDEX idx_exit_review_summaries_reason ON exit_review_summaries(exit_reason, run_time DESC);

CREATE INDEX idx_exit_review_summaries_run ON exit_review_summaries(run_time DESC);

CREATE INDEX idx_factor_effectiveness_bucket ON factor_effectiveness(bucket, run_time DESC);

CREATE INDEX idx_factor_effectiveness_factor ON factor_effectiveness(factor_name, layer, profile);

CREATE INDEX idx_factor_effectiveness_run ON factor_effectiveness(run_time DESC);

CREATE INDEX idx_fc15m_sym_time ON futures_candles_15m(symbol, time DESC);

CREATE INDEX idx_fc1h_sym_time ON futures_candles_1h(symbol, time DESC);

CREATE INDEX idx_fc24h_sym_time ON futures_candles_24h(symbol, time DESC);

CREATE INDEX idx_fc6h_sym_time ON futures_candles_6h(symbol, time DESC);

CREATE INDEX idx_fills_account_time ON fills(account_id, created_at DESC);

CREATE INDEX idx_fills_alpha_symbol ON fills(alpha_symbol, created_at DESC);

CREATE INDEX idx_fills_order_id ON fills(order_id);

CREATE INDEX idx_fills_position_id ON fills(position_id);

CREATE INDEX idx_fills_symbol_created ON fills(symbol, created_at DESC);

CREATE UNIQUE INDEX idx_fills_trade_id ON fills(trade_id);

CREATE INDEX idx_fut_sym ON futures_data(symbol, time);

CREATE INDEX idx_fut_time_symbol ON futures_data(time DESC, symbol);

CREATE INDEX idx_income_ledger_symbol ON exchange_income_ledger(symbol, income_time DESC);

CREATE INDEX idx_income_ledger_time ON exchange_income_ledger(income_time DESC);

CREATE INDEX idx_income_ledger_type ON exchange_income_ledger(income_type, income_time DESC);

CREATE INDEX idx_market_universe_futures ON market_universe(futures_symbol);

CREATE INDEX idx_market_universe_pool_ready ON market_universe(pool_type, selected, data_ready, universe_rank);

CREATE INDEX idx_ob_symbol ON orderbook_snapshots(symbol);

CREATE INDEX idx_ob_symbol_timestamp ON orderbook_snapshots(symbol, timestamp DESC);

CREATE INDEX idx_ob_timestamp ON orderbook_snapshots(timestamp);

CREATE INDEX idx_oc_chain_time ON onchain_flows(chain, time DESC);

CREATE INDEX idx_oc_sym ON onchain_flows(symbol, time);

CREATE INDEX idx_oc_time_symbol ON onchain_flows(time DESC, symbol);

CREATE INDEX idx_orderbook_depth_symbol_time ON orderbook_depth(symbol, time DESC);

CREATE INDEX idx_orderbook_depth_time_symbol ON orderbook_depth(time DESC, symbol);

CREATE INDEX idx_orders_account_time ON orders(account_id, created_at DESC);

CREATE INDEX idx_orders_alpha_symbol ON orders(alpha_symbol, created_at DESC);

CREATE INDEX idx_orders_position_id ON orders(position_id);

CREATE INDEX idx_orders_status_created ON orders(status, created_at DESC);

CREATE INDEX idx_orders_symbol_created ON orders(symbol, created_at DESC);

CREATE INDEX idx_policy_audit_candidate ON strategy_policy_audit(candidate_id);

CREATE INDEX idx_policy_audit_created ON strategy_policy_audit(created_at DESC);

CREATE INDEX idx_policy_candidates_created ON strategy_policy_candidates(created_at DESC);

CREATE UNIQUE INDEX idx_policy_candidates_dedupe ON strategy_policy_candidates(dedupe_key) WHERE dedupe_key IS NOT NULL;

CREATE INDEX idx_policy_candidates_status ON strategy_policy_candidates(status);

CREATE INDEX idx_policy_candidates_target_status ON strategy_policy_candidates(target, status);

CREATE INDEX idx_policy_experiments_version ON policy_experiments(policy_version, created_at DESC);

CREATE INDEX idx_policy_reviews_category ON policy_reviews(category, target_type, run_time DESC);

CREATE INDEX idx_policy_reviews_run ON policy_reviews(run_time DESC);

CREATE INDEX idx_policy_versions_status ON policy_versions(status, category, target_type);

CREATE INDEX idx_position_history_alpha ON position_history(alpha_symbol);

CREATE INDEX idx_position_history_strategy ON position_history(strategy_source);

CREATE INDEX idx_position_history_update ON position_history(update_time DESC);

CREATE INDEX idx_position_roll_events_created ON position_roll_events(created_at DESC);

CREATE INDEX idx_position_roll_events_position ON position_roll_events(position_id, created_at DESC);

CREATE INDEX idx_position_roll_events_symbol ON position_roll_events(symbol, created_at DESC);

CREATE INDEX idx_position_trades_account_exit ON position_trades(account_id, exit_time DESC);

CREATE INDEX idx_position_trades_exit ON position_trades(exit_time DESC);

CREATE INDEX idx_position_trades_source ON position_trades(source, exit_time DESC);

CREATE INDEX idx_position_trades_symbol ON position_trades(symbol, exit_time DESC);

CREATE INDEX idx_positions_history_account_time ON positions_history(account_id, time DESC);

CREATE INDEX idx_positions_symbol ON positions_history(symbol);

CREATE INDEX idx_positions_symbol_time ON positions_history(symbol, time DESC);

CREATE INDEX idx_positions_time ON positions_history(time);

CREATE INDEX idx_shadow_candidate ON shadow_decisions(candidate_id);

CREATE INDEX idx_shadow_created ON shadow_decisions(created_at DESC);

CREATE INDEX idx_shadow_symbol_created ON shadow_decisions(symbol, created_at DESC);

CREATE INDEX idx_signal_outcomes_complete ON signal_outcomes(is_complete);

CREATE INDEX idx_signal_outcomes_complete_time ON signal_outcomes(is_complete, signal_time DESC);

CREATE UNIQUE INDEX idx_signal_outcomes_decision ON signal_outcomes(decision_id);

CREATE INDEX idx_signal_outcomes_run ON signal_outcomes(run_id);

CREATE INDEX idx_signal_outcomes_run_complete ON signal_outcomes(run_id, is_complete);

CREATE INDEX idx_signal_outcomes_run_side ON signal_outcomes(run_id, best_side);

CREATE INDEX idx_signal_outcomes_scan ON signal_outcomes(scan_id);

CREATE INDEX idx_signal_outcomes_symbol ON signal_outcomes(symbol);

CREATE INDEX idx_signal_outcomes_symbol_time ON signal_outcomes(symbol, signal_time DESC);

CREATE INDEX idx_strategy_decisions_created ON strategy_decisions(created_at DESC);

CREATE INDEX idx_strategy_decisions_result_time ON strategy_decisions(decision_result, time DESC);

CREATE INDEX idx_strategy_decisions_run ON strategy_decisions(run_id);

CREATE INDEX idx_strategy_decisions_run_filter ON strategy_decisions(run_id, filter_reason);

CREATE INDEX idx_strategy_decisions_run_result ON strategy_decisions(run_id, decision_result);

CREATE INDEX idx_strategy_decisions_run_stage ON strategy_decisions(run_id, decision_stage);

CREATE INDEX idx_strategy_decisions_run_symbol ON strategy_decisions(run_id, symbol);

CREATE INDEX idx_strategy_decisions_scan ON strategy_decisions(scan_id);

CREATE INDEX idx_strategy_decisions_stage_time ON strategy_decisions(decision_stage, time DESC);

CREATE INDEX idx_strategy_decisions_symbol ON strategy_decisions(symbol);

CREATE INDEX idx_strategy_decisions_symbol_time ON strategy_decisions(symbol, time DESC);

CREATE INDEX idx_strategy_decisions_time ON strategy_decisions(time);

CREATE INDEX idx_strategy_decisions_time_id ON strategy_decisions(time DESC, id DESC);

CREATE INDEX idx_symbol_snapshots_active_volume ON symbol_snapshots(active, quote_volume DESC);

CREATE INDEX idx_symbol_snapshots_symbol_date ON symbol_snapshots(symbol, date DESC);

CREATE INDEX idx_symbols_active_last_seen ON symbols(is_active, last_seen DESC);

CREATE INDEX idx_trade_cooldown_until_symbol ON trade_cooldown(cooldown_until, symbol);

CREATE INDEX idx_trade_entry_reviews_group ON trade_entry_reviews(strategy_source, category, entry_template, review_label);

CREATE INDEX idx_trade_entry_reviews_status ON trade_entry_reviews(position_status, reviewed_at);

CREATE INDEX idx_trade_entry_reviews_time ON trade_entry_reviews(entry_time DESC);

CREATE INDEX idx_trade_exit_reviews_exit ON trade_exit_reviews(exit_time DESC);

CREATE INDEX idx_trade_exit_reviews_label ON trade_exit_reviews(review_label, exit_time DESC);

CREATE INDEX idx_trade_exit_reviews_reason ON trade_exit_reviews(exit_reason, exit_time DESC);

CREATE INDEX idx_trades_alpha_symbol ON trades(alpha_symbol, created_at DESC);

CREATE INDEX idx_trades_created ON trades(created_at DESC);

CREATE INDEX idx_trades_exit_time ON trades(exit_time);

CREATE INDEX idx_trades_position_id ON trades(position_id);

CREATE INDEX idx_trades_source_created ON trades(source, created_at DESC);

CREATE INDEX idx_trades_strategy_created ON trades(strategy_source, created_at DESC);

CREATE INDEX idx_trades_symbol ON trades(symbol);

CREATE INDEX idx_trades_symbol_created ON trades(symbol, created_at DESC);

CREATE INDEX idx_trading_runtime_updated ON trading_runtime_controls(updated_at DESC);

CREATE INDEX idx_train_scan ON training_samples(scan_id);

CREATE INDEX idx_train_sym_time ON training_samples(symbol, timestamp);

CREATE INDEX idx_user_favorites_symbol ON user_favorites(symbol);
