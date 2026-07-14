# Margin Hard Stop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make losing positions exit only when current-position margin ROI reaches -10% for Alpha or -12% for normal and bluechip positions.

**Architecture:** Keep one margin-ROI threshold as the loss-side exit invariant. Convert that threshold to a leverage-adjusted exchange stop price at sizing time, and use the same threshold against live unrealized PnL in position management; ATR remains available for volatility, leverage, sizing, and profit management but cannot close a losing position.

**Tech Stack:** Python, unittest, Binance USDS-M Futures adapter, SQLite position state.

## Global Constraints

- Alpha hard stop is 10% of current position margin.
- Normal and bluechip hard stop is 12% of current position margin.
- Current position margin is average entry price times current quantity divided by leverage.
- Loss-side ATR, structure, score, order-book, drawdown, and momentum signals may record a hold reason but may not close or partially close.
- Profit-side TP, trailing stop, partial protection, and roll behavior remain enabled.
- Hard stops do not wait for candle confirmation.

---

### Task 1: Lock Margin ROI Behavior

**Files:**
- Modify: `tests/test_alpha_position_management.py`
- Modify: `tests/test_normal_soft_exit.py`
- Create: `tests/test_margin_hard_stop.py`

**Interfaces:**
- Consumes: `ExecutionEngine._build_position_actions`, `ExecutionEngine._build_alpha_position_action`, and `calculate_position`.
- Produces: regression coverage for Alpha -10%, normal/bluechip -12%, loss-side hold behavior, and leverage-adjusted stop prices.

- [ ] Add failing tests proving that hard-stop decisions use margin ROI rather than raw price return.
- [ ] Add failing tests proving structural and ordinary weak signals cannot exit a position while margin ROI remains above the hard stop.
- [ ] Add a failing test proving the generated stop distance is `margin_hard_stop_pct / leverage`.
- [ ] Run the focused tests and confirm failures are caused by the existing price-return and ATR behavior.

### Task 2: Implement the Single Loss-Side Invariant

**Files:**
- Modify: `trader/config.py`
- Modify: `trader/risk.py`
- Modify: `trader/execution.py`

**Interfaces:**
- Consumes: live position fields `entry_price`, `quantity`, `leverage`, and `unrealized_pnl`.
- Produces: `margin_hard_stop` close actions and `margin_hard_stop` exchange stop prices.

- [ ] Interpret configured `hard_stop_pct` values as margin ROI thresholds.
- [ ] Generate the initial exchange stop distance as `entry_price * hard_stop_pct / leverage` and identify the model as `margin_hard_stop`.
- [ ] Compare live `pnl_pct = unrealized_pnl / current_margin * 100` with -10% or -12% and emit one full-close stop action.
- [ ] Convert all other loss-side exit paths to hold-only decisions while leaving profitable management unchanged.
- [ ] Run focused tests until green.

### Task 3: Verify the Trading Surface

**Files:**
- Modify only if a failing regression reveals a required compatibility correction.

**Interfaces:**
- Consumes: the completed risk and execution behavior.
- Produces: verified backend behavior suitable for service restart.

- [ ] Run all backend unit tests.
- [ ] Inspect the diff for accidental profit-side or account-scoping changes.
- [ ] Restart the trader only after the test suite passes, preserving its intentionally stopped state unless explicitly requested otherwise.

