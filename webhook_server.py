"""
Commit Guardian — Webhook Server v5

Fixes in this version:
- Double-press race condition: check resolved flag before acting
- on_decline resolves review in finally block so it's always cleaned up
- /settings and /status properly guard against unauthenticated users
"""

import asyncio
import json
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

import database as db
from ai_service import CommitDecision, ai_service
from config import CONFIG, logger
from github_service import GitHubService, GitHubServiceError, RollbackError, WebhookVerificationError
from telegram_service import TelegramAPIError, telegram_service
from timeout_worker import run_timeout_worker


# ──────────────────────────────────────────────
# Lifecycle
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    logger.info("Commit Guardian v5 starting — multi-user + timeout worker")
    cleanup_task = asyncio.create_task(_cleanup_loop())
    timeout_task = asyncio.create_task(run_timeout_worker())
    yield
    logger.info("Shutting down…")
    cleanup_task.cancel()
    timeout_task.cancel()
    await telegram_service.close()


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        removed = db.cleanup_old_reviews(hours=48)
        if removed:
            logger.info("Cleaned up %d old resolved reviews", removed)


app = FastAPI(title="Commit Guardian", version="5.0.0", lifespan=lifespan)


# ──────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "healthy", "version": "5.0.0"}

@app.get("/")
async def root():
    return {
        "service": "Commit Guardian v5",
        "endpoints": {
            "github_webhook":   "POST /webhook/github/{chat_id}",
            "telegram_webhook": "POST /webhook/telegram",
        }
    }


# ──────────────────────────────────────────────
# GitHub Webhook — per-user URL
# ──────────────────────────────────────────────

@app.post("/webhook/github/{chat_id}", status_code=status.HTTP_200_OK)
async def github_webhook(
    chat_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Optional[str] = Header(None),
    x_github_event:      Optional[str] = Header(None),
    x_github_delivery:   Optional[str] = Header(None),
):
    if x_github_event != "push":
        return {"status": "ignored"}

    user = db.get_user(chat_id)
    if not user or not db.is_setup_complete(chat_id):
        # Return 200 to prevent GitHub retrying — user just isn't set up
        logger.warning("Webhook for unknown/incomplete user %s — ignoring", chat_id)
        return {"status": "ignored", "reason": "user not configured"}

    payload_bytes = await request.body()

    try:
        GitHubService.verify_webhook_signature(
            payload_bytes, x_hub_signature_256, user["webhook_secret"]
        )
    except WebhookVerificationError as exc:
        logger.warning("Signature failure for user %s: %s", chat_id, exc)
        raise HTTPException(status_code=401, detail="Invalid webhook signature") from exc

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    repo_full = payload.get("repository", {}).get("full_name", "")
    if not repo_full or "/" not in repo_full:
        raise HTTPException(status_code=400, detail="Missing repository info")

    owner, repo = repo_full.split("/", 1)
    branch      = payload.get("ref", "").replace("refs/heads/", "")
    pusher      = payload.get("pusher", {})

    # Bug 2 fix: only process the branch the user configured
    configured_branch = user.get("branch", "main")
    if branch != configured_branch:
        logger.info("Ignoring push to branch '%s' — user configured '%s'", branch, configured_branch)
        return {"status": "ignored", "reason": f"branch '{branch}' not monitored"}
    commits     = payload.get("commits", []) or (
        [payload["head_commit"]] if payload.get("head_commit") else []
    )

    queued = 0
    for c in commits:
        sha = c.get("id")
        if sha and not db.get_review(sha):
            background_tasks.add_task(
                process_commit, chat_id, user["github_token"],
                owner, repo, sha,
                pusher.get("name", "Unknown"), pusher.get("email", "N/A"), branch,
            )
            queued += 1

    return {"status": "ok", "commits_queued": queued}


# ──────────────────────────────────────────────
# Commit Processing Pipeline
# ──────────────────────────────────────────────

async def process_commit(
    chat_id, github_token, owner, repo,
    commit_sha, pusher_name, pusher_email, branch,
) -> None:
    try:
        await _process_commit_inner(
            chat_id, github_token, owner, repo,
            commit_sha, pusher_name, pusher_email, branch,
        )
    except Exception as exc:
        logger.error(
            "Unhandled error for %s: %s\n%s",
            commit_sha[:7], exc, traceback.format_exc(),
        )


