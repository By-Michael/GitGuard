"""
SQLite database layer for Commit Guardian (multi-user).

Security hardening in this version:
- upsert_user uses a strict column whitelist to prevent SQL injection via field names
- get_timed_out_reviews excludes users with timeout_hours=0 (disabled)
- resolve_review is idempotent (safe to call twice)
"""

import sqlite3
import secrets
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List

DB_PATH = Path(__file__).parent / "commit_guardian.db"

_local = threading.local()

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


def _get_conn() -> sqlite3.Connection:
    if not getattr(_local, "conn", None):
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


def init_db() -> None:
    conn = _get_conn()
    conn.executescript("""
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
            created_at           TEXT DEFAULT (datetime('now')),
            updated_at           TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS active_reviews (
            commit_sha        TEXT PRIMARY KEY,
            chat_id           TEXT NOT NULL,
            owner             TEXT NOT NULL,
            repo              TEXT NOT NULL,
            branch            TEXT NOT NULL,
            message_id        INTEGER,
            status            TEXT DEFAULT 'pending',
            decision_json     TEXT,
            commit_meta_json  TEXT,
            created_at        TEXT DEFAULT (datetime('now')),
            resolved          INTEGER DEFAULT 0,
            timed_out         INTEGER DEFAULT 0,
            FOREIGN KEY (chat_id) REFERENCES users(chat_id)
        );

        CREATE INDEX IF NOT EXISTS idx_reviews_pending
            ON active_reviews(resolved, timed_out, status, chat_id);

        CREATE INDEX IF NOT EXISTS idx_reviews_chat
            ON active_reviews(chat_id, resolved);

        -- Fix #3: atomic dedup gate prevents duplicate reviews on webhook retry.
        -- Only the coroutine whose INSERT actually changes a row gets to queue
        -- the background task; subsequent inserts for the same SHA are no-ops.
        CREATE TABLE IF NOT EXISTS queued_shas (
            sha        TEXT PRIMARY KEY,
            chat_id    TEXT NOT NULL,
            queued_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    # Migration: add token_alert_sent_at to pre-existing databases.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "token_alert_sent_at" not in existing_cols:
        conn.execute("ALTER TABLE users ADD COLUMN token_alert_sent_at TEXT DEFAULT NULL")
    conn.commit()


def should_send_token_alert(chat_id: str, cooldown_hours: int = 12) -> bool:
    """
    Return True only if we haven't sent a token-invalid alert within the
    last `cooldown_hours` hours.  Prevents spamming the user with the same
    error on every webhook push while their token stays broken.
    """
    import datetime
    user = get_user(chat_id)
    if not user:
        return False
    last = user.get("token_alert_sent_at")
    if not last:
        return True
    try:
        sent_at = datetime.datetime.fromisoformat(last)
        delta   = datetime.datetime.utcnow() - sent_at
        return delta.total_seconds() > cooldown_hours * 3600
    except ValueError:
        return True


def mark_token_alert_sent(chat_id: str) -> None:
    """Record current UTC time so duplicate token alerts are suppressed."""
    _get_conn().execute(
        "UPDATE users SET token_alert_sent_at = datetime('now') WHERE chat_id = ?",
        (chat_id,),
    )
    _get_conn().commit()


def clear_token_alert(chat_id: str) -> None:
    """Reset alert flag after the user successfully provides a fresh token."""
    _get_conn().execute(
        "UPDATE users SET token_alert_sent_at = NULL WHERE chat_id = ?",
        (chat_id,),
    )
    _get_conn().commit()


def try_queue_sha(sha: str, chat_id: str) -> bool:
    """
    Atomically claim a commit SHA for processing.

    Fix #3 (dedup race condition): INSERT OR IGNORE is atomic at the SQLite
    level; only the first caller for a given SHA gets rowcount == 1.
    Any concurrent or retried webhook delivery for the same SHA gets 0 and
    should NOT queue a background task.
    """
    cur = _get_conn().execute(
        "INSERT OR IGNORE INTO queued_shas (sha, chat_id) VALUES (?, ?)",
        (sha, chat_id),
    )
    _get_conn().commit()
    return cur.rowcount == 1


# ── User CRUD ─────────────────────────────────────────────────────────────────

def get_user(chat_id: str) -> Optional[Dict[str, Any]]:
    row = _get_conn().execute(
        "SELECT * FROM users WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    return dict(row) if row else None


def upsert_user(chat_id: str, **fields) -> None:
    """
    Create or update a user row.

    SECURITY: Only columns in _ALLOWED_USER_COLUMNS are accepted.
    Any unknown key is dropped with a warning rather than interpolated
    into SQL — prevents SQL injection through field name manipulation.
    """
    # Bug 4 fix: allow explicit NULL resets via the CLEAR sentinel.
    # Fields set to None are still dropped (no-op), but fields set to CLEAR
    # are converted to None so they actually get written as NULL.
    safe_fields = {}
    for k, v in fields.items():
        if k not in _ALLOWED_USER_COLUMNS:
            continue
        if isinstance(v, _CLEAR):
            safe_fields[k] = None   # write NULL
        elif v is not None:
            safe_fields[k] = v      # write value
        # plain None → skip (no-op, preserves existing value)

    user = get_user(chat_id)
    if user is None:
        webhook_secret = safe_fields.pop("webhook_secret", secrets.token_hex(24))
        conn = _get_conn()
        conn.execute(
            """INSERT INTO users (chat_id, webhook_secret, onboard_step)
               VALUES (?, ?, 'await_repo')""",
            (chat_id, webhook_secret),
        )
        conn.commit()
        if safe_fields:
            upsert_user(chat_id, **safe_fields)
        return

    if not safe_fields:
        return

    # Build parameterised SET clause — column names come from the whitelist only
    set_parts = [f"{k} = ?" for k in safe_fields]
    set_parts.append("updated_at = datetime('now')")
    values     = list(safe_fields.values())

    _get_conn().execute(
        f"UPDATE users SET {', '.join(set_parts)} WHERE chat_id = ?",
        (*values, chat_id),
    )
    _get_conn().commit()


def is_setup_complete(chat_id: str) -> bool:
    user = get_user(chat_id)
    # Bug 5 fix: also require onboard_step == "done" so mid-setup users
    # (who may have a token/repo from a prior run) don't get processed.
    return bool(
        user
        and user.get("onboard_step") == "done"
        and user.get("github_token")
        and user.get("owner")
        and user.get("repo")
    )


# ── Active reviews CRUD ───────────────────────────────────────────────────────

def save_review(
    commit_sha: str,
    chat_id: str,
    owner: str,
    repo: str,
    branch: str,
    message_id: int,
    decision_json: str,
    commit_meta_json: str,
) -> None:
    _get_conn().execute(
        """INSERT OR REPLACE INTO active_reviews
           (commit_sha, chat_id, owner, repo, branch, message_id,
            decision_json, commit_meta_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (commit_sha, chat_id, owner, repo, branch, message_id,
         decision_json, commit_meta_json),
    )
    _get_conn().commit()


def get_review(commit_sha: str) -> Optional[Dict[str, Any]]:
    row = _get_conn().execute(
        "SELECT * FROM active_reviews WHERE commit_sha = ?", (commit_sha,)
    ).fetchone()
    return dict(row) if row else None


def resolve_review(commit_sha: str, status: str) -> None:
    """Idempotent — safe to call twice on the same SHA."""
    _get_conn().execute(
        """UPDATE active_reviews
           SET status = ?, resolved = 1
           WHERE commit_sha = ? AND resolved = 0""",
        (status, commit_sha),
    )
    _get_conn().commit()


def get_accepted_reviews_after(chat_id: str, created_at: str) -> List[Dict[str, Any]]:
    """
    Return reviews for this user that were ACCEPTED and created AFTER the given
    timestamp. Used to detect commits stacked on top of a commit being declined —
    declining the older commit would also wipe the newer accepted ones.
    """
    rows = _get_conn().execute(
        """SELECT * FROM active_reviews
           WHERE chat_id = ?
             AND status = 'accepted'
             AND resolved = 1
             AND created_at > ?
           ORDER BY created_at ASC""",
        (chat_id, created_at),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_reviews_for_user(chat_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Return ALL reviews (resolved and pending) for commit history view."""
    rows = _get_conn().execute(
        """SELECT * FROM active_reviews
           WHERE chat_id = ?
           ORDER BY created_at DESC LIMIT ?""",
        (chat_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_active_reviews_for_user(chat_id: str) -> List[Dict[str, Any]]:
    rows = _get_conn().execute(
        """SELECT * FROM active_reviews
           WHERE chat_id = ? AND resolved = 0
           ORDER BY created_at DESC LIMIT 10""",
        (chat_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_timed_out_reviews() -> List[Dict[str, Any]]:
    """
    Return pending reviews that have exceeded the owner's timeout.

    FIXED: excludes users with timeout_hours = 0 (auto-action disabled)
    by adding  AND u.timeout_hours > 0  to the WHERE clause.
    """
    rows = _get_conn().execute(
        """SELECT r.*, u.timeout_hours, u.timeout_action, u.github_token
           FROM active_reviews r
           JOIN users u ON r.chat_id = u.chat_id
           WHERE r.resolved  = 0
             AND r.timed_out = 0
             AND r.status    = 'pending'
             AND u.timeout_hours > 0
             AND u.timeout_action != 'none'
             AND (
                 CAST((julianday('now') - julianday(r.created_at)) * 24 AS INTEGER)
                 >= u.timeout_hours
             )"""
    ).fetchall()
    return [dict(r) for r in rows]


def mark_timed_out(commit_sha: str) -> None:
    _get_conn().execute(
        "UPDATE active_reviews SET timed_out = 1 WHERE commit_sha = ?",
        (commit_sha,),
    )
    _get_conn().commit()


def cleanup_old_reviews(hours: int = 48) -> int:
    # Fix #7: use SQLite datetime modifier syntax with a bound parameter
    # so the hours value can never be injected as raw SQL.
    cur = _get_conn().execute(
        "DELETE FROM active_reviews "
        "WHERE resolved = 1 "
        "AND created_at < datetime('now', ? || ' hours')",
        (f"-{int(hours)}",),
    )
    _get_conn().commit()
    return cur.rowcount
