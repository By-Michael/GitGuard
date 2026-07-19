"""
Central exception hierarchy for Commit Guardian — Phase 4.

Every module-specific exception in the codebase (AIServiceError,
GitHubServiceError, TelegramServiceError, DatabaseError, ...) now inherits
from GitGuardError so that:

  1. Callers that need to catch "anything GitGuard raised on purpose" can
     do `except GitGuardError` instead of a bare `except Exception` (which
     also swallows real bugs like AttributeError/TypeError).
  2. Every exception carries a machine-readable `.category` used by
     structured logging and by alerting.py to decide when repeated
     failures in the same category should page the admin.
  3. Every exception carries `.retryable` so job_worker/http_utils can make
     retry decisions based on the exception type instead of string-sniffing
     messages.

Category values are intentionally coarse (one per external dependency +
"internal") — alerting groups on this field.
"""

from typing import Optional


class GitGuardError(Exception):
    """Base class for every exception GitGuard raises on purpose."""

    category: str = "internal"
    retryable: bool = False

    def __init__(self, message: str, *, category: Optional[str] = None, retryable: Optional[bool] = None):
        super().__init__(message)
        if category is not None:
            self.category = category
        if retryable is not None:
            self.retryable = retryable


# ── Configuration / startup ─────────────────────────────────────────────────

class ConfigError(GitGuardError):
    category = "config"
    retryable = False


# ── Database ─────────────────────────────────────────────────────────────────

class DatabaseError(GitGuardError):
    category = "database"
    retryable = True


class DatabaseConnectionError(DatabaseError):
    retryable = True


# ── GitHub ───────────────────────────────────────────────────────────────────

class GitHubServiceError(GitGuardError):
    category = "github"
    retryable = True


class WebhookVerificationError(GitHubServiceError):
    """Signature mismatch — never retry, this is a security-relevant reject."""
    retryable = False


class GitHubAPIError(GitHubServiceError):
    retryable = True


class GitHubAuthError(GitHubAPIError):
    """Token invalid/revoked — retrying with the same token will never work."""
    retryable = False


class RollbackError(GitHubServiceError):
    retryable = False


# ── AI / Groq ────────────────────────────────────────────────────────────────

class AIServiceError(GitGuardError):
    category = "ai"
    retryable = True


class AIAnalysisError(AIServiceError):
    retryable = True


# ── Telegram ─────────────────────────────────────────────────────────────────

class TelegramServiceError(GitGuardError):
    category = "telegram"
    retryable = True


class TelegramAPIError(TelegramServiceError):
    retryable = True


# ── Background jobs ──────────────────────────────────────────────────────────

class JobError(GitGuardError):
    category = "jobs"
    retryable = True


class UnknownJobTypeError(JobError):
    retryable = False
