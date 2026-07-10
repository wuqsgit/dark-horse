# Trading Ledger Safety Design

## Goal

Fix five verified defects in partial-close execution, Alpha repeated reductions, position-trade consolidation, PnL percentage normalization, and exit-review freshness.

## Design

### Partial close execution

The exchange order is the source of truth. A partial close must submit successfully before a local trade row or management state is written. The executor returns an explicit success flag so callers do not mark no-op or failed closes as completed.

### Alpha volume-regime protection

Persist the last successfully protected Alpha volume regime in `position_history`. A continuous occurrence of the same regime can reduce the position only once. A worse regime may trigger one additional reduction, while a return to a normal regime clears the marker and rearms protection.

### Position-trade consolidation

Use `position_id` whenever it exists. Entry-key fallback consolidation additionally requires time continuity and no intervening opening order, preventing independent positions at the same price from being merged across sessions.

### PnL percentage units

`position_trades.pnl_pct` and `trades.pnl_pct` use percentage points: `-0.99` means `-0.99%`. Exit review converts this value to a ratio exactly once by dividing by 100.

### Exit-review freshness

Review the newest position trades first. Upserts refresh all source fields as well as derived candle metrics so rebuilt ledger records cannot leave stale review metadata.

## Verification

Regression tests cover failed exchange orders, repeated Alpha regime signals, separated same-price positions, percentage values around one percent, and newest-first review selection. The complete test suite and Python compilation must pass before services restart.
