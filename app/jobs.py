"""The job queue and the worker that processes it.

Jobs live in a SQLite table, so a restart mid-analysis doesn't lose them. One worker is plenty for this
load; Redis or Celery would add a second service to deploy for no gain yet.

QUEUED -> RUNNING -> DONE, or back to QUEUED with a delay on failure, until FAILED.
"""

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.desperado import CallbackRejected, DesperadoClient
from app.models import MatchRecord, WebhookPayload


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Job:
    event_id: str
    room_code: str
    match_id: str
    payload: dict
    status: JobStatus
    attempts: int
    last_error: str | None


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    event_id      TEXT PRIMARY KEY,          -- = match id; makes duplicate webhooks harmless
    room_code     TEXT NOT NULL,
    match_id      TEXT NOT NULL,
    payload       TEXT NOT NULL,             -- the webhook body, as JSON
    status        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    run_after     REAL NOT NULL,             -- unix time; used for retry delays
    last_error    TEXT,
    report        TEXT,                      -- the finished report, as JSON
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs (status, run_after);
"""


class JobStore:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection shared by the API and the worker, both on the same event-loop thread.
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def enqueue(self, event_id: str, room_code: str, match_id: str, payload: dict) -> bool:
        """Adds a job. Returns False if this event was already received (a duplicate delivery)."""
        now = time.time()
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO jobs (event_id, room_code, match_id, payload, status, run_after, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, room_code, match_id, json.dumps(payload), JobStatus.QUEUED.value, now, now, now),
        )
        return cursor.rowcount == 1

    def claim_next(self, now: float | None = None) -> Job | None:
        """Atomically moves the oldest ready job to RUNNING and returns it."""
        now = now if now is not None else time.time()
        row = self._db.execute(
            "UPDATE jobs SET status = ?, attempts = attempts + 1, updated_at = ? "
            "WHERE event_id = (SELECT event_id FROM jobs WHERE status = ? AND run_after <= ? "
            "                  ORDER BY created_at LIMIT 1) "
            "RETURNING *",
            (JobStatus.RUNNING.value, now, JobStatus.QUEUED.value, now),
        ).fetchone()
        return _to_job(row) if row else None

    def complete(self, event_id: str, report: dict) -> None:
        self._set(event_id, JobStatus.DONE, report=json.dumps(report))

    def retry_later(self, event_id: str, error: str, delay_seconds: float) -> None:
        self._db.execute(
            "UPDATE jobs SET status = ?, last_error = ?, run_after = ?, updated_at = ? WHERE event_id = ?",
            (JobStatus.QUEUED.value, error, time.time() + delay_seconds, time.time(), event_id),
        )

    def fail(self, event_id: str, error: str) -> None:
        self._set(event_id, JobStatus.FAILED, last_error=error)

    def requeue_interrupted(self) -> int:
        """At startup: jobs left RUNNING by a crash go back to the queue."""
        cursor = self._db.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE status = ?",
            (JobStatus.QUEUED.value, time.time(), JobStatus.RUNNING.value),
        )
        return cursor.rowcount

    def get(self, event_id: str) -> Job | None:
        row = self._db.execute("SELECT * FROM jobs WHERE event_id = ?", (event_id,)).fetchone()
        return _to_job(row) if row else None

    def get_report(self, event_id: str) -> dict | None:
        row = self._db.execute("SELECT report FROM jobs WHERE event_id = ?", (event_id,)).fetchone()
        return json.loads(row["report"]) if row and row["report"] else None

    def close(self) -> None:
        self._db.close()

    def _set(self, event_id: str, status: JobStatus, *, report: str | None = None,
             last_error: str | None = None) -> None:
        self._db.execute(
            "UPDATE jobs SET status = ?, report = COALESCE(?, report), last_error = COALESCE(?, last_error), "
            "updated_at = ? WHERE event_id = ?",
            (status.value, report, last_error, time.time(), event_id),
        )


def _to_job(row: sqlite3.Row) -> Job:
    return Job(
        event_id=row["event_id"],
        room_code=row["room_code"],
        match_id=row["match_id"],
        payload=json.loads(row["payload"]),
        status=JobStatus(row["status"]),
        attempts=row["attempts"],
        last_error=row["last_error"],
    )


log = logging.getLogger(__name__)

ReportBuilder = Callable[[MatchRecord], dict]


class Worker:
    def __init__(self, store: JobStore, desperado: DesperadoClient, build: ReportBuilder, *,
                 max_attempts: int = 3, poll_seconds: float = 1.0, retry_delay_seconds: float = 10.0):
        self._store = store
        self._desperado = desperado
        self._max_attempts = max_attempts
        self._poll_seconds = poll_seconds
        self._retry_delay = retry_delay_seconds
        self._build = build
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        requeued = self._store.requeue_interrupted()
        if requeued:
            log.info("Re-queued %d job(s) interrupted by a restart", requeued)
        while not self._stopping.is_set():
            worked = await self.run_once()
            if not worked:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_seconds)
                except TimeoutError:
                    pass

    def stop(self) -> None:
        self._stopping.set()

    async def run_once(self) -> bool:
        """Processes one ready job, if any. Returns True if it did some work."""
        job = self._store.claim_next()
        if job is None:
            return False
        await self._process(job)
        return True

    async def _process(self, job: Job) -> None:
        # 1. Build the report. Analysis errors are retried a few times, then reported as FAILED.
        try:
            match = WebhookPayload.model_validate(job.payload).match
            # Analysis is CPU-bound (Stockfish, later): run it off the event loop so the API stays responsive.
            report = await asyncio.to_thread(self._build, match)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            if job.attempts < self._max_attempts:
                log.warning("Job %s attempt %d failed (%s); retrying", job.event_id, job.attempts, error)
                self._store.retry_later(job.event_id, error, self._retry_delay)
            else:
                log.error("Job %s failed permanently: %s", job.event_id, error)
                self._store.fail(job.event_id, error)
                await self._notify_failure(job)
            return

        self._store.complete(job.event_id, report)

        # 2. Deliver it. The report is already saved, so a delivery problem never loses the work.
        try:
            await self._desperado.send_report(job.room_code, job.match_id, report)
        except CallbackRejected as e:
            log.warning("Desperado rejected the report for %s: %s", job.event_id, e)  # e.g. 409 stale match
        except Exception as e:
            log.error("Could not deliver the report for %s: %s", job.event_id, e)

    async def _notify_failure(self, job: Job) -> None:
        try:
            await self._desperado.send_failure(job.room_code, job.match_id)
        except Exception as e:
            log.error("Could not tell Desperado that %s failed: %s", job.event_id, e)
