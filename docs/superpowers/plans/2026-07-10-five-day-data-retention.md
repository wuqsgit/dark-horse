# Five-Day Data Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delete regenerable data older than five days every day while permanently preserving trading accounting and current state.

**Architecture:** An explicit table-to-time-column allowlist in `shared.db` performs bounded deletes and WAL checkpointing. Engine owns one daily 03:30 cron job; collectors share the same five-day constant.

**Tech Stack:** Python, SQLite, APScheduler, unittest.

## Global Constraints

- Retention is exactly five days.
- Accounting and current-state tables are never part of the cleanup allowlist.
- Daily cleanup never runs VACUUM.
- Initial deployment runs one offline VACUUM without a backup.

### Task 1: Retention Function

**Files:**
- Modify: `shared/db.py`
- Create: `tests/test_data_retention.py`

- [ ] Write tests with expired/current rows in a disposable SQLite database.
- [ ] Verify tests fail because `cleanup_old_operational_data` is missing.
- [ ] Implement explicit allowlist, batched deletion, and per-table counts.
- [ ] Verify old operational rows are deleted and accounting rows remain.

### Task 2: Daily Scheduling And Collector Retention

**Files:**
- Modify: `engine/run.py`
- Modify: `pipeline/binance_http.py`
- Modify: `alpha_pipeline/main.py`
- Modify: `alpha_pipeline/collector.py`
- Create: `tests/test_retention_schedule.py`

- [ ] Write a failing test for the 03:30 schedule configuration.
- [ ] Add a single exported retention constant and daily maintenance callback.
- [ ] Change all candle cleanup calls from 90 days to five days.
- [ ] Run the complete test suite.

### Task 3: Live Cleanup And Verification

**Files:**
- Modify: `db/init.sql`

- [ ] Stop all DarkHorse service parent/child processes.
- [ ] Run five-day cleanup, WAL truncate, `PRAGMA integrity_check`, and offline `VACUUM` without backup.
- [ ] Export the resulting schema snapshot.
- [ ] Restart all seven services once.
- [ ] Verify 32+ tests, frontend build, ports 3000/8000, market readiness, logs, and reduced database/WAL sizes.
