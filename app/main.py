"""Desperado coach service: settings and the web app.

Run locally: python -m uvicorn app.main:app --reload --port 8000
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from functools import lru_cache

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.desperado import DesperadoClient, SIGNATURE_HEADER, TIMESTAMP_HEADER, verify
from app.jobs import JobStore, Worker
from app.models import WebhookPayload
from app.report import make_report_builder


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    coach_secret: str                          # shared with Desperado (COACH_SECRET)
    desperado_url: str = "http://localhost:8080"
    database_path: str = "data/coach.db"
    max_skew_seconds: int = 300                # reject signed requests older than 5 minutes

    stockfish_path: str = "stockfish"         # path to the engine binary (STOCKFISH_PATH)
    analysis_depth: int = 10                   # engine search depth per position

    worker_poll_seconds: float = 1.0
    job_max_attempts: int = 3                  # analysis attempts before giving up
    callback_max_attempts: int = 5             # delivery attempts back to Desperado


@lru_cache
def get_settings() -> Settings:
    return Settings()


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("coach")


def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = JobStore(settings.database_path)
        http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        desperado = DesperadoClient(settings.desperado_url, settings.coach_secret, http,
                                    max_attempts=settings.callback_max_attempts)
        build = make_report_builder(settings.stockfish_path, settings.analysis_depth)
        worker = Worker(store, desperado, build, max_attempts=settings.job_max_attempts,
                        poll_seconds=settings.worker_poll_seconds)
        app.state.store, app.state.worker = store, worker

        task = asyncio.create_task(worker.run_forever()) if start_worker else None
        try:
            yield
        finally:
            worker.stop()
            if task:
                await task
            await http.aclose()
            store.close()

    app = FastAPI(title="Desperado coach", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "UP"}

    @app.post("/webhooks/desperado", status_code=202)
    async def receive_webhook(request: Request) -> Response:
        body = await request.body()   # raw bytes: the signature covers exactly these
        if not verify(settings.coach_secret, request.headers.get(TIMESTAMP_HEADER),
                      request.headers.get(SIGNATURE_HEADER), body, settings.max_skew_seconds):
            raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            raw = json.loads(body)
            payload = WebhookPayload.model_validate(raw)
        except (json.JSONDecodeError, ValidationError) as e:
            raise HTTPException(status_code=400, detail=f"Malformed payload: {e}") from e
        if payload.type != "match.finished":
            raise HTTPException(status_code=400, detail=f"Unsupported event type: {payload.type}")

        created = request.app.state.store.enqueue(
            payload.event_id, payload.match.room_code, payload.match.match_id, raw)
        log.info("Webhook %s for room %s: %s", payload.event_id, payload.match.room_code,
                 "queued" if created else "duplicate, ignored")
        # 202 either way: a duplicate delivery means Desperado retried; we already have the job.
        return Response(status_code=202)

    @app.get("/jobs/{event_id}")
    async def job_status(event_id: str, request: Request) -> dict:
        """Debugging aid: where a job is, and its report once done."""
        store: JobStore = request.app.state.store
        job = store.get(event_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        return {"eventId": job.event_id, "status": job.status.value, "attempts": job.attempts,
                "lastError": job.last_error, "report": store.get_report(event_id)}

    return app


app = create_app()
