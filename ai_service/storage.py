import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _json(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"), default=str)


def _hour_bucket(value: str) -> str:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")


class AIStore:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_db(self):
        conn = self.connect()
        try:
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
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

    def add_sample(self, sample: dict) -> tuple[int, bool]:
        bucket = _hour_bucket(sample["observed_at"])
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
                    hour_bucket, entry_price, stop_pct, features_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    sample_key, model_key, symbol, side, template, sample.get("category"),
                    sample["observed_at"], bucket, float(sample["entry_price"]),
                    float(sample["stop_pct"]), _json(sample.get("features")),
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

    def record_decision(self, decision: dict) -> int:
        conn = self.connect()
        try:
            cursor = conn.execute(
                """INSERT INTO entry_quality_decisions
                   (observed_at, account_id, model_key, symbol, model_version, quality_score,
                    decision, mode, size_factor, reasons_json, features_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision["observed_at"], decision.get("account_id"), decision["model_key"],
                    str(decision["symbol"]).upper(), decision.get("model_version"),
                    decision.get("quality_score"), decision["decision"], decision.get("mode") or "live",
                    decision.get("size_factor"), _json(decision.get("reasons")),
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
                    validation_count, baseline_mean_r, allowed_mean_r, metrics_json, updated_at)
                   VALUES (?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                   ON CONFLICT(model_key) DO UPDATE SET
                     version=excluded.version, status='ready', artifact_path=excluded.artifact_path,
                     trained_at=excluded.trained_at, sample_count=excluded.sample_count,
                     validation_count=excluded.validation_count,
                     baseline_mean_r=excluded.baseline_mean_r,
                     allowed_mean_r=excluded.allowed_mean_r,
                     metrics_json=excluded.metrics_json, updated_at=datetime('now')""",
                (
                    model["model_key"], model["version"], model["artifact_path"], model["trained_at"],
                    int(model["sample_count"]), int(model["validation_count"]),
                    model.get("baseline_mean_r"), model.get("allowed_mean_r"), _json(model.get("metrics")),
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