async def _process_commit_inner(
    chat_id, github_token, owner, repo,
    commit_sha, pusher_name, pusher_email, branch,
) -> None:
    gh = GitHubService(token=github_token)

    # Step 1: fetch commit
    proc = await telegram_service.send_processing(
        chat_id,
        f"🔄 _New push on `{branch}` — fetching `{commit_sha[:7]}`…_",
    )
    try:
        commit_metadata = await gh.fetch_commit_metadata(owner, repo, commit_sha)
    except GitHubServiceError as exc:
        await telegram_service.delete_message(chat_id, proc)
        await telegram_service.send_message(
            chat_id,
            f"⚠️ *Failed to fetch commit*\n\n`{commit_sha[:7]}`\nRepo: `{owner}/{repo}`\n\n`{exc}`",
        )
        await gh.close()
        return

    # Revert guard — our own rollback commits never need review
    if telegram_service._is_revert_commit(commit_metadata):
        await telegram_service.delete_message(chat_id, proc)
        reverted = telegram_service._extract_reverted_sha(commit_metadata)
        await telegram_service.send_message(
            chat_id,
            f"⏪ *Rollback confirmed* — `{commit_sha[:7]}` applied to `{branch}`.\n"
            f"_Reverted commit `{reverted}`. No review needed._",
        )
        await gh.close()
        return

    commit_metadata["_pusher_name"]    = pusher_name
    commit_metadata["_pusher_email"]   = pusher_email
    commit_metadata["_branch"]         = branch
    commit_metadata["_repo_full_name"] = f"{owner}/{repo}"

    # Step 2: repo context
    try:
        await telegram_service.edit_message(
            chat_id, proc,
            f"🔄 _Loading repo context for `{owner}/{repo}`…_",
        )
        repo_context = await gh.fetch_repo_context(owner, repo)
    except Exception as exc:
        logger.warning("Repo context failed for %s/%s: %s", owner, repo, exc)
        repo_context = {"repository": f"{owner}/{repo}", "files": [], "tree": [], "readme": None}

    # Step 3: AI analysis
    try:
        await telegram_service.edit_message(
            chat_id, proc,
            f"🤖 _Analysing `{commit_sha[:7]}` with AI…_",
        )
        decision = await ai_service.analyze_commit(commit_metadata, repo_context)
    except Exception as exc:
        await telegram_service.delete_message(chat_id, proc)
        await telegram_service.send_message(
            chat_id,
            f"⚠️ *AI Analysis Failed*\n\n`{commit_sha[:7]}`\n`{str(exc)[:300]}`\n\n"
            f"The commit is still in the repo — please review it manually:\n"
            f"{commit_metadata.get('url', 'N/A')}",
        )
        await gh.close()
        return

    # Step 4: send review card
    # Bug 1 fix: delete the processing message AFTER the card is confirmed sent.
    # If send_review_request raises, fall back to a plain error message so the
    # user always sees something instead of a silent black hole.
    try:
        message_id = await telegram_service.send_review_request(chat_id, commit_metadata, decision)
    except Exception as exc:
        logger.error("Failed to send review card for %s: %s", commit_sha[:7], exc)
        await telegram_service.delete_message(chat_id, proc)
        await telegram_service.send_message(
            chat_id,
            f"⚠️ *Failed to deliver review card*\n\n"
            f"`{commit_sha[:7]}` was analysed but the card could not be sent.\n"
            f"Error: `{str(exc)[:300]}`\n\n"
            f"AI decision: *{decision.decision.upper()}* | Risk: *{decision.risk_level.upper()}*\n"
            f"Review manually: {commit_metadata.get('url', 'N/A')}",
        )
        await gh.close()
        return

    await telegram_service.delete_message(chat_id, proc)

    if message_id == -1:
        await gh.close()
        return

    db.save_review(
        commit_sha       = commit_sha,
        chat_id          = chat_id,
        owner            = owner,
        repo             = repo,
        branch           = branch,
        message_id       = message_id,
        decision_json    = json.dumps(decision.to_dict()),
        commit_meta_json = json.dumps(commit_metadata),
    )

    logger.info("Review sent to %s for %s (msg %d)", chat_id, commit_sha[:7], message_id)
    await gh.close()


# ──────────────────────────────────────────────
# Telegram Webhook
# ──────────────────────────────────────────────

@app.post("/webhook/telegram", status_code=status.HTTP_200_OK)
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    cb = update.get("callback_query")
    if cb:
        await _handle_callback(cb)
        return {"ok": True}

    msg = update.get("message")
    if msg:
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text    = msg.get("text", "").strip()

        if text.lower() in ("/start", "/setup"):
            await telegram_service.handle_start(chat_id)
            return {"ok": True}

        if text.lower() == "/settings":
            await telegram_service.handle_settings(chat_id)
            return {"ok": True}

        if text.lower() == "/status":
            await _handle_status(chat_id)
            return {"ok": True}

        if text.lower() == "/help":
            await _handle_help(chat_id)
            return {"ok": True}

        # Mid-onboarding message
        await telegram_service.handle_message(chat_id, text)

    return {"ok": True}


