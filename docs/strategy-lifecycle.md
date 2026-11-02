# Trading Lifecycle V1

## Rollout

`STRATEGY_LIFECYCLE_MODE=shadow` is the default. Existing signal generation and
position management remain in charge, while new entry and management policies
record observations in `strategy_events`. Arbitration and stop-priority fixes
are safety invariants in both modes. Nothing in this change deploys to production.

`STRATEGY_LIFECYCLE_MODE=enforce` activates the unified exit policy, fixed initial
risk basis, frozen armed Alpha setup levels, pullback/recovery adds, entry age and
chase gates, and durable order intents. Do not activate without historical and
dry-run comparison. Legacy positions without an initial plan cannot add under
the new risk gate; existing stop management still applies.

## Ownership

- Existing scanners and Alpha state machine own signal generation.
- `trader/action_coordinator.py` owns per-account/symbol/direction arbitration.
- `trader/lifecycle_policy.py` owns pure entry, exit and additional-risk checks.
- `trader/roll_policy.py` owns staged pullback/recovery eligibility.
- `shared/trading_journal.py` owns immutable plans, intents and audit events.
- Existing exchange and execution code own fills and exchange protection.
- Existing direction-scoped position history owns mutable management state.

Only one action may survive arbitration per position per cycle. Stops precede
full exits, partial reductions, adds and entries. Hedge sides remain separate.

## Durable Execution

Open identity is based on setup ID or scan ID, adds on position ID and layer,
and reductions on position ID and management stage. Missing identity fails
closed in enforce mode. Intent claims are transactional and account-scoped.

A timeout is an unknown outcome, not a failed order. Its client ID is queried
before any further submission. A known fill resumes local processing. An
unresolved intent blocks new exposure on that position, but not an independent
emergency reduction. Protection acknowledgement precedes completion. Repeated
completed intents never submit again. Unknown orders are not automatically
declared absent or retried. They can require operator investigation; the report
tool is intentionally read-only.

## Research

Run `python -m backtest.lifecycle_replay input.json` with an object containing
`bars`, timestamped `signals`, and optional `config`. Each bar has UTC `time`
(bar open), `open`, `high`, `low`, `close`, and optionally `funding_payment` (signed
cash outflow). Each entry signal has `action`, `symbol`, `side`, `position_side`,
`quantity`, `entry_price`, `stop_loss`, and `signal_time`. Add simulation is opt-in
using `enable_adds` and requires causal `technical` values in the bars.

Close-derived actions fill on the next open. Previously known stops are evaluated
conservatively with gap fills. Fees and slippage apply on both legs; open exposure
and pending last-bar actions are reported rather than silently liquidated.
The simulator is single-instrument, not a substitute for portfolio execution,
order-book liquidity, partial-fill, latency or exchange-availability simulation.
It shares the pure production policies, not the entire legacy execution engine.

Run `python -m tools.lifecycle_report --database /path/to/alphadog.db --account 1`
for unresolved intents and old/new management action differences. Differences
are not claims of improved PnL. Use time-separated development/validation data,
include losing symbols and costs, and follow with dry-run validation before any
enforce-mode deployment. No validated return, win rate or profit guarantee is
provided by this implementation or its unit tests.
