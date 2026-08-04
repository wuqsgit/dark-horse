import unittest

from alpha_engine.scoring import AlphaScoringEngine


class AlphaOiHorizonTest(unittest.TestCase):
    def test_futures_sync_uses_elapsed_time_for_oi_horizons(self):
        futures_rows = [
            {
                "time": "2026-07-28T00:00:00Z",
                "open_interest": 100,
                "funding_rate": 0.0,
                "mark_price": 1.0,
            },
            {
                "time": "2026-07-28T00:10:00Z",
                "open_interest": 110,
                "funding_rate": 0.0,
                "mark_price": 1.0,
            },
            {
                "time": "2026-07-28T04:00:00Z",
                "open_interest": 125,
                "funding_rate": 0.0,
                "mark_price": 1.0,
            },
        ]

        result = AlphaScoringEngine()._compute_futures_sync(
            "AKEUSDT",
            [],
            futures_rows,
        )

        self.assertAlmostEqual(result["oi_change_4h"], 0.25)
        self.assertFalse(result["oi_change_24h_available"])


if __name__ == "__main__":
    unittest.main()
