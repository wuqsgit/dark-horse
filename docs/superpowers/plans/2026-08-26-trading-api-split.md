# Trading API Split and Historical Trade Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the blocking aggregate trading endpoint with account-centered APIs and guarantee one accurate historical row per account, symbol, and direction.

**Architecture:** Exchange data is refreshed only by a background worker and served through a persisted account snapshot. A new `shared.trade_history` module reconstructs complete flat-to-flat cycles from deduplicated fills, joins exchange income, and exposes a display projection without changing the cycle-level `position_trades` ledger used by AI.

**Tech Stack:** Python 3, FastAPI, asyncio, SQLite, React 18, Vite, Node test runner, unittest/pytest.

**Spec:** `docs/superpowers/specs/2026-08-26-trading-api-split-design.md`

## Global Constraints

- Delete `/api/trading/status`, `/api/trading/statu`, and `/api/trading/stats`; they must return 404.
- No HTTP handler may call Binance, synchronize fills/income, or rebuild the trading ledger.
- `GET /api/trading/accounts/status` serves only cached balance, equity, summary, and current-position data with a 30-second freshness target.
- Full historical output contains at most one row per `(account_id, symbol, direction)`.
- Weighted entry price is `sum(entry price * entry quantity) / sum(entry quantity)` over deduplicated entry fills in complete cycles.
- Keep cycle-level position data for AI and policy review.
- Do not add new runtime dependencies.
- Do not commit `.runtime/*.pid` files or unrelated working-tree changes.

---

## File Map

- Create `shared/trade_history.py`: fill deduplication, cycle reconstruction, income allocation, display aggregation, filters, and cursor pagination.
- Create `shared/account_status_snapshot.py`: atomic persisted snapshot storage and stale/fresh metadata.
- Modify `shared/db.py`: add focused account/time query helpers and required indexes; leave `fetch_position_trade_groups` cycle semantics intact.
- Modify `db/init.sql`: mirror every added SQLite index.
- Modify `api/main.py`: slim the account snapshot, add account history/decision/runtime routes, and remove legacy routes/cache warmers.
- Create `frontend/src/api/tradingData.js`: clients for account config, account snapshot, history, decisions, and runtime.
- Modify `frontend/src/components/LiveTrading.jsx`: independent polling and lazy history/decision loading with cancellation.
- Modify `README.md`: publish only the replacement endpoint contracts.
- Create `tests/test_trade_history_summary.py`: fill/cycle/history correctness coverage.
- Create `tests/test_trading_api_split.py`: endpoint ownership, 404, snapshot persistence, and non-blocking tests.
- Create `frontend/src/components/tradingData.test.mjs`: frontend request and cancellation tests.
- Modify `tests/test_multi_account_trading.py`: remove legacy cache expectations and assert slim snapshot fields.

---

### Task 1: Canonical Fill-to-Cycle Reconstruction

**Files:**
- Create: `shared/trade_history.py`
- Create: `tests/test_trade_history_summary.py`

**Interfaces:**
- Produces: `deduplicate_fills(rows: Iterable[Mapping]) -> list[dict]`
- Produces: `reconstruct_position_cycles(rows: Iterable[Mapping]) -> list[dict]`
- A cycle contains `account_id`, `symbol`, `direction`, `entry_fills`, `exit_fills`, `entry_time`, `exit_time`, `entry_quantity`, `exit_quantity`, `entry_price`, `exit_price`, `trade_ids`, and `complete`.

- [ ] **Step 1: Write failing tests for deduplication and weighted prices**

