# Live Account Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist exchange balances, positions, and open orders continuously and make the live-account API read those current-state tables.

**Architecture:** A new multi-account worker combines Binance Futures User Data Stream notifications with a ten-second signed HTTP reconciliation. Full reconciliations replace account current state transactionally; API reads are bounded by account and current positions only.

**Tech Stack:** Python 3.10+, asyncio, httpx, websockets, FastAPI, SQLite, unittest/pytest

**Spec:** `docs/superpowers/specs/2026-08-29-live-account-stream.md`

## Global Constraints

- `trader` remains the only order-submission service.
- Preserve the `/api/trading/accounts/status` response contract.
- Use one transaction per completed account reconciliation.
- HTTP reconciliation interval defaults to 10 seconds.
- API freshness target is 1-2 seconds after a WebSocket account event.

---

### Task 1: Current-State Storage

**Files:**
- Modify: `shared/db.py`
- Modify: `db/init.sql`
- Create: `shared/live_account_store.py`
- Test: `tests/test_live_account_store.py`

**Interfaces:**
- Produces: `replace_live_account_snapshot(account_id, balance, positions, orders, *, source, exchange_event_time)` and `fetch_live_account_snapshot(account_id)`.

- [ ] Write tests proving full snapshots are atomic, missing positions/orders are removed, and account rows remain isolated.
- [ ] Run the tests and verify they fail because the storage API and tables do not exist.
- [ ] Add the four tables, indexes, normalization, transactional replacement, and bounded reads.
- [ ] Run storage and schema tests until they pass.

### Task 2: Binance Account Synchronizer

**Files:**
- Create: `account_stream/__init__.py`
- Create: `account_stream/binance.py`
- Create: `account_stream/service.py`
- Create: `account_stream/main.py`
- Modify: `trader/exchange.py`
- Test: `tests/test_account_stream.py`

**Interfaces:**
- Consumes: `replace_live_account_snapshot(...)`.
- Produces: `AccountSynchronizer.run()` and `BinanceUserStreamClient` listen-key lifecycle methods.

- [ ] Write tests for payload normalization, periodic reconcile, event-triggered reconcile, listen-key expiry, and account isolation.
- [ ] Run the tests and verify expected missing-feature failures.
- [ ] Implement one-request account snapshot reuse, open-order loading, listen-key lifecycle, reconnect/backoff, and per-account orchestration.
- [ ] Run synchronizer tests until they pass.

### Task 3: Read-Only Live Account API

**Files:**
- Modify: `api/main.py`
- Test: `tests/test_trading_api_split.py`

**Interfaces:**
- Consumes: `fetch_live_account_snapshot(account_id)`.
- Preserves: `GET /api/trading/accounts/status` response fields and stale metadata.

- [ ] Write tests proving status reads do not instantiate `BinanceFutures` and do not execute the historical `strategy_decisions GROUP BY symbol` query.
- [ ] Run those tests and verify they fail against the request-time refresh implementation.
- [ ] Replace request-time exchange refresh with current-state reads and bounded local enrichment.
- [ ] Run API split tests until they pass.

### Task 4: Service Lifecycle and Regression

**Files:**
- Modify: `start.sh`
- Modify: `tests/test_start_script.py`

**Interfaces:**
- Produces: managed `Account Stream` process with PID/log files under the existing runtime directory.

- [ ] Add a failing start-script assertion for `Account Stream` stop/start coverage.
- [ ] Add the worker to restart order after API initialization and before Trader.
- [ ] Run focused backend tests, full backend tests, frontend build, and Bash syntax validation.
- [ ] Review the final diff for secret leakage and accidental unrelated changes.
