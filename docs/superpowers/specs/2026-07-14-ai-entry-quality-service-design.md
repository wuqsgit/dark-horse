# AI Entry Quality Service Design

## Goal

Add an independent AI service that reviews planned entries without owning exchange credentials or order execution. Once a model is ready, entries with quality score >=62 are allowed, scores from 55 to <62 are reduced to a 5% margin probe, and scores below 55 are rejected.

## Runtime Rules

- AI service runs independently on port 8010 and is included in one-click startup.
- If the AI service is unavailable, times out, or reports an expired model, Trader blocks every new entry but continues managing existing positions.
- If the AI service is healthy but a model is still collecting samples, existing entry rules remain active for that model group.
- Alpha and normal entries use separate models. Normal coin categories are model features in the first version.
- A ready model takes effect immediately; there is no shadow-only period.

## Data and Labels

- AI data is written to `db/ai_quality.db`; `alphadog.db` is read-only to the AI service.
- Training samples are deduplicated by model group, symbol, side, template, and UTC hour.
- Each sample stores a fixed numeric feature vector, entry price, stop percentage, and decision context.
- After 24 hours, futures 15-minute candles label the sample positive only when +1R is reached before -1R. A same-candle collision is labeled negative, and no +1R within 24 hours is negative.
- Compact samples and decisions are retained for 180 days. Raw K-line retention remains unchanged.

## Model Lifecycle

- XGBoost is used for the first version.
- Training runs once daily and may also be triggered through an internal API.
- Time order is preserved: the newest 20% is validation data.
- Publishing requires at least 300 total labeled samples, at least 60 validation samples, and allowed-group average R above the unfiltered baseline.
- Published artifacts and metadata live under `models/entry_quality/`.
- Models expire after 48 hours without a successful replacement.

## Interfaces

- `POST /v1/entry-quality/evaluate`: persist a candidate and return allow, probe, reject, or collecting.
- `POST /v1/outcomes/label`: label all eligible pending samples.
- `POST /v1/models/train`: train and publish eligible models.
- `GET /v1/status`: health, model state, versions, sample progress, and daily counters.
- `GET /v1/decisions`: recent decisions for frontend display and audit.

## Observability

- Header status shows `AI LIVE`, `AI COLLECTING`, or `AI ERROR`.
- Status details show per-model version, training time, sample count, last evaluation, and daily allow/probe/reject counts.
- Scan and Alpha scan rows show the latest AI score and decision for each symbol.
- Every decision stores the model version, canonical inputs, outcome, reasons, account id, and whether it affected trading.

## Safety

- The AI service never receives Binance credentials and cannot place orders.
- Trader enforces the final decision and preserves all account, position, and hard-stop controls.
- Calls use a 300ms timeout. Transport failures fail closed for new entries only.
- Profit management and loss management for existing positions never depend on AI service availability.