```python
def test_duplicate_trade_id_is_counted_once():
    fills = [
        fill(1, "AKEUSDT", "BUY", 100, 0.008, "t1", "2026-08-22 01:00:00"),
        fill(1, "AKEUSDT", "BUY", 100, 0.008, "A1:t1", "2026-08-22 01:00:00"),
        fill(1, "AKEUSDT", "SELL", 100, 0.009, "t2", "2026-08-22 02:00:00"),
    ]
    cycles = reconstruct_position_cycles(fills)
    assert len(cycles) == 1
    assert cycles[0]["entry_quantity"] == 100


def test_additions_use_fill_weighted_entry_price():
    fills = [
        fill(1, "AKEUSDT", "BUY", 80, 10, "1", "2026-08-22 01:00:00"),
        fill(1, "AKEUSDT", "BUY", 20, 12, "2", "2026-08-22 01:01:00"),
        fill(1, "AKEUSDT", "SELL", 100, 13, "3", "2026-08-22 02:00:00"),
    ]
    cycle = reconstruct_position_cycles(fills)[0]
    assert cycle["entry_price"] == pytest.approx((80 * 10 + 20 * 12) / 100)
    assert cycle["entry_quantity"] == 100
```

- [ ] **Step 2: Run the tests and confirm the module is missing**

Run: `python -m pytest tests/test_trade_history_summary.py -v`

Expected: FAIL because `shared.trade_history` does not exist.

- [ ] **Step 3: Implement stable fill normalization and deduplication**

```python
def _fill_identity(row):
    trade_id = str(row.get("trade_id") or "").split(":")[-1]
    if trade_id:
        return (int(row["account_id"]), row["symbol"].upper(), trade_id)
    return (
        int(row["account_id"]), row["symbol"].upper(), row["side"].upper(),
        round(float(row.get("quantity") or 0), 12),
        round(float(row.get("price") or 0), 12), str(row.get("created_at") or ""),
    )


def deduplicate_fills(rows):
    unique = {}
    for original in rows:
        row = dict(original)
        unique.setdefault(_fill_identity(row), row)
    return sorted(unique.values(), key=_fill_sort_key)
```

- [ ] **Step 4: Implement flat-to-flat cycle reconstruction**

Implement signed-quantity accounting per `(account_id, symbol, position_side)`.
For `BOTH`, BUY is positive and SELL is negative. Split a reversal fill at the
zero crossing so the first portion closes the current cycle and the remainder
opens the opposite cycle. Classify entry and exit fills from the cycle direction,
and calculate weighted prices from the split quantities.

```python
def _weighted_price(rows):
    quantity = sum(float(row["quantity"]) for row in rows)
    return (
        sum(float(row["price"]) * float(row["quantity"]) for row in rows) / quantity
        if quantity > 0 else None
    )
```

- [ ] **Step 5: Add and pass lifecycle regression tests**

Add tests for partial exits, close/reopen in the same direction, LONG and SHORT
for one symbol, direct reversal, and exclusion of an incomplete final cycle.

Run: `python -m pytest tests/test_trade_history_summary.py -v`

Expected: PASS.

- [ ] **Step 6: Commit the cycle engine**

```bash
git add shared/trade_history.py tests/test_trade_history_summary.py
git commit -m "feat: reconstruct position cycles from exchange fills"
```

---

### Task 2: Historical Display Summary and Pagination

**Files:**
- Modify: `shared/trade_history.py`
- Modify: `shared/db.py`
- Modify: `db/init.sql`
- Modify: `tests/test_trade_history_summary.py`
- Modify: `tests/test_init_sql_schema.py`

**Interfaces:**
- Consumes: `reconstruct_position_cycles(rows)` from Task 1.
- Produces: `fetch_trade_history_summaries(account_id: int, *, cursor: str | None = None, limit: int = 20, symbol: str | None = None, direction: str | None = None, source: str | None = None, from_time: str | None = None, to_time: str | None = None) -> dict`.
- Returns: `{"items": list[dict], "next_cursor": str | None, "stats": dict, "reconcile_status": str}`.

- [ ] **Step 1: Write an AKE-shaped failing regression test**

