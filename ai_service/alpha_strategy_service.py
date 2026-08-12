"""Shadow-first AI interface for Alpha Strategy V2."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ai_service.alpha_features_v3 import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    vectorize_alpha_features,
)
from ai_service.config import MODEL_DIR
from ai_service.model import XGBoostBackend

STAGES = {"setup", "trigger", "acceptance", "retest"}


class AlphaStrategyService:
    def __init__(
        self,
        store,
        *,
        predictor: Callable[[dict], dict] | None = None,
        execution_mode: str = "shadow",
        backend=None,
        model_dir=None,
        min_training_samples: int = 1000,
        min_validation_samples: int = 300,
        now_fn=None,
    ):
        self.store = store
        self.predictor = predictor
        self.backend = backend or XGBoostBackend()
        self.model_dir = str(
            model_dir or (Path(MODEL_DIR).parent / "alpha_strategy")
        )
        self.min_training_samples = int(min_training_samples)
        self.min_validation_samples = int(min_validation_samples)
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._models = {}
        self.execution_mode = (
            execution_mode if execution_mode in {"shadow", "live"} else "shadow"
        )

    def _validate(self, payload: dict) -> None:
        required = (
            "request_id",
            "market_env",
            "futures_symbol",
            "stage",
            "candle_close_time",
            "feature_schema_version",
            "features",
        )
        missing = [name for name in required if payload.get(name) is None]
        if missing:
            raise ValueError("missing Alpha Strategy fields: " + ", ".join(missing))
        if int(payload["feature_schema_version"]) != FEATURE_SCHEMA_VERSION:
            raise ValueError(
                f"Alpha Strategy feature schema "
                f"{payload['feature_schema_version']} != {FEATURE_SCHEMA_VERSION}"
            )
        if str(payload["stage"]).lower() not in STAGES:
            raise ValueError(f"unsupported Alpha Strategy stage: {payload['stage']}")
        if str(payload["market_env"]).lower() != "mainnet":
            raise ValueError("Alpha Strategy market_env must be mainnet")

    def evaluate(self, payload: dict) -> dict:
        self._validate(payload)
        sample = {
            **payload,
            "model_key": (
                f"alpha_{str(payload['stage']).lower()}_v1_"
                f"{str(payload['market_env']).lower()}"
            ),
        }
        self.store.add_alpha_strategy_sample(sample)
        prediction = (
            dict(self.predictor(payload) or {})
            if self.predictor is not None
            else self._predict_with_published_models(payload)
        )
        if prediction is None:
            result = {
                "status": "collecting",
                "applied": False,
                "model_versions": {},
                "p_setup_success": None,
                "p_followthrough": None,
                "p_fakeout": None,
                "expected_r": None,
                "recommended_action": "observe",
                "max_position_factor": 0.0,
                "reasons": ["collecting Alpha Strategy V4 samples"],
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
            }
        else:
            action = self._recommended_action(payload, prediction)
            result = {
                "status": self.execution_mode,
                "applied": self.execution_mode == "live",
                "model_versions": prediction.get("model_versions") or {},
                "p_setup_success": prediction.get("p_setup_success"),
                "p_followthrough": prediction.get("p_followthrough"),
                "p_fakeout": prediction.get("p_fakeout"),
                "expected_r": prediction.get("expected_r"),
                "recommended_action": action,
                "max_position_factor": float(
                    prediction.get("max_position_factor") or 0.0
                ),
                "reasons": list(prediction.get("reasons") or []),
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
            }
        self.store.record_alpha_strategy_decision(payload, result)
        return result

    @staticmethod
    def _model_requirements(stage: str, market_env: str) -> list[tuple[str, str]]:
        env = str(market_env).lower()
        if env != "mainnet":
            raise ValueError("Alpha Strategy market_env must be mainnet")
        stage_key = str(stage).lower()
        if stage_key == "setup":
            return [(f"alpha_setup_v1_{env}", "setup_success")]
        return [
            (f"alpha_trigger_v1_{env}", "followthrough"),
            (f"alpha_fakeout_v1_{env}", "fakeout"),
        ]

    def _load_model(self, metadata: dict):
        version = metadata["version"]
        if version not in self._models:
            self._models[version] = self.backend.load(metadata["artifact_path"])
        return self._models[version]

    def _predict_with_published_models(self, payload: dict) -> dict | None:
        requirements = self._model_requirements(
            payload["stage"],
            payload["market_env"],
        )
        probabilities = {}
        versions = {}
        reasons = []
        vector = vectorize_alpha_features(payload.get("features") or {})
        for model_key, target in requirements:
            metadata = self.store.get_alpha_strategy_model(
                model_key=model_key,
                target=target,
            )
            if not metadata:
                return None
            model = self._load_model(metadata)
            probability = max(
                0.0,
                min(1.0, float(self.backend.predict_one(model, vector))),
            )
            probabilities[target] = probability
            versions[target] = metadata["version"]
            if hasattr(self.backend, "explain"):
                reasons.extend(self.backend.explain(model, vector)[:2])

        stage = str(payload["stage"]).lower()
        p_setup = probabilities.get("setup_success")
        p_follow = probabilities.get("followthrough")
        p_fakeout = probabilities.get("fakeout")
        if stage == "setup":
            expected_r = (
                p_setup * 2 - (1 - p_setup)
                if p_setup is not None
                else None
            )
            position_factor = 0.0
        else:
            expected_r = (
                p_follow * 2 - (1 - p_follow) - p_fakeout * 0.5
                if p_follow is not None and p_fakeout is not None
                else None
            )
            position_factor = {
                "trigger": 0.30,
                "acceptance": 0.70,
                "retest": 1.00,
            }.get(stage, 0.0)
        return {
            "model_versions": versions,
            "p_setup_success": p_setup,
            "p_followthrough": p_follow,
            "p_fakeout": p_fakeout,
            "expected_r": expected_r,
            "max_position_factor": (
                position_factor
                if expected_r is not None and expected_r > 0
                else 0.0
            ),
            "reasons": list(dict.fromkeys(reasons))[:3],
        }

    @staticmethod
    def _recommended_action(payload: dict, prediction: dict) -> str:
        stage = str(payload["stage"]).lower()
        p_setup = float(prediction.get("p_setup_success") or 0)
        p_follow = float(prediction.get("p_followthrough") or 0)
        raw_fakeout = prediction.get("p_fakeout")
        p_fakeout = float(raw_fakeout if raw_fakeout is not None else 1)
        if stage == "setup":
            return "watch" if p_setup >= 0.55 else "observe"
        if stage == "trigger":
            return "probe" if p_follow >= 0.65 and p_fakeout <= 0.35 else "observe"
        if stage in {"acceptance", "retest"}:
            return "confirm" if p_follow >= 0.70 and p_fakeout <= 0.25 else "observe"
        return "observe"

    def observe_many(self, candidates: list[dict]) -> dict:
        received = len(candidates or [])
        created = 0
        for payload in candidates or []:
            self._validate(payload)
            _, was_created = self.store.add_alpha_strategy_sample(
                {
                    **payload,
                    "model_key": (
                        f"alpha_{str(payload['stage']).lower()}_v1_"
                        f"{str(payload['market_env']).lower()}"
                    ),
                }
            )
            created += int(was_created)
        return {
            "received": received,
            "created": created,
            "duplicates": received - created,
        }

    def status(self) -> dict:
        models = self.store.list_alpha_strategy_models()
        model_status = []
        for model in models:
            item = dict(model)
            if item.get("status") in {"champion", "challenger"}:
                item["drift"] = self._model_drift(item)
            else:
                item["drift"] = {
                    "status": "not_monitored",
                    "recent_samples": 0,
                    "max_mean_shift_z": None,
                    "max_missing_rate_delta": None,
                }
            model_status.append(item)
        has_published_model = bool(
            self.predictor is not None
            or any(model["status"] == "champion" for model in models)
        )
        return {
            "status": (
                self.execution_mode
                if has_published_model
                else "collecting"
            ),
            "execution_mode": self.execution_mode,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "samples": self.store.alpha_strategy_sample_counts(),
            "training_requirements": {
                "minimum_training_samples": self.min_training_samples,
                "minimum_validation_samples": self.min_validation_samples,
            },
            "feature_quality": self.store.alpha_strategy_quality_summary(),
            "execution_outcomes": (
                self.store.alpha_strategy_execution_summary()
            ),
            "models": model_status,
            "recent_training_runs": (
                self.store.list_alpha_strategy_model_runs(30)
            ),
        }

    @staticmethod
    def _feature_profile(samples: list[dict]) -> dict:
        profile = {}
        count = len(samples)
        for name in FEATURE_NAMES:
            values = []
            for row in samples:
                value = (row.get("features") or {}).get(name)
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(parsed):
                    values.append(parsed)
            if not values:
                profile[name] = {
                    "mean": None,
                    "std": None,
                    "missing_rate": 1.0,
                }
                continue
            average = sum(values) / len(values)
            variance = sum((value - average) ** 2 for value in values) / len(values)
            profile[name] = {
                "mean": average,
                "std": math.sqrt(variance),
                "missing_rate": 1 - len(values) / max(1, count),
            }
        return profile

    def _model_drift(self, model: dict) -> dict:
        baseline = (model.get("metrics") or {}).get("feature_profile") or {}
        recent = self.store.recent_alpha_strategy_samples(
            model_key=model["model_key"],
            limit=500,
        )
        if not baseline or len(recent) < 30:
            return {
                "status": "insufficient",
                "recent_samples": len(recent),
                "max_mean_shift_z": None,
                "max_missing_rate_delta": None,
            }
        current = self._feature_profile(recent)
        shifts = {}
        missing_deltas = {}
        for name in FEATURE_NAMES:
            old = baseline.get(name) or {}
            new = current.get(name) or {}
            old_mean = old.get("mean")
            new_mean = new.get("mean")
            old_std = old.get("std")
            if old_mean is not None and new_mean is not None:
                denominator = max(abs(float(old_std or 0)), 1e-9)
                shifts[name] = abs(float(new_mean) - float(old_mean)) / denominator
            missing_deltas[name] = abs(
                float(new.get("missing_rate") or 0)
                - float(old.get("missing_rate") or 0)
            )
        max_shift_name = max(shifts, key=shifts.get) if shifts else None
        max_missing_name = (
            max(missing_deltas, key=missing_deltas.get)
            if missing_deltas
            else None
        )
        max_shift = shifts.get(max_shift_name) if max_shift_name else None
        max_missing = (
            missing_deltas.get(max_missing_name)
            if max_missing_name
            else None
        )
        drifted = (
            (max_shift is not None and max_shift > 3.0)
            or (max_missing is not None and max_missing > 0.15)
        )
        return {
            "status": "drift" if drifted else "stable",
            "recent_samples": len(recent),
            "max_mean_shift_z": (
                round(max_shift, 4) if max_shift is not None else None
            ),
            "max_mean_shift_feature": max_shift_name,
            "max_missing_rate_delta": (
                round(max_missing, 4) if max_missing is not None else None
            ),
            "max_missing_feature": max_missing_name,
        }

    @staticmethod
    def _average_precision(labels: list[int], probabilities: list[float]) -> float:
        positives = sum(labels)
        if positives <= 0:
            return 0.0
        ranked = sorted(
            zip(probabilities, labels),
            key=lambda item: item[0],
            reverse=True,
        )
        hits = 0
        total = 0.0
        for rank, (_, label) in enumerate(ranked, 1):
            if label:
                hits += 1
                total += hits / rank
        return total / positives

    def train(
        self,
        *,
        market_env: str,
        stage: str,
        target: str,
    ) -> dict:
        env = str(market_env).lower()
        stage_key = str(stage).lower()
        target_key = str(target).lower()
        if target_key == "setup_success":
            model_key = f"alpha_setup_v1_{env}"
            sample_model_key = f"alpha_setup_v1_{env}"
        elif target_key == "followthrough":
            model_key = f"alpha_trigger_v1_{env}"
            sample_model_key = f"alpha_{stage_key}_v1_{env}"
        elif target_key == "fakeout":
            model_key = f"alpha_fakeout_v1_{env}"
            sample_model_key = f"alpha_{stage_key}_v1_{env}"
        else:
            raise ValueError(f"unsupported Alpha model target: {target}")

        samples = self.store.labeled_alpha_strategy_samples(
            model_key=sample_model_key,
            target=target_key,
        )
        samples = [
            row for row in samples
            if int(row.get("feature_schema_version") or 0)
            == FEATURE_SCHEMA_VERSION
            and (row.get("feature_quality") or {}).get("status") == "ready"
        ]
        # Thin adjacent observations from one continuous move so one trend
        # cannot dominate the training set.
        thinned = []
        last_by_symbol = {}
        for row in samples:
            timestamp = datetime.fromisoformat(
                str(row["candle_close_time"]).replace("Z", "+00:00")
            )
            previous = last_by_symbol.get(row["futures_symbol"])
            if previous and timestamp - previous < timedelta(hours=1):
                continue
            last_by_symbol[row["futures_symbol"]] = timestamp
            thinned.append(row)
        samples = thinned
        count = len(samples)
        if count < self.min_training_samples:
            result = {
                "status": "not_ready",
                "model_key": model_key,
                "market_env": env,
                "stage": stage_key,
                "target": target_key,
                "labeled_samples": count,
                "required_samples": self.min_training_samples,
            }
            self.store.record_alpha_strategy_model_run(
                {**result, "sample_count": count}
            )
            return result

        validation_count = max(
            self.min_validation_samples,
            int(math.ceil(count * 0.20)),
        )
        if validation_count >= count:
            result = {
                "status": "not_ready",
                "model_key": model_key,
                "market_env": env,
                "stage": stage_key,
                "target": target_key,
                "sample_count": count,
                "validation_count": validation_count,
                "labeled_samples": count,
                "reason": "validation_window_too_large",
            }
            self.store.record_alpha_strategy_model_run(result)
            return result
        validation = samples[-validation_count:]
        validation_start = datetime.fromisoformat(
            str(validation[0]["candle_close_time"]).replace("Z", "+00:00")
        )
        purge_cutoff = validation_start - timedelta(hours=24)
        training = [
            row for row in samples[:-validation_count]
            if datetime.fromisoformat(
                str(row["candle_close_time"]).replace("Z", "+00:00")
            )
            <= purge_cutoff
        ]
        labels = [int(row["label"]) for row in training]
        validation_labels = [int(row["label"]) for row in validation]
        if len(training) < 2 or len(set(labels)) < 2 or len(set(validation_labels)) < 2:
            result = {
                "status": "not_ready",
                "model_key": model_key,
                "market_env": env,
                "stage": stage_key,
                "target": target_key,
                "sample_count": count,
                "validation_count": validation_count,
                "labeled_samples": count,
                "reason": "needs_both_classes_after_time_purge",
            }
            self.store.record_alpha_strategy_model_run(result)
            return result
        model = self.backend.fit(
            [vectorize_alpha_features(row["features"]) for row in training],
            labels,
            FEATURE_NAMES,
        )
        probabilities = self.backend.predict_many(
            model,
            [
                vectorize_alpha_features(row["features"])
                for row in validation
            ],
        )
        positive_rate = sum(validation_labels) / len(validation_labels)
        average_precision = self._average_precision(
            validation_labels,
            probabilities,
        )
        brier = sum(
            (probability - label) ** 2
            for probability, label in zip(probabilities, validation_labels)
        ) / len(validation_labels)
        ranked = sorted(
            zip(probabilities, validation),
            key=lambda item: item[0],
            reverse=True,
        )
        top_count = max(1, int(math.ceil(len(ranked) * 0.20)))
        selected_rows = [
            row
            for _, row in (
                ranked[-top_count:]
                if target_key == "fakeout"
                else ranked[:top_count]
            )
        ]

        def realized_r(row):
            labels_row = row.get("labels") or {}
            return (
                min(3.0, float(labels_row.get("mfe_r") or 0))
                if int(labels_row.get("followthrough") or 0)
                else -min(1.5, abs(float(labels_row.get("mae_r") or -1)))
            )

        baseline_ev = sum(realized_r(row) for row in validation) / len(validation)
        selected_ev = (
            sum(realized_r(row) for row in selected_rows)
            / len(selected_rows)
        )
        metrics = {
            "positive_rate": round(positive_rate, 6),
            "pr_auc": round(average_precision, 6),
            "brier_score": round(brier, 6),
            "baseline_mean_r": round(baseline_ev, 6),
            "selected20_mean_r": round(selected_ev, 6),
            "selected20_definition": (
                "lowest_fakeout_probability"
                if target_key == "fakeout"
                else "highest_success_probability"
            ),
            "feature_names": list(FEATURE_NAMES),
            "purge_gap_hours": 24,
            "training_count": len(training),
        }
        calibration = []
        for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
            pairs = [
                (probability, label)
                for probability, label in zip(
                    probabilities,
                    validation_labels,
                )
                if lower <= probability < lower + 0.2
                or (lower == 0.8 and probability == 1.0)
            ]
            if pairs:
                calibration.append(
                    {
                        "lower": lower,
                        "upper": lower + 0.2,
                        "count": len(pairs),
                        "mean_probability": round(
                            sum(item[0] for item in pairs) / len(pairs),
                            6,
                        ),
                        "positive_rate": round(
                            sum(item[1] for item in pairs) / len(pairs),
                            6,
                        ),
                    }
                )
        metrics["calibration"] = calibration
        if target_key == "fakeout":
            positives = sum(validation_labels)
            fakeout_recall = (
                sum(
                    label == 1 and probability >= 0.5
                    for probability, label in zip(
                        probabilities,
                        validation_labels,
                    )
                )
                / positives
                if positives
                else 0.0
            )
            metrics["fakeout_recall_at_0_5"] = round(fakeout_recall, 6)
        else:
            fakeout_recall = None
        if (
            average_precision <= positive_rate
            or brier > 0.35
            or selected_ev <= baseline_ev
            or (
                target_key == "fakeout"
                and fakeout_recall is not None
                and fakeout_recall < 0.55
            )
        ):
            result = {
                "status": "rejected",
                "model_key": model_key,
                "market_env": env,
                "stage": stage_key,
                "target": target_key,
                "sample_count": count,
                "validation_count": validation_count,
                "metrics": metrics,
                "reason": "validation_threshold_not_met",
            }
            self.store.record_alpha_strategy_model_run(result)
            return result

        importance = (
            self.backend.feature_importance(model, FEATURE_NAMES)
            if hasattr(self.backend, "feature_importance")
            else {}
        )
        importance_total = sum(max(0.0, float(value)) for value in importance.values())
        importance_share = {
            name: max(0.0, float(value)) / importance_total
            for name, value in importance.items()
        } if importance_total > 0 else {}
        if importance_share and max(importance_share.values()) > 0.60:
            result = {
                "status": "rejected",
                "model_key": model_key,
                "market_env": env,
                "stage": stage_key,
                "target": target_key,
                "sample_count": count,
                "validation_count": validation_count,
                "reason": "single_feature_dominance",
                "max_feature_importance_share": max(importance_share.values()),
            }
            self.store.record_alpha_strategy_model_run(result)
            return result
        metrics["feature_importance"] = importance_share
        metrics["feature_profile"] = self._feature_profile(training)
        now = self.now_fn()
        version = f"{model_key}_{target_key}_{now.strftime('%Y%m%dT%H%M%SZ')}"
        artifact_path = str(Path(self.model_dir) / f"{version}.json")
        self.backend.save(model, artifact_path)
        existing = self.store.get_alpha_strategy_model(
            model_key=model_key,
            target=target_key,
        )
        publish_status = "challenger" if existing else "champion"
        self.store.publish_alpha_strategy_model(
            {
                "version": version,
                "model_key": model_key,
                "market_env": env,
                "stage": stage_key,
                "target": target_key,
                "artifact_path": artifact_path,
                "trained_at": now.isoformat().replace("+00:00", "Z"),
                "sample_count": count,
                "validation_count": validation_count,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "metrics": metrics,
            },
            status=publish_status,
        )
        result = {
            "status": publish_status,
            "model_key": model_key,
            "market_env": env,
            "stage": stage_key,
            "target": target_key,
            "version": version,
            "sample_count": count,
            "validation_count": validation_count,
            "metrics": metrics,
        }
        self.store.record_alpha_strategy_model_run(result)
        self._models[version] = model
        return result

    def promote(self, version: str) -> bool:
        return self.store.promote_alpha_strategy_model(version)

    def rollback(self, *, model_key: str, target: str) -> str | None:
        return self.store.rollback_alpha_strategy_model(
            model_key=model_key,
            target=target,
        )
