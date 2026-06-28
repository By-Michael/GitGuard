"""
Configuration module for the GitHub Commit Guardian system (multi-user).

Only three values are global now:
  - TELEGRAM_BOT_TOKEN
  - GROQ_API_KEY / GROQ_MODEL / GROQ_* tuning
  - PUBLIC_URL  (used to build per-user webhook URLs shown in onboarding)

Everything else (GitHub token, webhook secret, repo, branch) is stored
per-user in the SQLite database (database.py).
"""

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(funcName)s:%(lineno)d | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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


CONFIG = Config()