Use several complete AKE LONG cycles with additions, partial exits, duplicate
trade IDs, and changing entry prices. Assert one returned item, exact unique
entry quantity, weighted entry/exit price, summed PnL, `position_count`, and
`close_count`.

```python
result = fetch_trade_history_summaries(account_id=2, limit=20)
assert len(result["items"]) == 1
row = result["items"][0]
assert (row["symbol"], row["side"]) == ("AKEUSDT", "LONG")
assert row["quantity"] == 169001
assert row["entry_price"] == pytest.approx(0.00873160732776729)
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `python -m pytest tests/test_trade_history_summary.py -k summary -v`

Expected: FAIL because the summary facade is absent.

- [ ] **Step 3: Add indexed database readers**

Add account-scoped readers that select only the requested account and optional
time/symbol bounds. Add these indexes in both runtime initialization and
`db/init.sql`:

```sql
CREATE INDEX IF NOT EXISTS idx_fills_account_symbol_time
ON fills(account_id, symbol, created_at, id);

CREATE INDEX IF NOT EXISTS idx_income_account_symbol_time
ON exchange_income_ledger(account_id, symbol, income_time, id);
```

- [ ] **Step 4: Implement income allocation and lifetime grouping**

Match income by normalized trade ID first and by cycle symbol/time bounds second.
Group complete cycles only by `(account_id, symbol, direction)`. Calculate entry
and exit averages from cycle fill notionals, not `position_trades.quantity`.
Calculate `pnl_pct` only when exact per-cycle leverage/margin is available;
otherwise return `None`. Apply the optional strategy source filter to cycles
before grouping and compute win/loss statistics from those cycles rather than
from the lifetime summary rows.

```python
key = (cycle["account_id"], cycle["symbol"], cycle["direction"])
summary[key]["entry_notional"] += sum(
    float(fill["price"]) * float(fill["quantity"])
    for fill in cycle["entry_fills"]
)
summary[key]["quantity"] += cycle["entry_quantity"]
summary[key]["position_count"] += 1
```

- [ ] **Step 5: Implement fallback and cursor validation**

When no fills exist in the requested scope, aggregate cycle-level
`position_trades` by account/symbol/side and return
`reconcile_status="incomplete"`. Encode the cursor as URL-safe base64 JSON
containing `[exit_time, symbol, side]`; reject malformed cursors with
`ValueError("invalid_history_cursor")`. Apply date filters to cycle `exit_time`.

- [ ] **Step 6: Run history and schema tests**

Run: `python -m pytest tests/test_trade_history_summary.py tests/test_init_sql_schema.py -v`

Expected: PASS.

- [ ] **Step 7: Commit the summary projection**

```bash
git add shared/trade_history.py shared/db.py db/init.sql tests/test_trade_history_summary.py tests/test_init_sql_schema.py
git commit -m "feat: add canonical historical trade summaries"
```

---

### Task 3: Slim and Persist the Account Status Snapshot

**Files:**
- Create: `shared/account_status_snapshot.py`
- Modify: `api/main.py`
- Create: `tests/test_trading_api_split.py`
- Modify: `tests/test_multi_account_trading.py`

**Interfaces:**
- Produces: `load_account_snapshot(path: str) -> dict | None`.
- Produces: `save_account_snapshot(path: str, payload: dict) -> None` using atomic replace.
- `GET /api/trading/accounts/status` returns `accounts`, `summary`, `snapshot_at`, `age_seconds`, `fresh`, and `last_error`.

- [ ] **Step 1: Write failing snapshot ownership tests**

```python
def test_account_status_snapshot_excludes_lazy_sections(client, seeded_snapshot):
    response = client.get("/api/trading/accounts/status")
    assert response.status_code == 200
    account = response.json()["accounts"][0]
    assert "recent_trades" not in account
    assert "decision_panel" not in account
    assert "runtime_diagnostics" not in account


