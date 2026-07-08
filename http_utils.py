"""
Shared outbound-HTTP resilience layer — Phase 2.

Used by github_service.py, telegram_service.py, and ai_service.py so all
three external integrations (GitHub, Telegram, Groq) get the same
transient-failure handling instead of three different ad hoc approaches
(one of which — analyze_commit's Groq retry loop — existed; the other
outbound calls in the codebase had none).

Design intent, read this before changing retry_statuses:
- request_with_retry() returns the httpx.Response as-is for any status
  NOT in `retry_statuses`, even 4xx/404/409/422. Callers keep doing their
  own `raise_for_status()` / manual status-code branching exactly as
  before — this layer only adds resilience for genuinely transient
  failures, it never changes business-logic behavior for a real error.
- Only 429 and 5xx are retried, plus connection-level errors
  (httpx.RequestError: DNS failure, connection reset, timeout). A 404 or
  401 will never be retried — retrying a permanent error just delays the
  inevitable failure and wastes the caller's time budget.
- Retry-After is honored when present (both header form, used by GitHub/
  Groq, and Telegram's body-embedded `parameters.retry_after`) instead of
  blindly guessing a backoff — respecting the server's stated wait time
  is what keeps you from getting harder-throttled.
- Backoff is exponential with jitter, capped at max_delay, so many
  concurrent commits retrying at once don't all retry in lockstep and
  hammer the API in a synchronized burst (thundering herd).
"""

import asyncio
import logging
import random
from typing import Any, Optional

import httpx

logger = logging.getLogger("commit_guardian.http")

# Sensible shared defaults for a single-instance, few-hundred-user deployment.
# Explicit per-phase timeouts instead of one blanket float: a slow Groq
# completion should get a long read timeout, but a hung TCP connect
# attempt should fail fast rather than tying up a pool slot.
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=15.0, pool=10.0)
LONG_READ_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=15.0, pool=10.0)

# One process talking to a handful of external hosts (GitHub, Groq,
# Telegram) — generous keep-alive pool avoids repeated TLS handshakes
# under concurrent commit processing without over-provisioning sockets.
DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30.0)

_DEFAULT_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _parse_retry_after_header(response: Optional[httpx.Response]) -> Optional[float]:
    if response is None:
        return None
    val = response.headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except ValueError:
        return None


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_attempts: int = 3,
    retry_statuses: frozenset = _DEFAULT_RETRY_STATUSES,
    base_delay: float = 1.0,
    max_delay: float = 20.0,
    retry_after_getter: Optional[Any] = None,
    **kwargs,
) -> httpx.Response:
    """
    Drop-in replacement for `await client.request(method, url, **kwargs)`
    with retry on transient failures.

    retry_after_getter: optional `Callable[[httpx.Response], Optional[float]]`
    for APIs (like Telegram) that put their retry hint in the JSON body
    instead of a Retry-After header. Falls back to the header if unset or
    returns None.

    Raises httpx.RequestError if every attempt fails at the connection
    level. Otherwise always returns a Response — including one whose
    status is still in retry_statuses after the final attempt — so
    callers keep full control of what counts as an error for them.
    """
    last_exc: Optional[httpx.RequestError] = None
    response: Optional[httpx.Response] = None

    for attempt in range(max_attempts):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.RequestError as exc:
            last_exc = exc
            if attempt == max_attempts - 1:
                logger.warning("%s %s failed after %d attempts: %s", method, url, max_attempts, exc)
                raise
            delay = min(max_delay, base_delay * (2 ** attempt)) + random.uniform(0, 0.5)
            logger.info(
                "%s %s network error (attempt %d/%d) — retrying in %.1fs: %s",
                method, url, attempt + 1, max_attempts, delay, exc,
            )
            await asyncio.sleep(delay)
            continue

        if response.status_code not in retry_statuses:
            return response

        if attempt == max_attempts - 1:
            return response  # exhausted — hand the caller the last (still-bad) response

        wait: Optional[float] = None
        if retry_after_getter:
            try:
                wait = retry_after_getter(response)
            except Exception:
                wait = None
        if wait is None:
            wait = _parse_retry_after_header(response)
        if wait is None:
            wait = min(max_delay, base_delay * (2 ** attempt))
        wait = min(wait, max_delay) + random.uniform(0, 0.5)

        logger.info(
            "%s %s got %d (attempt %d/%d) — retrying in %.1fs",
            method, url, response.status_code, attempt + 1, max_attempts, wait,
        )
        await asyncio.sleep(wait)

    # Unreachable in practice (loop always returns/raises), but keeps
    # type checkers happy and fails safe instead of returning None.
    if response is not None:
        return response
    assert last_exc is not None
    raise last_exc
