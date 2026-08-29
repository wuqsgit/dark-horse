# Live Account Stream Design

## Goal

Move live balances, positions, and open orders out of request-time API aggregation and into independently maintained current-state tables.

## Boundaries

- `account_stream` owns exchange account synchronization and never submits orders.
- `trader` remains the only service allowed to submit or cancel trading orders.
- `api` reads prepared database state and performs only bounded joins to local position-management state.
- `frontend` continues to consume `/api/trading/accounts/status` without a response-contract migration.

## Storage

- `account_live_balances`: one current futures-account balance row per account.
- `account_live_positions`: one current position row per account, symbol, and position side.
- `account_live_orders`: one current open-order row per account and exchange order id.
- `account_stream_state`: one synchronization-health row per account.

Every current-state row carries `snapshot_version`, `exchange_event_time`, `source`, and `updated_at`. A successful HTTP reconciliation replaces the full account snapshot in one SQLite transaction. WebSocket account/order events request an immediate reconciliation; a ten-second timer repairs missed events and refreshes mark-to-market values.

## Availability

- Each enabled account runs independently so one bad credential does not stop other accounts.
- Listen keys are kept alive before expiry and recreated after disconnect or expiration.
- HTTP reconciliation uses bounded retry/backoff.
- The API serves the last successful rows during an exchange outage and marks them stale from `account_stream_state`.
- Empty positions and orders are authoritative and delete rows absent from a completed reconciliation.