def test_snapshot_is_loaded_from_disk_without_exchange_calls(tmp_path):
    save_account_snapshot(tmp_path / "snapshot.json", {"accounts": [{"account_id": 1}]})
    assert load_account_snapshot(tmp_path / "snapshot.json")["accounts"][0]["account_id"] == 1
```

- [ ] **Step 2: Run the snapshot tests and verify failure**

Run: `python -m pytest tests/test_trading_api_split.py tests/test_multi_account_trading.py -k snapshot -v`

Expected: FAIL because lazy sections remain and persistence helpers are absent.

- [ ] **Step 3: Implement atomic snapshot persistence**

Write JSON to `<path>.tmp`, flush and `os.fsync`, then `os.replace`. Loading a
missing or invalid file returns `None` and logs a warning without failing API
startup. Use `.runtime/trading-account-status.json` by default.

- [ ] **Step 4: Slim `_account_status_payload`**

Remove `fetch_position_trade_groups`, `_account_decision_panel`, and
`build_live_diagnostics` from the account refresh path. Retain local position
management enrichment and portfolio totals needed by the live page. Derive
closed-trade statistics outside the snapshot only in the lazy history response.

- [ ] **Step 5: Persist only successful refreshes and expose freshness metadata**

On startup, load the persisted snapshot before starting the refresher. Run
`_refresh_all_account_statuses_sync` only through `asyncio.to_thread`. On refresh
failure preserve the last successful `accounts` payload and update `last_error`;
never replace it with an empty snapshot.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest tests/test_trading_api_split.py tests/test_multi_account_trading.py -k "snapshot or account_status" -v`

Expected: PASS.

- [ ] **Step 7: Commit snapshot isolation**

```bash
git add shared/account_status_snapshot.py api/main.py tests/test_trading_api_split.py tests/test_multi_account_trading.py
git commit -m "feat: serve persisted account status snapshots"
```

---

### Task 4: Add Replacement APIs and Delete Legacy Routes

**Files:**
- Modify: `api/main.py`
- Modify: `tests/test_trading_api_split.py`

**Interfaces:**
- Consumes: `fetch_trade_history_summaries(...)` from Task 2.
- Consumes: `_account_decision_panel(conn, account_id)`.
- Produces: account history, account decisions, and runtime status endpoints from the spec.

- [ ] **Step 1: Write failing route-contract tests**

```python
@pytest.mark.parametrize("path", [
    "/api/trading/status", "/api/trading/statu", "/api/trading/stats",
])
def test_legacy_trading_routes_are_removed(client, path):
    assert client.get(path).status_code == 404


def test_history_route_returns_one_symbol_direction_row(client):
    response = client.get("/api/trading/accounts/2/history?limit=20")
    assert response.status_code == 200
    keys = [(r["symbol"], r["side"]) for r in response.json()["items"]]
    assert len(keys) == len(set(keys))
```

- [ ] **Step 2: Run the route tests and verify failure**

Run: `python -m pytest tests/test_trading_api_split.py -k "route or history or decisions or runtime" -v`

Expected: FAIL because legacy routes still exist and replacements are absent.

- [ ] **Step 3: Add history validation and response mapping**

Validate account existence before reading history. Convert
`invalid_history_cursor` and invalid direction/date values to HTTP 400 with a
stable `detail.code`. Return 404 for an unknown account. Do not invoke exchange
or rebuild functions.

- [ ] **Step 4: Add account decisions and runtime status routes**

The decisions handler opens one local connection and returns
`_account_decision_panel`. Runtime status returns safe trading controls and
`build_live_diagnostics` for configured accounts; it must not pass an exchange
object or trigger credential validation.

```python
@app.get("/api/trading/accounts/{account_id}/decisions")
async def get_account_decisions(account_id: int, user=Depends(get_user)):
    account = get_account(account_id)
    if not account:
        raise HTTPException(status_code=404, detail={"code": "account_not_found"})
    with closing(get_conn()) as conn:
        return _account_decision_panel(conn, account_id)
```

