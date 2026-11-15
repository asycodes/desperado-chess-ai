"""Talking to Desperado: request signatures (both directions) and sending reports back.

signature = "sha256=" + hex(HMAC-SHA256(secret, f"{timestamp}.{raw_body}")). The raw bytes are signed,
so always verify against the exact bytes received.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time

import httpx


TIMESTAMP_HEADER = "X-Desperado-Timestamp"
SIGNATURE_HEADER = "X-Desperado-Signature"
EVENT_ID_HEADER = "X-Desperado-Event-Id"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    message = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(secret: str, timestamp_header: str | None, signature_header: str | None,
           body: bytes, max_skew_seconds: int, now: float | None = None) -> bool:
    if not timestamp_header or not signature_header:
        return False
    try:
        timestamp = int(timestamp_header)
    except ValueError:
        return False
    current = int(now if now is not None else time.time())
    if abs(current - timestamp) > max_skew_seconds:
        return False
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected, signature_header)  # constant time


log = logging.getLogger(__name__)


class CallbackRejected(Exception):
    """Desperado answered 4xx: retrying won't help (bad signature, stale match, malformed)."""


class DesperadoClient:
    def __init__(self, base_url: str, secret: str, http: httpx.AsyncClient, max_attempts: int = 5,
                 first_delay_seconds: float = 2.0):
        self._base_url = base_url.rstrip("/")
        self._secret = secret
        self._http = http
        self._max_attempts = max_attempts
        self._first_delay = first_delay_seconds

    async def send_report(self, room_code: str, match_id: str, report: dict) -> None:
        await self._post(room_code, {"matchId": match_id, "status": "READY", "report": report})

    async def send_failure(self, room_code: str, match_id: str) -> None:
        await self._post(room_code, {"matchId": match_id, "status": "FAILED", "report": None})

    async def _post(self, room_code: str, body_obj: dict) -> None:
        # Serialise once and send exactly these bytes: the signature covers them.
        body = json.dumps(body_obj, separators=(",", ":")).encode()
        url = f"{self._base_url}/api/internal/coach/reports/{room_code}"

        for attempt in range(1, self._max_attempts + 1):
            timestamp = int(time.time())   # re-signed per attempt so it never goes stale
            try:
                response = await self._http.post(url, content=body, headers={
                    "Content-Type": "application/json",
                    TIMESTAMP_HEADER: str(timestamp),
                    SIGNATURE_HEADER: sign(self._secret, timestamp, body),
                })
            except httpx.TransportError as e:   # refused, timeout, DNS: Desperado may be asleep
                error = f"{type(e).__name__}: {e}"
            else:
                if response.status_code < 300:
                    log.info("Callback for %s delivered (attempt %d)", room_code, attempt)
                    return
                if 400 <= response.status_code < 500:
                    raise CallbackRejected(f"Desperado answered {response.status_code} for {room_code}")
                error = f"HTTP {response.status_code}"

            if attempt == self._max_attempts:
                raise RuntimeError(f"Callback for {room_code} failed after {attempt} attempts: {error}")
            delay = self._first_delay * 2 ** (attempt - 1)
            log.warning("Callback for %s attempt %d failed (%s); retrying in %.0f s", room_code, attempt, error, delay)
            await asyncio.sleep(delay)
