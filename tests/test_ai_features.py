import unittest

from ai_service.features import FEATURE_SCHEMA_VERSION, extract_feature_payload


class AIFeatureExtractionTest(unittest.TestCase):
    def test_extracts_normal_engine_nested_features(self):
        features, quality = extract_feature_payload(
            {
                "composite_score": 78,
                "entry_alpha": 72,
                "hold_alpha": 66,
                "relative_strength": 61,
                "raw_features": {
                    "technical": {
                        "trend_score": 74,
                        "return_6h": 0.08,
                        "return_24h": 0.14,
                        "atr_ratio": 0.03,
                        "ema20_50_ratio": 1.012,
                        "volume_change_pct": 1.8,
                    },
                    "futures": {"funding_rate": 0.0002, "oi_change_pct": 0.06},
                    "depth": {"spread_pct": 0.0005},
                    "market_phase": {"confidence": 80},
                },
            },
            category="large_cap",
        )

        self.assertEqual(features["score"], 78)
        self.assertEqual(features["trend_score"], 74)
        self.assertEqual(features["entry_alpha"], 72)
        self.assertEqual(features["oi_change_pct"], 0.06)
        self.assertEqual(features["market_phase_confidence"], 80)
        self.assertEqual(quality["schema_version"], FEATURE_SCHEMA_VERSION)
        self.assertGreaterEqual(quality["present_count"], 12)

    def test_extracts_alpha_dual_market_features(self):
        features, quality = extract_feature_payload(
            {
                "alpha_score": 84,
                "returns": {"ret_15m": 0.04, "ret_6h": 0.18, "pct_24h": 0.32},
                "volume": {"alpha_volume_growth_6h": 3.1},
                "dual_market_volume": {
                    "futures_volume_ratio_6h": 2.4,
                    "volume_sync_score": 2.4,
                },
                "futures_sync": {"funding_rate": -0.0003, "oi_change_4h": 0.12},
                "depth": {"spread_pct": 0.001},
                "alpha_trend": {"trend_score": 81},
                "market_phase": {"confidence": 75},
            },
            category="alpha",
        )

        self.assertEqual(features["score"], 84)
        self.assertEqual(features["return_6h"], 0.18)
        self.assertEqual(features["spot_volume_ratio_6h"], 3.1)
        self.assertEqual(features["futures_volume_ratio_6h"], 2.4)
        self.assertEqual(features["volume_sync_score"], 2.4)
        self.assertEqual(features["oi_change_pct"], 0.12)
        self.assertGreaterEqual(quality["coverage"], 0.4)


if __name__ == "__main__":
    unittest.main()
