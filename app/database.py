"""
PostgreSQL database layer for Commit Guardian (multi-user) — Phase 1 rewrite.

Why this replaced the SQLite version:
- Render's web service filesystem is ephemeral. A SQLite file on disk is
  wiped on every redeploy (and isn't shared if you ever run >1 instance).
  Postgres gives durable storage that survives deploys/restarts.
- Uses asyncpg with a connection pool instead of a blocking sqlite3 call
  on the event loop. All public functions are now `async def` — every
  call site was already inside an `async def`, so callers just add `await`.

Design choices carried over on purpose (don't second-guess these without
reason):
- upsert_user() still uses a strict column whitelist (_ALLOWED_USER_COLUMNS)
  to prevent SQL injection via field names — column names are never
  interpolated from caller-controlled data.
- get_timed_out_reviews() still excludes users with timeout_hours=0.
- resolve_review() is still idempotent (safe to call twice).
- created_at/updated_at are kept as TEXT in 'YYYY-MM-DD HH:MI:SS' (UTC)
  format — identical to what SQLite's datetime('now') produced — instead
  of switching to native TIMESTAMPTZ. This is deliberate: several call
  sites elsewhere in the codebase do string slicing (`created_at[:10]`)
  and ISO parsing on this value. Changing the wire format would ripple
  through webhook_server.py and telegram_service.py for no functional
  gain in Phase 1. Revisit as a dedicated cleanup later if you want
  native timestamp types.
"""

import asyncio
import json
import logging
import secrets
from typing import Any, Dict, List, Optional

import asyncpg

from .config import CONFIG
from .exceptions import DatabaseConnectionError

logger = logging.getLogger("commit_guardian.db")


class _CLEAR:
    """Sentinel value: pass CLEAR as a field value to set it to NULL in the DB."""
    pass


CLEAR = _CLEAR()

# Strict whitelist of columns that callers are allowed to update.
# Any key not in this set is silently dropped — never interpolated into SQL.
_ALLOWED_USER_COLUMNS = frozenset({
    "github_token", "webhook_secret", "owner", "repo", "branch",
    "timeout_hours", "timeout_action", "onboard_step",
    "token_alert_sent_at",   # ISO timestamp of last "token no longer works" alert
})

_pool: Optional[asyncpg.Pool] = None


def _require_pool() -> asyncpg.Pool:
    if _pool is None:
        raise DatabaseConnectionError(
            "Database pool not initialised — call database.init_db() during app startup."
        )
    return _pool


# ── Lifecycle ────────────────────────────────────────────────────────────────

async def connect() -> None:
    """Create the connection pool. Idempotent — safe to call more than once."""
    global _pool
    if _pool is not None:
        return
    logger.info("Connecting to Postgres (pool size %d-%d)…", CONFIG.db_pool_min, CONFIG.db_pool_max)
    try:
        _pool = await asyncpg.create_pool(
            dsn=CONFIG.database_url,
            min_size=CONFIG.db_pool_min,
            max_size=CONFIG.db_pool_max,
            command_timeout=CONFIG.db_command_timeout_seconds,
            # Fails fast on a dead/unreachable DB at startup instead of hanging
            # the whole app forever waiting for a connection.
            timeout=CONFIG.db_connect_timeout_seconds,
            # REQUIRED when the DB sits behind PgBouncer (or any pooler) in
            # transaction/statement pooling mode — which is the default on
            # Render's managed Postgres. Server-side prepared statements are
            # bound to one physical connection; PgBouncer can silently swap
            # the physical connection between queries in the same asyncpg
            # session, causing InvalidSQLStatementNameError. Setting the
            # cache size to 0 makes asyncpg use unnamed statements instead.
            statement_cache_size=0,
        )
    except (asyncpg.PostgresError, OSError, asyncio.TimeoutError) as exc:
        raise DatabaseConnectionError(f"Could not connect to Postgres: {exc}") from exc
    logger.info("Postgres pool ready")


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Postgres pool closed")


