from pathlib import Path


class XGBoostBackend:
    def __init__(self):
        self.xgb = None

    def _runtime(self):
        if self.xgb is not None:
            return self.xgb
        try:
            import xgboost as xgb
        except ImportError as exc:
            raise RuntimeError("xgboost is required by ai_service") from exc
        self.xgb = xgb
        return xgb

    def fit(self, rows, labels, feature_names):
        model = self._runtime().XGBClassifier(
            n_estimators=160,
            max_depth=3,
            learning_rate=0.05,
            subsample=0.85,
            colsample_bytree=0.85,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=42,
            n_jobs=1,
        )
        model.fit(rows, labels, verbose=False)
        return model

    def predict_many(self, model, rows):
        return [float(value) for value in model.predict_proba(rows)[:, 1]]

    def predict_one(self, model, features):
        return self.predict_many(model, [features])[0]

    def save(self, model, artifact_path):
        Path(artifact_path).parent.mkdir(parents=True, exist_ok=True)
        model.save_model(artifact_path)

    def load(self, artifact_path):
        model = self._runtime().XGBClassifier()
        model.load_model(artifact_path)
        return model

    def explain(self, model, features):
        booster = model.get_booster()
        scores = booster.get_score(importance_type="gain")
        top = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:2]
        return [f"{name} is influential" for name, _ in top] or ["model probability threshold"]
