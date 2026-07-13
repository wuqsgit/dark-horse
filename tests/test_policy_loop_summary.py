import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shared.db as db
import shared.policy_loop as policy_loop


class PolicyLoopSummaryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_patch = patch.object(db, "DB_PATH", str(root / "policy.db"))
        self.entry_patch = patch.object(policy_loop, "ENTRY_POLICY_PATH", root / "entry_policy.json")
        self.exit_patch = patch.object(policy_loop, "EXIT_POLICY_PATH", root / "exit_policy.json")
        self.db_patch.start()
        self.entry_patch.start()
        self.exit_patch.start()
        db.init_db()
        policy_loop.ENTRY_POLICY_PATH.write_text(
            json.dumps({"version": "test", "rules": [{"id": "a"}, {"id": "b"}]}),
            encoding="utf-8",
        )
        policy_loop.EXIT_POLICY_PATH.write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.exit_patch.stop()
        self.entry_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def _seed_policy_state(self):
        conn = db.get_conn()
        try:
            conn.execute(
                """INSERT INTO strategy_policy_candidates
                   (source_type, target, action, title, status, sample_size)
                   VALUES ('policy_review', 'entry_filter', 'soften', 'review rule', 'active', 12)"""
            )
            conn.execute(
                """INSERT INTO strategy_policy_candidates
                   (source_type, target, action, title, status, sample_size)
                   VALUES ('factor_effectiveness', 'score_weight', 'increase', 'offline suggestion', 'proposed', 100)"""
            )
            conn.execute(
                """INSERT INTO policy_versions
                   (version_id, category, strategy_source, target_type, policy_json, status)
                   VALUES ('v1', 'narrative', 'normal', 'entry_filter', '{}', 'active')"""
            )
            conn.commit()
        finally:
            conn.close()

    def test_default_summary_omits_category_and_diagnosis_payloads(self):
        result = policy_loop.fetch_policy_loop_summary()

        self.assertNotIn("categories", result)
        self.assertNotIn("reviews", result)
        self.assertNotIn("actions", result)
        self.assertIn("entry_reviews", result)
        self.assertIn("entry_summaries", result)
        self.assertIn("entry_review_status", result)

    def test_summary_reports_automatic_policy_state(self):
        self._seed_policy_state()

        result = policy_loop.fetch_policy_loop_summary()

        self.assertEqual(result["auto_policy_status"], {
            "review_candidates": 1,
            "active_candidates": 1,
            "offline_suggestions": 1,
            "active_versions": 1,
            "entry_rules": 2,
        })

    def test_diagnostics_remain_available_for_compatibility_calls(self):
        result = policy_loop.fetch_policy_loop_summary(include_diagnostics=True)

        self.assertIn("categories", result)
        self.assertIn("reviews", result)


if __name__ == "__main__":
    unittest.main()
