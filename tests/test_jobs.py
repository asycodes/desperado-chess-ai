"""The job queue and the worker.
"""

import json
import time

import httpx

from app.desperado import DesperadoClient, SIGNATURE_HEADER, TIMESTAMP_HEADER, verify
from app.jobs import JobStatus, JobStore, Worker


def make_store() -> JobStore:
    return JobStore(":memory:")


def test_duplicate_event_is_ignored():
    store = make_store()
    assert store.enqueue("e1", "ABCD", "m1", {"x": 1}) is True
    assert store.enqueue("e1", "ABCD", "m1", {"x": 1}) is False


def test_claim_takes_oldest_ready_job_and_marks_it_running():
    store = make_store()
    store.enqueue("e1", "ABCD", "m1", {})
    store.enqueue("e2", "WXYZ", "m2", {})

    job = store.claim_next()
    assert job.event_id == "e1"
    assert job.status == JobStatus.RUNNING
    assert job.attempts == 1
    assert store.claim_next().event_id == "e2"
    assert store.claim_next() is None


def test_retry_later_hides_the_job_until_its_delay_passes():
    store = make_store()
    store.enqueue("e1", "ABCD", "m1", {})
    store.claim_next()
    store.retry_later("e1", "boom", delay_seconds=60)

    assert store.claim_next() is None
    assert store.claim_next(now=time.time() + 61).attempts == 2


def test_jobs_interrupted_by_a_restart_are_requeued():
    store = make_store()
    store.enqueue("e1", "ABCD", "m1", {})
    store.claim_next()                     # RUNNING when the "crash" happens

    assert store.requeue_interrupted() == 1
    assert store.get("e1").status == JobStatus.QUEUED


def test_completed_job_keeps_its_report():
    store = make_store()
    store.enqueue("e1", "ABCD", "m1", {})
    store.claim_next()
    store.complete("e1", {"awards": []})

    assert store.get("e1").status == JobStatus.DONE
    assert store.get_report("e1") == {"awards": []}


SECRET = "test-secret"


class FakeDesperado:
    """Records callbacks and answers with a chosen status code."""

    def __init__(self, status_code: int = 204):
        self.status_code = status_code
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests]


def standings_only(match):
    """A stand-in for the real pipeline: these tests are about queueing and delivery, not analysis."""
    ranked = sorted(match.players, key=lambda p: p.score, reverse=True)
    return {"matchId": match.match_id, "standings": [{"name": p.name, "score": p.score} for p in ranked]}


def make_worker(fake: FakeDesperado, build=standings_only, max_attempts: int = 3):
    store = JobStore(":memory:")
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    desperado = DesperadoClient("http://desperado.test", SECRET, http, max_attempts=2, first_delay_seconds=0)
    worker = Worker(store, desperado, build, max_attempts=max_attempts, retry_delay_seconds=0)
    return store, worker


async def test_job_is_processed_and_report_is_sent_back_signed(sample_payload):
    fake = FakeDesperado()
    store, worker = make_worker(fake)
    store.enqueue("match-sample-1", "HSGG", "match-sample-1", sample_payload)

    assert await worker.run_once() is True

    assert store.get("match-sample-1").status == JobStatus.DONE
    request = fake.requests[0]
    assert request.url.path == "/api/internal/coach/reports/HSGG"
    assert verify(SECRET, request.headers[TIMESTAMP_HEADER], request.headers[SIGNATURE_HEADER],
                  request.content, max_skew_seconds=300)
    body = fake.bodies()[0]
    assert body["matchId"] == "match-sample-1"
    assert body["status"] == "READY"
    assert body["report"]["standings"][0]["name"] == "Bob"


async def test_failing_analysis_is_retried_then_reported_as_failed(sample_payload):
    fake = FakeDesperado()

    def broken(_match):
        raise ValueError("engine crashed")

    store, worker = make_worker(fake, build=broken, max_attempts=2)
    store.enqueue("match-sample-1", "HSGG", "match-sample-1", sample_payload)

    await worker.run_once()                       # attempt 1 -> back in the queue
    assert store.get("match-sample-1").status == JobStatus.QUEUED
    assert fake.requests == []

    await worker.run_once()                       # attempt 2 -> gives up
    job = store.get("match-sample-1")
    assert job.status == JobStatus.FAILED
    assert "engine crashed" in job.last_error
    assert fake.bodies() == [{"matchId": "match-sample-1", "status": "FAILED", "report": None}]


async def test_stale_match_answer_from_desperado_does_not_crash_the_worker(sample_payload):
    fake = FakeDesperado(status_code=409)          # a rematch already finished
    store, worker = make_worker(fake)
    store.enqueue("match-sample-1", "HSGG", "match-sample-1", sample_payload)

    await worker.run_once()

    assert store.get("match-sample-1").status == JobStatus.DONE   # the work is kept
    assert len(fake.requests) == 1                                # 4xx: not retried


async def test_unreachable_desperado_is_retried_then_given_up(sample_payload):
    fake = FakeDesperado(status_code=503)
    store, worker = make_worker(fake)
    store.enqueue("match-sample-1", "HSGG", "match-sample-1", sample_payload)

    await worker.run_once()

    assert len(fake.requests) == 2                                # max_attempts=2 in make_worker
    assert store.get("match-sample-1").status == JobStatus.DONE
    assert store.get_report("match-sample-1") is not None


async def test_empty_queue_does_nothing():
    store, worker = make_worker(FakeDesperado())
    assert await worker.run_once() is False
