import unittest

from shared.policy_loop import summarize_trade_lifecycle_reviews


def _review(
    symbol,
    *,
    source="normal",
    category="discovery",
    issue="reasonable",
    pnl=1.0,
):
    row = {
        "symbol": symbol,
        "strategy_source": source,
        "category": category,
        "net_pnl": pnl,
        "pnl_pct": pnl / 100,
        "entry_mfe": 0.03,
        "entry_mae": -0.01,
        "post_mfe": 0.01,
        "post_mae": -0.01,
        "exit_review_label": "reviewed",
    }
    if issue == "entry_confirmation":
        row.update(net_pnl=-2.0, pnl_pct=-0.02, entry_mfe=0.005, entry_mae=-0.06, post_mfe=0.01)
    elif issue == "early_exit":
        row.update(net_pnl=1.0, pnl_pct=0.01, entry_mfe=0.04, entry_mae=-0.01, post_mfe=0.12)
    elif issue == "pending":
        row.update(exit_review_label="pending", post_mfe=None, post_mae=None)
    return row


class LifecycleCategorySummaryTest(unittest.TestCase):
    def test_category_with_fewer_than_eight_valid_samples_is_hidden(self):
        rows = [_review(f"S{i}", issue="entry_confirmation") for i in range(7)]

        self.assertEqual(summarize_trade_lifecycle_reviews(rows), [])

    def test_issue_must_reach_count_and_rate_thresholds(self):
        rows = [
            *[_review(f"BAD{i}", issue="entry_confirmation") for i in range(2)],
            *[_review(f"OK{i}") for i in range(6)],
        ]

        self.assertEqual(summarize_trade_lifecycle_reviews(rows), [])

        rows.append(_review("BAD2", issue="entry_confirmation"))
        result = summarize_trade_lifecycle_reviews(rows)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["issue_type"], "entry_confirmation")
        self.assertEqual(result[0]["issue_count"], 3)
        self.assertAlmostEqual(result[0]["issue_rate"], 3 / 9)

    def test_each_category_keeps_only_the_strongest_issue(self):
        rows = [
            *[_review(f"BAD{i}", issue="entry_confirmation") for i in range(4)],
            *[_review(f"EARLY{i}", issue="early_exit") for i in range(3)],
            _review("OK"),
        ]

        result = summarize_trade_lifecycle_reviews(rows)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["issue_type"], "entry_confirmation")
        self.assertEqual(result[0]["issue_count"], 4)

    def test_same_action_is_merged_across_normal_categories(self):
        rows = []
        for category in ("discovery", "fundamental"):
            rows.extend(_review(f"{category}-BAD{i}", category=category, issue="entry_confirmation") for i in range(4))
            rows.extend(_review(f"{category}-OK{i}", category=category) for i in range(4))

        result = summarize_trade_lifecycle_reviews(rows)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["categories"], ["discovery", "fundamental"])
        self.assertEqual(result[0]["sample_size"], 16)
        self.assertEqual(result[0]["issue_count"], 8)
        self.assertLessEqual(len(result[0]["representative_symbols"]), 3)

    def test_alpha_entry_problem_has_concrete_confirmation_advice(self):
        rows = [
            *[_review(f"A{i}", source="alpha", category="alpha", issue="entry_confirmation") for i in range(5)],
            *[_review(f"OK{i}", source="alpha", category="alpha") for i in range(3)],
        ]

        result = summarize_trade_lifecycle_reviews(rows)

        self.assertEqual(result[0]["priority"], "急需修复")
        self.assertIn("not_confirmed", result[0]["recommendation"])
        self.assertIn("合约同步放量", result[0]["recommendation"])
        self.assertIn("价格结构站稳", result[0]["recommendation"])

    def test_global_result_limit_is_enforced(self):
        rows = []
        for index in range(3):
            source = f"source-{index}"
            rows.extend(_review(f"{source}-BAD{i}", source=source, issue="entry_confirmation") for i in range(4))
            rows.extend(_review(f"{source}-OK{i}", source=source) for i in range(4))

        result = summarize_trade_lifecycle_reviews(rows, limit=2)

        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
