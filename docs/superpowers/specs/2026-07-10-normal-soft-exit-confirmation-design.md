# Normal Soft Exit Confirmation Design

## Goal

Prevent repeated soft-exit reductions from draining profitable normal positions while preserving every hard-risk exit.

## Scope

Apply only to normal-position soft signals: weak hold alpha, score decay, and category momentum reversal. Alpha position rules, hard stops, trailing stops, TP1/TP2, and residual cleanup are unchanged.

## Rules

- A symbol may execute at most one normal soft reduction in 60 minutes.
- Strong trend means price remains on the favorable side of EMA20, EMA20 slope agrees with the position, EMA20/EMA50 is not inverted, and 6h/24h returns are not both adverse.
- A profitable weak-hold signal during a strong trend reduces 20% once, then leaves the remainder under its existing stop/trailing management.
- Confirmed weakness means price and EMA20 slope both break against the position, or both 6h and 24h returns are adverse. It may reduce 25% once.
- Ambiguous soft weakness produces a hold decision.
- Hard stops, trailing stops, TP actions, and residual cleanup bypass this cooldown.

## Evidence Constraint

The current recommendation is based on two XPINUSDT exits with the same reason, not two independent symbols. The change therefore targets that reason pattern and does not alter all ordinary exits.

