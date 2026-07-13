import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BacktestManualReviewControlsTest(unittest.TestCase):
    def test_manual_review_buttons_and_routes_are_removed(self):
        panel = (ROOT / "frontend" / "src" / "components" / "BacktestPanel.jsx").read_text(encoding="utf-8")
        api = (ROOT / "api" / "main.py").read_text(encoding="utf-8")

        self.assertNotIn("立即复盘并自动生效", panel)
        self.assertNotIn("复盘平仓", panel)
        self.assertNotIn("/policy/review/run", panel)
        self.assertNotIn("/policy/exit-review/run", panel)
        self.assertNotIn('@app.post("/api/policy/review/run")', api)
        self.assertNotIn('@app.post("/api/policy/exit-review/run")', api)


if __name__ == "__main__":
    unittest.main()
