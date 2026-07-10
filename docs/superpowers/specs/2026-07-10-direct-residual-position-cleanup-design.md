# Direct Residual Position Cleanup Design

## Goal

Close economically meaningless live positions on every trader loop instead of waiting for a partial-close event.

## Rule

Before normal position management, calculate current notional and effective margin from the exchange position. Create a full close with reason `residual_position_cleanup` when either condition is true:

- effective margin is below 5 USDT; or
- current notional is below 1.5 times the exchange minimum notional.

This check runs before stop, take-profit, partial-close, and roll planning so only one action is produced for the symbol. Execution uses the existing exchange-confirmed full-close path. Existing post-partial-close residual verification remains as a second safety net.

## Verification

- A sub-5-USDT current position immediately plans a full close.
- A position below the exchange notional buffer immediately plans a full close.
- A normal-sized position continues through existing management.
- Existing Python tests and live service health remain green.
