# Spot Distribution Short and Trend Runner

## Entry

`alpha_strategy_v2.spot_distribution_short.enabled` replaces the old short entry
confirmations. Eligible tracked Alpha spot assets require both a rolling 24-hour
spot return <= -30 percent and reported taker sell quote volume greater than buy
quote volume. Recent minute records provide volume sums only: candle bodies,
support breaks, scores, OI and a subsequent 15-minute confirmation are not used.

The ticker must be no more than 15 minutes old; the most recent two volume records
must be no more than 3 minutes old. Missing, unknown or estimated flow cannot
qualify. Estimated flow from `alpha-flow-estimated` is explicitly excluded.
Availability of recent candles does not establish that their buy/sell attribution
is independently verified trade-by-trade data; this strategy trusts the reported
exchange taker field only where the collector has not marked it estimated.

Entries use the existing Alpha sizing, ATR stop and exchange protection. Account
switches, tradability, freshness, exposure limits, pending-order and loss controls
remain. Each continuously qualifying episode emits one durable event, shared
across accounts with separate claims. A definite return above the threshold or
loss of seller dominance ends the episode; a missing-data period does not.
There is no automatic re-entry or add from an already consumed episode. Events
expire after five minutes. A rejected event is not an instruction to bypass risk.

## Exit

`trend_runner.enabled` activates the shared runner manager for production account
positions, even while `strategy_lifecycle.mode` remains `shadow`. This is an
active management change, not merely a shadow observation.

- At 1R: tighten the stop to entry plus/minus the 0.2 percent cost buffer, no trim.
- At 2R: one 25 percent reduction, retaining at least 75 percent of initial size.
- Track the remainder at 2.5 ATR from its favorable price extreme.
- Never loosen an existing protection stop, including pre-upgrade stops.
- Once the favorable extreme has reached 1R, do not time-exit the position.
- No score-fade or repeated small-profit reductions from the legacy manager.
- Structure/price stops and emergency account-risk controls still apply.

Existing already-reduced positions are not restored or automatically increased.
Their tighter stops are not widened. Direct spot-short positions ignore unrelated
old V2 setup invalidations and are managed by their own price protection.

This change has no live deployment side effect until code is deployed/restarted.
Unit tests are correctness checks, not evidence of positive trading expectancy.