- [ ] **Step 5: Delete the old aggregate implementation and cache aliases**

Remove `get_trading_status`, `get_trading_stats`, `_build_local_trading_status`,
legacy route decorators, `_FAST_CACHE_PATHS` entries, middleware alias copying,
startup status warming, and obsolete trading cache globals/imports.

- [ ] **Step 6: Prove handlers remain responsive during exchange timeout**

Patch `BinanceFutures.get_margin_balance` to sleep/raise while a background
refresh runs. In the same test request all replacement GET routes and assert
they complete from local data without calling the patched method in the request
thread.

- [ ] **Step 7: Run API tests and commit**

Run: `python -m pytest tests/test_trading_api_split.py tests/test_multi_account_trading.py -v`

Expected: PASS.

```bash
git add api/main.py tests/test_trading_api_split.py tests/test_multi_account_trading.py
git commit -m "feat: split account trading APIs"
```

---

### Task 5: Add Frontend Trading Data Clients

**Files:**
- Create: `frontend/src/api/tradingData.js`
- Create: `frontend/src/components/tradingData.test.mjs`
- Delete: `frontend/src/api/tradingAccountsStatus.js`
- Delete: `frontend/src/components/tradingAccountsStatus.test.mjs`

**Interfaces:**
- Produces: `fetchTradingAccounts(options)`.
- Produces: `fetchTradingAccountsStatus({force, signal})` with 30-second dedupe.
- Produces: `fetchTradingHistory(accountId, params, {signal})`.
- Produces: `fetchTradingDecisions(accountId, {signal})`.
- Produces: `fetchTradingRuntimeStatus({signal})`.

- [ ] **Step 1: Write failing client tests**

Test exact URLs, account ID encoding, omission of empty query parameters,
30-second status deduplication, non-deduped lazy history, HTTP error messages,
and forwarding of `AbortSignal`.

```javascript
test('history encodes account and filters', async () => {
  const calls = [];
  const client = createTradingDataClient(async (url, options) => {
    calls.push([url, options]);
    return { ok: true, json: async () => ({ items: [] }) };
  });
  await client.history(2, { symbol: 'AKEUSDT', direction: 'LONG', limit: 20 });
  assert.equal(calls[0][0], '/api/trading/accounts/2/history?symbol=AKEUSDT&direction=LONG&limit=20');
});
```

- [ ] **Step 2: Run frontend tests and verify failure**

Run: `npm test -- --test-name-pattern="trading data"`

Working directory: `frontend`

Expected: FAIL because `tradingData.js` does not exist.

- [ ] **Step 3: Implement the focused client module**

Use one `requestJson` helper, preserve only status in-flight/cached state, and
pass cancellation signals directly to `fetch`.

```javascript
async function requestJson(fetchImpl, url, signal) {
  const response = await fetchImpl(url, { signal });
  if (!response.ok) throw new Error(`${url}: ${response.status}`);
  return response.json();
}
```

- [ ] **Step 4: Run tests and replace the old client**

Run: `npm test`

Working directory: `frontend`

Expected: PASS.

- [ ] **Step 5: Commit frontend clients**

```bash
git add frontend/src/api/tradingData.js frontend/src/components/tradingData.test.mjs frontend/src/api/tradingAccountsStatus.js frontend/src/components/tradingAccountsStatus.test.mjs
git commit -m "feat: add split trading API clients"
```

---

### Task 6: Migrate Live Trading to Polling and Lazy Sections

**Files:**
- Modify: `frontend/src/components/LiveTrading.jsx`
- Modify: `frontend/src/components/liveTradingAccountSelection.test.mjs`
- Modify: `frontend/src/styles.css` only if an existing state lacks styling.

**Interfaces:**
- Consumes: all functions exported by `frontend/src/api/tradingData.js`.
- Produces: no request to a removed route and no frontend history aggregation.

