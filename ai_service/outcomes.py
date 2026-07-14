import sqlite3
from datetime import datetime, timedelta, timezone

from ai_service.labels import label_path


def _parse_time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class OutcomeLabeler:
    def __init__(self, store, market_db_path, *, now_fn=None):
        self.store = store
        self.market_db_path = str(market_db_path)
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def _candles(self, sample):
        start = _parse_time(sample["observed_at"])
        end = start + timedelta(hours=24)
        conn = sqlite3.connect(f"file:{self.market_db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT time, high, low FROM futures_candles_15m
                   WHERE symbol=? AND datetime(time) >= datetime(?) AND datetime(time) <= datetime(?)
                   ORDER BY datetime(time)""",
                (sample["symbol"], _iso(start), _iso(end)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    @staticmethod
    def _path_is_complete(sample, candles):
        if len(candles) < 80:
            return False
        end = _parse_time(sample["observed_at"]) + timedelta(hours=24)
        return _parse_time(candles[-1]["time"]) >= end - timedelta(minutes=15)

    def label_pending(self, limit=1000):
        now = self.now_fn()
        mature_before = _iso(now - timedelta(hours=24))
        result = {"checked": 0, "labeled": 0, "waiting_for_candles": 0, "missing": 0}
        for sample in self.store.pending_samples(mature_before, limit=limit):
            result["checked"] += 1
            try:
                candles = self._candles(sample)
            except (sqlite3.Error, OSError):
                candles = []
            if not self._path_is_complete(sample, candles):
                age = now - _parse_time(sample["observed_at"])
                if age >= timedelta(hours=72):
                    self.store.mark_sample_missing(sample["id"], "futures_15m_path_missing")
                    result["missing"] += 1
                else:
                    result["waiting_for_candles"] += 1
                continue
            outcome = label_path(
                sample["entry_price"], sample["stop_pct"], sample["side"], candles,
            )
            self.store.set_sample_label(sample["id"], **outcome, labeled_at=_iso(now))
            result["labeled"] += 1
        return result
