# Alpha Soft Exit Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Alpha positions with a 0% to -3% floating loss from being closed by a transient soft signal unless the next closed 15-minute candle confirms a breakdown.

**Architecture:** Keep hard stops, trailing stops, take-profit actions, and strong-risk exits unchanged. Reuse `strategy_decisions` for the pending confirmation marker and read already-collected futures 15-minute candles; no new table or background process is introduced.

**Tech Stack:** Python 3, SQLite, `unittest`, existing trader execution engine.

## Global Constraints

- Apply only to Alpha soft-exit signals while `-3.0 <= pnl_pct < 0`.
- Wait for exactly one newly closed 15-minute futures candle.
- Confirm exit only when that candle closes below the lowest low of the three closed candles available when confirmation starts.
- Alpha hard stop at about -10%, trailing stop, TP1, TP2, and liquidity safety behavior remain immediate.
- Missing or stale candle data must hold the position, never force an exit.

---

### Task 1: Alpha Soft Exit Confirmation

**Files:**
- Modify: `trader/execution.py`
- Modify: `tests/test_alpha_position_management.py`

**Interfaces:**
- Consumes: `strategy_decisions`, `futures_candles_15m`, the current position `position_id`, and the existing Alpha `soft_hold_reason`.
- Produces: `_evaluate_alpha_soft_exit_confirmation(...) -> dict` with status `pending`, `waiting`, `confirmed`, `cancelled`, or `unavailable`.

- [x] **Step 1: Write failing policy tests**

Add tests proving that the first soft signal creates a pending confirmation, the same candle remains waiting, the next candle below the stored three-candle low confirms a close, a recovered candle cancels the exit, and hard stop bypasses confirmation.

- [x] **Step 2: Run tests to verify failure**

Run: `.venv\Scripts\python.exe -m unittest tests.test_alpha_position_management -v`

Expected: the new confirmation tests fail because the helper and behavior do not exist.

- [x] **Step 3: Implement the minimal confirmation helper and integration**

Read closed futures candles from `futures_candles_15m`, load the latest marker for the same `position_id` from `strategy_decisions`, and integrate the result immediately before the existing Alpha hold decision is recorded.

- [x] **Step 4: Run focused and trader tests**

Run: `.venv\Scripts\python.exe -m unittest tests.test_alpha_position_management tests.test_normal_soft_exit -v`

Expected: all tests pass.

- [x] **Step 5: Restart the trader and verify startup**

Run the repository's existing service restart command and confirm the trader process stays running without startup errors.
