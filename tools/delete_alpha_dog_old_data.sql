-- Delete alpha-dog / alphadog.db operational, test, and historical records older than 5 days.
-- Generated from the live SQLite schema in alphadog.db.
--
-- Review before running. This intentionally avoids current-state/config tables such as
-- users, trading_accounts, trading_runtime_controls, symbols, alpha_symbols,
-- market_universe, position_history, account_position_history, and cooldown tables.

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- Market candles and regenerable market data.
DELETE FROM "candles_15m" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "candles_1h" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "candles_6h" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "candles_24h" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "alpha_candles_15m" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "alpha_candles_1h" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "alpha_candles_6h" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "alpha_candles_24h" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "futures_candles_15m" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "futures_candles_1h" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "futures_candles_6h" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "futures_candles_24h" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "futures_data" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "onchain_flows" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "orderbook_depth" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "orderbook_snapshots" WHERE "timestamp" IS NOT NULL AND julianday("timestamp") < julianday('now', '-5 days');
DELETE FROM "alpha_orderbook_snapshots" WHERE "timestamp" IS NOT NULL AND julianday("timestamp") < julianday('now', '-5 days');
DELETE FROM "symbol_snapshots" WHERE "date" IS NOT NULL AND julianday("date") < julianday('now', '-5 days');

-- Scan, score, model, and review records.
DELETE FROM "alpha_scores" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "alpha_scan_scores" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "alpha_trade_candidates" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "training_samples" WHERE "timestamp" IS NOT NULL AND julianday("timestamp") < julianday('now', '-5 days');
DELETE FROM "strategy_decisions" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "decision_actions" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "decision_outcomes" WHERE "signal_time" IS NOT NULL AND julianday("signal_time") < julianday('now', '-5 days');
DELETE FROM "signal_outcomes" WHERE "signal_time" IS NOT NULL AND julianday("signal_time") < julianday('now', '-5 days');
DELETE FROM "policy_reviews" WHERE "run_time" IS NOT NULL AND julianday("run_time") < julianday('now', '-5 days');
DELETE FROM "exit_review_summaries" WHERE "run_time" IS NOT NULL AND julianday("run_time") < julianday('now', '-5 days');
DELETE FROM "factor_effectiveness" WHERE "run_time" IS NOT NULL AND julianday("run_time") < julianday('now', '-5 days');
DELETE FROM "shadow_decisions" WHERE "created_at" IS NOT NULL AND julianday("created_at") < julianday('now', '-5 days');
DELETE FROM "strategy_policy_audit" WHERE "created_at" IS NOT NULL AND julianday("created_at") < julianday('now', '-5 days');

-- Historical trade/accounting records. These use close/settlement time where available.
DELETE FROM "positions_history" WHERE "time" IS NOT NULL AND julianday("time") < julianday('now', '-5 days');
DELETE FROM "position_trades" WHERE "exit_time" IS NOT NULL AND julianday("exit_time") < julianday('now', '-5 days');
DELETE FROM "trade_entry_reviews" WHERE "exit_time" IS NOT NULL AND julianday("exit_time") < julianday('now', '-5 days');
DELETE FROM "trade_exit_reviews" WHERE "exit_time" IS NOT NULL AND julianday("exit_time") < julianday('now', '-5 days');
DELETE FROM "trades" WHERE "exit_time" IS NOT NULL AND julianday("exit_time") < julianday('now', '-5 days');
DELETE FROM "trades_paginated" WHERE "exit_time" IS NOT NULL AND julianday("exit_time") < julianday('now', '-5 days');
DELETE FROM "exchange_income_ledger" WHERE "income_time" IS NOT NULL AND julianday("income_time") < julianday('now', '-5 days');
DELETE FROM "fills" WHERE "created_at" IS NOT NULL AND julianday("created_at") < julianday('now', '-5 days');
DELETE FROM "orders" WHERE "created_at" IS NOT NULL AND julianday("created_at") < julianday('now', '-5 days');
DELETE FROM "position_roll_events" WHERE "created_at" IS NOT NULL AND julianday("created_at") < julianday('now', '-5 days');

-- Policy experiment/audit history.
DELETE FROM "policy_experiments" WHERE "created_at" IS NOT NULL AND julianday("created_at") < julianday('now', '-5 days');
DELETE FROM "strategy_policy_candidates" WHERE "created_at" IS NOT NULL AND julianday("created_at") < julianday('now', '-5 days');

COMMIT;
PRAGMA foreign_keys = ON;
PRAGMA wal_checkpoint(TRUNCATE);
