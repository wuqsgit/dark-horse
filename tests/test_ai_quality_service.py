import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from ai_service.service import EntryQualityService, ModelUnavailable
from ai_service.storage import AIStore


class FakeBackend:
    def __init__(self, probability=0.70, validation_probabilities=None):
        self.probability = probability
        self.validation_probabilities = validation_probabilities
        self.saved = []

    def load(self, artifact_path):
        return {"artifact_path": artifact_path}

    def predict_one(self, model, features):
        return self.probability

    def explain(self, model, features):
        return ["trend_score supports entry", "spread_pct adds risk"]

    def fit(self, rows, labels, feature_names):
        return {"trained": len(rows)}

    def predict_many(self, model, rows):
        if self.validation_probabilities is not None:
            return self.validation_probabilities[-len(rows):]
        return [self.probability for _ in rows]

    def save(self, model, artifact_path):
        self.saved.append(artifact_path)
        os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
        with open(artifact_path, "w", encoding="utf-8") as handle:
            handle.write("fake")


def candidate(score=82.0):
    return {
        "account_id": 1,
        "model_key": "alpha",
        "symbol": "B2USDT",
        "side": "LONG",
        "template": "alpha_trend_probe",
        "category": "alpha",
        "observed_at": "2026-07-14T10:05:00Z",
        "entry_price": 0.5,
        "stop_pct": 0.04,
        "features": {"score": score, "trend_score": 75, "spread_pct": 0.0004},
    }


class AIQualityServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AIStore(os.path.join(self.tmp.name, "ai.db"))
        self.model_dir = os.path.join(self.tmp.name, "models")

    def tearDown(self):
        self.tmp.cleanup()

    def service(self, probability=0.70, now=None):
        return EntryQualityService(
            self.store,
            FakeBackend(probability),
            model_dir=self.model_dir,
            now_fn=lambda: now or datetime(2026, 7, 14, 11, tzinfo=timezone.utc),
        )

    def publish(self, trained_at=None):
        trained = trained_at or "2026-07-14T09:00:00Z"
        self.store.publish_model({
            "model_key": "alpha",
            "version": "alpha-v1",
            "artifact_path": os.path.join(self.model_dir, "alpha-v1.json"),
            "trained_at": trained,
            "sample_count": 300,
            "validation_count": 60,
            "baseline_mean_r": 0.02,
            "allowed_mean_r": 0.25,
            "metrics": {"validation_accuracy": 0.61},
        })

    def test_collecting_model_records_sample_but_does_not_gate_entry(self):
        result = self.service().evaluate(candidate())
        self.assertEqual(result["status"], "collecting")
        self.assertEqual(result["decision"], "collecting")
        self.assertFalse(result["applied"])
        self.assertEqual(self.store.sample_counts("alpha")["total"], 1)

    def test_observe_many_collects_candidates_without_creating_gate_decisions(self):
        service = self.service()
        payload = [
            candidate(),
            {**candidate(), "observed_at": "2026-07-14T10:45:00Z"},
            {**candidate(), "symbol": "AKEUSDT", "observed_at": "2026-07-14T10:10:00Z"},
        ]

        result = service.observe_many(payload)

        self.assertEqual(result, {"received": 3, "created": 2, "duplicates": 1})
        self.assertEqual(self.store.sample_counts("alpha")["total"], 2)
        self.assertEqual(self.store.list_decisions(), [])

    def test_status_reports_total_pending_and_labeled_sample_progress(self):
        first_id, _ = self.store.add_sample(candidate())
        self.store.set_sample_label(
            first_id, label=1, first_event="plus_1r", mfe_r=1.2, mae_r=-0.2,
        )
        self.store.add_sample({**candidate(), "observed_at": "2026-07-14T11:05:00Z"})
        self.store.add_sample({**candidate(), "observed_at": "2026-07-13T11:05:00Z"})

        alpha = self.service().status()["models"]["alpha"]

        self.assertEqual(alpha["total_samples"], 3)
        self.assertEqual(alpha["pending_samples"], 2)
        self.assertEqual(alpha["sample_count"], 1)
        self.assertEqual(alpha["collected_today"], 2)

    def test_ready_model_applies_allow_probe_and_reject_thresholds(self):
        self.publish()
        allow = self.service(0.70).evaluate(candidate())
        probe = self.service(0.58).evaluate({**candidate(), "observed_at": "2026-07-14T11:05:00Z"})
        reject = self.service(0.40).evaluate({**candidate(), "observed_at": "2026-07-14T12:05:00Z"})

        self.assertEqual((allow["decision"], allow["quality_score"]), ("allow", 70.0))
        self.assertEqual((probe["decision"], probe["target_margin_pct"]), ("probe", 0.05))
        self.assertEqual(reject["decision"], "reject")
        self.assertTrue(all(item["applied"] for item in (allow, probe, reject)))

    def test_expired_model_raises_instead_of_falling_back(self):
        self.publish("2026-07-11T09:00:00Z")
        with self.assertRaises(ModelUnavailable):
            self.service().evaluate(candidate())

    def test_training_does_not_publish_below_minimum_sample_count(self):
        service = EntryQualityService(
            self.store,
            FakeBackend(),
            model_dir=self.model_dir,
            min_training_samples=3,
            min_validation_samples=1,
            now_fn=lambda: datetime(2026, 7, 14, 11, tzinfo=timezone.utc),
        )
        for idx in range(2):
            sample_id, _ = self.store.add_sample({
                **candidate(), "observed_at": f"2026-07-14T0{idx}:05:00Z",
            })
            self.store.set_sample_label(sample_id, label=idx % 2, first_event="test", mfe_r=1, mae_r=-1)

        result = service.train("alpha")
        self.assertEqual(result["status"], "not_ready")
        self.assertEqual(result["labeled_samples"], 2)
        self.assertIsNone(self.store.get_model("alpha"))

    def test_training_publishes_when_ready_and_allowed_group_improves_r(self):
        backend = FakeBackend(validation_probabilities=[0.20, 0.30, 0.40, 0.45, 0.80])
        service = EntryQualityService(
            self.store,
            backend,
            model_dir=self.model_dir,
            min_training_samples=5,
            min_validation_samples=1,
            now_fn=lambda: datetime(2026, 7, 14, 11, tzinfo=timezone.utc),
        )
        for idx, label in enumerate([0, 1, 0, 0, 1]):
            sample_id, _ = self.store.add_sample({
                **candidate(), "observed_at": f"2026-07-14T0{idx}:05:00Z",
            })
            self.store.set_sample_label(
                sample_id, label=label, first_event="plus_1r" if label else "minus_1r",
                mfe_r=1.2 if label else 0.3, mae_r=-0.2 if label else -1.1,
            )

        result = service.train("alpha")
        model = self.store.get_model("alpha")
        self.assertEqual(result["status"], "published")
        self.assertEqual(model["status"], "ready")
        self.assertEqual(model["sample_count"], 5)
        self.assertTrue(os.path.exists(model["artifact_path"]))

    def test_training_waits_when_history_contains_only_one_outcome_class(self):
        service = EntryQualityService(
            self.store,
            FakeBackend(),
            model_dir=self.model_dir,
            min_training_samples=3,
            min_validation_samples=1,
            now_fn=lambda: datetime(2026, 7, 14, 11, tzinfo=timezone.utc),
        )
        for idx in range(3):
            sample_id, _ = self.store.add_sample({
                **candidate(), "observed_at": f"2026-07-14T0{idx}:05:00Z",
            })
            self.store.set_sample_label(
                sample_id, label=0, first_event="minus_1r", mfe_r=0.1, mae_r=-1.0,
            )

        result = service.train("alpha")

        self.assertEqual(result["status"], "not_ready")
        self.assertEqual(result["reason"], "needs_both_outcome_classes")


if __name__ == "__main__":
    unittest.main()
