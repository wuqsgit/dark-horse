import unittest

import shared.policy_loop as policy_loop
from shared.policy_loop import (
    _build_simple_exit_review_text,
    _exit_summary_recommendation,
    _pct_for_review,
    _simple_exit_conclusion,
    _lifecycle_conclusion,
)
import sqlite3


class PolicyLoopExitReviewTest(unittest.TestCase):
    def test_simple_exit_review_explains_entry_exit_followup_and_advice(self):
        trade = {
            "symbol": "B2USDT",
            "strategy_source": "alpha",
            "category": "alpha",
            "side": "LONG",
            "entry_reason": "alpha_volume_price->alpha_alpha_trend_probe alpha_score=83.0 LONG",
            "exit_reason": "alpha_volume_regime_profit_protect regime=suspicious pnl=5.3%",
            "net_pnl": 4.69,
            "pnl_pct": 0.0477,
            "holding_minutes": 234,
        }
        metrics = {
            "bars_observed": 12,
            "return_1h": 0.021,
            "return_4h": 0.0849,
            "return_12h": 0.076,
            "return_24h": None,
            "max_favorable_return": 0.0849,
            "max_adverse_return": -0.0083,
        }

        label = _simple_exit_conclusion(trade, metrics)
        text = _build_simple_exit_review_text(trade, metrics, label)

        self.assertEqual(label, "平早了")
        self.assertIn("开仓：", text)
        self.assertIn("alpha_score=83.0", text)
        self.assertIn("平仓：", text)
        self.assertIn("成交量状态可疑", text)
        self.assertIn("后续：", text)
        self.assertIn("8.49%", text)
        self.assertIn("结论：平早了", text)
        self.assertIn("部分止盈", text)

    def test_exit_summary_recommendation_is_category_specific(self):
        advice = _exit_summary_recommendation(
            category="alpha",
            source="alpha",
            label="平早了",
            sample=6,
            total_pnl=8.2,
            avg_mfe=0.078,
            avg_mae=-0.012,
        )

        self.assertIn("Alpha", advice)
        self.assertIn("平早", advice)
        self.assertIn("部分止盈", advice)
        self.assertIn("移动止盈", advice)

    def test_stored_percent_values_are_always_converted_to_ratios(self):
        self.assertAlmostEqual(_pct_for_review(-1.35), -0.0135)
        self.assertAlmostEqual(_pct_for_review(-0.99), -0.0099)
        self.assertAlmostEqual(_pct_for_review(0.047), 0.00047)

    def test_exit_review_selects_newest_position_trades_first(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE position_trades (position_trade_id TEXT, symbol TEXT, exit_time TEXT)"
        )
        conn.executemany(
            "INSERT INTO position_trades VALUES (?, 'B2USDT', ?)",
            [
                ("old", "2026-07-08 01:00:00"),
                ("middle", "2026-07-09 01:00:00"),
                ("new", "2026-07-10 01:00:00"),
            ],
        )

        select_rows = getattr(
            policy_loop,
            "_select_position_trades_for_review",
            lambda db_conn, limit: db_conn.execute(
                "SELECT * FROM position_trades ORDER BY datetime(exit_time) ASC LIMIT ?",
                (limit,),
            ).fetchall(),
        )
        rows = select_rows(conn, 2)

        self.assertEqual([row["position_trade_id"] for row in rows], ["new", "middle"])

    def test_lifecycle_review_does_not_infer_entry_problem_when_followup_data_is_pending(self):
        conclusion, recommendation = _lifecycle_conclusion(
            {"category": "alpha", "max_favorable_return": 0, "max_adverse_return": -0.05, "net_pnl": -4},
            {"review_label": "pending", "max_favorable_return": 0, "max_adverse_return": 0},
        )

        self.assertEqual(conclusion, "后续数据不足")
        self.assertIn("调整策略", recommendation)


if __name__ == "__main__":
    unittest.main()
