import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Body, FastAPI, HTTPException, Query

from ai_service.config import (
    AI_DB_PATH,
    AI_EXECUTION_MODE,
    MAIN_DB_PATH,
    MODEL_DIR,
    SAMPLE_RETENTION_DAYS,
)
from ai_service.alpha_labels import AlphaStrategyLabeler
from ai_service.alpha_strategy_service import AlphaStrategyService
from ai_service.model import XGBoostBackend
from ai_service.outcomes import OutcomeLabeler
from ai_service.service import EntryQualityService, ModelUnavailable
from ai_service.storage import AIStore


logger = logging.getLogger("ai_service")


def create_app(
    service: EntryQualityService | None = None,
    *,
    labeler: OutcomeLabeler | None = None,
    alpha_strategy: AlphaStrategyService | None = None,
    alpha_labeler: AlphaStrategyLabeler | None = None,
    start_scheduler: bool = True,
) -> FastAPI:
    quality = service or EntryQualityService(AIStore(AI_DB_PATH), XGBoostBackend(), model_dir=MODEL_DIR)
    alpha = alpha_strategy or AlphaStrategyService(
        quality.store,
        execution_mode=AI_EXECUTION_MODE,
    )
    outcomes = labeler or OutcomeLabeler(
        quality.store, MAIN_DB_PATH, enable_backfill=True,
    )
    alpha_outcomes = alpha_labeler or AlphaStrategyLabeler(
        quality.store,
        MAIN_DB_PATH,
    )
    maintenance = {"last_label": None, "last_label_result": None, "last_train": None, "last_error": None}

    async def maintenance_loop():
        last_train_day = None
        while True:
            try:
                counterfactual_count = await asyncio.to_thread(
                    quality.store.backfill_alpha_trigger_samples,
                    limit=5000,
                )
                label_result = await asyncio.to_thread(outcomes.label_pending)
                alpha_label_result = await asyncio.to_thread(
                    alpha_outcomes.label_pending,
                    limit=2000,
                )
                alpha_execution_result = await asyncio.to_thread(
                    alpha_outcomes.sync_execution_outcomes
                )
                now = datetime.now(timezone.utc)
                maintenance["last_label"] = now.isoformat().replace("+00:00", "Z")
                maintenance["last_label_result"] = label_result
                maintenance["last_alpha_label_result"] = alpha_label_result
                maintenance["last_alpha_counterfactual_backfill"] = {
                    "created": counterfactual_count,
                }
                maintenance["last_alpha_execution_result"] = (
                    alpha_execution_result
                )
                if last_train_day != now.date():
                    train_result = await asyncio.to_thread(
                        lambda: {key: quality.train(key) for key in ("alpha", "normal")}
                    )
                    maintenance["last_train"] = now.isoformat().replace("+00:00", "Z")
                    maintenance["last_train_result"] = train_result
                    maintenance["last_alpha_train_result"] = await asyncio.to_thread(
                        lambda: [
                            alpha.train(
                                market_env=env,
                                stage=stage,
                                target=target,
                            )
                            for env in ("testnet", "mainnet")
                            for stage, target in (
                                ("setup", "setup_success"),
                                ("trigger", "followthrough"),
                                ("trigger", "fakeout"),
                            )
                        ]
                    )
                    maintenance["cleanup_result"] = await asyncio.to_thread(
                        quality.store.cleanup,
                        (now - timedelta(days=SAMPLE_RETENTION_DAYS)).isoformat().replace("+00:00", "Z"),
                    )
                    last_train_day = now.date()
                maintenance["last_error"] = None
            except Exception as exc:
                maintenance["last_error"] = str(exc)
                logger.exception("AI maintenance failed")
            await asyncio.sleep(3600)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(maintenance_loop()) if start_scheduler else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="DarkHorse AI Entry Quality", version="1.0", lifespan=lifespan)
    app.state.quality_service = quality
    app.state.alpha_strategy_service = alpha
    app.state.alpha_strategy_labeler = alpha_outcomes
    app.state.outcome_labeler = outcomes
    app.state.maintenance = maintenance

    @app.get("/v1/status")
    def status():
        return {
            **quality.status(),
            "alpha_strategy_v2": alpha.status(),
            "maintenance": maintenance,
        }

    @app.get("/v1/entry-quality/status")
    def entry_quality_status():
        return {
            **quality.status(),
            "maintenance": maintenance,
        }

    @app.post("/v2/alpha-strategy/evaluate")
    def evaluate_alpha_strategy(payload: dict = Body(...)):
        try:
            return alpha.evaluate(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v2/alpha-strategy/observe")
    def observe_alpha_strategy(payload: dict = Body(...)):
        try:
            return alpha.observe_many(payload.get("candidates") or [])
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v2/alpha-strategy/status")
    def alpha_strategy_status():
        return alpha.status()

    @app.post("/v2/alpha-strategy/outcomes/label")
    def label_alpha_strategy(payload: dict = Body(default={})):
        labels = alpha_outcomes.label_pending(
            market_env=payload.get("market_env"),
            limit=int(payload.get("limit") or 1000),
        )
        return {
            **labels,
            "execution_outcomes": alpha_outcomes.sync_execution_outcomes(
                limit=int(payload.get("execution_limit") or 2000),
            ),
        }

    @app.post("/v2/alpha-strategy/models/train")
    def train_alpha_strategy(payload: dict = Body(...)):
        try:
            return alpha.train(
                market_env=payload["market_env"],
                stage=payload["stage"],
                target=payload["target"],
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v2/alpha-strategy/models/promote")
    def promote_alpha_strategy(payload: dict = Body(...)):
        if not alpha.promote(str(payload.get("version") or "")):
            raise HTTPException(status_code=404, detail="model version not found")
        return {"status": "promoted", "version": payload["version"]}

    @app.post("/v2/alpha-strategy/models/rollback")
    def rollback_alpha_strategy(payload: dict = Body(...)):
        version = alpha.rollback(
            model_key=str(payload.get("model_key") or ""),
            target=str(payload.get("target") or ""),
        )
        if not version:
            raise HTTPException(status_code=404, detail="rollback model not found")
        return {"status": "rolled_back", "version": version}

    @app.post("/v1/entry-quality/evaluate")
    def evaluate(payload: dict = Body(...)):
        try:
            return quality.evaluate(payload)
        except ModelUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/v1/entry-quality/observe")
    def observe(payload: dict = Body(...)):
        return quality.observe_many(payload.get("candidates") or [])

    @app.post("/v1/models/train")
    def train(payload: dict = Body(default={})):
        key = payload.get("model_key")
        if key:
            return quality.train(str(key))
        return {model_key: quality.train(model_key) for model_key in ("alpha", "normal")}

    @app.post("/v1/outcomes/label")
    def label_outcomes():
        result = outcomes.label_pending()
        maintenance["last_label"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        maintenance["last_label_result"] = result
        return result

    @app.get("/v1/decisions")
    def decisions(limit: int = Query(default=100, ge=1, le=1000)):
        return {"decisions": quality.store.list_decisions(limit)}

    return app


app = create_app()
