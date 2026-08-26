# Trading API Split and Historical Trade Summary Design

## Context

The current `GET /api/trading/status` endpoint combines account balances, live
positions, historical trades, strategy decisions, runtime diagnostics, Binance
requests, and ledger rebuilds. A cold Binance request can retry for tens of
seconds while running synchronously inside the single Uvicorn event loop. During
that time unrelated endpoints, including `GET /api/trading/accounts`, are also
blocked.

The historical trade path has a separate correctness problem. The persisted
`position_trades` rows and the read-time grouping use several incompatible
identities, including local `position_id`, income time gaps, entry price, and
entry time. Adding to a position changes its weighted entry price, so one
symbol and direction can be split into many displayed rows. In production,
account 2 has 22 AKEUSDT LONG summary rows whose quantities total 237,869,
while deduplicated raw fills contain 169,001 BUY and 169,001 SELL quantity.
Grouping the already duplicated summary rows would preserve the error.

## Goals

- Replace the old aggregate trading status API with account-centered endpoints.
- Keep every HTTP request independent of live Binance latency.
- Auto-refresh only balances, equity, and current positions every 30 seconds.
- Load history and strategy decisions lazily for the selected account.
- Display exactly one historical row for each `(account_id, symbol, direction)`.
- Calculate historical prices and quantities from deduplicated exchange fills.
- Preserve position-cycle records for AI training, policy review, and evidence.
- Return stale-but-valid account snapshots when an exchange refresh fails.

## Non-Goals

- Changing entry, exit, roll, or risk-management strategy behavior.
- Collapsing the cycle-level AI training ledger into lifetime symbol summaries.
- Making an HTTP request wait for an exchange reconciliation or ledger rebuild.
- Retaining compatibility aliases for the old trading status endpoint.

## API Surface

### `GET /api/trading/accounts`

Returns saved account configuration and switches. It reads only local account
configuration and never contacts Binance.

### `GET /api/trading/accounts/status`

Returns one cached snapshot containing all configured accounts:

- balance, available balance, equity, unrealized PnL, and margin usage;
- current positions and their local management state;
- snapshot timestamps, age, freshness, and the last refresh error;
- small portfolio totals needed by the account selector.

It does not contain historical trades, decisions, or runtime diagnostics. The
handler only reads the in-memory or persisted snapshot. It never refreshes the
exchange synchronously. The normal snapshot TTL and frontend poll interval are
30 seconds.

### `GET /api/trading/accounts/{account_id}/history`

Returns the selected account's historical trade summaries. Supported query
parameters are `cursor`, `limit`, `symbol`, `direction`, `source`, `from`, and `to`.
Pagination is cursor-based and deterministic. Each returned row represents one
`(account_id, symbol, direction)` summary within the requested date filter.
The `source` filter selects matching position cycles before aggregation; it does
not become part of the grouping key. The response also contains cycle-level
win/loss statistics so portfolio metrics are not inferred from lifetime rows.

The endpoint calls one canonical backend history-summary service. No controller
or frontend component may implement a second grouping algorithm.

### `GET /api/trading/accounts/{account_id}/decisions`

Returns the latest strategy run, top rejection reasons, and recent decisions
for the selected account. It performs local indexed database reads only.

### `GET /api/trading/runtime/status`

Returns process and pipeline health, last successful run times, and trader
diagnostics. It does not contact Binance and does not include account history.

### Removed Endpoints

The following routes are deleted and must return 404:

- `GET /api/trading/status`
- `GET /api/trading/statu`
- `GET /api/trading/stats`

The frontend and tests are migrated in the same release; there is no transition
period or compatibility response.

## Historical Trade Model

### Two Deliberate Views

The system keeps two different views because they serve different purposes:

1. **Position-cycle ledger:** one flat-to-flat position lifecycle. AI review,
   policy evidence, and per-trade metrics continue to use this view.
2. **Historical display summary:** one lifetime row per account, symbol, and
   direction, optionally constrained by an explicit date filter.

The display summary is a projection, not a destructive rewrite of cycle data.
This prevents UI requirements from degrading AI training data.

### Source Precedence

1. Deduplicated rows in `fills` are the preferred source for quantities,
   entry/exit prices, direction, and times.
2. `exchange_income_ledger` is authoritative for realized PnL, commission,
   funding, and other exchange adjustments.
3. Existing cycle-level `position_trades` is used only when raw fills are
   unavailable for old data. Such rows receive `reconcile_status=incomplete`.

The unique fill identity is `(account_id, symbol, normalized_trade_id)`. When a
trade ID is absent, the fallback identity uses immutable execution facts:
account, symbol, side, quantity, price, and exchange timestamp.

### Cycle Reconstruction

Fills are ordered by exchange time and stable row ID. `position_side` is used
when supplied by the exchange. In one-way mode, signed quantity reconstructs
the running position:

- positive net quantity is LONG;
- negative net quantity is SHORT;
- a cycle starts when net quantity leaves zero;
- additions and partial closes remain in that cycle;
- a cycle closes only when net quantity returns to zero;
- a direct reversal closes the first cycle at zero and starts the opposite
  cycle with the residual quantity.

Only complete cycles are included in historical summaries. The current open
position remains exclusively in the account status snapshot.

### Summary Grouping and Calculations

After cycle reconstruction, complete cycles are grouped only by:

