import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shared.db as db
from shared.policy_loop import fetch_position_action_evidence


class PositionActionEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp_dir.name) / "actions.db"))
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_actions_are_scoped_to_position_symbol_side_and_lifetime(self):
        db.record_entry_review_snapshot({
            "position_trade_id": "p-1", "symbol": "ETHUSDT", "side": "LONG",
            "entry_time": "2026-07-11T01:00:00Z", "entry_price": 1700,
        })
        conn = db.get_conn()
        try:
            conn.execute("UPDATE trade_entry_reviews SET exit_time='2026-07-11T03:00:00Z' WHERE position_trade_id='p-1'")
            conn.executemany(
                "INSERT INTO decision_actions(action_id,time,symbol,side,action_type) VALUES(?,?,?,?,?)",
                [
                    ("inside", "2026-07-11T02:00:00Z", "ETHUSDT", "LONG", "close"),
                    ("wrong-side", "2026-07-11T02:00:00Z", "ETHUSDT", "SHORT", "close"),
                    ("too-late", "2026-07-11T05:00:00Z", "ETHUSDT", "LONG", "open"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        rows = fetch_position_action_evidence("p-1")
        self.assertEqual([row["action_id"] for row in rows], ["inside"])


if __name__ == "__main__":
    unittest.main()
