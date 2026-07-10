# Dual-Market Candle Pipeline Design

## 1. Decision Summary

DarkHorse will only collect and analyze symbols that have both a spot market and a Binance USDT perpetual futures market.

- Normal pool: top 150 symbols ranked by effective two-sided turnover.
- Alpha pool: top 80 Alpha spot symbols that map to a Binance USDT perpetual.
- Open positions are always collected until fully closed, even after falling out of a top pool.
- Spot and futures candles are stored separately and never overwrite or aggregate into each other.
- Futures candles drive executable price structure, ATR, leverage, stops, R:R, and post-exit review.
- Spot and futures volume are analyzed separately, then combined only as derived confirmation/divergence features.
- Every candle table keeps at most 90 days of data.

## 2. Current Problems

The normal collector selects symbols from Binance Futures but requests candles from the Binance Spot endpoint. Futures-only symbols therefore return HTTP 400 and remain stale. The `symbols` table also accumulates historical members because symbols that leave the current top pool are not deactivated.

The Alpha collector correctly maps Alpha spot symbols to futures, but it writes mapped futures candles into the shared `candles_*` tables. Because those tables use `(time, symbol)` as the primary key, spot and futures data cannot coexist for the same symbol and source identity is lost.

These issues propagate into scoring, ATR, risk sizing, breakout detection, exit review, and frontend freshness reporting.

## 3. Goals

1. Build explicit normal Top 150 and Alpha Top 80 universes.
2. Require both spot and futures availability before allowing a new position.
3. Collect both markets for every selected symbol.
4. Keep source identity through storage, feature computation, decisions, and diagnostics.
5. Prevent stale or one-sided data from entering the opening workflow.
6. Preserve position management when a held symbol leaves the selected universe.
7. Keep API usage, SQLite size, and collection duration bounded.

## 4. Non-Goals

- Do not merge spot and futures OHLCV into a synthetic candle.
- Do not increase the normal pool beyond 150 or Alpha pool beyond 80.
- Do not allow Alpha shorts.
- Do not redesign existing category scoring unrelated to dual-market data.
- Do not change account position limits, stop percentages, or leverage bands in this change.

## 5. Universe Construction

### 5.1 Normal Top 150

Refresh every 10 minutes from public Binance metadata and 24-hour tickers.

Eligibility requirements:

- Spot symbol status is `TRADING`.
- Spot quote asset is `USDT`.
- Futures symbol status is `TRADING`.
- Futures contract type is `PERPETUAL`.
- Futures quote asset is `USDT`.
- Symbol exists with the same Binance symbol on both markets.
- Exclude leveraged-token naming patterns such as `UP`, `DOWN`, `BULL`, and `BEAR` where applicable.

Ranking value:

```text
effective_quote_volume_24h = min(
    spot_quote_volume_24h,
    futures_quote_volume_24h
)
```

Sort descending by `effective_quote_volume_24h`, then by futures quote volume, then by symbol. Select the first 150.

Using the minimum rather than the sum prevents a deep futures market with a shallow spot market, or the reverse, from appearing more liquid than it really is.

### 5.2 Alpha Top 80

Eligibility requirements:

- Alpha spot symbol exists and is `TRADING`.
- Alpha 24-hour quote volume is greater than zero.
- Base asset maps to a Binance `TRADING` USDT perpetual.
- Binance futures 24-hour quote volume is at least `100,000 USDT`.

Rank descending by Alpha spot 24-hour quote volume and select the first 80. Keep the current Alpha Top 80 behavior; only storage and futures-source handling change.

### 5.3 Forced Collection Set

Build an additional set from current exchange positions and local `position_history`.

- A held normal symbol forces Binance spot and futures collection.
- A held Alpha symbol forces Alpha spot and mapped futures collection.
- Forced symbols are not automatically eligible for a new entry.
- Remove the force only after the exchange position is confirmed closed.

The final collection set is the selected universe union forced positions. Normal entry selection still uses only Top 150; Alpha entry selection still uses only Top 80.

## 6. Database Design

### 6.1 Existing Spot Tables

Keep these tables and make their meaning explicit:

- `candles_15m`: Binance spot only.
- `candles_1h`: Binance spot only.
- `candles_6h`: Binance spot only.
- `candles_24h`: Binance spot only.
- `alpha_candles_*`: Alpha spot only.