async def init_db() -> None:
    """Create the connection pool (if needed) and ensure the schema exists."""
    await connect()
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    chat_id              TEXT PRIMARY KEY,
                    github_token         TEXT,
                    webhook_secret       TEXT NOT NULL,
                    owner                TEXT,
                    repo                 TEXT,
                    branch               TEXT DEFAULT 'main',
                    timeout_hours        INTEGER DEFAULT 24,
                    timeout_action       TEXT DEFAULT 'accept',
                    onboard_step         TEXT DEFAULT 'await_repo',
                    token_alert_sent_at  TEXT DEFAULT NULL,
                    created_at           TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'),
                    updated_at           TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
                );

                -- commit_sha alone is not a safe primary key in a multi-user system:
                -- two chat_ids can watch the same repo and get identical commit SHAs.
                -- Composite PK keeps each user's copy of a shared commit independent.
                CREATE TABLE IF NOT EXISTS active_reviews (
                    commit_sha        TEXT NOT NULL,
                    chat_id           TEXT NOT NULL,
                    owner             TEXT NOT NULL,
                    repo              TEXT NOT NULL,
                    branch            TEXT NOT NULL,
                    message_id        BIGINT,
                    status            TEXT DEFAULT 'pending',
                    decision_json     TEXT,
                    commit_meta_json  TEXT,
                    created_at        TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'),
                    resolved          INTEGER DEFAULT 0,
                    timed_out         INTEGER DEFAULT 0,
                    PRIMARY KEY (commit_sha, chat_id),
                    FOREIGN KEY (chat_id) REFERENCES users(chat_id)
                );

                CREATE INDEX IF NOT EXISTS idx_reviews_pending
                    ON active_reviews(resolved, timed_out, status, chat_id);

                CREATE INDEX IF NOT EXISTS idx_reviews_chat
                    ON active_reviews(chat_id, resolved);

                -- Atomic dedup gate prevents duplicate reviews on webhook retry.
                -- Only the coroutine whose INSERT actually adds a row gets to queue
                -- the background task; subsequent inserts for the same SHA are no-ops.
                CREATE TABLE IF NOT EXISTS queued_shas (
                    sha        TEXT NOT NULL,
                    chat_id    TEXT NOT NULL,
                    queued_at  TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'),
                    PRIMARY KEY (sha, chat_id)
                );

                -- Phase 3: crash-safe background jobs. Replaces bare
                -- asyncio.create_task() calls for commit analysis — a row
                -- here survives a process crash/restart, whereas an in-memory
                -- task just vanishes. See job_worker.py for the poll loop and
                -- reset_stuck_jobs() below for crash recovery at startup.
                CREATE TABLE IF NOT EXISTS jobs (
                    id          SERIAL PRIMARY KEY,
                    job_type    TEXT NOT NULL,
                    payload     JSONB NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'pending',
                    attempts    INTEGER NOT NULL DEFAULT 0,
                    last_error  TEXT,
                    created_at  TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS'),
                    updated_at  TEXT DEFAULT to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                    ON jobs(status, created_at);
                """
            )
            # Additive-column migration guard, same pattern as the old SQLite
            # version: safe to run on every startup, no-op once applied.
            existing_cols = {
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
                )
            }
            if "token_alert_sent_at" not in existing_cols:
                await conn.execute("ALTER TABLE users ADD COLUMN token_alert_sent_at TEXT DEFAULT NULL")
    logger.info("Schema ready")


# ── Token-alert cooldown ─────────────────────────────────────────────────────

async def should_send_token_alert(chat_id: str, cooldown_hours: int = 12) -> bool:
    """
    Return True only if we haven't sent a token-invalid alert within the
    last `cooldown_hours` hours. Prevents spamming the user with the same
    error on every webhook push while their token stays broken.
    """
    import datetime
    user = await get_user(chat_id)
    if not user:
        return False
    last = user.get("token_alert_sent_at")
    if not last:
        return True
    try:
        sent_at = datetime.datetime.fromisoformat(last)
        delta = datetime.datetime.utcnow() - sent_at
        return delta.total_seconds() > cooldown_hours * 3600
    except ValueError:
        return True


async def mark_token_alert_sent(chat_id: str) -> None:
    await _require_pool().execute(
        "UPDATE users SET token_alert_sent_at = to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS') "
        "WHERE chat_id = $1",
        chat_id,
    )


async def clear_token_alert(chat_id: str) -> None:
    await _require_pool().execute(
        "UPDATE users SET token_alert_sent_at = NULL WHERE chat_id = $1", chat_id
    )


async def try_queue_sha(sha: str, chat_id: str) -> bool:
    """
    Atomic dedup gate. Returns True only for the coroutine whose INSERT
    actually added a row — i.e. the first delivery of this (sha, chat_id).
    Retried webhook deliveries for the same SHA get False and are skipped.
    """
    result = await _require_pool().execute(
        "INSERT INTO queued_shas (sha, chat_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        sha, chat_id,
    )
    # asyncpg returns a status string like "INSERT 0 1" (1 row) or "INSERT 0 0" (conflict, no-op)
    return result.endswith(" 1")


# ── User CRUD ─────────────────────────────────────────────────────────────────

async def get_user(chat_id: str) -> Optional[Dict[str, Any]]:
    row = await _require_pool().fetchrow("SELECT * FROM users WHERE chat_id = $1", chat_id)
    return dict(row) if row else None


async def upsert_user(chat_id: str, **fields) -> None:
    """
    Create or update a user row.

    SECURITY: Only columns in _ALLOWED_USER_COLUMNS are accepted. Any
    unknown key is dropped rather than interpolated into SQL — prevents
    SQL injection through field-name manipulation.
    """
    safe_fields: Dict[str, Any] = {}
    for k, v in fields.items():
        if k not in _ALLOWED_USER_COLUMNS:
            continue
        if isinstance(v, _CLEAR):
            safe_fields[k] = None   # write NULL
        elif v is not None:
            safe_fields[k] = v      # write value
        # plain None → skip (no-op, preserves existing value)

    pool = _require_pool()
    user = await get_user(chat_id)
    if user is None:
        webhook_secret = safe_fields.pop("webhook_secret", secrets.token_hex(24))
        await pool.execute(
            "INSERT INTO users (chat_id, webhook_secret, onboard_step) VALUES ($1, $2, 'await_repo')",
            chat_id, webhook_secret,
        )
        if safe_fields:
            await upsert_user(chat_id, **safe_fields)
        return

    if not safe_fields:
        return

    set_parts = [f"{k} = ${i + 2}" for i, k in enumerate(safe_fields)]
    set_parts.append("updated_at = to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')")
    values = list(safe_fields.values())

    await pool.execute(
        f"UPDATE users SET {', '.join(set_parts)} WHERE chat_id = $1",
        chat_id, *values,
    )


async def is_setup_complete(chat_id: str) -> bool:
    user = await get_user(chat_id)
    return bool(
        user
        and user.get("onboard_step") == "done"
        and user.get("github_token")
        and user.get("owner")
        and user.get("repo")
    )


# ── Active reviews CRUD ───────────────────────────────────────────────────────

async def save_review(
    commit_sha: str,
    chat_id: str,
    owner: str,
    repo: str,
    branch: str,
    message_id: int,
    decision_json: str,
    commit_meta_json: str,
) -> None:
    await _require_pool().execute(
        """INSERT INTO active_reviews
           (commit_sha, chat_id, owner, repo, branch, message_id,
            decision_json, commit_meta_json)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
           ON CONFLICT (commit_sha, chat_id) DO UPDATE SET
               owner = EXCLUDED.owner, repo = EXCLUDED.repo, branch = EXCLUDED.branch,
               message_id = EXCLUDED.message_id, decision_json = EXCLUDED.decision_json,
               commit_meta_json = EXCLUDED.commit_meta_json""",
        commit_sha, chat_id, owner, repo, branch, message_id,
        decision_json, commit_meta_json,
    )


async def get_review(commit_sha: str, chat_id: str) -> Optional[Dict[str, Any]]:
    row = await _require_pool().fetchrow(
        "SELECT * FROM active_reviews WHERE commit_sha = $1 AND chat_id = $2",
        commit_sha, chat_id,
    )
    return dict(row) if row else None


async def resolve_review(commit_sha: str, chat_id: str, status: str) -> None:
    """Idempotent — safe to call twice on the same (SHA, chat_id)."""
    await _require_pool().execute(
        """UPDATE active_reviews
           SET status = $1, resolved = 1
           WHERE commit_sha = $2 AND chat_id = $3 AND resolved = 0""",
        status, commit_sha, chat_id,
    )


async def get_accepted_reviews_after(chat_id: str, created_at: str) -> List[Dict[str, Any]]:
    """
    Reviews for this user that were ACCEPTED and created AFTER the given
    timestamp. Used to detect commits stacked on top of a commit being
    declined — declining the older commit would also wipe the newer
    accepted ones.
    """
    rows = await _require_pool().fetch(
        """SELECT * FROM active_reviews
           WHERE chat_id = $1
             AND status = 'accepted'
             AND resolved = 1
             AND created_at > $2
           ORDER BY created_at ASC""",
        chat_id, created_at,
    )
    return [dict(r) for r in rows]


async def get_all_reviews_for_user(chat_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Return ALL reviews (resolved and pending) for commit history view."""
    rows = await _require_pool().fetch(
        """SELECT * FROM active_reviews
           WHERE chat_id = $1
           ORDER BY created_at DESC LIMIT $2""",
        chat_id, limit,
    )
    return [dict(r) for r in rows]


async def get_active_reviews_for_user(chat_id: str) -> List[Dict[str, Any]]:
    rows = await _require_pool().fetch(
        """SELECT * FROM active_reviews
           WHERE chat_id = $1 AND resolved = 0
           ORDER BY created_at DESC LIMIT 10""",
        chat_id,
    )
    return [dict(r) for r in rows]


async def get_timed_out_reviews() -> List[Dict[str, Any]]:
    """
    Pending reviews that have exceeded the owner's timeout.
    Excludes users with timeout_hours = 0 (auto-action disabled).
    """
    rows = await _require_pool().fetch(
        """SELECT r.*, u.timeout_hours, u.timeout_action, u.github_token
           FROM active_reviews r
           JOIN users u ON r.chat_id = u.chat_id
           WHERE r.resolved  = 0
             AND r.timed_out = 0
             AND r.status    = 'pending'
             AND u.timeout_hours > 0
             AND u.timeout_action != 'none'
             AND EXTRACT(EPOCH FROM (
                     (now() AT TIME ZONE 'utc') - r.created_at::timestamp
                 )) / 3600.0 >= u.timeout_hours"""
    )
    return [dict(r) for r in rows]


async def mark_timed_out(commit_sha: str, chat_id: str) -> None:
    await _require_pool().execute(
        "UPDATE active_reviews SET timed_out = 1 WHERE commit_sha = $1 AND chat_id = $2",
        commit_sha, chat_id,
    )


async def cleanup_old_reviews(hours: int = 48) -> int:
    result = await _require_pool().execute(
        """DELETE FROM active_reviews
           WHERE resolved = 1
             AND created_at::timestamp < (now() AT TIME ZONE 'utc') - ($1 || ' hours')::interval""",
        str(int(hours)),
    )
    # asyncpg execute() returns "DELETE <n>"
    try:
        return int(result.split(" ")[-1])
    except (ValueError, IndexError):
        return 0


# ── Background jobs (Phase 3) ────────────────────────────────────────────────

async def create_job(job_type: str, payload: Dict[str, Any]) -> int:
    """Queue a job. Replaces firing an asyncio.create_task() directly."""
    row = await _require_pool().fetchrow(
        """INSERT INTO jobs (job_type, payload, status)
           VALUES ($1, $2::jsonb, 'pending')
           RETURNING id""",
        job_type, json.dumps(payload),
    )
    return row["id"]


async def claim_next_job() -> Optional[Dict[str, Any]]:
    """
    Atomically claim the oldest pending job and flip it to 'processing'.

    Uses SELECT ... FOR UPDATE SKIP LOCKED inside a transaction so multiple
    worker coroutines polling this same table (even across processes, if
    this ever scales beyond one instance) can never claim the same row
    twice — a locked row is just skipped rather than blocking the claimer.

    Returns None if there's nothing pending.
    """
    pool = _require_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """SELECT id, job_type, payload, attempts
                   FROM jobs
                   WHERE status = 'pending'
                   ORDER BY created_at ASC
                   LIMIT 1
                   FOR UPDATE SKIP LOCKED"""
            )
            if row is None:
                return None
            new_attempts = row["attempts"] + 1
            await conn.execute(
                """UPDATE jobs
                   SET status = 'processing', attempts = $2,
                       updated_at = to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
                   WHERE id = $1""",
                row["id"], new_attempts,
            )
            return {
                "id": row["id"],
                "job_type": row["job_type"],
                "payload": json.loads(row["payload"]),
                "attempts": new_attempts,
            }


