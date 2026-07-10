# Trading Ledger Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct five trading execution and ledger-review defects without losing existing data.

**Architecture:** Keep exchange execution authoritative, persist one small Alpha protection state, constrain fallback ledger merging by time continuity, and make review data units and ordering explicit. Existing tables are migrated additively.

**Tech Stack:** Python, unittest, SQLite, Binance exchange adapter

## Global Constraints

- Preserve existing database data.
- Do not change full-close behavior or current risk thresholds.
- Record a partial trade only after a successful exchange close.
- Treat stored `pnl_pct` as percentage points.

---

### Task 1: Partial-close execution and Alpha regime state

**Files:**
- Modify: `trader/execution.py`
- Modify: `shared/db.py`
- Test: `tests/test_partial_close_execution.py`
- Test: `tests/test_alpha_position_management.py`

- [ ] Add failing tests for exchange failure/no-op and repeated regime protection.
- [ ] Make `_execute_partial_close` return success and persist only after exchange success.
- [ ] Add additive Alpha protection state columns and reset/rearm logic.
- [ ] Run the focused tests.

### Task 2: Position-trade consolidation safety

**Files:**
- Modify: `shared/db.py`
- Test: `tests/test_position_trade_consolidation.py`

- [ ] Add a failing test proving separated same-price positions remain independent.
- [ ] Split entry-key groups by time continuity and intervening opening orders.
- [ ] Run the focused test.

### Task 3: Review percentage units and freshness

**Files:**
- Modify: `shared/policy_loop.py`
- Test: `tests/test_policy_loop_exit_review.py`

- [ ] Add failing tests for `-0.99%`, newest-first selection, and upsert refresh.
- [ ] Normalize percentage points exactly once.
- [ ] Select newest records and refresh all review source fields on conflict.
- [ ] Run the focused tests.

### Task 4: Full verification and service restart

**Files:**
- Verify all modified Python files and tests.

- [ ] Run `python -m unittest discover -s tests -v`.
- [ ] Run Python compilation and `git diff --check`.
- [ ] Restart API and trader services and verify their processes stay alive.
