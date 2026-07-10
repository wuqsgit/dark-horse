# Dual-Market Candle Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build reliable Top 150 normal and Top 80 Alpha universes that require both spot and futures markets, store each market separately, and block new trades whenever either side of the candle set is stale or incomplete.

**Architecture:** Put deterministic universe selection and readiness checks in a small shared module, persist current state in `market_universe`, and keep spot, Alpha spot, and futures candles in separate tables. Pipelines own collection and readiness updates; Engine and Trader consume futures prices plus spot/futures volume features and enforce the same persisted readiness gate.

**Tech Stack:** Python 3, asyncio/aiohttp, SQLite, pytest, React/Vite.

## Global Constraints

- Normal universe contains only Binance symbols that are `TRADING` on spot and USDT perpetual futures, ranked by `min(spot_quote_volume_24h, futures_quote_volume_24h)`, Top 150.
- Alpha universe contains only Alpha symbols with a Binance USDT perpetual mapping and futures quote volume at least 100,000 USDT, ranked by Alpha spot quote volume, Top 80.
- Existing positions force candle collection until closed but never become eligible for a new entry solely because they are forced.
- `candles_*` stores Binance spot only, `alpha_candles_*` stores Alpha spot only, and `futures_candles_*` stores Binance futures only.
- Each cycle re-fetches the latest 48 candles, retries failures at most twice, and retains no candle older than 90 days.
- Readiness requires fresh 15m and 1h candles on both markets and at least 32 closed 15m plus 48 closed 1h candles per market.
- `data_ready=0` excludes scoring and blocks Trader entry; position protection continues independently.
- Price/trend/risk features use futures candles. Spot and Alpha spot remain the source for volume authenticity. Raw spot and futures volumes are never summed.

---

### Task 1: Shared Schema, Universe Selection, And Readiness

**Files:**
- Create: `shared/market_universe.py`
- Modify: `shared/db.py`
- Create: `tests/test_market_universe.py`
- Create: `tests/test_market_universe_db.py`

**Interfaces:**
- Produces: `build_normal_universe(...)`, `build_alpha_universe(...)`, `assess_dual_market_readiness(...)` in `shared.market_universe`.
- Produces: `upsert_market_universe(rows)`, `fetch_market_universe(pool_type=None, selected_only=False, ready_only=False)`, `update_market_readiness(...)`, `insert_futures_candles(table, rows)`, `fetch_futures_candles(table, symbols, ...)`, and `prune_candles(retention_days=90)` in `shared.db`.

- [ ] **Step 1: Write failing pure-function tests**

```python
def test_normal_pool_requires_both_markets_and_ranks_by_weaker_volume():
    rows = build_normal_universe(spot_markets, futures_markets, limit=2)
    assert [row["futures_symbol"] for row in rows] == ["BBBUSDT", "AAAUSDT"]
    assert rows[0]["effective_quote_volume_24h"] == 800_000

def test_readiness_rejects_stale_or_short_market():
    result = assess_dual_market_readiness(now, spot_state, futures_state)
    assert result.ready is False
    assert "futures_1h_count" in result.error
```

- [ ] **Step 2: Run `python -m pytest tests/test_market_universe.py -v` and confirm failure because `shared.market_universe` does not exist**

- [ ] **Step 3: Implement immutable selection and readiness helpers**

```python
@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    error: str | None

def assess_dual_market_readiness(now, spot, futures):
    checks = {
        "spot_15m_age": spot.age_15m <= timedelta(minutes=20),
        "spot_1h_age": spot.age_1h <= timedelta(minutes=75),
        "spot_15m_count": spot.count_15m >= 32,
        "spot_1h_count": spot.count_1h >= 48,
        "futures_15m_age": futures.age_15m <= timedelta(minutes=20),
        "futures_1h_age": futures.age_1h <= timedelta(minutes=75),
        "futures_15m_count": futures.count_15m >= 32,
        "futures_1h_count": futures.count_1h >= 48,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return ReadinessResult(not failed, ",".join(failed) or None)
```

- [ ] **Step 4: Run the pure-function tests and confirm they pass**

- [ ] **Step 5: Write failing SQLite schema and repository tests**

```python
def test_market_universe_and_futures_tables_are_created(temp_db):
    init_db()
    assert table_exists("market_universe")
    assert table_exists("futures_candles_15m")
    assert table_exists("futures_candles_1h")

def test_ready_only_query_excludes_forced_unready_symbols(temp_db):
    upsert_market_universe([forced_unready_row, selected_ready_row])
    assert fetch_market_universe("normal", selected_only=True, ready_only=True) == [selected_ready_row]
```

