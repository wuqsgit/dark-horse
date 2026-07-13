# Neutral Dynamic Leverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace threshold-based entry leverage with one volatility formula plus small, explicit symbol and risk-class caps.

**Architecture:** `trader.risk.calculate_position` continues to own position sizing. Its leverage helper will calculate a neutral stop proxy from ATR percentage, derive leverage from a 20% target margin loss, then apply symbol or class caps; existing notional, risk-budget, and actual stop calculations remain unchanged.

**Tech Stack:** Python 3, existing trader configuration, `unittest`.

## Global Constraints

- Neutral stop proxy is `clamp(2 * atr_pct, 0.025, 0.10)`.
- Raw leverage is `floor(0.20 / neutral_stop_proxy)`.
- Final leverage is at least 2x and no more than the configured symbol or class cap.
- Symbol caps are BTC 8x, ETH 6x, SOL 5x, LINK 5x, and AAVE 5x.
- Narrative, Meme, and Alpha class caps are 3x.
- Existing positions are not resized; the formula applies to newly planned entries.
- Existing account-risk, margin, notional, hard-stop, and take-profit logic remains in force.

---

### Task 1: Formula And Caps

**Files:**
- Modify: `trader/risk.py`
- Modify: `trader/config.py`
- Modify: `tests/test_dynamic_leverage.py`

**Interfaces:**
- Consumes: `atr_pct`, sizing class configuration, and the futures symbol.
- Produces: `_dynamic_leverage(atr_pct, sizing, symbol=None) -> int` and `leverage_stop_pct` in `calculate_position` output.

- [x] **Step 1: Replace old threshold tests with formula examples**

Cover BTC 8x, ETH 6x, SOL 5x, LINK 5x, AAVE 3x, DOGE 3x, an Alpha symbol capped at 3x, and extreme volatility at 2x.

- [x] **Step 2: Run the focused test and confirm failure**

Run: `.venv\Scripts\python.exe -m unittest tests.test_dynamic_leverage -v`

Expected: failures because the current helper still uses `atr_leverage_steps` and has no symbol cap input.

- [x] **Step 3: Implement the formula and configuration**

Add the neutral formula configuration, symbol caps, and class caps. Pass `symbol` into `_dynamic_leverage` from `calculate_position`, and expose the neutral stop proxy for decision logging.

- [x] **Step 4: Verify focused and full suites**

Run: `.venv\Scripts\python.exe -m unittest tests.test_dynamic_leverage -v`

Run: `.venv\Scripts\python.exe -m unittest discover -s tests -q`

Expected: all tests pass.

- [x] **Step 5: Restart Trader and inspect startup**

Restart only `trader.runner`, verify the process stays alive, and confirm a complete Testnet loop without an exception.
