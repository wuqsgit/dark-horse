import unittest

from shared.policy_loop import build_execution_entry_snapshot, classify_entry_review, build_entry_group_recommendation


class EntryReviewClassificationTest(unittest.TestCase):
    def test_reasonable_when_one_r_is_reached_first(self):
        result = classify_entry_review(0.05, [0.01, 0.055, -0.01], False)
        self.assertEqual(result["label"], "reasonable")

    def test_early_when_drawdown_happens_before_one_r(self):
        result = classify_entry_review(0.05, [-0.04, 0.01, 0.06], False)
        self.assertEqual(result["label"], "early")

    def test_chased_when_drawdown_is_not_recovered(self):
        result = classify_entry_review(0.05, [-0.04, -0.02, 0.01], True)
        self.assertEqual(result["label"], "chased")

    def test_bad_condition_when_stop_is_reached_without_half_r(self):
        result = classify_entry_review(0.05, [0.01, -0.02, -0.055], False, is_closed=True)
        self.assertEqual(result["label"], "bad_condition")

    def test_pending_without_reliable_risk(self):
        self.assertEqual(classify_entry_review(None, [0.2], False)["label"], "pending")

    def test_group_recommendation_respects_sample_thresholds(self):
        self.assertEqual(build_entry_group_recommendation({"sample_size": 3})["action_type"], "observe")
        broad = build_entry_group_recommendation({"sample_size": 6, "early_count": 4})
        self.assertEqual(broad["action_type"], "broad")
        concrete = build_entry_group_recommendation({"sample_size": 10, "early_count": 6})
        self.assertEqual(concrete["action_type"], "improve")
        self.assertIn("确认", concrete["recommendation"])

    def test_execution_snapshot_uses_real_risk_and_position_values(self):
        snapshot = build_execution_entry_snapshot({
            "position_id": "live-1", "symbol": "ETHUSDT", "position_side": "LONG",
            "entry_price": 1700, "quantity": 0.3, "leverage": 6, "margin": 85,
            "stop_loss": 1649, "score": 78, "strategy_source": "normal",
            "entry_template": "trend_pullback", "trend_score": 74,
        })
        self.assertEqual(snapshot["position_trade_id"], "live-1")
        self.assertEqual(snapshot["stop_pct"], 0.03)
        self.assertEqual(snapshot["notional"], 510)
        self.assertIn("评分78.0", snapshot["entry_reason_text"])


if __name__ == "__main__":
    unittest.main()