async def mark_job_done(job_id: int) -> None:
    await _require_pool().execute(
        """UPDATE jobs SET status = 'done',
               updated_at = to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
           WHERE id = $1""",
        job_id,
    )


async def requeue_job(job_id: int, error_message: str) -> None:
    """Send a failed-but-still-retryable job back to 'pending', recording the error."""
    await _require_pool().execute(
        """UPDATE jobs
           SET status = 'pending', last_error = $2,
               updated_at = to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
           WHERE id = $1""",
        job_id, (error_message or "")[:2000],
    )


async def mark_job_failed(job_id: int, error_message: str) -> None:
    """Give up on a job after it has exhausted its retries."""
    await _require_pool().execute(
        """UPDATE jobs
           SET status = 'failed', last_error = $2,
               updated_at = to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
           WHERE id = $1""",
        job_id, (error_message or "")[:2000],
    )


async def reset_stuck_jobs() -> int:
    """
    Crash recovery. Call once at startup, before workers start.

    Any job still 'processing' at this point is a leftover from a previous
    process that died mid-job (crash, OOM, Render redeploy) — in this
    single-process setup there's no way a 'processing' row is legitimately
    still in flight when the app is just booting. Reset it to 'pending' so
    a worker picks it back up automatically instead of it being lost.
    """
    result = await _require_pool().execute(
        """UPDATE jobs
           SET status = 'pending',
               updated_at = to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')
           WHERE status = 'processing'"""
    )
    try:
        n = int(result.split(" ")[-1])
    except (ValueError, IndexError):
        n = 0
    if n:
        logger.warning("Reset %d job(s) stuck in 'processing' from a previous crash", n)
    return n