- [ ] **Step 6: Run `python -m pytest tests/test_market_universe_db.py -v` and confirm missing-table/helper failures**

- [ ] **Step 7: Add four futures candle tables, `market_universe`, indexes, repository helpers, and 90-day pruning**

```sql
CREATE TABLE IF NOT EXISTS market_universe (
  pool_type TEXT NOT NULL,
  source_symbol TEXT NOT NULL,
  spot_symbol TEXT,
  futures_symbol TEXT NOT NULL,
  spot_quote_volume_24h REAL NOT NULL DEFAULT 0,
  futures_quote_volume_24h REAL NOT NULL DEFAULT 0,
  effective_quote_volume_24h REAL NOT NULL DEFAULT 0,
  universe_rank INTEGER,
  selected INTEGER NOT NULL DEFAULT 0,
  forced_position INTEGER NOT NULL DEFAULT 0,
  data_ready INTEGER NOT NULL DEFAULT 0,
  data_error TEXT,
  data_checked_at TEXT,
  selection_reason TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (pool_type, source_symbol)
);
```

- [ ] **Step 8: Run Task 1 tests and the existing suite; confirm all pass**

### Task 2: Normal Top 150 Dual-Market Collector

**Files:**
- Modify: `pipeline/binance_http.py`
- Modify: `pipeline/main.py`
- Create: `pipeline/candle_health.py`
- Create: `tests/test_binance_dual_market_collector.py`
- Create: `tests/test_candle_health.py`

**Interfaces:**
- Consumes: Task 1 universe and DB helpers.
- Produces: `BinanceHTTPCollector.get_normal_universe(limit=150)`, market-specific K-line fetches, `retry_async(operation, retries=2)`, and `refresh_universe_readiness(pool_type)`.

- [ ] **Step 1: Write failing tests proving intersection selection, separate endpoints, 48-candle refresh, and three total attempts**

```python
async def test_normal_collector_writes_spot_and_futures_to_separate_tables():
    await collector.collect_all([selected_row])
    assert db.spot_rows[0][1] == "BTCUSDT"
    assert db.futures_rows[0][1] == "BTCUSDT"
    assert requests["spot_limit"] == 48
    assert requests["futures_limit"] == 48
```

- [ ] **Step 2: Run Task 2 tests and confirm they fail against the current futures-only ranking and spot-only writer**

- [ ] **Step 3: Split Binance metadata/ticker loading into spot and futures sources and build Top 150 through `build_normal_universe`**

```python
spot_exchange_info = await self.get_json("/api/v3/exchangeInfo")
futures_exchange_info = await self.get_json("/fapi/v1/exchangeInfo", futures=True)
spot_tickers = await self.get_json("/api/v3/ticker/24hr")
futures_tickers = await self.get_json("/fapi/v1/ticker/24hr", futures=True)
return build_normal_universe(spot_exchange_info, futures_exchange_info, spot_tickers, futures_tickers, limit)
```

- [ ] **Step 4: Add explicit spot and futures K-line methods and route writes only to their matching tables**

- [ ] **Step 5: Add bounded retries, post-cycle readiness calculation, and 90-day pruning**

- [ ] **Step 6: Change normal default pool size from 200 to 150 and include open positions as forced collection rows**

- [ ] **Step 7: Run Task 2 tests and the full Python test suite; confirm all pass**

### Task 3: Alpha Top 80 Dual-Market Collector

**Files:**
- Modify: `alpha_pipeline/collector.py`
- Modify: `alpha_pipeline/main.py`
- Create: `tests/test_alpha_dual_market_collector.py`

**Interfaces:**
- Consumes: Task 1 DB helpers and Task 2 retry/readiness helpers.
- Produces: persisted Alpha Top 80 rows and writes mapped Binance futures only to `futures_candles_*`.

- [ ] **Step 1: Write failing tests for mapped-only selection, futures volume floor, Top 80 limit, separate writes, and readiness**

```python
async def test_alpha_collector_skips_unmapped_and_low_liquidity_symbols():
    rows = await collector.build_universe(market_top_n=80)
    assert "ALPHA_UNMAPPEDUSDT" not in source_symbols(rows)
    assert "ALPHA_THINUSDT" not in source_symbols(rows)

async def test_alpha_futures_never_write_to_normal_spot_tables():
    await collector.collect_mapped_futures(rows)
    assert db.normal_spot_rows == []
    assert db.futures_rows
```

- [ ] **Step 2: Run Task 3 tests and confirm current writes to `candles_*` fail the assertions**

- [ ] **Step 3: Build and persist the mapped Top 80 Alpha universe using Alpha spot volume and Binance futures volume floor**

