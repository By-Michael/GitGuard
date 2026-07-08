"""
Crash-safe background job worker for Commit Guardian — Phase 3.

Problem this replaces:
- Commits used to be processed via `asyncio.create_task(process_commit(...))`
  fired directly from the webhook handler. If the process died mid-flight
  (Render redeploy, OOM, any crash) that task just vanished — no error, no
  retry, no record — leaving the user staring at a "🔄 Analyzing…" message
  that would never resolve.

Fix:
- The webhook handler inserts a row into the `jobs` table (status='pending')
  instead of spawning a task directly (see database.create_job()).
- This module runs a small pool of worker coroutines *in the same process*
  (no Redis, no Celery, no new infra) that poll that table, atomically claim
  a job with `FOR UPDATE SKIP LOCKED` (database.claim_next_job()), and run
  the existing commit-analysis pipeline against it.
- Success -> 'done'. Failure -> retried up to CONFIG.job_max_attempts times,
  then 'failed' with the error message recorded on the row.
- On process restart, any job stuck in 'processing' from before the crash is
  reset to 'pending' by database.reset_stuck_jobs() (called once at startup,
  before workers start), so it resumes automatically instead of being lost.

Note: recovery re-runs the pipeline from the top (re-fetch commit, re-send
review card, etc.) rather than resuming mid-step — there's no per-step
checkpointing. That's an intentional simplification: it's idempotent enough
(worst case the user sees one extra "fetching…" flicker) and avoids a much
more complex step-log design for a single-instance, low-budget setup.
"""

import asyncio
import logging
import traceback
from typing import Any, Dict, List

import alerting
import database as db
from config import CONFIG
from exceptions import UnknownJobTypeError

logger = logging.getLogger("commit_guardian.jobs")

# Set while workers are running; cleared on shutdown so idle workers wake up
# and exit immediately instead of waiting out a full poll interval.
_stop_event: asyncio.Event = asyncio.Event()


def start_job_workers() -> List[asyncio.Task]:
    """Launch CONFIG.job_worker_count polling loops. Call once at app startup."""
    _stop_event.clear()
    tasks = [asyncio.create_task(_worker_loop(i)) for i in range(CONFIG.job_worker_count)]
    logger.info(
        "Started %d job worker(s), polling every %.0fs",
        CONFIG.job_worker_count, CONFIG.job_poll_interval_seconds,
    )
    return tasks


async def stop_job_workers(tasks: List[asyncio.Task], grace_seconds: float = 15.0) -> None:
    """
    Signal workers to stop picking up new jobs, give in-flight jobs a grace
    period to finish naturally, then cancel anything still running.

    It's safe to cut this short: any job still 'processing' when we cancel
    gets picked back up as 'pending' on the next startup by
    database.reset_stuck_jobs() — that's the whole point of this design.
    """
    _stop_event.set()
    if not tasks:
        return
    _done, pending = await asyncio.wait(tasks, timeout=grace_seconds)
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.wait(pending, timeout=5)


async def _worker_loop(worker_id: int) -> None:
    while not _stop_event.is_set():
        try:
            job = await db.claim_next_job()
        except Exception as exc:
            logger.error("Worker %d: error claiming job: %s", worker_id, exc, exc_info=True)
            job = None

        if job is None:
            try:
                await asyncio.wait_for(_stop_event.wait(), timeout=CONFIG.job_poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
            continue

        await _run_job(worker_id, job)


async def _run_job(worker_id: int, job: Dict[str, Any]) -> None:
    job_id   = job["id"]
    job_type = job["job_type"]
    payload  = job["payload"]
    attempts = job["attempts"]

    logger.info("Worker %d: running job %d (%s), attempt %d", worker_id, job_id, job_type, attempts)

    try:
        if job_type == "process_commit":
            await _run_process_commit(payload)
        else:
            raise UnknownJobTypeError(f"Unknown job_type: {job_type!r}")
    except Exception as exc:
        error_msg = str(exc)
        logger.error(
            "Worker %d: job %d (%s) failed on attempt %d/%d: %s\n%s",
            worker_id, job_id, job_type, attempts, CONFIG.job_max_attempts, exc,
            traceback.format_exc(),
        )
        # Phase 4: every job failure counts toward the "jobs" category (plus
        # the exception's own category if it's a GitGuardError, e.g. "ai" or
        # "github") — a burst here usually means an upstream dependency is
        # down, not that this one commit was unusual.
        await alerting.record_failure(exc, detail=f"job {job_id} ({job_type}): {error_msg}")
        await alerting.record_failure("jobs", detail=f"job {job_id} ({job_type}): {error_msg}")

        if attempts >= CONFIG.job_max_attempts:
            await db.mark_job_failed(job_id, error_msg)
            await _notify_job_permanently_failed(job_type, payload, error_msg)
        else:
            await db.requeue_job(job_id, error_msg)
    else:
        await db.mark_job_done(job_id)
        logger.info("Worker %d: job %d (%s) done", worker_id, job_id, job_type)


async def _run_process_commit(payload: Dict[str, Any]) -> None:
    # Lazy import: webhook_server imports this module (to start/stop the
    # workers), so importing webhook_server back at module load time here
    # would be circular. Importing inside the function defers it until
    # webhook_server has finished loading.
    from webhook_server import _process_commit_inner

    user = await db.get_user(payload["chat_id"])
    if not user or not user.get("github_token"):
        raise RuntimeError(f"User {payload['chat_id']} has no github_token — cannot process commit")

    await _process_commit_inner(
        payload["chat_id"], user["github_token"],
        payload["owner"], payload["repo"], payload["commit_sha"],
        payload["pusher_name"], payload["pusher_email"], payload["branch"],
    )


async def _notify_job_permanently_failed(job_type: str, payload: Dict[str, Any], error_msg: str) -> None:
    """Best-effort user-facing notice once a job exhausts its retries."""
    if job_type != "process_commit":
        return
    try:
        from telegram_service import telegram_service
        sha = payload.get("commit_sha", "")[:7]
        await telegram_service.send_message(
            payload["chat_id"],
            f"⚠️ *Commit processing failed after {CONFIG.job_max_attempts} attempts*\n\n"
            f"`{sha}` in `{payload.get('owner')}/{payload.get('repo')}`\n"
            f"`{error_msg[:300]}`\n\n"
            f"The commit is still in your repo — please review it manually.",
        )
    except Exception as exc:
        logger.warning("Could not send job-failed notification: %s", exc)