```text
(account_id, uppercase(symbol), LONG|SHORT)
```

Entry price is calculated from actual entry fills:

```text
sum(entry_fill_price * entry_fill_quantity) / sum(entry_fill_quantity)
```

Exit price uses the equivalent exit-fill weighted average. Summary quantity is
the sum of entry-fill quantities across included complete cycles. It is not the
sum of intermediate `position_trades.quantity` values.

The displayed PnL is the sum of exchange realized PnL, commission, funding, and
adjustments associated with the included cycles. PnL percentage is computed
from total net PnL divided by summed cycle margin. When exact leverage is not
recoverable, `pnl_pct` is null rather than silently assuming leverage 3.

Other fields are defined as follows:

- `entry_time`: earliest included cycle entry;
- `exit_time`: latest included cycle exit;
- `position_count`: number of complete flat-to-flat cycles;
- `close_count`: number of deduplicated exit fills;
- `strategy_sources`: distinct strategy sources seen in included cycles;
- `reconcile_status`: `ok` only when fills and income reconcile within the
  configured numeric tolerance, otherwise `incomplete` or `mismatch`.

For the full-history request, a symbol and direction can occur only once in the
response. Date and source filters create the same one-row projection after
selecting cycles whose exit time and strategy source match the request.

## Background Refresh and Persistence

One background owner refreshes each enabled account. Blocking Binance client
calls run in a worker thread and never on the asyncio event loop. Successful
snapshots are stored in memory and persisted locally. On process start, the API
loads the last successful persisted snapshot before scheduling refresh work.

Refresh failures retain the last successful payload, increase its age, and
record a sanitized error. A slow or unavailable exchange therefore affects
freshness, not HTTP response time.

Income synchronization, fill synchronization, and position-cycle rebuilds stay
in the trader/background pipeline. The history HTTP handler reads reconciled
local data and never triggers synchronization or rebuilding.

## Frontend Data Flow

Initial page load requests account configuration, the account status snapshot,
and runtime status independently. Every 30 seconds the page refreshes only the
status snapshot and runtime status.

History is requested when the live-trading history section becomes visible or
the selected account/filter changes. Decisions are requested when their section
becomes visible. Requests are cancellable so switching accounts cannot render a
late response for the previous account.

The frontend renders history rows exactly as returned. It may format values and
show `position_count`, but it must not group, sum, or repair rows. Legacy
`status` state and fallback selection logic are removed.

## Error Handling

- Unknown account IDs return 404.
- Invalid cursor or filter values return 400 with a stable error code.
- Missing raw fills do not invent prices or leverage; affected fields are null
  and reconciliation state explains the limitation.
- Snapshot responses include `fresh`, `snapshot_at`, `age_seconds`, and
  `last_error` so stale data is explicit.
- No endpoint returns decrypted API credentials or raw exchange errors that may
  contain sensitive request data.

## Migration

1. Add indexes required for account-, symbol-, direction-, and time-scoped fill
   and income reads; synchronize them to `db/init.sql`.
2. Implement and test the canonical cycle reconstruction and display-summary
   service without modifying existing production rows.
3. Backfill/reconcile cycle-level data from deduplicated fills in background.
4. Add the five replacement endpoints and remove all three legacy routes.
5. Migrate the frontend to the new account-centered loading flow.
6. Remove old cache aliases, startup warmers, response models, and documentation.
7. On deployment, build the persisted account snapshot and history projection
   before exposing the new frontend. The deployment is atomic because the old
   endpoint is intentionally unsupported.

## Testing

### History Correctness

- repeated additions with changing weighted entry price produce one display row;
- partial exits and multiple take-profits do not split a display row;
- close and later reopen in the same direction increase `position_count` but
  still produce one display row;
- long and short histories for the same symbol remain separate;
- direct reversal creates one completed direction cycle and one new cycle;
- duplicate fills and duplicate income rows do not change totals;
- open residual quantity is excluded from closed history;
- weighted entry price exactly follows the required formula;
- missing fills produce an explicit incomplete result without fabricated values;
- AKE-shaped regression data verifies no quantity inflation.

### API and Performance

- removed routes return 404;
- account configuration never instantiates an exchange client;
- status, history, decisions, and runtime contracts contain only their owned data;
- a simulated 36-second Binance timeout does not delay any HTTP endpoint;
- persisted stale snapshots are served immediately after restart;
- account switching cannot mix history or decisions between accounts;
- cursor pagination remains stable while new trades arrive.

### Frontend

- only status and runtime poll every 30 seconds;
- history and decisions load lazily;
- no request references a removed endpoint;
- no frontend code groups historical rows;
- empty, loading, stale, incomplete, and error states render correctly.

## Acceptance Criteria

- Production AKEUSDT LONG history for account 2 renders as one row rather than
  22 rows, with quantity derived from 169,001 deduplicated entry fills for the
  observed dataset and weighted entry price approximately `0.0087316073`.
- Every full-history response contains at most one row for each account, symbol,
  and direction.
- Lightweight public endpoint P95 is below 500 ms; history first-page P95 is
  below 800 ms under normal local database load.
- Binance timeout and retry behavior does not block the API event loop.
- AI and policy review retain access to individual position cycles.
- All replacement endpoint, history regression, and frontend tests pass before
  deployment.
