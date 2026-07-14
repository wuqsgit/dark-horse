import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_service.config import (
    ALLOW_THRESHOLD,
    MIN_TRAINING_SAMPLES,
    MIN_VALIDATION_SAMPLES,
    MODEL_MAX_AGE_HOURS,
    PROBE_MARGIN_PCT,
    PROBE_THRESHOLD,
    VALIDATION_FRACTION,
)
from ai_service.features import FEATURE_NAMES, canonical_features, vectorize


class ModelUnavailable(RuntimeError):
    pass


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class EntryQualityService:
    def __init__(
        self,
        store,
        backend,
        *,
        model_dir,
        min_training_samples=MIN_TRAINING_SAMPLES,
        min_validation_samples=MIN_VALIDATION_SAMPLES,
        model_max_age_hours=MODEL_MAX_AGE_HOURS,
        now_fn=None,
    ):
        self.store = store
        self.backend = backend
        self.model_dir = str(model_dir)
        self.min_training_samples = int(min_training_samples)
        self.min_validation_samples = int(min_validation_samples)
        self.model_max_age_hours = float(model_max_age_hours)
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._models = {}

    def _model_is_expired(self, model: dict) -> bool:
        return self.now_fn() - _parse_time(model["trained_at"]) > timedelta(hours=self.model_max_age_hours)

    def _load_model(self, metadata: dict):
        version = metadata["version"]
        if version not in self._models:
            self._models = {version: self.backend.load(metadata["artifact_path"])}
        return self._models[version]

    def evaluate(self, candidate: dict) -> dict:
        features = canonical_features(candidate.get("features") or {}, candidate.get("category"))
        sample = {**candidate, "features": features}
        self.store.add_sample(sample)
        metadata = self.store.get_model(candidate["model_key"])

        if not metadata or metadata.get("status") != "ready":
            result = {
                "status": "collecting", "decision": "collecting", "applied": False,
                "quality_score": None, "model_version": None, "target_margin_pct": None,
                "reasons": ["model is collecting labeled samples"],
            }
        else:
            if self._model_is_expired(metadata):
                raise ModelUnavailable(f"{candidate['model_key']} model expired")
            model = self._load_model(metadata)
            probability = max(0.0, min(1.0, float(self.backend.predict_one(model, vectorize(features, candidate.get("category"))))))
            quality = round(probability * 100, 2)
            if quality >= ALLOW_THRESHOLD:
                decision, target_margin = "allow", None
            elif quality >= PROBE_THRESHOLD:
                decision, target_margin = "probe", PROBE_MARGIN_PCT
            else:
                decision, target_margin = "reject", None
            result = {
                "status": "live", "decision": decision, "applied": True,
                "quality_score": quality, "model_version": metadata["version"],
                "target_margin_pct": target_margin,
                "reasons": list(self.backend.explain(model, vectorize(features, candidate.get("category"))))[:3],
            }

        self.store.record_decision({
            **candidate,
            "features": features,
            "model_version": result.get("model_version"),
            "quality_score": result.get("quality_score"),
            "decision": result["decision"],
            "mode": result["status"],
            "size_factor": result.get("target_margin_pct"),
            "reasons": result.get("reasons") or [],
        })
        return result

    def observe_many(self, candidates: list[dict]) -> dict:
        received = len(candidates or [])
        created = 0
        for candidate in candidates or []:
            features = canonical_features(candidate.get("features") or {}, candidate.get("category"))
            _, was_created = self.store.add_sample({**candidate, "features": features})
            created += int(was_created)
        return {"received": received, "created": created, "duplicates": received - created}

    def train(self, model_key: str) -> dict:
        samples = self.store.labeled_samples(model_key)
        count = len(samples)
        if count < self.min_training_samples:
            return {"status": "not_ready", "model_key": model_key, "labeled_samples": count}

        validation_count = max(self.min_validation_samples, int(math.ceil(count * VALIDATION_FRACTION)))
        if validation_count >= count:
            return {"status": "not_ready", "model_key": model_key, "labeled_samples": count}
        train_rows = samples[:-validation_count]
        validation_rows = samples[-validation_count:]
        if len({int(row["label"]) for row in train_rows}) < 2:
            return {
                "status": "not_ready", "model_key": model_key, "labeled_samples": count,
                "reason": "needs_both_outcome_classes",
            }
        model = self.backend.fit(
            [vectorize(row["features"], row.get("category")) for row in train_rows],
            [int(row["label"]) for row in train_rows],
            FEATURE_NAMES,
        )
        probabilities = self.backend.predict_many(
            model,
            [vectorize(row["features"], row.get("category")) for row in validation_rows],
        )
        baseline_mean_r = sum(1.0 if row["label"] else -1.0 for row in samples) / count
        allowed = [
            row for row, probability in zip(validation_rows, probabilities)
            if float(probability) * 100 >= PROBE_THRESHOLD
        ]
        allowed_mean_r = (
            sum(1.0 if row["label"] else -1.0 for row in allowed) / len(allowed)
            if allowed else -1.0
        )
        if not allowed or allowed_mean_r <= baseline_mean_r:
            return {
                "status": "rejected", "model_key": model_key, "labeled_samples": count,
                "baseline_mean_r": baseline_mean_r, "allowed_mean_r": allowed_mean_r,
            }

        now = self.now_fn()
        version = f"{model_key}_{now.strftime('%Y%m%dT%H%M%SZ')}"
        artifact_path = str(Path(self.model_dir) / f"{version}.json")
        self.backend.save(model, artifact_path)
        self.store.publish_model({
            "model_key": model_key, "version": version, "artifact_path": artifact_path,
            "trained_at": _utc_iso(now), "sample_count": count,
            "validation_count": validation_count, "baseline_mean_r": baseline_mean_r,
            "allowed_mean_r": allowed_mean_r,
            "metrics": {"allowed_count": len(allowed), "validation_count": validation_count},
        })
        self._models = {version: model}
        return {
            "status": "published", "model_key": model_key, "version": version,
            "labeled_samples": count, "validation_count": validation_count,
            "baseline_mean_r": baseline_mean_r, "allowed_mean_r": allowed_mean_r,
        }

    def status(self) -> dict:
        now = self.now_fn()
        models = {}
        service_status = "collecting"
        for key in ("alpha", "normal"):
            metadata = self.store.get_model(key)
            counts = self.store.sample_counts(key)
            utc_date = now.strftime("%Y-%m-%d")
            if metadata and metadata.get("status") == "ready":
                expired = self._model_is_expired(metadata)
                state = "error" if expired else "live"
                service_status = "error" if expired else ("live" if service_status != "error" else service_status)
            else:
                state = "collecting"
            models[key] = {
                "status": state,
                "version": metadata.get("version") if metadata else None,
                "trained_at": metadata.get("trained_at") if metadata else None,
                "sample_count": counts["labeled"],
                "total_samples": counts["total"],
                "pending_samples": counts["pending"],
                "collected_today": self.store.collected_today(key, utc_date),
                "required_samples": self.min_training_samples,
                "validation_count": metadata.get("validation_count") if metadata else 0,
                "decisions_today": self.store.decision_counts(key, utc_date),
            }
        return {"status": service_status, "time": _utc_iso(now), "models": models}
