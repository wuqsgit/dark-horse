# Direct Residual Position Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close sub-5-USDT or below-minimum-notional live positions during every trader loop.

**Architecture:** Reuse `is_residual_position` at the start of `_build_position_actions`. A residual position produces one ordinary full-close action, which the existing exchange-confirmed close path executes and records.

**Tech Stack:** Python, SQLite, unittest.

## Global Constraints

- Effective margin threshold is 5 USDT.
- Minimum-notional buffer is 1.5 times the exchange minimum.
- Exit reason is `residual_position_cleanup`.
- Residual cleanup runs before all other position actions.

---

### Task 1: Plan Direct Residual Cleanup

**Files:**
- Modify: `trader/execution.py`
- Modify: `tests/test_residual_position_cleanup.py`

**Interfaces:**
- Consumes: `is_residual_position(quantity, mark_price, leverage, exchange_info, config)`.
- Produces: a full-close action from `_build_position_actions` with reason `residual_position_cleanup`.

- [ ] Add a failing test where a current position has effective margin below 5 USDT and assert one full-close action.
- [ ] Add a failing test where notional is below 1.5 times exchange minimum and assert one full-close action.
- [ ] Run `.venv\\Scripts\\python.exe -m unittest tests.test_residual_position_cleanup -v` and confirm the new tests fail.
- [ ] Call `is_residual_position` before normal position rules and append a full-close action using the position's opposite side.
- [ ] Keep the position unchanged when exchange symbol information is temporarily unavailable.
- [ ] Run focused tests, full unittest discovery, Python compilation, and `git diff --check`.
- [ ] Restart Trader and verify the live API and Trader log.
