# Simple Roll Trading Design

## Goal

Allow one controlled add-on to a proven profitable trend while ensuring the combined remaining position cannot turn the trade from profit into loss. Keep the implementation small, observable, and exchange-verifiable.

## Scope

- One roll layer only.
- Add 25% of the initial position quantity.
- No leverage changes and no cross-margin adjustment.
- No reconstruction of missing legacy position state.
- No category-specific roll state machine.

## Eligibility

A position may roll only when all conditions pass:

1. Complete local position state exists, including initial quantity, initial stop, initial risk per unit, ATR, and TP1 execution state.
2. TP1 was confirmed by an actual exchange partial-close fill.
3. Current profit is at least 1.5R.
4. No close or partial-close action is already planned for the symbol.
5. The position has never rolled before.
6. Long: price is above EMA20 and EMA20 slope is positive.
7. Short: price is below EMA20 and EMA20 slope is negative.
8. Alpha positions additionally require synchronized Alpha spot and futures volume and must not use the `high_risk_watch` profile.

`R` is based on the original trade risk:

```text
initial_risk_per_unit = abs(entry_price - initial_stop_loss)
current_r = favorable_price_move / initial_risk_per_unit
```

If any required local state is missing, the result is `roll_state_incomplete`; the system does not reconstruct, backfill, mark TP1, or roll that position.

## Roll Size

```text
raw_add_quantity = initial_quantity * 0.25
add_quantity = exchange_step_adjusted(raw_add_quantity)
```

The action is rejected if quantity is below exchange minimum quantity or added notional is below exchange minimum notional. Size is always based on the initial quantity, never the current quantity.

## Execution

1. Read the latest exchange position.
2. Revalidate eligibility immediately before the order.
3. Estimate the blended entry and protection price.
4. Reject the roll if the protection price would already be beyond the current mark price.
5. Place the add-on market order with the existing leverage unchanged.
6. Read the exchange position again to obtain actual total quantity and blended entry.
7. Place one protection stop for the full remaining quantity.
8. Only after the new stop is accepted, cancel superseded stop orders and mark `roll_layer=1`.

If the add-on fills but full-position protection fails, immediately reduce the confirmed added quantity and record `roll_protection_failed`. The roll is not marked successful.

## Profit Protection

After roll execution, the full-position protection price is:

```text
LONG  = blended_entry * 1.0015
SHORT = blended_entry * 0.9985
```

The 0.15% buffer covers fees, funding, and modest slippage. The protection stop covers the full remaining exchange quantity, not only the add-on.

After the position makes a new favorable move, the stop follows:

```text
LONG  trailing = highest_price - 2 * ATR
SHORT trailing = lowest_price + 2 * ATR
```

The effective stop is the tighter of the break-even protection, trailing stop, and existing stop. It may only move toward profit. Exchange stop replacement occurs only when the improvement exceeds one price tick.

## Residual Position Cleanup

Before every partial close, predict the remaining position. After execution, verify it using exchange data. Convert to or follow with a complete close when either condition is true:

```text
remaining_margin < 5 USDT
remaining_notional < exchange_min_notional * 1.5
```

The reason is `residual_position_cleanup`. This rule applies independently of roll eligibility, including positions with incomplete legacy state.

## Data Model

Add `initial_quantity` and `protected_stop` to `position_history`. Reuse existing fields for initial stop, ATR, TP1, roll layer, roll price/time, highest/lowest price, and trailing state.

Persist these fields at new-position creation. Partial closes update current quantity but never overwrite `initial_quantity`.

## API And Frontend

Live position payloads expose:

- `r_multiple`
- `roll_status`: `state_incomplete`, `waiting_tp1`, `waiting_1_5r`, `trend_not_confirmed`, `alpha_not_synced`, `eligible`, or `completed`
- `roll_price`
- `protected_stop`

The live page displays these values without adding another workflow or control panel.

## Tests

- Missing local state cannot roll and is not reconstructed.
- TP1 database flag without an exchange-confirmed partial fill cannot roll.
- Less than 1.5R cannot roll.
- EMA20 direction mismatch cannot roll.
- Alpha volume desynchronization cannot roll.
- Roll quantity equals 25% of initial quantity and only one roll is allowed.
- Existing leverage is not modified.
- Full-position protection is confirmed before roll success is recorded.
- Protection failure unwinds only the confirmed add-on quantity.
- Protection and trailing stops never loosen.
- Remaining margin below 5 USDT triggers a full close.
- Remaining notional below 1.5 times exchange minimum triggers a full close.
