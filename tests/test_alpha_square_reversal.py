import unittest
from datetime import datetime, timedelta, timezone

from alpha_engine.square_sentiment import evaluate_square_reversal
from alpha_engine.volume_price import evaluate_alpha_volume_price
from alpha_pipeline.square_collector import summarize_square_posts


def _raw_square_features(**overrides):
    sentiment = {
        "bearish_ratio": 0.85,
        "effective_post_count": 24,
        "unique_authors": 20,
        "top3_author_share": 0.15,
        "baseline_bearish_ratio_24h": 0.40,
        "substantive_risk_count": 0,
        "age_minutes": 4,
    }
    sentiment.update(overrides)
    return {
        "returns": {
            "ret_15m": 0.2,
            "ret_1h": -0.5,
            "ret_6h": -2.0,
            "pct_24h": -5.0,
        },
        "volume": {"alpha_volume_growth_6h": 2.0},
        "depth": {
            "spread_pct": 0.10,
            "imbalance": 1.0,
            "bid_depth": 100,
            "ask_depth": 100,
        },
        "risk": {"range_24h_pct": 10, "pullback_from_high_pct": 4},
        "futures_sync": {
            "available": True,
            "futures_volume_growth_6h": 1.8,
            "oi_change_4h": 0.0,
            "oi_change_24h": 0.0,
            "funding_rate": 0.0,
            "sync_score": 70,
        },
        "square_sentiment": sentiment,
    }


class AlphaSquareReversalTest(unittest.TestCase):
    def test_extreme_bearishness_creates_half_position_probe(self):
        raw = _raw_square_features()

        result = evaluate_alpha_volume_price(raw, alpha_score=82)

        self.assertTrue(result["allow_long"])
        self.assertEqual(result["action"], "normal_review_probe")
        self.assertEqual(result["state"], "alpha_square_sentiment_reversal")
        self.assertEqual(result["max_position_factor"], 0.5)

    def test_substantive_project_risk_blocks_contrarian_candidate(self):
        raw = _raw_square_features(substantive_risk_count=2)

        result = evaluate_square_reversal(raw, alpha_score=86)

        self.assertFalse(result["candidate"])
        self.assertIn(
            "no_substantive_risk",
            result["reasons"][0],
        )

    def test_low_author_diversity_does_not_trigger(self):
        raw = _raw_square_features(unique_authors=8, top3_author_share=0.70)

        result = evaluate_square_reversal(raw, alpha_score=90)

        self.assertFalse(result["candidate"])
        self.assertIn("unique_authors_15", result["reasons"][0])
        self.assertIn("author_concentration_ok", result["reasons"][0])

    def test_summary_deduplicates_and_builds_24h_baseline(self):
        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        posts = []
        for index in range(20):
            posts.append(
                {
                    "post_id": f"current-{index}",
                    "base_asset": "AKE",
                    "published_at": (
                        now - timedelta(minutes=5)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "author_id": f"author-{index}",
                    "content": f"AKE 看空 {index}",
                    "sentiment": "bearish",
                    "substantive_risk": 0,
                    "engagement": 0,
                }
            )
        for index in range(20):
            posts.append(
                {
                    "post_id": f"baseline-{index}",
                    "base_asset": "AKE",
                    "published_at": (
                        now - timedelta(hours=2)
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "author_id": f"old-author-{index}",
                    "content": f"AKE baseline {index}",
                    "sentiment": "bearish" if index < 5 else "bullish",
                    "substantive_risk": 0,
                    "engagement": 0,
                }
            )

        snapshot = summarize_square_posts(
            posts,
            base_asset="AKE",
            now=now,
        )

        self.assertEqual(snapshot[3], 20)
        self.assertEqual(snapshot[4], 20)
        self.assertEqual(snapshot[5], 1.0)
        self.assertEqual(snapshot[6], 0.25)
        self.assertLessEqual(snapshot[7], 0.15)


if __name__ == "__main__":
    unittest.main()