- [ ] **Step 1: Add a failing source-contract test**

Read `LiveTrading.jsx` in the Node test and assert it contains calls through the
new client, contains no `/api/trading/status`, and does not use `reduce` or a
grouping map on historical rows. Keep account-selection unit tests intact.

- [ ] **Step 2: Run the focused frontend tests and verify failure**

Run: `npm test -- --test-name-pattern="live trading"`

Working directory: `frontend`

Expected: FAIL because the component still fetches the old endpoint.

- [ ] **Step 3: Split initial loading from polling**

Initial load requests accounts, account snapshot, and runtime status. The
30-second interval refreshes only status and runtime. Remove `status` state and
the fallback `status?.runtime_diagnostics` path.

- [ ] **Step 4: Add cancellable lazy history and decisions effects**

When the selected account changes, abort the prior history and decisions
requests, reset paging, and load the new account. Keep history rows exactly as
returned by the backend. Use `position_count` for the badge text:

```jsx
{t.position_count > 1 ? (
  <span className="mini-pill" style={{ marginLeft: 6 }}>
    合并 {t.position_count}
  </span>
) : null}
```

- [ ] **Step 5: Switch history paging to server cursors**

Store pages as `{items, next_cursor, stats}`. Next requests use the current
`next_cursor`; previous uses a stack of prior cursors. Changing account,
strategy source, symbol, or direction clears the cursor stack and reloads page
one by passing the filter to the backend. Do not slice or aggregate the returned list.

- [ ] **Step 6: Run frontend tests and production build**

Run: `npm test && npm run build`

Working directory: `frontend`

Expected: all tests pass and Vite completes without warnings about unresolved imports.

- [ ] **Step 7: Commit the live page migration**

```bash
git add frontend/src/components/LiveTrading.jsx frontend/src/components/liveTradingAccountSelection.test.mjs frontend/src/styles.css
git commit -m "feat: migrate live trading to split APIs"
```

---

### Task 7: Documentation, Full Verification, and Performance Probe

**Files:**
- Modify: `README.md`
- Modify: test files only when a documented old-route expectation remains.

**Interfaces:**
- Verifies all interfaces from Tasks 1-6.

- [ ] **Step 1: Replace endpoint documentation**

Document the five replacement GET routes, 30-second snapshot freshness, lazy
history/decisions behavior, cursor fields, and the one-row history grouping
contract. Remove every old route example.

- [ ] **Step 2: Search for forbidden legacy references**

Run:

```bash
rg -n "/api/trading/(status|statu|stats)" api frontend shared tests README.md
```

Expected: only explicit 404 assertions in `tests/test_trading_api_split.py`.

- [ ] **Step 3: Run the complete backend suite**

Run: `python -m pytest -q`

Expected: PASS with no failures.

- [ ] **Step 4: Run frontend tests and build**

Run: `npm test && npm run build`

Working directory: `frontend`

Expected: PASS and successful production bundle.

- [ ] **Step 5: Run API latency and timeout isolation probes**

Start the API locally, seed a persisted snapshot, and measure each replacement
route with `curl`. Confirm lightweight server-side requests are below 100 ms in
the local environment and a forced background Binance timeout does not change
request latency. Record measured values in the final implementation report.

- [ ] **Step 6: Verify the AKE production-shaped fixture**

Run:

```bash
python -m pytest tests/test_trade_history_summary.py -k ake -vv
```

Expected: one AKEUSDT LONG summary, quantity 169001, and weighted entry price
approximately `0.00873160732776729`.

- [ ] **Step 7: Commit documentation and final test adjustments**

```bash
git add README.md tests frontend/src
git commit -m "docs: document split trading APIs"
```

- [ ] **Step 8: Inspect final repository state**

Run: `git status --short && git log --oneline -8`

Expected: only pre-existing `.runtime` PID changes remain; implementation files
are committed in the seven task commits.
