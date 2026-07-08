"""
Configuration module for the GitHub Commit Guardian system (multi-user).

Only three values are global now:
  - TELEGRAM_BOT_TOKEN
  - GROQ_API_KEY / GROQ_MODEL / GROQ_* tuning
  - PUBLIC_URL  (used to build per-user webhook URLs shown in onboarding)

Everything else (GitHub token, webhook secret, repo, branch) is stored
per-user in the SQLite database (database.py).
"""

import json
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")


class _JsonFormatter(logging.Formatter):
    """
    Phase 4 structured logging. Emits one JSON object per line so logs are
    greppable/parseable by Render's log pipeline (or anything downstream)
    without regexing a human-formatted string. Includes `category` when a
    GitGuardError (see exceptions.py) is the active exception, so failures
    can be grouped by dependency (github/ai/telegram/database/jobs) — this
    is also what alerting.py keys off of.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "func": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }
        if record.exc_info:
            exc_type, exc_val, _ = record.exc_info
            payload["exc_type"] = exc_type.__name__ if exc_type else None
            payload["exc_message"] = str(exc_val) if exc_val else None
            category = getattr(exc_val, "category", None)
            if category:
                payload["category"] = category
        for key in ("chat_id", "commit_sha", "job_id", "category"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, default=str)


_LOG_FORMAT = os.getenv("LOG_FORMAT", "text").lower()  # "text" | "json"
_handler = logging.StreamHandler()
if _LOG_FORMAT == "json":
    _handler.setFormatter(_JsonFormatter())
else:
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger("commit_guardian")


def _get_env(key: str, required: bool = True, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(key, default)
    if required and not value:
        raise EnvironmentError(
            f"Missing required environment variable: {key}. "
            f"Please set it in your .env file or environment."
        )
    return value


@dataclass(frozen=True)
class Config:
    # ── Shared / global ───────────────────────────────────────────────────────
    telegram_bot_token: str = field(default_factory=lambda: _get_env("TELEGRAM_BOT_TOKEN"))

    groq_api_key:     str   = field(default_factory=lambda: _get_env("GROQ_API_KEY"))
    groq_model:       str   = field(default_factory=lambda: _get_env("GROQ_MODEL", default="llama-3.3-70b-versatile"))
    groq_max_tokens:  int   = field(default_factory=lambda: int(_get_env("GROQ_MAX_TOKENS", default="4096")))
    groq_temperature: float = field(default_factory=lambda: float(_get_env("GROQ_TEMPERATURE", default="0.3")))

    # Public-facing URL so onboarding can print the correct webhook URL
    public_url: str = field(default_factory=lambda: _get_env("PUBLIC_URL", default="https://YOUR-SERVER-URL"))

    # ── Server ────────────────────────────────────────────────────────────────
    server_host: str = field(default_factory=lambda: _get_env("SERVER_HOST", default="0.0.0.0"))
    server_port: int = field(default_factory=lambda: int(_get_env("SERVER_PORT", default="8000")))

    # ── Database (Postgres) ──────────────────────────────────────────────────
    # Render injects this automatically if you attach a Postgres instance to
    # the web service (Environment → "Add Database"). For local dev, point it
    # at a local/dockerized Postgres — see docker-compose.yml.
    # asyncpg accepts postgres:// and postgresql:// schemes directly.
    database_url: str = field(default_factory=lambda: _get_env("DATABASE_URL"))
    db_pool_min: int = field(default_factory=lambda: int(_get_env("DB_POOL_MIN", default="2")))
    db_pool_max: int = field(default_factory=lambda: int(_get_env("DB_POOL_MAX", default="10")))
    db_command_timeout_seconds: float = field(
        default_factory=lambda: float(_get_env("DB_COMMAND_TIMEOUT_SECONDS", default="10"))
    )
    db_connect_timeout_seconds: float = field(
        default_factory=lambda: float(_get_env("DB_CONNECT_TIMEOUT_SECONDS", default="10"))
    )

    # ── Analysis tuning ───────────────────────────────────────────────────────
    max_context_files:  int   = field(default_factory=lambda: int(_get_env("MAX_CONTEXT_FILES", default="50")))
    max_file_size_bytes: int  = field(default_factory=lambda: int(_get_env("MAX_FILE_SIZE_BYTES", default="50000")))
    excluded_patterns:  tuple = field(
        default_factory=lambda: tuple(
            _get_env(
                "EXCLUDED_PATTERNS",
                default=".git,node_modules,__pycache__,.venv,*.lock,*.min.js,*.min.css,build,dist,.png,.jpg,.jpeg,.gif,.ico,.woff,.woff2,.ttf,.eot,.mp4,.mp3,.pdf,.zip,.tar.gz",
            ).split(",")
        )
    )
    rollback_strategy: str = field(
        default_factory=lambda: _get_env("ROLLBACK_STRATEGY", default="revert")
    )

    # Optional Telegram webhook secret — verifies updates actually come from Telegram.
    # Set TELEGRAM_WEBHOOK_SECRET in .env to enable (see .env.example).
    telegram_webhook_secret: Optional[str] = field(
        default_factory=lambda: _get_env("TELEGRAM_WEBHOOK_SECRET", required=False, default=None)
    )

    # ── Admin alerting (Phase 4) ─────────────────────────────────────────────
    # Telegram chat_id (your own, or an ops group) that gets pinged when a
    # failure category repeats past threshold within the window — separate
    # from the per-user, per-commit failure notices that already exist.
    # Leave unset to disable (a warning is logged once at startup instead).
    admin_chat_id: Optional[str] = field(
        default_factory=lambda: _get_env("ADMIN_CHAT_ID", required=False, default=None)
    )
    alert_failure_threshold: int = field(
        default_factory=lambda: int(_get_env("ALERT_FAILURE_THRESHOLD", default="5"))
    )
    alert_window_seconds: float = field(
        default_factory=lambda: float(_get_env("ALERT_WINDOW_SECONDS", default="600"))
    )
    alert_cooldown_seconds: float = field(
        default_factory=lambda: float(_get_env("ALERT_COOLDOWN_SECONDS", default="1800"))
    )

    # ── Background job worker (Phase 3 — crash-safe jobs table) ─────────────
    # Commit analysis now runs off a `jobs` row in Postgres instead of a bare
    # asyncio.create_task, so an in-flight job survives a restart/crash.
    job_poll_interval_seconds: float = field(
        default_factory=lambda: float(_get_env("JOB_POLL_INTERVAL_SECONDS", default="3"))
    )
    job_worker_count: int = field(
        default_factory=lambda: int(_get_env("JOB_WORKER_COUNT", default="3"))
    )
    job_max_attempts: int = field(
        default_factory=lambda: int(_get_env("JOB_MAX_ATTEMPTS", default="3"))
    )


CONFIG = Config()
