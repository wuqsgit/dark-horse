import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Body, FastAPI, HTTPException, Query

from ai_service.config import AI_DB_PATH, MAIN_DB_PATH, MODEL_DIR, SAMPLE_RETENTION_DAYS
from ai_service.model import XGBoostBackend
from ai_service.outcomes import OutcomeLabeler
from ai_service.service import EntryQualityService, ModelUnavailable
from ai_service.storage import AIStore


logger = logging.getLogger("ai_service")


def create_app(
    service: EntryQualityService | None = None,
    *,
    labeler: OutcomeLabeler | None = None,
    start_scheduler: bool = True,
) -> FastAPI:
    quality = service or EntryQualityService(AIStore(AI_DB_PATH), XGBoostBackend(), model_dir=MODEL_DIR)
    outcomes = labeler or OutcomeLabeler(quality.store, MAIN_DB_PATH)
    maintenance = {"last_label": None, "last_label_result": None, "last_train": None, "last_error": None}

    async def maintenance_loop():
        last_train_day = None
        while True:
            try:
                label_result = await asyncio.to_thread(outcomes.label_pending)
                now = datetime.now(timezone.utc)
                maintenance["last_label"] = now.isoformat().replace("+00:00", "Z")
                maintenance["last_label_result"] = label_result
                if last_train_day != now.date():
                    train_result = await asyncio.to_thread(
                        lambda: {key: quality.train(key) for key in ("alpha", "normal")}
                    )
                    maintenance["last_train"] = now.isoformat().replace("+00:00", "Z")
                    maintenance["last_train_result"] = train_result
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
    app.state.outcome_labeler = outcomes
    app.state.maintenance = maintenance

    @app.get("/v1/status")
    def status():
        return {**quality.status(), "maintenance": maintenance}

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
