# Simple Protected Roll Trading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one 25% winner-only roll with full-position break-even protection and automatic cleanup of economically meaningless residual positions.

**Architecture:** Pure helpers calculate roll eligibility, R, add quantity, protection price, and residual status. `ExecutionEngine` uses those helpers to plan one roll, then verifies exchange fills and protection before persisting success. Existing position state remains authoritative; incomplete legacy state is never reconstructed.

**Tech Stack:** Python, SQLite, Binance Futures HTTP adapter, unittest, React.

## Global Constraints

- One roll layer, equal to 25% of initial quantity.
- TP1 must be exchange-confirmed and current profit must be at least 1.5R.
- Existing leverage is unchanged; cross margin is never adjusted.
- Full-position protection must succeed before roll success is persisted.
- Incomplete legacy state cannot roll and is not reconstructed.
- Remaining margin below 5 USDT or notional below 1.5 times exchange minimum is fully closed.

### Task 1: Pure Roll And Residual Rules

**Files:**
- Create: `trader/roll_policy.py`
- Modify: `trader/config.py`
- Create: `tests/test_roll_policy.py`

**Interfaces:**
- Produces `evaluate_roll(position, state, technical, alpha_sync, config) -> RollDecision`.
- Produces `calculate_roll_quantity(initial_quantity, exchange_info, config) -> float`.
- Produces `calculate_protected_stop(side, blended_entry, config) -> float`.
- Produces `is_residual_position(quantity, mark_price, leverage, exchange_info, config) -> bool`.

- [ ] Write failing tests proving incomplete state, missing TP1, less than 1.5R, trend mismatch, and Alpha desynchronization are blocked.
- [ ] Run `python -m unittest tests.test_roll_policy -v` and confirm missing-module failure.
- [ ] Implement immutable decision/result types and minimal calculations.
- [ ] Add the single-layer configuration values and remove multi-layer behavior from active config.
- [ ] Run Task 1 tests and confirm they pass.

### Task 2: Persist Initial State And Plan One Roll

**Files:**
- Modify: `shared/db.py`
- Modify: `trader/risk.py`
- Modify: `trader/execution.py`
- Create: `tests/test_simple_roll_planning.py`

**Interfaces:**
- Adds `initial_quantity` and `protected_stop` to `position_history`.
- New positions persist initial quantity, stop, ATR, and TP1 state once; current quantity updates never overwrite initial quantity.

- [ ] Write failing tests for initial state persistence and exactly one 25% roll action.
- [ ] Run the tests and confirm missing columns/behavior failures.
- [ ] Add idempotent schema migration and DB write/read support.
- [ ] Replace `_build_roll_actions` multi-layer checks with `evaluate_roll` and initial-quantity sizing.
- [ ] Ensure incomplete state records `roll_state_incomplete` without reconstruction.
- [ ] Run focused and full tests.

### Task 3: Exchange-Verified Full-Position Protection

**Files:**
- Modify: `trader/exchange.py`
- Modify: `trader/execution.py`
- Create: `tests/test_roll_execution_protection.py`

**Interfaces:**
- Adds exchange helpers for minimum notional, open protective stops, stop creation, and cancellation.
- Roll success requires add fill, refreshed exchange position, and accepted full-quantity protection stop.

- [ ] Write failing tests for unchanged leverage, full-quantity protection, success persistence order, and add-on unwind after protection failure.
- [ ] Run tests and confirm current `_execute_roll_add` violates the assertions.
- [ ] Implement protection replacement while retaining the old stop until the new stop is accepted.
- [ ] Remove `set_leverage` from roll execution.
- [ ] On protection failure, reduce the confirmed add-on and record `roll_protection_failed`.
- [ ] Run focused and full tests.

### Task 4: Residual Cleanup And Visibility

**Files:**
- Modify: `trader/execution.py`
- Modify: `api/main.py`
- Modify: `frontend/src/components/LiveTrading.jsx`
- Create: `tests/test_residual_position_cleanup.py`

**Interfaces:**
- Partial-close planning/execution converts or follows with full close when the verified remainder is residual.
- Live payload exposes `r_multiple`, `roll_status`, `roll_price`, and `protected_stop`.

- [ ] Write failing tests for remaining margin below 5 USDT and remaining notional below 1.5 times exchange minimum.
- [ ] Run tests and confirm residual positions survive today.
- [ ] Add pre-execution prediction and post-fill exchange verification with reason `residual_position_cleanup`.
- [ ] Add compact live-position fields and labels.
- [ ] Run all Python tests and `npm.cmd run build` in `frontend`.
- [ ] Restart all services once and verify ports, logs, AAVE remains `state_incomplete`, and healthy positions expose roll status.
