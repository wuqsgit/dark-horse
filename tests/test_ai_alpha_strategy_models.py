import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_service.alpha_features_v3 import FEATURE_NAMES
from ai_service.alpha_strategy_service import AlphaStrategyService
from ai_service.storage import AIStore


class DeterministicBackend:
    def __init__(self):
        self.saved = []

    def fit(self, rows, labels, feature_names):
        return {"rows": len(rows)}

    def predict_many(self, model, rows):
        return [0.9 if row[0] > 0.5 else 0.1 for row in rows]

    def predict_one(self, model, row):
        return self.predict_many(model, [row])[0]

    def save(self, model, artifact_path):
        self.saved.append(artifact_path)

    def load(self, artifact_path):
        return {"artifact_path": artifact_path}

    def explain(self, model, row):
        return ["ret_15m supports prediction"]

    def feature_importance(self, model, feature_names):
        return {
            FEATURE_NAMES[0]: 1.0,
            FEATURE_NAMES[1]: 1.0,
        }


class AIAlphaStrategyModelsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.store = AIStore(root / "ai.db")
        self.backend = DeterministicBackend()
        self.now = datetime(2026, 7, 28, tzinfo=timezone.utc)
        self.service = AlphaStrategyService(
            self.store,
            backend=self.backend,
            model_dir=root / "models",
            min_training_samples=12,
            min_validation_samples=4,
            now_fn=lambda: self.now,
        )
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for index in range(12):
            label = index % 2
            sample_id, _ = self.store.add_alpha_strategy_sample(
                {
                    "request_id": f"trigger-{index}",
                    "market_env": "mainnet",
                    "model_key": "alpha_trigger_v1_mainnet",
                    "futures_symbol": f"AKE{index % 3}USDT",
                    "alpha_symbol": f"AKE{index % 3}ALPHAUSDT",
                    "stage": "trigger",
                    "setup_type": "accumulation",
                    "candle_close_time": (
                        start + timedelta(days=index)
                    ).isoformat().replace("+00:00", "Z"),
                    "feature_schema_version": 3,
                    "features": {
                        "ret_15m": float(label),
                        "ret_30m": float(1 - label),
                    },
                    "feature_quality": {
                        "status": "ready",
                        "coverage": 0.9,
                    },
                }
            )
            self.store.label_alpha_strategy_sample(
                sample_id,
                {
                    "followthrough": label,
                    "fakeout": 1 - label,
                    "mfe_r": 2.2 if label else 0.2,
                    "mae_r": -0.2 if label else -1.0,
                },
            )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_train_champion_challenger_promote_and_rollback(self):
        first = self.service.train(
            market_env="mainnet",
            stage="trigger",
            target="followthrough",
        )
        self.now += timedelta(days=1)
        second = self.service.train(
            market_env="mainnet",
            stage="trigger",
            target="followthrough",
        )

        self.assertEqual(first["status"], "champion")
        self.assertEqual(second["status"], "challenger")
        self.assertGreater(
            second["metrics"]["selected20_mean_r"],
            second["metrics"]["baseline_mean_r"],
        )
        self.assertIn("feature_profile", second["metrics"])
        self.assertTrue(self.service.promote(second["version"]))
        champion = self.store.get_alpha_strategy_model(
            model_key="alpha_trigger_v1_mainnet",
            target="followthrough",
        )
        self.assertEqual(champion["version"], second["version"])
        rolled_back = self.service.rollback(
            model_key="alpha_trigger_v1_mainnet",
            target="followthrough",
        )
        self.assertEqual(rolled_back, first["version"])
        runs = self.store.list_alpha_strategy_model_runs()
        self.assertEqual(len(runs), 2)


if __name__ == "__main__":
    unittest.main()