async def _handle_status(chat_id: str) -> None:
    user = db.get_user(chat_id)
    if not user:
        await telegram_service.send_message(chat_id, "Send /start to get set up first.")
        return

    reviews        = db.get_active_reviews_for_user(chat_id)
    timeout_hours  = user.get("timeout_hours", 24)
    timeout_action = user.get("timeout_action", "accept")
    timeout_str    = "Disabled" if (timeout_hours == 0 or timeout_action == "none") \
                     else f"Auto-{timeout_action} after {timeout_hours}h"

    lines = "\n".join(
        f"• `{r['commit_sha'][:7]}` — {r['status']} ({r['created_at'][:10]})"
        for r in reviews
    ) or "_No pending reviews_"

    await telegram_service.send_message(
        chat_id,
        f"📊 *Your Status*\n\n"
        f"• Repo: `{user.get('owner','?')}/{user.get('repo','?')}`\n"
        f"• Branch: `{user.get('branch','main')}`\n"
        f"• Timeout: {timeout_str}\n\n"
        f"*Pending reviews:*\n{lines}",
    )


async def _handle_help(chat_id: str) -> None:
    await telegram_service.send_message(
        chat_id,
        "🤖 *Commit Guardian — Commands*\n\n"
        "/start — Set up or reconfigure your repo\n"
        "/status — See your pending reviews\n"
        "/settings — Change timeout settings\n"
        "/help — This message\n\n"
        "*How it works:*\n"
        "1. You set up your repo via /start\n"
        "2. Every push triggers an AI review\n"
        "3. You Accept, Decline (rollback), or request a Report\n"
        "4. If you don't respond within your timeout, I auto-act",
    )


async def _handle_callback(callback_query: Dict[str, Any]) -> None:
    cq_id      = callback_query.get("id")
    data_str   = callback_query.get("data", "")
    message    = callback_query.get("message", {})
    message_id = message.get("message_id")
    chat_id    = str(message.get("chat", {}).get("id", ""))

    # ── Settings shortcuts ────────────────────────────────────────────────────
    if data_str.startswith("cfg:"):
        action = data_str.split(":", 1)[1]
        await telegram_service.answer_callback(cq_id)
        if action == "restart":
            await telegram_service.handle_start(chat_id)
        elif action == "timeout":
            db.upsert_user(chat_id, onboard_step="await_timeout_hours")
            await telegram_service.send_message(
                chat_id,
                "⏰ *Change Timeout*\n\nHow many hours before I auto-act?\n_(Send `0` to disable)_",
            )
        return

    # ── Review button ─────────────────────────────────────────────────────────
    try:
        action_code, commit_sha = data_str.split(":", 1)
        action = {"acc": "accept", "dec": "decline", "rep": "report"}.get(action_code)
    except ValueError:
        await telegram_service.answer_callback(cq_id, "Invalid button data", show_alert=True)
        return

    if not action:
        await telegram_service.answer_callback(cq_id, f"Unknown action: {action_code}", show_alert=True)
        return

    review = db.get_review(commit_sha)
    if not review:
        await telegram_service.answer_callback(
            cq_id, "Review not found — it may have been deleted.", show_alert=True
        )
        return

    # ── RACE CONDITION FIX: check resolved BEFORE acting ─────────────────────
    if review.get("resolved"):
        await telegram_service.answer_callback(
            cq_id,
            "This review was already resolved — no action taken.",
            show_alert=True,
        )
        return

    # ── Ownership check ───────────────────────────────────────────────────────
    if review["chat_id"] != chat_id:
        await telegram_service.answer_callback(cq_id, "This is not your review.", show_alert=True)
        return

    commit_metadata = json.loads(review["commit_meta_json"])
    decision_dict   = json.loads(review["decision_json"])
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

    user = db.get_user(chat_id)
    if not user:
        await telegram_service.answer_callback(cq_id, "User not found.", show_alert=True)
        return

    gh = GitHubService(token=user["github_token"])

    try:
        if action == "accept":
            async def on_accept() -> None:
                # Resolve first — if Telegram edit fails after this, the
                # DB is still correct and buttons won't work again
                db.resolve_review(commit_sha, "accepted")

            await telegram_service.handle_accept(
                chat_id, cq_id, message_id, commit_metadata, decision, on_accept
            )

        elif action == "decline":
            async def on_decline() -> Dict[str, Any]:
                try:
                    result = await gh.rollback_commit(
                        review["owner"], review["repo"], commit_sha, branch=review["branch"]
                    )
                    return result
                finally:
                    # Always mark resolved regardless of rollback outcome
                    # so buttons are disabled and user gets the result message
                    db.resolve_review(commit_sha, "declined")

            await telegram_service.handle_decline(
                chat_id, cq_id, message_id, commit_metadata, decision, on_decline
            )

        elif action == "report":
            # Report doesn't resolve — user can still Accept/Decline after
            await telegram_service.handle_report(chat_id, cq_id, commit_metadata, decision)

    finally:
        await gh.close()


# ──────────────────────────────────────────────
# Error handlers
# ──────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_error(request: Request, exc: Exception):
    logger.error(
        "Unhandled: %s %s — %s\n%s",
        request.method, request.url.path, exc, traceback.format_exc(),
    )
    return JSONResponse(status_code=500, content={"error": str(exc)[:200]})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webhook_server:app", host=CONFIG.server_host, port=CONFIG.server_port, reload=False)
