# Explosive Move Capture Specification

## Goal

Capture at least 80% of replay-labeled explosive Alpha moves that satisfy the
system's tradeability and market-data requirements, and submit a real opening
order for at least 80% of the qualifying live triggers.

The percentage is an engineering recall/execution objective, not a guaranteed
profitability rate.

## Event Definition

An `explosive_breakout` event is created for a mapped Alpha futures symbol when:

- Alpha discovery score is at least 80.
- Alpha spot six-hour volume ratio is at least 3.5x.
- Futures six-hour volume ratio is at least 1.5x.
- The 15-minute return is at least -1% and the one-hour return is at least -2%.
- Futures data is available and the existing breakout confirmation has passed.
- Spread is below the existing 0.35% hard limit.

The event is deduplicated by symbol, direction, and setup. Repeated scanner
polls must not create repeated opening orders.

## Entry Behavior

- The first trigger opens one normal Alpha position (`1.0x`).
- AI entry quality is advisory for this event. It may annotate or reduce a
  normal signal, but it cannot reject or silently remove an explosive event.
- Market phase, `high_risk_watch`, category occupancy, one order-book snapshot,
  and overheat/cooldown are soft controls for this event. They may reduce the
  first order to `0.5x`, but cannot remove it.
- Existing hard safety controls remain: account disabled, unsupported contract,
  stale market data, duplicate live symbol exposure, capital/position capacity,
  spread at or above 0.35%, excessive execution slippage, and exchange failure.
- The first order retains the current market-order execution path. Exchange
  errors are recorded as terminal execution outcomes rather than disappearing.

## Confirmation Add

- A profitable triggered position may add one additional normal position after
  continuation confirmation.
- Total explosive-event exposure is capped at `2.0x` normal position size.
- A setup may perform at most one initial open and one confirmation add.
- Confirmation requires the position to remain above its trigger price and the
  latest Alpha volume-price state to remain entry-capable.

## Execution Contract

Every planned explosive opening must end in exactly one observable terminal
state: `opened`, `rejected`, or `error`. The terminal record carries the same
run ID, scan ID, symbol, account ID, and setup identifier as the planned action.
No gate may remove an action without recording the reason.

## Metrics

- `explosive_signal_recall`: labeled explosive moves triggered / tradeable
  labeled explosive moves; target >= 80%.
- `explosive_execution_rate`: opened / valid explosive triggers; target >= 80%.
- `execution_terminal_coverage`: terminal outcomes / planned explosive opens;
  target = 100%.
- `duplicate_initial_open_rate`: target = 0%.
- `execution_slippage_p95`: target <= 0.5%.

## Replay Acceptance

BTRUSDT on 2026-08-26 must produce an explosive trigger around the confirmed
breakout at 0.03639 and retain the opening action through AI quality gating.
Negative/control fixtures must continue to reject missing volume confirmation,
hard spread violations, stale data, and duplicate exposure.
