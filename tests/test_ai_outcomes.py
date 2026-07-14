import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from ai_service.outcomes import OutcomeLabeler
from ai_service.storage import AIStore


class AIOutcomeLabelerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ai_path = os.path.join(self.tmp.name, "ai.db")
        self.market_path = os.path.join(self.tmp.name, "market.db")
        self.store = AIStore(self.ai_path)
        conn = sqlite3.connect(self.market_path)
        conn.execute(
            "CREATE TABLE futures_candles_15m "
            "(time TEXT, symbol TEXT, open REAL, high REAL, low REAL, close REAL)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def add_sample(self, observed_at="2026-07-13T10:00:00Z"):
        sample_id, _ = self.store.add_sample({
            "model_key": "alpha", "symbol": "B2USDT", "side": "LONG",
            "template": "probe", "category": "alpha", "observed_at": observed_at,
            "entry_price": 100, "stop_pct": 0.10, "features": {"score": 80},
        })
        return sample_id

    def add_candles(self, start, count=97):
        conn = sqlite3.connect(self.market_path)
        rows = []
        for idx in range(count):
            time = start + timedelta(minutes=15 * idx)
            high = 111 if idx == 10 else 105
            rows.append((time.isoformat().replace("+00:00", "Z"), "B2USDT", 100, high, 99, 100))
        conn.executemany("INSERT INTO futures_candles_15m VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.commit()
        conn.close()

    def test_labels_mature_sample_from_complete_24h_futures_path(self):
        sample_id = self.add_sample()
        self.add_candles(datetime(2026, 7, 13, 10, tzinfo=timezone.utc))
        labeler = OutcomeLabeler(
            self.store, self.market_path,
            now_fn=lambda: datetime(2026, 7, 14, 11, tzinfo=timezone.utc),
        )

        result = labeler.label_pending()

        row = self.store.labeled_samples("alpha")[0]
        self.assertEqual(result["labeled"], 1)
        self.assertEqual(row["id"], sample_id)
        self.assertEqual((row["label"], row["first_event"]), (1, "plus_1r"))

    def test_keeps_recent_incomplete_path_pending(self):
        self.add_sample()
        self.add_candles(datetime(2026, 7, 13, 10, tzinfo=timezone.utc), count=20)
        labeler = OutcomeLabeler(
            self.store, self.market_path,
            now_fn=lambda: datetime(2026, 7, 14, 11, tzinfo=timezone.utc),
        )

        result = labeler.label_pending()

        self.assertEqual(result["waiting_for_candles"], 1)
        self.assertEqual(self.store.sample_counts("alpha")["pending"], 1)

    def test_marks_missing_after_three_days_without_complete_path(self):
        self.add_sample("2026-07-10T10:00:00Z")
        labeler = OutcomeLabeler(
            self.store, self.market_path,
            now_fn=lambda: datetime(2026, 7, 14, 11, tzinfo=timezone.utc),
        )

        result = labeler.label_pending()

        self.assertEqual(result["missing"], 1)
        self.assertEqual(self.store.sample_counts("alpha")["pending"], 0)


if __name__ == "__main__":
    unittest.main()
