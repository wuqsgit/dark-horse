import unittest
from unittest.mock import patch

from trader.portfolio_risk import check_category_position_limit
from trader.selection import CandidateSelector
from trader.execution import ExecutionEngine


class ExistingCorePositionExchange:
    def get_positions(self):
        return [{"symbol": "BTCUSDT", "side": "LONG", "quantity": 1}]


class PortfolioCategoryLimitTest(unittest.TestCase):
    def test_core_bluechip_position_blocks_other_core_bluechips(self):
        positions = [{"symbol": "BTCUSDT"}]

        eth_ok, eth_reason = check_category_position_limit(
            positions, "ETHUSDT"
        )
        bch_ok, _ = check_category_position_limit(positions, "BCHUSDT")

        self.assertFalse(eth_ok)
        self.assertIn("class=core_bluechip", eth_reason)
        self.assertIn("BTCUSDT", eth_reason)
        self.assertTrue(bch_ok)

    def test_large_cap_position_blocks_every_other_large_cap(self):
        positions = [{"symbol": "BCHUSDT"}]

        paxg_ok, paxg_reason = check_category_position_limit(
            positions, "PAXGUSDT"
        )
        arb_ok, _ = check_category_position_limit(positions, "ARBUSDT")

        self.assertFalse(paxg_ok)
        self.assertIn("class=large_cap", paxg_reason)
        self.assertTrue(arb_ok)

    def test_planned_open_also_occupies_the_category(self):
        allowed, reason = check_category_position_limit(
            [],
            "ETHUSDT",
            [{"action": "open", "symbol": "BTCUSDT"}],
        )

        self.assertFalse(allowed)
        self.assertIn("BTCUSDT", reason)

    def test_full_close_releases_the_category_for_replacement(self):
        allowed, _ = check_category_position_limit(
            [{"symbol": "BTCUSDT"}],
            "ETHUSDT",
            [{"action": "close", "symbol": "BTCUSDT"}],
        )

        self.assertTrue(allowed)

    def test_candidate_selector_never_backfills_a_duplicate_category(self):
        rows = [
            {"symbol": "BTCUSDT", "composite_score": 100},
            {"symbol": "ETHUSDT", "composite_score": 99},
            {"symbol": "BCHUSDT", "composite_score": 98},
            {"symbol": "PAXGUSDT", "composite_score": 97},
            {"symbol": "ARBUSDT", "composite_score": 96},
            {"symbol": "OPUSDT", "composite_score": 95},
            {"symbol": "DOGEUSDT", "composite_score": 94},
        ]
        with patch.object(CandidateSelector, "_load_blacklist", return_value=set()), \
             patch.object(CandidateSelector, "_load_token_map", return_value={}):
            selector = CandidateSelector()
        selector._opportunity_score = lambda row: row["composite_score"]

        selected = selector.select_candidates(rows, [], max_positions=7)
        symbols = [row["symbol"] for row in selected]

        self.assertEqual(symbols, ["BTCUSDT", "BCHUSDT", "ARBUSDT", "DOGEUSDT"])
        self.assertNotIn("ETHUSDT", symbols)
        self.assertNotIn("PAXGUSDT", symbols)
        self.assertNotIn("OPUSDT", symbols)

    def test_execution_rechecks_category_before_submitting_order(self):
        engine = ExecutionEngine(ExistingCorePositionExchange())
        engine._record_decision = lambda *args, **kwargs: None

        results = engine.execute([{
            "action": "open",
            "symbol": "ETHUSDT",
            "side": "BUY",
            "position_side": "LONG",
            "quantity": 1,
            "entry_price": 100,
        }])

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "blocked")
        self.assertEqual(results[0]["error"], "category_position_limit")


if __name__ == "__main__":
    unittest.main()
