# Five-Day Data Retention Design

## Goal

Keep regenerable market and decision data for five days, run cleanup once per day at 03:30 Asia/Shanghai, and preserve all accounting and current-state data.

## Retained For Five Days

- Spot, Alpha spot, and futures candle tables.
- Futures snapshots, on-chain flows, order-book snapshots, and symbol snapshots.
- Normal and Alpha score snapshots, Alpha candidates, training samples.
- Strategy decisions, decision actions, outcomes, policy reviews, and exit-review summaries.

Each table is listed explicitly with its timestamp column. Cleanup never discovers tables dynamically.

## Permanently Protected

- `trades`, `position_trades`, `orders`, `fills`, and `exchange_income_ledger`.
- `position_history`, `positions_history`, and `position_roll_events`.
- Users, favorites, symbols, current market universe, runtime controls, cooldowns, policy versions, active policy candidates, and configuration.

## Execution

- `shared.db.cleanup_old_operational_data(retention_days=5)` deletes expired rows in bounded batches and returns per-table counts.
- Engine schedules the cleanup with an APScheduler cron job at 03:30 local time.
- Candle collectors also use the same five-day retention constant.
- Cleanup finishes with `PRAGMA wal_checkpoint(TRUNCATE)` after write transactions close.
- Errors are logged per run and do not stop scoring or trading services.

## Initial Maintenance

After deployment, stop all DarkHorse writers, run cleanup immediately, checkpoint WAL, run `VACUUM` once without creating a backup, then restart all services. Daily cleanup does not run `VACUUM`; SQLite reuses freed pages and avoids a daily exclusive lock.

## Verification

- Tests prove rows older than five days are deleted from allowlisted tables.
- Tests prove accounting and current-state tables remain unchanged.
- Verify the daily scheduler is registered once.
- Verify database integrity, file sizes, service health, and market readiness after restart.
