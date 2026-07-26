import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_service.config import (
    AI_EXECUTION_MODE,
    ALLOW_THRESHOLD,
    ESTIMATED_COST_R,
    MAX_LIVE_POSITION_FACTOR,
    MIN_FEATURE_COVERAGE,
    MIN_TRAINING_SAMPLES,
    MIN_USABLE_FEATURES,
    MIN_VALIDATION_SAMPLES,
    MODEL_LAST_KNOWN_GOOD_HOURS,
    MODEL_MAX_AGE_HOURS,
    PROBE_MARGIN_PCT,
    PROBE_THRESHOLD,
    VALIDATION_FRACTION,
)
from ai_service.features import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    canonical_features,
    extract_feature_payload,
    vectorize,
)


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
        model_last_known_good_hours=MODEL_LAST_KNOWN_GOOD_HOURS,
        min_usable_features=MIN_USABLE_FEATURES,
        execution_mode=AI_EXECUTION_MODE,
        max_position_factor=MAX_LIVE_POSITION_FACTOR,
        now_fn=None,
    ):
        self.store = store
        self.backend = backend
        self.model_dir = str(model_dir)
        self.min_training_samples = int(min_training_samples)
        self.min_validation_samples = int(min_validation_samples)
        self.model_max_age_hours = float(model_max_age_hours)
        self.model_last_known_good_hours = float(model_last_known_good_hours)
        self.min_usable_features = int(min_usable_features)
        self.execution_mode = execution_mode if execution_mode in {"shadow", "live"} else "shadow"
        self.max_position_factor = max(0.0, float(max_position_factor))
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._models = {}

    def _model_is_expired(self, model: dict) -> bool:
        return self.now_fn() - _parse_time(model["trained_at"]) > timedelta(hours=self.model_max_age_hours)

    def _model_is_unusable(self, model: dict) -> bool:
        return self.now_fn() - _parse_time(model["trained_at"]) > timedelta(
            hours=self.model_last_known_good_hours
        )

    def _load_model(self, metadata: dict):
        version = metadata["version"]
        if version not in self._models:
            self._models = {version: self.backend.load(metadata["artifact_path"])}
        return self._models[version]

    def evaluate(self, candidate: dict) -> dict:
        features, inferred_quality = extract_feature_payload(
            candidate.get("features") or {}, category=candidate.get("category"),
        )
        quality = candidate.get("feature_quality") or inferred_quality
        sample = {
            **candidate,
            "features": features,
            "feature_schema_version": int(
                candidate.get("feature_schema_version") or FEATURE_SCHEMA_VERSION
            ),
            "feature_quality": quality,
        }
        self.store.add_sample(sample)
        metadata = self.store.get_model(candidate["model_key"])

        model_schema = int((metadata or {}).get("feature_schema_version") or 1)
        if (
            not metadata
            or metadata.get("status") != "ready"
            or model_schema != FEATURE_SCHEMA_VERSION
        ):
            result = {
                "status": "collecting", "decision": "collecting", "applied": False,
                "quality_score": None, "model_version": None, "target_margin_pct": None,
                "expected_r": None, "position_factor": None,
                "reasons": [
                    "collecting feature schema v2 samples"
                    if metadata and model_schema != FEATURE_SCHEMA_VERSION
                    else "model is collecting labeled samples"
                ],
            }
        else:
            if self._model_is_unusable(metadata):
                result = {
                    "status": "fallback", "decision": "rule_fallback", "applied": False,
                    "quality_score": None, "model_version": metadata["version"],
                    "target_margin_pct": None, "expected_r": None, "position_factor": None,
                    "reasons": ["model is older than the last-known-good window"],
                }
                self._record(candidate, features, result)
                return result
            model = self._load_model(metadata)
            probability = max(0.0, min(1.0, float(self.backend.predict_one(model, vectorize(features, candidate.get("category"))))))
            quality = round(probability * 100, 2)
            metrics = metadata.get("metrics") or {}
            avg_win_r = float(metrics.get("avg_win_r") or 1.0)
            avg_loss_r = abs(float(metrics.get("avg_loss_r") or 1.0))
            expected_r = round(
                probability * avg_win_r - (1.0 - probability) * avg_loss_r - ESTIMATED_COST_R,
                4,
            )
            if quality >= ALLOW_THRESHOLD:
                decision, target_margin = "allow", None
            elif quality >= PROBE_THRESHOLD:
                decision, target_margin = "probe", PROBE_MARGIN_PCT
            else:
                decision, target_margin = "reject", None
            recommended_factor = self._position_factor(expected_r, decision)
            stale = self._model_is_expired(metadata)
            applied = self.execution_mode == "live"
            result = {
                "status": "stale_live" if stale and applied else self.execution_mode,
                "decision": decision, "applied": applied,
                "quality_score": quality, "model_version": metadata["version"],
                "target_margin_pct": target_margin,
                "expected_r": expected_r,
                "position_factor": min(recommended_factor, self.max_position_factor),
                "reasons": list(self.backend.explain(model, vectorize(features, candidate.get("category"))))[:3],
            }

        self._record(candidate, features, result)
        return result

    @staticmethod
    def _position_factor(expected_r: float, decision: str) -> float:
        if decision == "reject" or expected_r < 0:
            return 0.0
        if expected_r < 0.15:
            return 0.5
        if expected_r < 0.35:
            return 1.0
        if expected_r < 0.50:
            return 1.5
        return 2.0

    def _record(self, candidate: dict, features: dict, result: dict) -> None:
        self.store.record_decision({
            **candidate,
            "features": features,
            "model_version": result.get("model_version"),
            "quality_score": result.get("quality_score"),
            "decision": result["decision"],
            "mode": result["status"],
            "size_factor": result.get("position_factor"),
            "expected_r": result.get("expected_r"),
            "applied": result.get("applied"),
            "reasons": result.get("reasons") or [],
        })

    def observe_many(self, candidates: list[dict]) -> dict:
        received = len(candidates or [])
        created = 0
        for candidate in candidates or []:
            features = canonical_features(candidate.get("features") or {}, candidate.get("category"))
            _, was_created = self.store.add_sample({**candidate, "features": features})
            created += int(was_created)
        return {"received": received, "created": created, "duplicates": received - created}

    def train(self, model_key: str) -> dict:
        all_samples = self.store.labeled_samples(model_key)
        samples = [
            row for row in all_samples
            if int(row.get("feature_schema_version") or 1) == FEATURE_SCHEMA_VERSION
            and int((row.get("feature_quality") or {}).get("present_count") or 0)
            >= self.min_usable_features
        ]
        count = len(samples)
        if count < self.min_training_samples:
            return {
                "status": "not_ready", "model_key": model_key,
                "labeled_samples": count, "total_labeled_samples": len(all_samples),
                "reason": "feature_quality_insufficient",
            }

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
        def realized_r(row):
            if int(row["label"]):
                return min(3.0, max(1.0, float(row.get("mfe_r") or 1.0)))
            return -min(1.5, max(0.25, abs(float(row.get("mae_r") or -1.0))))

        baseline_mean_r = sum(realized_r(row) for row in validation_rows) / len(validation_rows)
        allowed = [
            row for row, probability in zip(validation_rows, probabilities)
            if float(probability) * 100 >= PROBE_THRESHOLD
        ]
        allowed_mean_r = (
            sum(realized_r(row) for row in allowed) / len(allowed)
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
        winners = [row for row in train_rows if int(row["label"])]
        losers = [row for row in train_rows if not int(row["label"])]
        avg_win_r = (
            sum(min(3.0, max(1.0, float(row.get("mfe_r") or 1.0))) for row in winners)
            / len(winners)
            if winners else 1.0
        )
        avg_loss_r = (
            sum(min(1.5, max(0.25, abs(float(row.get("mae_r") or -1.0)))) for row in losers)
            / len(losers)
            if losers else 1.0
        )
        feature_coverage = {
            name: round(
                sum(
                    1 for row in samples
                    if name in (row.get("feature_quality") or {}).get("present_features", [])
                ) / count,
                4,
            )
            for name in FEATURE_NAMES
            if name != "category_code"
        }
        usable_feature_count = sum(
            1 for coverage in feature_coverage.values()
            if coverage >= MIN_FEATURE_COVERAGE
        )
        if usable_feature_count < self.min_usable_features:
            return {
                "status": "rejected", "model_key": model_key,
                "labeled_samples": count, "reason": "feature_coverage_insufficient",
                "usable_feature_count": usable_feature_count,
            }
        importance = (
            self.backend.feature_importance(model, FEATURE_NAMES)
            if hasattr(self.backend, "feature_importance")
            else {}
        )
        importance_total = sum(max(0.0, float(value)) for value in importance.values())
        importance_share = {
            name: round(max(0.0, float(value)) / importance_total, 4)
            for name, value in importance.items()
        } if importance_total > 0 else {}
        max_importance_share = max(importance_share.values(), default=0.0)
        if importance_share and max_importance_share > 0.60:
            return {
                "status": "rejected", "model_key": model_key,
                "labeled_samples": count, "reason": "single_feature_dominance",
                "max_feature_importance_share": max_importance_share,
                "feature_importance": importance_share,
            }
        self.backend.save(model, artifact_path)
        self.store.publish_model({
            "model_key": model_key, "version": version, "artifact_path": artifact_path,
            "trained_at": _utc_iso(now), "sample_count": count,
            "validation_count": validation_count, "baseline_mean_r": baseline_mean_r,
            "allowed_mean_r": allowed_mean_r,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "metrics": {
                "allowed_count": len(allowed), "validation_count": validation_count,
                "validation_coverage": round(len(allowed) / validation_count, 4),
                "avg_win_r": round(avg_win_r, 4), "avg_loss_r": round(avg_loss_r, 4),
                "usable_feature_count": usable_feature_count,
                "feature_coverage": feature_coverage,
                "feature_importance": importance_share,
                "max_feature_importance_share": max_importance_share,
            },
        })
        self._models = {version: model}
        return {
            "status": "published", "model_key": model_key, "version": version,
            "labeled_samples": count, "validation_count": validation_count,
            "baseline_mean_r": baseline_mean_r, "allowed_mean_r": allowed_mean_r,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "usable_feature_count": usable_feature_count,
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
                compatible = int(metadata.get("feature_schema_version") or 1) == FEATURE_SCHEMA_VERSION
                unusable = compatible and self._model_is_unusable(metadata)
                if not compatible:
                    state = "collecting"
                elif unusable:
                    state = "error"
                    service_status = "error"
                elif self.execution_mode == "shadow":
                    state = "shadow"
                    if service_status not in {"error", "live"}:
                        service_status = "shadow"
                else:
                    state = "live"
                    if service_status != "error":
                        service_status = "live"
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
                "feature_schema_version": int(metadata.get("feature_schema_version") or 1) if metadata else None,
                "execution_mode": self.execution_mode,
                "feature_quality": self.store.feature_quality_summary(
                    key, FEATURE_SCHEMA_VERSION,
                ),
                "decisions_today": self.store.decision_counts(key, utc_date),
            }
        return {"status": service_status, "time": _utc_iso(now), "models": models}
