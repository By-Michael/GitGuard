"""
Timeout Worker for Commit Guardian — v5 fixes:

- auto_decline failure no longer re-calls mark_timed_out (already set at top)
  and leaves review with resolved=0 so manual buttons still work
- auto_decline failure updates the review card to REMOVE the buttons so
  users don't get confused by stale Accept/Decline that won't re-trigger the worker
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import database as db
from ai_service import CommitDecision

logger = logging.getLogger("commit_guardian.timeout")

CHECK_INTERVAL_SECONDS = 60


async def run_timeout_worker() -> None:
    logger.info("Timeout worker started (checking every %ds)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            await _check_and_act()
        except Exception as exc:
            logger.error("Timeout worker error: %s", exc, exc_info=True)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _check_and_act() -> None:
    stale = db.get_timed_out_reviews()
    if not stale:
        return

    logger.info("Found %d timed-out review(s)", len(stale))

    from github_service import GitHubService
    from telegram_service import telegram_service

    for review in stale:
        commit_sha    = review["commit_sha"]
        chat_id       = review["chat_id"]
        action        = review["timeout_action"]
        timeout_hours = review["timeout_hours"]
        github_token  = review["github_token"]
        owner         = review["owner"]
        repo          = review["repo"]
        branch        = review["branch"]
        message_id    = review["message_id"]
        sha_short     = commit_sha[:7]

        # Mark timed_out=1 immediately — prevents re-processing in next cycle
        # even if the actions below fail or take a long time
        db.mark_timed_out(commit_sha)

        try:
            commit_metadata = json.loads(review["commit_meta_json"])
            decision_dict   = json.loads(review["decision_json"])
        except (json.JSONDecodeError, KeyError) as exc:
            logger.error("Cannot deserialise review %s: %s", sha_short, exc)
            db.resolve_review(commit_sha, "error")
            continue

        decision = CommitDecision(
            decision         = decision_dict.get("decision", "review"),
            confidence_score = decision_dict.get("confidence_score", 0.5),
            risk_level       = decision_dict.get("risk_level", "medium"),
            summary          = decision_dict.get("summary", ""),
            reasoning        = decision_dict.get("reasoning", []),
            concerns         = decision_dict.get("concerns", []),
            positive_aspects = decision_dict.get("positive_aspects", []),
            recommendations  = decision_dict.get("recommendations", []),
            suggested_action = decision_dict.get("suggested_action", ""),
        )

        logger.info("Auto-%s commit %s for user %s (timeout=%dh)", action, sha_short, chat_id, timeout_hours)

        if action == "accept":
            await _auto_accept(telegram_service, chat_id, message_id, commit_sha, commit_metadata, timeout_hours)

        elif action == "decline":
            gh = GitHubService(token=github_token)
            try:
                await _auto_decline(
                    telegram_service, gh, chat_id, message_id,
                    commit_sha, commit_metadata, owner, repo, branch, timeout_hours,
                )
            finally:
                await gh.close()


async def _auto_accept(tg, chat_id, message_id, commit_sha, commit_metadata, timeout_hours) -> None:
    sha_short = commit_sha[:7]
    text = (
        f"⏰ *REVIEW TIMED OUT — AUTO-ACCEPTED*\n\n"
        f"`{sha_short}` — {commit_metadata.get('message','')[:80]}\n"
        f"by {commit_metadata.get('author_name','Unknown')}\n\n"
        f"No response within *{timeout_hours} hours* — commit kept per your timeout setting.\n\n"
        f"_Auto-actioned at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
    )
    try:
        await tg.edit_message(chat_id, message_id, text, reply_markup={"inline_keyboard": []})
    except Exception as exc:
        logger.warning("Could not edit message %d: %s — sending new message", message_id, exc)
        await tg.send_message(
            chat_id,
            f"⏰ *Auto-accepted* `{sha_short}` after {timeout_hours}h with no response.\n"
            f"_{commit_metadata.get('message','')[:80]}_",
        )
    db.resolve_review(commit_sha, "auto_accepted")
    logger.info("Commit %s auto-accepted for %s", sha_short, chat_id)


async def _auto_decline(
    tg, gh, chat_id, message_id, commit_sha,
    commit_metadata, owner, repo, branch, timeout_hours,
) -> None:
    from github_service import RollbackError
    sha_short = commit_sha[:7]

    proc = await tg.send_processing(
        chat_id,
        f"⏰ _Review for `{sha_short}` timed out — auto-rolling back…_",
    )

    rollback_result: Dict[str, Any] = {}
    error_msg: Optional[str] = None
    try:
        rollback_result = await gh.rollback_commit(owner, repo, commit_sha, branch=branch)
    except RollbackError as exc:
        error_msg = str(exc)
        logger.error("Auto-decline rollback failed for %s: %s", sha_short, exc)
    except Exception as exc:
        error_msg = str(exc)
        logger.error("Unexpected error during auto-decline for %s: %s", sha_short, exc)

    await tg.delete_message(chat_id, proc)

    if rollback_result.get("success"):
        text = (
            f"⏰ *REVIEW TIMED OUT — AUTO-DECLINED & ROLLED BACK*\n\n"
            f"`{sha_short}` — {commit_metadata.get('message','')[:80]}\n"
            f"by {commit_metadata.get('author_name','Unknown')}\n\n"
            f"No response within *{timeout_hours} hours* — rolled back per your timeout setting.\n"
        )
        if rollback_result.get("revert_sha"):
            text += f"📝 Revert SHA: `{rollback_result['revert_sha'][:7]}`\n"
        text += f"\n_Auto-actioned at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_"
        # Fully resolved — buttons removed
        try:
            await tg.edit_message(chat_id, message_id, text, reply_markup={"inline_keyboard": []})
        except Exception:
            await tg.send_message(chat_id, text)
        db.resolve_review(commit_sha, "auto_declined")

    else:
        # Rollback failed — tell the user and remove buttons to avoid confusion
        # review is already marked timed_out=1 so worker won't retry it
        # but we resolve it so the buttons disappear and user knows to act manually
        text = (
            f"⏰ *REVIEW TIMED OUT — AUTO-DECLINE FAILED*\n\n"
            f"`{sha_short}` — {commit_metadata.get('message','')[:80]}\n\n"
            f"Tried to auto-rollback after {timeout_hours}h but failed:\n"
            f"`{error_msg or 'Unknown error'}`\n\n"
            f"⚠️ *Please rollback this commit manually:*\n"
            f"{commit_metadata.get('url', 'N/A')}"
        )
        try:
            await tg.edit_message(chat_id, message_id, text, reply_markup={"inline_keyboard": []})
        except Exception:
            await tg.send_message(chat_id, text)
        # Resolve so buttons are disabled — user must act manually via GitHub
        db.resolve_review(commit_sha, "auto_decline_failed")

    logger.info(
        "Commit %s auto-decline for %s — success: %s",
        sha_short, chat_id, rollback_result.get("success", False),
    )
