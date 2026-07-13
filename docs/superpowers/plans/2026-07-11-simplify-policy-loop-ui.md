# Simplify Policy Loop UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove low-value category and diagnosis views while making the empty automatic-policy state truthful and visible.

**Architecture:** The default backtest summary will stop querying category and diagnosis rows. Internal policy reviews remain available to the hourly auto-policy generator, and compatibility endpoints can explicitly request diagnostics when needed.

**Tech Stack:** Python, SQLite, FastAPI, React, Vite, unittest.

## Global Constraints

- Remove the `categories` and `issues` tabs from the backtest page.
- Do not delete internal `policy_reviews`; automatic policy generation depends on them.
- Default summary responses omit category and review payloads.
- Automatic-policy status reports review candidates, active versions, offline factor suggestions, and active entry rules.
- Do not change live entry thresholds from the current insufficient sample.

---

### Task 1: Summary Contract

**Files:**
- Modify: `shared/policy_loop.py`
- Modify: `api/main.py`
- Create: `tests/test_policy_loop_summary.py`

- [x] Add failing tests for the slim default summary and automatic-policy counts.
- [x] Implement optional diagnostics and status counts.
- [x] Keep compatibility endpoints on the explicit diagnostics path.

### Task 2: Backtest Page

**Files:**
- Modify: `frontend/src/components/BacktestPanel.jsx`

- [x] Remove category and issue state, tabs, and render branches.
- [x] Add compact automatic-policy status metrics.
- [x] Build the frontend and verify the page renders.

### Task 3: Verification

- [x] Run the full Python test suite.
- [x] Restart API and frontend.
- [x] Verify `/api/backtest/summary` and `http://localhost:3000` return 200.
