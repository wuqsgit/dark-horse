import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shared.db as db


class AlphaSymbolDetailQueryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path_patch = patch.object(
            db, "DB_PATH", str(Path(self.temp_dir.name) / "alpha-detail.db")
        )
        self.db_path_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def test_fetches_latest_candidate_for_only_the_requested_alpha_symbol(self):
        db.upsert_alpha_trade_candidate(
            "scan-old", "2026-07-11T00:00:00Z", "ALPHA_451USDT",
            futures_symbol="AKEUSDT", normal_score=61,
        )
        db.upsert_alpha_trade_candidate(
            "scan-new", "2026-07-11T01:00:00Z", "ALPHA_451USDT",
            futures_symbol="AKEUSDT", normal_score=79,
        )
        db.upsert_alpha_trade_candidate(
            "scan-other", "2026-07-11T02:00:00Z", "ALPHA_999USDT",
            futures_symbol="OTHERUSDT", normal_score=99,
        )

        helper = getattr(db, "fetch_latest_alpha_trade_candidate", None)
        self.assertIsNotNone(helper)
        row = helper("ALPHA_451USDT")

        self.assertEqual(row["scan_id"], "scan-new")
        self.assertEqual(row["normal_score"], 79)
        self.assertEqual(row["alpha_symbol"], "ALPHA_451USDT")

    def test_symbol_time_indexes_are_created(self):
        conn = db.get_conn()
        try:
            candidate_indexes = {
                row[1] for row in conn.execute("PRAGMA index_list('alpha_trade_candidates')")
            }
            score_indexes = {
                row[1] for row in conn.execute("PRAGMA index_list('alpha_scan_scores')")
            }
        finally:
            conn.close()

        self.assertIn("idx_alpha_trade_candidates_symbol_time", candidate_indexes)
        self.assertIn("idx_alpha_scan_scores_symbol_time", score_indexes)


if __name__ == "__main__":
    unittest.main()