### 6.2 New Futures Tables

Create:

- `futures_candles_15m`
- `futures_candles_1h`
- `futures_candles_6h`
- `futures_candles_24h`

Each table uses:

```sql
CREATE TABLE futures_candles_<interval> (
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    volume REAL DEFAULT 0,
    quote_vol REAL DEFAULT 0,
    trades INTEGER DEFAULT 0,
    PRIMARY KEY (time, symbol)
);
```

Required indexes:

```sql
CREATE INDEX idx_fc_<interval>_symbol_time
ON futures_candles_<interval>(symbol, time DESC);

CREATE INDEX idx_fc_<interval>_time_symbol
ON futures_candles_<interval>(time DESC, symbol);
```

### 6.3 Current Universe Table

Create `market_universe` to make the active pool auditable:

```sql
CREATE TABLE market_universe (
    pool_type TEXT NOT NULL,
    source_symbol TEXT NOT NULL,
    spot_symbol TEXT NOT NULL,
    futures_symbol TEXT NOT NULL,
    spot_quote_volume_24h REAL DEFAULT 0,
    futures_quote_volume_24h REAL DEFAULT 0,
    effective_quote_volume_24h REAL DEFAULT 0,
    universe_rank INTEGER,
    selected INTEGER DEFAULT 0,
    forced_position INTEGER DEFAULT 0,
    data_ready INTEGER DEFAULT 0,
    data_error TEXT,
    data_checked_at TEXT,
    selection_reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (pool_type, source_symbol)
);
```

`pool_type` is `normal` or `alpha`. For Alpha, `source_symbol` and `spot_symbol` are the Alpha symbol while `futures_symbol` is the mapped Binance contract.

Refresh the table transactionally each cycle. Rows not present in the new eligible set are marked `selected=0`; they are not left active indefinitely.

## 7. Collection Flow

### 7.1 Normal Pipeline

1. Fetch Binance spot exchange info and spot 24-hour tickers once.
2. Fetch Binance futures exchange info and futures 24-hour tickers once.
3. Build the intersection and rank Top 150.
4. Add forced normal positions.
5. Fetch spot candles from `/api/v3/klines` into `candles_*`.
6. Fetch futures candles from `/fapi/v1/klines` into `futures_candles_*`.
7. Batch `INSERT OR REPLACE` rows in one transaction per table.
8. Write coverage and failure counts to logs.

### 7.2 Alpha Pipeline

1. Refresh Alpha token and exchange-symbol metadata.
2. Resolve Binance futures mappings.
3. Fetch Binance futures tickers for the liquidity floor.
4. Select Alpha Top 80 and add forced Alpha positions.
5. Fetch Alpha spot candles into `alpha_candles_*`.
6. Add mapped futures symbols to the shared futures collection set.
7. Write mapped futures candles only into `futures_candles_*`.

### 7.3 Futures Request Deduplication

Normal Top 150 and Alpha Top 80 may map to the same futures symbol. Build one unique futures-symbol set before requesting futures candles. A symbol is requested once per interval per collection cycle.

### 7.4 Intervals and Backfill

Collection cadence remains every 10 minutes.

- Every cycle re-fetches the latest 48 candles for each interval and uses `INSERT OR REPLACE`.
- Re-fetching the rolling 48-candle window automatically repairs recent gaps without a separate repair queue.
- A failed symbol is retried immediately up to two additional times in the same cycle.
- Initial backfill may request older pages, but normal cycles never re-download the full history.
- All intervals are pruned at 90 days.

The implementation intentionally avoids a separate gap-repair scheduler, snapshot state machine, or multi-stage alert workflow.

## 8. Data Quality Gates

After each collection cycle, perform one readiness check for every selected or forced symbol:

- Spot 15m newest candle age must be at most 20 minutes.
- Futures 15m newest candle age must be at most 20 minutes.
- Spot 1h newest candle age must be at most 75 minutes.
- Futures 1h newest candle age must be at most 75 minutes.
- At least 32 closed 15m candles must exist on both required markets.
- At least 48 closed 1h candles must exist on both required markets.

