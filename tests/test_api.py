"""The webhook endpoint and request signing.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.desperado import sign, verify, SIGNATURE_HEADER, TIMESTAMP_HEADER
from app.main import Settings, create_app


SECRET = "test-secret"
BODY = b'{"eventId":"m-1"}'
NOW = 1794729600


def test_known_vector_matches_java():
    # The same inputs are asserted in Desperado's WebhookSignerTest, so both sides sign identically.
    assert sign(SECRET, NOW, BODY) == "sha256=5f76a60601886613d20670f190b62d59ba00c0df45bb80d31d31c760dd45aa18"


def test_valid_signature_is_accepted():
    assert verify(SECRET, str(NOW), sign(SECRET, NOW, BODY), BODY, max_skew_seconds=300, now=NOW)


def test_changed_body_is_rejected():
    assert not verify(SECRET, str(NOW), sign(SECRET, NOW, BODY), b'{"eventId":"m-2"}', 300, now=NOW)


def test_wrong_secret_is_rejected():
    assert not verify(SECRET, str(NOW), sign("other", NOW, BODY), BODY, 300, now=NOW)


def test_old_timestamp_is_rejected():
    old = NOW - 600
    assert not verify(SECRET, str(old), sign(SECRET, old, BODY), BODY, 300, now=NOW)


def test_missing_or_malformed_headers_are_rejected():
    good = sign(SECRET, NOW, BODY)
    assert not verify(SECRET, None, good, BODY, 300, now=NOW)
    assert not verify(SECRET, str(NOW), None, BODY, 300, now=NOW)
    assert not verify(SECRET, "soon", good, BODY, 300, now=NOW)




@pytest.fixture
def client(tmp_path):
    settings = Settings(coach_secret=SECRET, database_path=str(tmp_path / "coach.db"))
    with TestClient(create_app(settings, start_worker=False)) as c:
        yield c


def signed_headers(body: bytes, timestamp: int | None = None) -> dict:
    ts = timestamp or int(time.time())
    return {TIMESTAMP_HEADER: str(ts), SIGNATURE_HEADER: sign(SECRET, ts, body),
            "Content-Type": "application/json"}


def test_health(client):
    assert client.get("/health").json() == {"status": "UP"}


def test_signed_webhook_is_accepted_and_queued(client, sample_payload):
    body = json.dumps(sample_payload).encode()

    response = client.post("/webhooks/desperado", content=body, headers=signed_headers(body))

    assert response.status_code == 202
    job = client.get("/jobs/match-sample-1").json()
    assert job["status"] == "QUEUED"


def test_duplicate_delivery_is_accepted_but_queued_once(client, sample_payload):
    body = json.dumps(sample_payload).encode()
    client.post("/webhooks/desperado", content=body, headers=signed_headers(body))

    again = client.post("/webhooks/desperado", content=body, headers=signed_headers(body))

    assert again.status_code == 202
    assert client.get("/jobs/match-sample-1").json()["attempts"] == 0


def test_bad_signature_is_rejected(client, sample_payload):
    body = json.dumps(sample_payload).encode()
    headers = signed_headers(body)
    headers[SIGNATURE_HEADER] = "sha256=" + "0" * 64

    assert client.post("/webhooks/desperado", content=body, headers=headers).status_code == 401
    assert client.get("/jobs/match-sample-1").status_code == 404


def test_body_changed_after_signing_is_rejected(client, sample_payload):
    body = json.dumps(sample_payload).encode()
    headers = signed_headers(body)
    tampered = body.replace(b'"score": 16', b'"score": 99')

    assert client.post("/webhooks/desperado", content=tampered, headers=headers).status_code == 401


def test_malformed_payload_is_rejected_with_400(client):
    body = b'{"eventId": "x", "type": "match.finished"}'   # no match
    assert client.post("/webhooks/desperado", content=body, headers=signed_headers(body)).status_code == 400
