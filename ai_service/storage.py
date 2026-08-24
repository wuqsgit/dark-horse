import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


_SCHEMA_LOCK = threading.RLock()


def _json(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"), default=str)


def _sample_bucket(value: str) -> str:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    minute = (dt.minute // 15) * 15
    return dt.replace(minute=minute, second=0, microsecond=0).isoformat().replace("+00:00", "Z")


class AIStore:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def init_db(self):
        with _SCHEMA_LOCK:
            conn = self.connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS entry_quality_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sample_key TEXT NOT NULL UNIQUE,
                    model_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    template TEXT NOT NULL,
                    category TEXT,
                    observed_at TEXT NOT NULL,
                    hour_bucket TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    stop_pct REAL NOT NULL,
                    features_json TEXT NOT NULL,
                    feature_schema_version INTEGER NOT NULL DEFAULT 1,
                    quality_json TEXT NOT NULL DEFAULT '{}',
                    label INTEGER,
                    first_event TEXT,
                    mfe_r REAL,
                    mae_r REAL,
                    label_status TEXT NOT NULL DEFAULT 'pending',
                    labeled_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_ai_samples_model_status
                    ON entry_quality_samples(model_key, label_status, observed_at);
                CREATE INDEX IF NOT EXISTS idx_ai_samples_symbol_time
                    ON entry_quality_samples(symbol, observed_at);

                CREATE TABLE IF NOT EXISTS entry_quality_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    account_id INTEGER,
                    model_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    model_version TEXT,
                    quality_score REAL,
                    decision TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    size_factor REAL,
                    expected_r REAL,
                    applied INTEGER NOT NULL DEFAULT 0,
                    reasons_json TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_ai_decisions_time
                    ON entry_quality_decisions(observed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_ai_decisions_symbol_time
                    ON entry_quality_decisions(symbol, observed_at DESC);

                CREATE TABLE IF NOT EXISTS entry_quality_models (
                    model_key TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    trained_at TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    validation_count INTEGER NOT NULL,
                    baseline_mean_r REAL,
                    allowed_mean_r REAL,
                    metrics_json TEXT NOT NULL,
                    feature_schema_version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS ai_model_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_key TEXT NOT NULL,
                    version TEXT,
                    status TEXT NOT NULL,
                    trained_at TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    validation_count INTEGER NOT NULL DEFAULT 0,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    feature_schema_version INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_ai_model_runs_key_time
                    ON ai_model_runs(model_key, trained_at DESC);

                CREATE TABLE IF NOT EXISTS ai_trade_attribution (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id INTEGER,
                    account_id INTEGER,
                    symbol TEXT NOT NULL,
                    model_key TEXT NOT NULL,
                    model_version TEXT,
                    rule_decision TEXT,
                    ai_decision TEXT,
                    execution_decision TEXT,
                    realized_pnl REAL,
                    realized_r REAL,
                    exit_reason TEXT,
                    opened_at TEXT,
                    closed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS alpha_strategy_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sample_key TEXT NOT NULL UNIQUE,
                    request_id TEXT NOT NULL,
                    market_env TEXT NOT NULL CHECK(market_env='mainnet'),
                    model_key TEXT NOT NULL,
                    futures_symbol TEXT NOT NULL,
                    alpha_symbol TEXT,
                    stage TEXT NOT NULL,
                    setup_type TEXT,
                    candle_close_time TEXT NOT NULL,
                    feature_schema_version INTEGER NOT NULL,
                    features_json TEXT NOT NULL,
                    quality_json TEXT NOT NULL,
                    label_status TEXT NOT NULL DEFAULT 'pending',
                    labels_json TEXT,
                    labeled_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_ai_alpha_samples_model_status
                    ON alpha_strategy_samples(
                        market_env, model_key, label_status, candle_close_time
                    );
                CREATE INDEX IF NOT EXISTS idx_ai_alpha_samples_symbol_time
                    ON alpha_strategy_samples(
                        market_env, futures_symbol, candle_close_time
                    );
                CREATE INDEX IF NOT EXISTS idx_ai_alpha_samples_model_time
                    ON alpha_strategy_samples(
                        model_key, candle_close_time DESC, id DESC
                    );

                CREATE TABLE IF NOT EXISTS alpha_strategy_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    market_env TEXT NOT NULL CHECK(market_env='mainnet'),
                    futures_symbol TEXT NOT NULL,
                    alpha_symbol TEXT,
                    stage TEXT NOT NULL,
                    setup_type TEXT,
                    candle_close_time TEXT NOT NULL,
                    status TEXT NOT NULL,
                    applied INTEGER NOT NULL DEFAULT 0,
                    recommended_action TEXT NOT NULL,
                    p_setup_success REAL,
                    p_followthrough REAL,
                    p_fakeout REAL,
                    expected_r REAL,
                    model_versions_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_ai_alpha_decisions_symbol_time
                    ON alpha_strategy_decisions(
                        market_env, futures_symbol, candle_close_time DESC
                    );

                CREATE TABLE IF NOT EXISTS alpha_strategy_models (
                    version TEXT PRIMARY KEY,
                    model_key TEXT NOT NULL,
                    market_env TEXT NOT NULL CHECK(market_env='mainnet'),
                    stage TEXT NOT NULL,
                    target TEXT NOT NULL,
                    status TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    trained_at TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    validation_count INTEGER NOT NULL,
                    feature_schema_version INTEGER NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_ai_alpha_models_lookup
                    ON alpha_strategy_models(
                        market_env, model_key, target, status, trained_at DESC
                    );

                CREATE TABLE IF NOT EXISTS alpha_strategy_model_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_key TEXT NOT NULL,
                    market_env TEXT NOT NULL CHECK(market_env='mainnet'),
                    stage TEXT NOT NULL,
                    target TEXT NOT NULL,
                    version TEXT,
                    status TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    validation_count INTEGER NOT NULL DEFAULT 0,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS alpha_strategy_execution_outcomes (
                    account_id INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    setup_id TEXT,
                    futures_symbol TEXT NOT NULL,
                    position_id TEXT,
                    exchange_order_id TEXT,
                    quantity REAL,
                    entry_price REAL,
                    invalidation_price REAL,
                    realized_pnl REAL,
                    realized_r REAL,
                    exit_reason TEXT,
                    status TEXT NOT NULL,
                    model_versions_json TEXT NOT NULL DEFAULT '{}',
                    submitted_at TEXT,
                    closed_at TEXT,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (account_id, event_id, action_type)
                );
                CREATE INDEX IF NOT EXISTS idx_ai_alpha_execution_status
                    ON alpha_strategy_execution_outcomes(
                        status, futures_symbol, submitted_at DESC
                    );
                """
                )
                self._ensure_column(conn, "entry_quality_samples", "feature_schema_version", "INTEGER NOT NULL DEFAULT 1")
                self._ensure_column(conn, "entry_quality_samples", "quality_json", "TEXT NOT NULL DEFAULT '{}'")
                self._ensure_column(conn, "entry_quality_decisions", "expected_r", "REAL")
                self._ensure_column(conn, "entry_quality_decisions", "applied", "INTEGER NOT NULL DEFAULT 0")
                self._ensure_column(conn, "entry_quality_models", "feature_schema_version", "INTEGER NOT NULL DEFAULT 1")
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _ensure_column(conn, table: str, column: str, definition: str) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def add_sample(self, sample: dict) -> tuple[int, bool]:
        bucket = _sample_bucket(sample["observed_at"])
        template = str(sample.get("template") or "default")
        model_key = str(sample["model_key"])
        symbol = str(sample["symbol"]).upper()
        side = str(sample.get("side") or "LONG").upper()
        sample_key = f"{model_key}:{symbol}:{side}:{template}:{bucket}"
        conn = self.connect()
        try:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO entry_quality_samples
                   (sample_key, model_key, symbol, side, template, category, observed_at,
                    hour_bucket, entry_price, stop_pct, features_json,
                    feature_schema_version, quality_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sample_key, model_key, symbol, side, template, sample.get("category"),
                    sample["observed_at"], bucket, float(sample["entry_price"]),
                    float(sample["stop_pct"]), _json(sample.get("features")),
                    int(sample.get("feature_schema_version") or 1),
                    _json(sample.get("feature_quality")),
                ),
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                "SELECT id FROM entry_quality_samples WHERE sample_key=?", (sample_key,)
            ).fetchone()
            conn.commit()
            return int(row["id"]), created
        finally:
            conn.close()

    def add_alpha_strategy_sample(self, sample: dict) -> tuple[int, bool]:
        market_env = str(sample["market_env"]).lower()
        model_key = str(sample.get("model_key") or f"alpha_{sample['stage']}_v1")
        symbol = str(sample["futures_symbol"]).upper()
        stage = str(sample["stage"]).lower()
        candle_time = str(sample["candle_close_time"])
        schema = int(sample["feature_schema_version"])
        sample_key = (
            f"{market_env}:{model_key}:{symbol}:{stage}:{candle_time}:v{schema}"
        )
        conn = self.connect()
        try:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO alpha_strategy_samples
                   (sample_key, request_id, market_env, model_key,
                    futures_symbol, alpha_symbol, stage, setup_type,
                    candle_close_time, feature_schema_version,
                    features_json, quality_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sample_key,
                    sample["request_id"],
                    market_env,
                    model_key,
                    symbol,
                    sample.get("alpha_symbol"),
                    stage,
                    sample.get("setup_type"),
                    candle_time,
                    schema,
                    _json(sample.get("features")),
                    _json(sample.get("feature_quality")),
                ),
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                "SELECT id FROM alpha_strategy_samples WHERE sample_key=?",
                (sample_key,),
            ).fetchone()
            conn.commit()
            return int(row["id"]), created
        finally:
            conn.close()

    def backfill_alpha_trigger_samples(
        self,
        *,
        market_env: str | None = None,
        limit: int = 5000,
    ) -> int:
        """Bootstrap hourly trigger/fakeout counterfactuals from setup bars."""
        clauses = [
            "stage='setup'",
            "setup_type IS NOT NULL",
            "feature_schema_version=4",
            "strftime('%M', candle_close_time)='00'",
        ]
        params = []
        if market_env:
            clauses.append("market_env=?")
            params.append(str(market_env).lower())
        conn = self.connect()
        try:
            cursor = conn.execute(
                f"""INSERT OR IGNORE INTO alpha_strategy_samples
                    (sample_key, request_id, market_env, model_key,
                     futures_symbol, alpha_symbol, stage, setup_type,
                     candle_close_time, feature_schema_version,
                     features_json, quality_json)
                    SELECT
                        market_env || ':alpha_trigger_v1_' || market_env || ':' ||
                            futures_symbol || ':trigger:' || candle_close_time ||
                            ':v' || feature_schema_version,
                        request_id || ':counterfactual-trigger',
                        market_env,
                        'alpha_trigger_v1_' || market_env,
                        futures_symbol,
                        alpha_symbol,
                        'trigger',
                        setup_type,
                        candle_close_time,
                        feature_schema_version,
                        features_json,
                        quality_json
                    FROM alpha_strategy_samples
                    WHERE {' AND '.join(clauses)}
                    ORDER BY datetime(candle_close_time), id
                    LIMIT ?""",
                [*params, max(1, int(limit))],
            )
            conn.commit()
            return max(0, int(cursor.rowcount or 0))
        finally:
            conn.close()

    def record_alpha_strategy_decision(self, payload: dict, result: dict) -> int:
        conn = self.connect()
        try:
            cursor = conn.execute(
                """INSERT INTO alpha_strategy_decisions
                   (request_id, market_env, futures_symbol, alpha_symbol,
                    stage, setup_type, candle_close_time, status, applied,
                    recommended_action, p_setup_success, p_followthrough,
                    p_fakeout, expected_r, model_versions_json, reasons_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    payload["request_id"],
                    str(payload["market_env"]).lower(),
                    str(payload["futures_symbol"]).upper(),
                    payload.get("alpha_symbol"),
                    str(payload["stage"]).lower(),
                    payload.get("setup_type"),
                    payload["candle_close_time"],
                    result["status"],
                    int(bool(result.get("applied"))),
                    result["recommended_action"],
                    result.get("p_setup_success"),
                    result.get("p_followthrough"),
                    result.get("p_fakeout"),
                    result.get("expected_r"),
                    _json(result.get("model_versions")),
                    _json(result.get("reasons")),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def alpha_strategy_sample_counts(self, market_env: str | None = None) -> dict:
        conn = self.connect()
        try:
            if market_env:
                row = conn.execute(
                    """SELECT COUNT(*) total,
                              SUM(CASE WHEN label_status='ready' THEN 1 ELSE 0 END) labeled,
                              SUM(CASE WHEN label_status='pending' THEN 1 ELSE 0 END) pending
                       FROM alpha_strategy_samples WHERE market_env=?""",
                    (str(market_env).lower(),),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT COUNT(*) total,
                              SUM(CASE WHEN label_status='ready' THEN 1 ELSE 0 END) labeled,
                              SUM(CASE WHEN label_status='pending' THEN 1 ELSE 0 END) pending
                       FROM alpha_strategy_samples"""
                ).fetchone()
            return {
                "total": int(row["total"] or 0),
                "labeled": int(row["labeled"] or 0),
                "pending": int(row["pending"] or 0),
            }
        finally:
            conn.close()

    def alpha_strategy_quality_summary(
        self,
        market_env: str | None = None,
        *,
        limit: int = 1000,
    ) -> dict:
        clauses = []
        params = []
        if market_env:
            clauses.append("market_env=?")
            params.append(str(market_env).lower())
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self.connect()
        try:
            rows = conn.execute(
                f"""SELECT quality_json FROM alpha_strategy_samples
                    {where} ORDER BY id DESC LIMIT ?""",
                (*params, int(limit)),
            ).fetchall()
        finally:
            conn.close()
        qualities = [json.loads(row["quality_json"] or "{}") for row in rows]
        if not qualities:
            return {
                "samples": 0,
                "average_coverage": 0.0,
                "ready_rate": 0.0,
                "most_missing": [],
            }
        missing = {}
        for quality in qualities:
            for name in quality.get("missing_features") or []:
                missing[name] = missing.get(name, 0) + 1
        return {
            "samples": len(qualities),
            "average_coverage": round(
                sum(float(row.get("coverage") or 0) for row in qualities)
                / len(qualities),
                4,
            ),
            "ready_rate": round(
                sum(row.get("status") == "ready" for row in qualities)
                / len(qualities),
                4,
            ),
            "most_missing": [
                {"feature": name, "count": count}
                for name, count in sorted(
                    missing.items(),
                    key=lambda item: (-item[1], item[0]),
                )[:10]
            ],
        }

    def recent_alpha_strategy_samples(
        self,
        *,
        model_key: str,
        limit: int = 500,
    ) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT features_json, quality_json, candle_close_time
                   FROM alpha_strategy_samples
                   WHERE model_key=?
                   ORDER BY candle_close_time DESC, id DESC
                   LIMIT ?""",
                (model_key, int(limit)),
            ).fetchall()
            return [
                {
                    "features": json.loads(row["features_json"] or "{}"),
                    "feature_quality": json.loads(row["quality_json"] or "{}"),
                    "candle_close_time": row["candle_close_time"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    def pending_alpha_strategy_samples(
        self,
        *,
        before_time: str,
        market_env: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        clauses = [
            "label_status='pending'",
            "datetime(candle_close_time) <= datetime(?)",
        ]
        params: list = [before_time]
        if market_env:
            clauses.append("market_env=?")
            params.append(str(market_env).lower())
        params.append(int(limit))
        conn = self.connect()
        try:
            rows = conn.execute(
                f"""SELECT * FROM alpha_strategy_samples
                    WHERE {' AND '.join(clauses)}
                    ORDER BY datetime(candle_close_time), id
                    LIMIT ?""",
                params,
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["features"] = json.loads(item.pop("features_json") or "{}")
                item["feature_quality"] = json.loads(
                    item.pop("quality_json") or "{}"
                )
                result.append(item)
            return result
        finally:
            conn.close()

    def label_alpha_strategy_sample(
        self,
        sample_id: int,
        labels: dict,
        *,
        status: str = "ready",
    ) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """UPDATE alpha_strategy_samples
                   SET label_status=?, labels_json=?, labeled_at=datetime('now')
                   WHERE id=?""",
                (str(status), _json(labels), int(sample_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def labeled_alpha_strategy_samples(
        self,
        *,
        model_key: str,
        target: str,
    ) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT * FROM alpha_strategy_samples
                   WHERE model_key=? AND label_status='ready'
                     AND labels_json IS NOT NULL
                   ORDER BY datetime(candle_close_time), id""",
                (model_key,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                labels = json.loads(item.pop("labels_json") or "{}")
                if labels.get(target) is None:
                    continue
                item["label"] = int(labels[target])
                item["labels"] = labels
                item["features"] = json.loads(item.pop("features_json") or "{}")
                item["feature_quality"] = json.loads(
                    item.pop("quality_json") or "{}"
                )
                result.append(item)
            return result
        finally:
            conn.close()

    def record_alpha_strategy_model_run(self, run: dict) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """INSERT INTO alpha_strategy_model_runs
                   (model_key, market_env, stage, target, version, status,
                    sample_count, validation_count, metrics_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run["model_key"],
                    run["market_env"],
                    run["stage"],
                    run["target"],
                    run.get("version"),
                    run["status"],
                    int(run.get("sample_count") or 0),
                    int(run.get("validation_count") or 0),
                    _json(run.get("metrics")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def publish_alpha_strategy_model(
        self,
        model: dict,
        *,
        status: str,
    ) -> None:
        if status not in {"champion", "challenger"}:
            raise ValueError(f"unsupported Alpha model status: {status}")
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if status == "challenger":
                conn.execute(
                    """UPDATE alpha_strategy_models
                       SET status='archived', updated_at=datetime('now')
                       WHERE market_env=? AND model_key=? AND target=?
                         AND status='challenger'""",
                    (
                        model["market_env"],
                        model["model_key"],
                        model["target"],
                    ),
                )
            conn.execute(
                """INSERT INTO alpha_strategy_models
                   (version, model_key, market_env, stage, target, status,
                    artifact_path, trained_at, sample_count, validation_count,
                    feature_schema_version, metrics_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    model["version"],
                    model["model_key"],
                    model["market_env"],
                    model["stage"],
                    model["target"],
                    status,
                    model["artifact_path"],
                    model["trained_at"],
                    int(model["sample_count"]),
                    int(model["validation_count"]),
                    int(model["feature_schema_version"]),
                    _json(model.get("metrics")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_alpha_strategy_model(
        self,
        *,
        model_key: str,
        target: str,
        include_challenger: bool = False,
    ) -> dict | None:
        statuses = ("champion", "challenger") if include_challenger else ("champion",)
        placeholders = ",".join("?" for _ in statuses)
        conn = self.connect()
        try:
            row = conn.execute(
                f"""SELECT * FROM alpha_strategy_models
                    WHERE model_key=? AND target=?
                      AND status IN ({placeholders})
                    ORDER BY CASE status WHEN 'champion' THEN 0 ELSE 1 END,
                             datetime(trained_at) DESC
                    LIMIT 1""",
                (model_key, target, *statuses),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
            return item
        finally:
            conn.close()

    def list_alpha_strategy_models(
        self,
        market_env: str | None = None,
    ) -> list[dict]:
        where = "WHERE market_env=?" if market_env else ""
        params = (str(market_env).lower(),) if market_env else ()
        conn = self.connect()
        try:
            rows = conn.execute(
                f"""SELECT * FROM alpha_strategy_models
                   {where}
                   ORDER BY market_env, model_key, target,
                            datetime(trained_at) DESC""",
                params,
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
                result.append(item)
            return result
        finally:
            conn.close()

    def list_alpha_strategy_model_runs(
        self,
        limit: int = 30,
        market_env: str | None = None,
    ) -> list[dict]:
        where = "WHERE market_env=?" if market_env else ""
        params = [str(market_env).lower()] if market_env else []
        params.append(int(limit))
        conn = self.connect()
        try:
            rows = conn.execute(
                f"""SELECT * FROM alpha_strategy_model_runs
                   {where} ORDER BY id DESC LIMIT ?""",
                params,
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["metrics"] = json.loads(
                    item.pop("metrics_json") or "{}"
                )
                result.append(item)
            return result
        finally:
            conn.close()

    def upsert_alpha_strategy_execution_outcomes(
        self,
        rows: list[dict],
    ) -> int:
        if not rows:
            return 0
        conn = self.connect()
        try:
            conn.executemany(
                """INSERT INTO alpha_strategy_execution_outcomes
                   (account_id, event_id, action_type, setup_id,
                    futures_symbol, position_id, exchange_order_id,
                    quantity, entry_price, invalidation_price, realized_pnl,
                    realized_r, exit_reason, status, model_versions_json,
                    submitted_at, closed_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                   ON CONFLICT(account_id, event_id, action_type) DO UPDATE SET
                     position_id=excluded.position_id,
                     exchange_order_id=excluded.exchange_order_id,
                     quantity=excluded.quantity,
                     entry_price=excluded.entry_price,
                     invalidation_price=excluded.invalidation_price,
                     realized_pnl=excluded.realized_pnl,
                     realized_r=excluded.realized_r,
                     exit_reason=excluded.exit_reason,
                     status=excluded.status,
                     model_versions_json=excluded.model_versions_json,
                     closed_at=excluded.closed_at,
                     updated_at=datetime('now')""",
                [
                    (
                        int(row["account_id"]),
                        row["event_id"],
                        row["action_type"],
                        row.get("setup_id"),
                        row["futures_symbol"],
                        row.get("position_id"),
                        row.get("exchange_order_id"),
                        row.get("quantity"),
                        row.get("entry_price"),
                        row.get("invalidation_price"),
                        row.get("realized_pnl"),
                        row.get("realized_r"),
                        row.get("exit_reason"),
                        row.get("status") or "open",
                        _json(row.get("model_versions")),
                        row.get("submitted_at"),
                        row.get("closed_at"),
                    )
                    for row in rows
                ],
            )
            conn.commit()
            return len(rows)
        finally:
            conn.close()

    def alpha_strategy_execution_summary(self) -> dict:
        conn = self.connect()
        try:
            summary = conn.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) closed,
                          SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open,
                          AVG(CASE WHEN status='closed' THEN realized_r END)
                              mean_realized_r,
                          SUM(CASE WHEN status='closed'
                                   AND realized_pnl > 0 THEN 1 ELSE 0 END) wins
                   FROM alpha_strategy_execution_outcomes"""
            ).fetchone()
            by_stage = [
                dict(row)
                for row in conn.execute(
                    """SELECT action_type, COUNT(*) count,
                              AVG(CASE WHEN status='closed'
                                      THEN realized_r END) mean_realized_r
                       FROM alpha_strategy_execution_outcomes
                       GROUP BY action_type ORDER BY action_type"""
                ).fetchall()
            ]
            closed = int(summary["closed"] or 0)
            return {
                "total": int(summary["total"] or 0),
                "closed": closed,
                "open": int(summary["open"] or 0),
                "wins": int(summary["wins"] or 0),
                "win_rate": (
                    round(int(summary["wins"] or 0) / closed, 4)
                    if closed
                    else 0.0
                ),
                "mean_realized_r": (
                    round(float(summary["mean_realized_r"]), 6)
                    if summary["mean_realized_r"] is not None
                    else None
                ),
                "by_stage": by_stage,
            }
        finally:
            conn.close()

    def promote_alpha_strategy_model(self, version: str) -> bool:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT * FROM alpha_strategy_models
                   WHERE version=? AND status IN ('challenger','champion')""",
                (version,),
            ).fetchone()
            if not row:
                conn.rollback()
                return False
            conn.execute(
                """UPDATE alpha_strategy_models
                   SET status='archived', updated_at=datetime('now')
                   WHERE market_env=? AND model_key=? AND target=?
                     AND status='champion' AND version<>?""",
                (
                    row["market_env"],
                    row["model_key"],
                    row["target"],
                    version,
                ),
            )
            conn.execute(
                """UPDATE alpha_strategy_models
                   SET status='champion', updated_at=datetime('now')
                   WHERE version=?""",
                (version,),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def rollback_alpha_strategy_model(
        self,
        *,
        model_key: str,
        target: str,
    ) -> str | None:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                """SELECT version FROM alpha_strategy_models
                   WHERE model_key=? AND target=? AND status='champion'
                   ORDER BY datetime(trained_at) DESC LIMIT 1""",
                (model_key, target),
            ).fetchone()
            previous = conn.execute(
                """SELECT version FROM alpha_strategy_models
                   WHERE model_key=? AND target=? AND status='archived'
                   ORDER BY datetime(trained_at) DESC LIMIT 1""",
                (model_key, target),
            ).fetchone()
            if not previous:
                conn.rollback()
                return None
            if current:
                conn.execute(
                    """UPDATE alpha_strategy_models SET status='archived',
                       updated_at=datetime('now') WHERE version=?""",
                    (current["version"],),
                )
            conn.execute(
                """UPDATE alpha_strategy_models SET status='champion',
                   updated_at=datetime('now') WHERE version=?""",
                (previous["version"],),
            )
            conn.commit()
            return str(previous["version"])
        finally:
            conn.close()

    def sample_counts(self, model_key: str) -> dict:
        conn = self.connect()
        try:
            row = conn.execute(
                """SELECT COUNT(*) total,
                          SUM(CASE WHEN label_status='ready' THEN 1 ELSE 0 END) labeled,
                          SUM(CASE WHEN label_status='pending' THEN 1 ELSE 0 END) pending
                   FROM entry_quality_samples WHERE model_key=?""",
                (model_key,),
            ).fetchone()
            return {key: int(row[key] or 0) for key in ("total", "labeled", "pending")}
        finally:
            conn.close()

    def collected_today(self, model_key: str, utc_date: str) -> int:
        conn = self.connect()
        try:
            row = conn.execute(
                """SELECT COUNT(*) n FROM entry_quality_samples
                   WHERE model_key=? AND substr(observed_at, 1, 10)=?""",
                (model_key, utc_date),
            ).fetchone()
            return int(row["n"] or 0)
        finally:
            conn.close()

    def feature_quality_summary(self, model_key: str, schema_version: int) -> dict:
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT quality_json FROM entry_quality_samples
                   WHERE model_key=? AND feature_schema_version=?
                   ORDER BY id DESC LIMIT 1000""",
                (model_key, int(schema_version)),
            ).fetchall()
            qualities = [json.loads(row["quality_json"] or "{}") for row in rows]
            if not qualities:
                return {
                    "schema_version": int(schema_version),
                    "samples": 0,
                    "average_coverage": 0.0,
                    "average_present_count": 0.0,
                }
            return {
                "schema_version": int(schema_version),
                "samples": len(qualities),
                "average_coverage": round(
                    sum(float(item.get("coverage") or 0) for item in qualities) / len(qualities),
                    4,
                ),
                "average_present_count": round(
                    sum(float(item.get("present_count") or 0) for item in qualities) / len(qualities),
                    2,
                ),
            }
        finally:
            conn.close()

    def record_decision(self, decision: dict) -> int:
        conn = self.connect()
        try:
            cursor = conn.execute(
                """INSERT INTO entry_quality_decisions
                   (observed_at, account_id, model_key, symbol, model_version, quality_score,
                    decision, mode, size_factor, expected_r, applied, reasons_json, features_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision["observed_at"], decision.get("account_id"), decision["model_key"],
                    str(decision["symbol"]).upper(), decision.get("model_version"),
                    decision.get("quality_score"), decision["decision"], decision.get("mode") or "live",
                    decision.get("size_factor"), decision.get("expected_r"),
                    1 if decision.get("applied") else 0, _json(decision.get("reasons")),
                    _json(decision.get("features")),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)
        finally:
            conn.close()

    def set_sample_label(
        self,
        sample_id: int,
        *,
        label: int,
        first_event: str,
        mfe_r: float,
        mae_r: float,
        labeled_at: str | None = None,
    ) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """UPDATE entry_quality_samples
                   SET label=?, first_event=?, mfe_r=?, mae_r=?, label_status='ready',
                       labeled_at=COALESCE(?, datetime('now'))
                   WHERE id=?""",
                (int(label), first_event, float(mfe_r), float(mae_r), labeled_at, int(sample_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def labeled_samples(self, model_key: str) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT * FROM entry_quality_samples
                   WHERE model_key=? AND label_status='ready' AND label IS NOT NULL
                   ORDER BY datetime(observed_at), id""",
                (model_key,),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["features"] = json.loads(item.pop("features_json") or "{}")
                item["feature_quality"] = json.loads(item.pop("quality_json") or "{}")
                result.append(item)
            return result
        finally:
            conn.close()

    def pending_samples(self, before_time: str, limit: int = 1000) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT * FROM entry_quality_samples
                   WHERE label_status='pending' AND datetime(observed_at) <= datetime(?)
                   ORDER BY datetime(observed_at), id LIMIT ?""",
                (before_time, int(limit)),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["features"] = json.loads(item.pop("features_json") or "{}")
                item["feature_quality"] = json.loads(item.pop("quality_json") or "{}")
                result.append(item)
            return result
        finally:
            conn.close()

    def mark_sample_missing(self, sample_id: int, reason: str) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """UPDATE entry_quality_samples
                   SET label_status='missing', first_event=?, labeled_at=datetime('now') WHERE id=?""",
                (reason, int(sample_id)),
            )
            conn.commit()
        finally:
            conn.close()

    def publish_model(self, model: dict) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """INSERT INTO entry_quality_models
                   (model_key, version, status, artifact_path, trained_at, sample_count,
                    validation_count, baseline_mean_r, allowed_mean_r, metrics_json,
                    feature_schema_version, updated_at)
                   VALUES (?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(model_key) DO UPDATE SET
                     version=excluded.version, status='ready', artifact_path=excluded.artifact_path,
                     trained_at=excluded.trained_at, sample_count=excluded.sample_count,
                     validation_count=excluded.validation_count,
                     baseline_mean_r=excluded.baseline_mean_r,
                     allowed_mean_r=excluded.allowed_mean_r,
                     metrics_json=excluded.metrics_json,
                     feature_schema_version=excluded.feature_schema_version,
                     updated_at=datetime('now')""",
                (
                    model["model_key"], model["version"], model["artifact_path"], model["trained_at"],
                    int(model["sample_count"]), int(model["validation_count"]),
                    model.get("baseline_mean_r"), model.get("allowed_mean_r"),
                    _json(model.get("metrics")), int(model.get("feature_schema_version") or 1),
                ),
            )
            conn.execute(
                """INSERT INTO ai_model_runs
                   (model_key, version, status, trained_at, sample_count, validation_count,
                    metrics_json, feature_schema_version)
                   VALUES (?, ?, 'published', ?, ?, ?, ?, ?)""",
                (
                    model["model_key"], model["version"], model["trained_at"],
                    int(model["sample_count"]), int(model["validation_count"]),
                    _json(model.get("metrics")), int(model.get("feature_schema_version") or 1),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_model(self, model_key: str) -> dict | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT * FROM entry_quality_models WHERE model_key=?", (model_key,)
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["metrics"] = json.loads(item.pop("metrics_json") or "{}")
            return item
        finally:
            conn.close()

    def list_decisions(self, limit: int = 100) -> list[dict]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM entry_quality_decisions ORDER BY datetime(observed_at) DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["reasons"] = json.loads(item.pop("reasons_json") or "[]")
                item["features"] = json.loads(item.pop("features_json") or "{}")
                result.append(item)
            return result
        finally:
            conn.close()

    def decision_counts(self, model_key: str, utc_date: str) -> dict:
        result = {"allow": 0, "probe": 0, "reject": 0, "collecting": 0, "total": 0}
        conn = self.connect()
        try:
            rows = conn.execute(
                """SELECT decision, COUNT(*) n FROM entry_quality_decisions
                   WHERE model_key=? AND substr(observed_at, 1, 10)=?
                   GROUP BY decision""",
                (model_key, utc_date),
            ).fetchall()
            for row in rows:
                key = str(row["decision"])
                if key in result:
                    result[key] = int(row["n"])
                result["total"] += int(row["n"])
            return result
        finally:
            conn.close()

    def cleanup(self, before_time: str) -> dict:
        conn = self.connect()
        try:
            samples = conn.execute(
                "DELETE FROM entry_quality_samples WHERE datetime(observed_at) < datetime(?)",
                (before_time,),
            ).rowcount
            decisions = conn.execute(
                "DELETE FROM entry_quality_decisions WHERE datetime(observed_at) < datetime(?)",
                (before_time,),
            ).rowcount
            conn.commit()
            return {"samples": int(samples), "decisions": int(decisions)}
        finally:
            conn.close()