For Alpha, replace Binance spot checks with Alpha spot checks. If all checks pass, set `market_universe.data_ready=1` and clear `data_error`. Otherwise set `data_ready=0`, store one concise reason, and keep the symbol out of scoring and new entries until a later cycle succeeds.

Reject reasons are explicit:

- `spot_candles_stale`
- `futures_candles_stale`
- `spot_candles_insufficient`
- `futures_candles_insufficient`
- `not_in_selected_universe`

Engine only scores rows with `selected=1 AND data_ready=1`. Trader checks `data_ready` again immediately before submitting a new opening order, so an old scan result cannot bypass the gate.

Held-position risk management never stops because `data_ready=0`. Hard stops and protective futures-side logic continue from exchange Mark Price and futures data; only new entries, additions, and optional volume-based exits are blocked or degraded to hold.

## 9. Feature Ownership

### 9.1 Futures-Owned Features

Use futures candles for features tied to executable price and risk:

- Current price and trend direction.
- EMA structure and slope.
- ATR and ATR percentage.
- Breakout levels and recent highs/lows.
- R:R and structural stop distance.
- Dynamic leverage and margin sizing.
- Trailing stops and profit giveback.
- Post-exit MFE/MAE and policy-loop review.

### 9.2 Spot-Owned Features

Use spot or Alpha spot candles for:

- Spot quote-volume ratio.
- Spot volume acceleration.
- Spot price/volume confirmation.
- Alpha burst-volume detection.
- Spot-side liquidity authenticity.

### 9.3 Dual-Market Features

Compute, but do not persist as replacement candles:

```text
spot_volume_ratio = latest_closed_spot_quote_vol / median(previous_20_spot_quote_vol)
futures_volume_ratio = latest_closed_futures_quote_vol / median(previous_20_futures_quote_vol)

spot_volume_strength = clamp(log2(max(spot_volume_ratio, 1)) / 2, 0, 1)
futures_volume_strength = clamp(log2(max(futures_volume_ratio, 1)) / 2, 0, 1)

volume_sync_score = 100 * min(spot_volume_strength, futures_volume_strength)
```

Derived states:

- `synchronized_expansion`: both ratios at least `1.5`.
- `strong_synchronized_expansion`: both ratios at least `2.0`.
- `futures_only_expansion`: futures at least `2.0`, spot below `1.2`.
- `spot_only_expansion`: spot at least `2.0`, futures below `1.2`.
- `neutral`: none of the above.

Opening score behavior:

- Synchronized expansion may add confirmation points.
- Futures-only expansion receives no spot-confirmation bonus and may receive a manipulation/divergence penalty for Alpha.
- Spot-only expansion remains observation until futures price structure confirms.
- Never sum spot and futures quote volume into a single raw volume field.

## 10. Module Impact

### `shared/db.py`

- Add futures candle tables, indexes, insert helpers, fetch helpers, retention cleanup, and universe table.
- Keep old fetch functions explicitly spot-only.
- Add source-specific names such as `fetch_spot_klines_1h` and `fetch_futures_klines_1h` to prevent accidental ambiguity.

### `pipeline/binance_http.py` and `pipeline/main.py`

- Replace futures-ranked/spot-request mismatch with intersection construction.
- Fetch normal Top 150 from both endpoints.
- Stop accumulating stale `symbols.is_active=1` rows; synchronize active state with `market_universe`.

### `alpha_pipeline/collector.py`

- Keep Top 80.
- Stop writing mapped futures candles into `candles_*`.
- Use shared futures-table insertion and request deduplication.

### `engine/db.py`, `engine/run.py`, and analyzers

- Load both spot and futures candles.
- Use futures candles for price/risk features.
- Add dual-volume features and data-quality gates.
- Score only current normal Top 150.

### `alpha_engine`

- Load Alpha spot and mapped futures candles separately.
- Preserve Alpha long-only behavior.
- Score only current Alpha Top 80.

### `trader/risk.py`, `trader/selection.py`, and `trader/execution.py`

- Use futures ATR and futures price structure.
- Require current universe membership and dual-market freshness for new opens.
- Exempt existing positions from universe membership while preserving futures freshness requirements.

### `shared/policy_loop.py`

- Use futures 1h candles for post-exit MFE/MAE because exits occur in futures.
- Store spot/futures volume state in evidence JSON when it explains an entry or exit.

