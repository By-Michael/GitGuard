"""
Admin alerting on repeated failures — Phase 4.

This is deliberately separate from the existing user-facing "your commit
processing failed" notice in job_worker.py: that tells ONE user about ONE
job. This module watches for a *pattern* across all users — e.g. the Groq
API is down, GitHub is rate-limiting the whole app, the DB pool is
unhealthy — and pings the operator (ADMIN_CHAT_ID) once that pattern is
clear, instead of the operator having to notice it in logs.

Design:
- In-memory sliding window per category (github/ai/telegram/database/jobs/
  internal — see exceptions.py). No new table: this is inherently
  best-effort/process-local, which is fine for a single-instance deploy
  (see job_worker.py's own single-instance assumption) and avoids adding
  DB load on the failure path, which is exactly when the DB might itself
  be the thing failing.
- `record_failure(exc_or_category, detail)` appends a timestamp; if the
  count within `alert_window_seconds` reaches `alert_failure_threshold`,
  it sends one Telegram message to admin_chat_id and enters a cooldown
  (`alert_cooldown_seconds`) so a sustained outage doesn't spam the admin
  chat once per failing request.
- Safe to call from anywhere, including inside an `except` block for an
  error that might itself be a Telegram failure — send failures here are
  swallowed and logged, never re-raised.
"""

import time
from collections import defaultdict
from typing import Deque, Dict, Optional
from collections import deque

from config import CONFIG, logger
from exceptions import GitGuardError

_failure_windows: Dict[str, Deque[float]] = defaultdict(deque)
_last_alert_sent: Dict[str, float] = {}

_warned_no_admin = False


def _category_of(exc_or_category) -> str:
    if isinstance(exc_or_category, str):
        return exc_or_category
    if isinstance(exc_or_category, GitGuardError):
        return exc_or_category.category
    return "internal"


async def record_failure(exc_or_category, detail: str = "") -> None:
    """
    Record a failure and, if it pushes the category over threshold within
    the configured window, send an admin alert (subject to cooldown).
    Never raises — alerting must not be able to break the request path it's
    observing.
    """
    global _warned_no_admin
    category = _category_of(exc_or_category)
    now = time.monotonic()
    window = _failure_windows[category]
    window.append(now)

    cutoff = now - CONFIG.alert_window_seconds
    while window and window[0] < cutoff:
        window.popleft()

    if len(window) < CONFIG.alert_failure_threshold:
        return

    last_sent = _last_alert_sent.get(category, 0.0)
    if now - last_sent < CONFIG.alert_cooldown_seconds:
        return  # already alerted recently for this category — avoid spam

    _last_alert_sent[category] = now

    if not CONFIG.admin_chat_id:
        if not _warned_no_admin:
            logger.warning(
                "ADMIN_CHAT_ID not set — %d '%s' failures in the last %.0fs would have "
                "paged an admin. Set ADMIN_CHAT_ID to enable alerting.",
                len(window), category, CONFIG.alert_window_seconds,
            )
            _warned_no_admin = True
        return

    try:
        # Lazy import: avoids a circular import at module load time
        # (telegram_service imports config, not alerting).
        from telegram_service import telegram_service
        await telegram_service.send_message(
            CONFIG.admin_chat_id,
            f"🚨 *GitGuard Admin Alert*\n\n"
            f"`{len(window)}` failures in category *{category}* within the last "
            f"`{int(CONFIG.alert_window_seconds)}s`.\n\n"
            f"Last error: `{(detail or 'no detail')[:300]}`\n\n"
            f"_This is a system health alert, not a single commit's review._",
        )
        logger.info("Admin alert sent for category '%s' (%d failures)", category, len(window))
    except Exception as exc:
        logger.error("Failed to deliver admin alert for category '%s': %s", category, exc)
