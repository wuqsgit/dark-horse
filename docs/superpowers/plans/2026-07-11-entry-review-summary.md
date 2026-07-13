# Entry Review Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the visible automatic-policy and action-stream views with explainable entry facts, entry quality reviews, grouped recommendations, and per-position action evidence.

**Architecture:** Persist immutable entry evidence in `trade_entry_reviews`; update only outcome fields during policy-loop review. Reuse `position_trades`, `strategy_decisions`, `decision_actions`, and futures candles, expose the result through the existing backtest summary, and render it in `BacktestPanel`.

**Tech Stack:** Python 3, SQLite, FastAPI, React/Vite, unittest/pytest.

## Global Constraints

- Entry snapshots are immutable after first insertion.
- Missing historical indicators remain null and are never replaced with current values.
- Current positions are displayed but excluded from grouped recommendations.
- Recommendations are display-only and never activate live policy changes.
- Raw `decision_actions` are retained for 5 days and are fetched only per position.

---

### Task 1: Entry Review Persistence

**Files:**
- Modify: `shared/db.py`
- Modify: `db/init.sql`
- Create: `tests/test_entry_review_persistence.py`

**Interfaces:**
- Produces: `record_entry_review_snapshot(snapshot: dict) -> bool`
- Produces: `fetch_entry_reviews(limit: int = 100) -> list[dict]`

- [ ] Write failing tests proving first-write-wins behavior and null historical fields.
- [ ] Run `python -m pytest tests/test_entry_review_persistence.py -v` and confirm missing-table/function failures.
- [ ] Add `trade_entry_reviews`, indexes, retention exemption, insert and fetch helpers.
- [ ] Run the focused test and confirm it passes.

### Task 2: Entry Review Classification and Summaries

**Files:**
- Modify: `shared/policy_loop.py`
- Create: `tests/test_policy_loop_entry_review.py`

**Interfaces:**
- Produces: `review_position_trade_entries(limit: int = 300) -> dict`
- Produces: `summarize_entry_reviews(recent_limit: int = 30) -> dict`
- Produces: `fetch_entry_review_summaries(limit: int = 100) -> list[dict]`

- [ ] Write failing tests for reasonable, early, chased, bad-condition, pending labels and 4/8/60% recommendation thresholds.
- [ ] Run the focused tests and confirm failures are caused by missing entry-review behavior.
- [ ] Implement immutable backfill, R-normalized outcome calculation, readable reason generation, and grouped recommendations.
- [ ] Run the focused tests and confirm they pass.

### Task 3: Summary and Action Evidence API

**Files:**
- Modify: `shared/policy_loop.py`
- Modify: `api/main.py`
- Modify: `tests/test_policy_loop_summary.py`
- Create: `tests/test_position_action_evidence.py`

**Interfaces:**
- Summary produces: `entry_reviews`, `entry_summaries`, `entry_review_status`
- API produces: `GET /api/policy-loop/positions/{position_trade_id}/actions`

- [ ] Write failing tests proving summary omits full `actions` and evidence lookup is scoped to one position.
- [ ] Run focused tests and confirm expected failures.
- [ ] Add summary fields, combined review execution, and position-scoped evidence lookup.
- [ ] Run focused tests and confirm they pass.

### Task 4: Automatic Review Trigger

**Files:**
- Modify: `trader/runner.py`
- Create: `tests/test_entry_review_runner.py`

**Interfaces:**
- Consumes: `review_position_trade_entries`, `summarize_entry_reviews`

- [ ] Write a failing orchestration test proving entry review runs after position reconstruction without breaking income sync on review failure.
- [ ] Run the focused test and confirm failure.
- [ ] Add the entry-review calls beside the existing exit-review calls with isolated error handling.
- [ ] Run the focused test and confirm it passes.

### Task 5: Entry Summary Frontend

**Files:**
- Modify: `frontend/src/components/BacktestPanel.jsx`

**Interfaces:**
- Consumes: `entry_reviews`, `entry_summaries`, `entry_review_status`

- [ ] Replace `policies` with `entrySummary` and remove the independent `actions` tab.
- [ ] Render entry fact and grouped summary tables, including current/history status and expandable action evidence.
- [ ] Run `npm run build` in `frontend` and fix any compile failures.

### Task 6: Full Verification and Restart

**Files:**
- Verify only.

- [ ] Run `python -m pytest -q` and confirm zero failures.
- [ ] Run `npm run build` in `frontend` and confirm exit code 0.
- [ ] Restart all DarkHorse services through the repository start script.
- [ ] Verify API and frontend return HTTP 200 and inspect the backtest page payload for entry review fields.