### API and Frontend

Expose for each candidate:

- Pool rank and effective turnover.
- Spot/futures newest candle times.
- Spot/futures volume ratios.
- Volume synchronization state and score.
- Data-quality rejection reason.

The existing candidate tables remain the primary UI surface; no separate marketing-style page is required.

## 11. Retention and SQLite Controls

Run cleanup after a successful collection cycle, no more than once per day:

```sql
DELETE FROM <candle_table>
WHERE time < datetime('now', '-90 days');
```

Use indexed time columns, batched deletes, WAL mode, and a bounded checkpoint after cleanup. Do not run `VACUUM` every cycle. Run it manually or on a low-frequency maintenance schedule only when free pages are materially high.

## 12. Migration and Rollout

1. Back up `alphadog.db` using SQLite online backup.
2. Create new tables and indexes additively.
3. Stop Pipeline, Alpha Pipeline, Engine, Trader, and API writers.
4. Rebuild `market_universe` from live exchange metadata.
5. Clear mixed-source rows from `candles_*`; these rows cannot be reliably relabeled after the fact.
6. Backfill normal Top 150 spot candles into `candles_*`.
7. Backfill unioned normal/Alpha futures candles into `futures_candles_*`.
8. Refresh Alpha Top 80 into `alpha_candles_*`.
9. Validate row counts, freshness, timestamp alignment, and source isolation.
10. Switch Engine, Alpha Engine, Trader, policy loop, and API readers.
11. Restart all services and require one successful collection/scoring cycle before considering the migration complete.

Rollback keeps the database backup and code branch. Because the schema change is additive, rollback can restore the backup and previous code without reverse-migrating tables.

## 13. Monitoring

Keep monitoring intentionally small. Every cycle logs and exposes:

- Normal readiness, for example `150/150`.
- Alpha readiness, for example `80/80`.
- Forced-position count.
- Symbols with `data_ready=0` and their single `data_error` reason.
- Collection duration.

The frontend shows green only when the selected pool is fully ready. Otherwise it shows the ready count and missing symbols. New entries are automatically disabled only for the affected symbols; no separate alert state machine is introduced.

The service log uses warning level when readiness is below the selected count or collection duration exceeds 10 minutes.

## 14. Testing

Unit tests:

- Universe intersection and deterministic Top 150/80 ranking.
- Effective turnover uses the minimum, not a sum.
- Forced positions remain in collection but not entry eligibility.
- Spot writes cannot enter futures tables and vice versa.
- Duplicate futures symbols across normal and Alpha pools are requested once.
- Freshness and bar-count gates produce exact rejection reasons.
- Rolling 48-candle upserts restore a simulated recent gap.
- A failed symbol is retried at most two additional times.
- `data_ready=0` prevents scoring and opening.
- Dual-market volume states cover synchronized and divergent scenarios.
- ATR, R:R, and post-exit review read futures candles.

Integration tests:

- Run one collection cycle against mocked spot/futures/Alpha APIs.
- Verify selected counts, source-isolated rows, and universe state.
- Verify readiness counts become `150/150` and `80/80` after a successful cycle.
- Simulate a symbol leaving Top 150 while held.
- Simulate one market becoming stale while futures hard-stop management continues.
- Verify 90-day cleanup leaves current data intact.

## 15. Acceptance Criteria

The change is complete only when:

1. Normal pool contains exactly Top 150 eligible dual-market symbols, or all eligible symbols if fewer than 150.
2. Alpha pool contains exactly Top 80 mapped symbols, or all eligible symbols if fewer than 80.
3. Every selected symbol has fresh required candles on both markets.
4. No selected futures-only symbol is sent to the Binance spot K-line endpoint.
5. `candles_*` contains Binance spot only, `futures_candles_*` contains Binance futures only, and `alpha_candles_*` contains Alpha spot only.
6. Engine and trader decisions expose source-specific freshness and volume evidence.
7. New entries are blocked on stale or incomplete dual-market data.
8. Existing positions continue risk management after leaving a top pool.
9. B2-style mapped Alpha positions retain correct Alpha and futures provenance.
10. All candle tables contain no rows older than 90 days.
11. A failed or incomplete symbol is marked `data_ready=0`, excluded from scoring, and rejected again by Trader before opening.