- [ ] **Step 4: Keep Alpha spot writes in `alpha_candles_*`, redirect all mapped futures writes to `futures_candles_*`, and skip symbols already refreshed in the normal cycle when their newest candle is current**

- [ ] **Step 5: Apply retries, readiness checks, forced-position inclusion, and 90-day pruning**

- [ ] **Step 6: Run Task 3 tests and the complete suite; confirm all pass**

### Task 4: Engine And Trader Data Ownership And Gates

**Files:**
- Modify: `engine/run.py`
- Modify: `engine/breakout_detector.py`
- Modify: `engine/db.py`
- Modify: `shared/db.py`
- Modify: `shared/policy_loop.py`
- Modify: `trader/runner.py`
- Modify: `trader/selection.py`
- Modify: `trader/risk.py`
- Create: `tests/test_engine_market_readiness.py`
- Create: `tests/test_trader_market_readiness.py`
- Create: `tests/test_market_data_ownership.py`

**Interfaces:**
- Consumes: persisted selected/ready universe and separate candle readers.
- Produces: Engine scoring limited to ready rows; Trader order submission guarded by a final readiness check; futures-owned trend/risk/review calculations.

- [ ] **Step 1: Write failing tests proving unready symbols are not scored or opened while open-position protection still runs**

```python
def test_engine_only_loads_selected_ready_symbols(db):
    assert fetch_active_symbols() == ["BTCUSDT"]

async def test_trader_rechecks_readiness_before_order(runner):
    result = await runner.try_open(unready_candidate)
    assert result.reason == "market_data_not_ready"
    assert exchange.orders == []
```

- [ ] **Step 2: Write failing ownership tests proving breakout, ATR, stop, and post-exit MFE/MAE use futures candles**

- [ ] **Step 3: Run Task 4 tests and confirm failures point to `candles_*` and missing readiness gates**

- [ ] **Step 4: Add explicit spot/Alpha spot/futures readers and return only selected ready symbols to normal and Alpha engines**

- [ ] **Step 5: Switch price/trend/risk/post-exit SQL to `futures_candles_*`; retain spot tables for normalized volume features**

- [ ] **Step 6: Calculate volume ratios separately and expose `volume_sync_score = min(normalized_spot_ratio, normalized_futures_ratio)` without summing raw volumes**

- [ ] **Step 7: Add the final Trader readiness check immediately before exchange order placement and log `market_data_not_ready` with `data_error`**

- [ ] **Step 8: Run Task 4 tests and full suite; confirm all pass**

### Task 5: API Visibility, Migration, Backfill, And Service Validation

**Files:**
- Modify: `api/main.py`
- Modify: `frontend/src/components/ScanTable.jsx`
- Modify: `frontend/src/components/AlphaScan.jsx`
- Modify: `frontend/src/styles.css`
- Create: `scripts/rebuild_market_candles.py`
- Create: `tests/test_market_data_api.py`
- Modify: `schema.sql`

**Interfaces:**
- Consumes: Task 1 `market_universe` state.
- Produces: `/api/market-data/health`, scan payload readiness fields, and a one-shot idempotent migration/backfill command.

- [ ] **Step 1: Write failing API tests for pool counts, ranks, latest spot/futures timestamps, normalized volume ratios, sync state, readiness, and errors**

```python
def test_market_data_health_reports_each_pool(client):
    payload = client.get("/api/market-data/health").json()
    assert payload["normal"]["selected"] == 150
    assert payload["alpha"]["limit"] == 80
    assert payload["normal"]["unready"] == 0
```

- [ ] **Step 2: Run the API tests and confirm 404 or missing-field failures**

- [ ] **Step 3: Add API health and scan response fields, then render compact ready/stale status and both market timestamps in normal and Alpha scan views**

- [ ] **Step 4: Implement an idempotent rebuild script that initializes schema, backs up the DB, clears mixed normal candle rows, rebuilds both universes, backfills spot/futures/Alpha candles, checks readiness, and exits nonzero if selected symbols remain unready**

- [ ] **Step 5: Regenerate `schema.sql` from the actual SQLite schema and run `python -m pytest -q` plus frontend build**

- [ ] **Step 6: Stop all writers, run the rebuild script, query selected/ready counts and max candle times, then restart Pipeline, Alpha Pipeline, Engine, Alpha Engine, Trader, API, and Frontend**

- [ ] **Step 7: Verify `http://127.0.0.1:8000/api/market-data/health` and `http://127.0.0.1:3000/`, inspect logs for collection/readiness/order errors, and confirm all selected rows are current before completion**

