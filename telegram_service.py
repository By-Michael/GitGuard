"""
Telegram Bot Service — v4 with timeout preference onboarding step.
Onboarding now has 5 steps:
  1. repo (owner/repo)
  2. branch
  3. github token
  4. timeout duration (hours)
  5. timeout action (accept / decline)
  hi there is there anyone there is there ai checking on this
"""

import asyncio
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, Optional

import httpx
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH

from ai_service import CommitDecision, ai_service
from config import CONFIG, logger
import database as db


class TelegramServiceError(Exception):
    pass

class TelegramAPIError(TelegramServiceError):
    pass


class TelegramService:

    EMOJI = {
        "pending":       "⏳",
        "accept":        "✅",
        "decline":       "❌",
        "report":        "📊",
        "risk_critical": "🔴",
        "risk_high":     "🟠",
        "risk_medium":   "🟡",
        "risk_low":      "🟢",
        "loading":       "🔄",
        "warning":       "⚠️",
        "rollback":      "⏪",
        "commit":        "📝",
        "user":          "👤",
        "clock":         "🕐",
        "files":         "📁",
        "ai":            "🤖",
        "rocket":        "🚀",
        "key":           "🔑",
        "link":          "🔗",
        "check":         "☑️",
        "timeout":       "⏰",
    }

    def __init__(self) -> None:
        self.token    = CONFIG.telegram_bot_token
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self._client: Optional[httpx.AsyncClient] = None

    # ── HTTP ──────────────────────────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _risk_emoji(self, risk_level: str) -> str:
        return {
            "critical": self.EMOJI["risk_critical"],
            "high":     self.EMOJI["risk_high"],
            "medium":   self.EMOJI["risk_medium"],
            "low":      self.EMOJI["risk_low"],
        }.get(risk_level.lower(), self.EMOJI["warning"])

    def _is_revert_commit(self, commit_metadata: Dict[str, Any]) -> bool:
        return commit_metadata.get("message", "").startswith("revert: rollback commit ")

    def _extract_reverted_sha(self, commit_metadata: Dict[str, Any]) -> Optional[str]:
        m = re.search(r"revert: rollback commit ([0-9a-f]{7,40})", commit_metadata.get("message", ""))
        return m.group(1) if m else None

    # ── Messaging primitives ──────────────────────────────────────────────────

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: Optional[Dict] = None,
        parse_mode: str = "Markdown",
    ) -> int:
        payload: Dict[str, Any] = {
            "chat_id":                  chat_id,
            "text":                     text,
            "parse_mode":               parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            client   = await self._get_client()
            response = await client.post(f"{self.base_url}/sendMessage", json=payload)
            response.raise_for_status()
            result = response.json()
            if not result.get("ok"):
                raise TelegramAPIError(f"Telegram error: {result.get('description')}")
            return result["result"]["message_id"]
        except httpx.HTTPError as exc:
            raise TelegramAPIError(f"Failed to send message: {exc}") from exc

    async def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "chat_id":                  chat_id,
            "message_id":               message_id,
            "text":                     text,
            "parse_mode":               "Markdown",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        try:
            client = await self._get_client()
            await client.post(f"{self.base_url}/editMessageText", json=payload)
        except httpx.HTTPError as exc:
            logger.warning("Could not edit message %d: %s", message_id, exc)

    async def delete_message(self, chat_id: str, message_id: int) -> None:
        try:
            client = await self._get_client()
            await client.post(
                f"{self.base_url}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
            )
        except httpx.HTTPError as exc:
            logger.warning("Could not delete message %d: %s", message_id, exc)

    async def send_processing(self, chat_id: str, text: str) -> int:
        return await self.send_message(chat_id, text)

    async def answer_callback(
        self, callback_query_id: str, text: Optional[str] = None, show_alert: bool = False
    ) -> None:
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True
        try:
            client = await self._get_client()
            await client.post(f"{self.base_url}/answerCallbackQuery", json=payload)
        except httpx.HTTPError as exc:
            logger.warning("Could not answer callback: %s", exc)

    async def send_document(
        self, chat_id: str, file_path: str,
        caption: Optional[str] = None, filename: Optional[str] = None,
    ) -> None:
        fname = filename or os.path.basename(file_path)
        try:
            client = await self._get_client()
            with open(file_path, "rb") as fh:
                files = {"document": (fname, fh, "application/octet-stream")}
                data: Dict[str, Any] = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption[:1024]
                response = await client.post(f"{self.base_url}/sendDocument", data=data, files=files)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TelegramAPIError(f"Failed to send document: {exc}") from exc

    # ── Onboarding wizard (5 steps) ───────────────────────────────────────────

    async def handle_start(self, chat_id: str) -> None:
        db.upsert_user(
            chat_id,
            onboard_step="await_repo",
            owner=db.CLEAR, repo=db.CLEAR, branch="main",
            github_token=db.CLEAR,
            timeout_hours=24,
            timeout_action="accept",
        )
        await self.send_message(
            chat_id,
            f"{self.EMOJI['rocket']} *Welcome to Commit Guardian!*\n\n"
            "I review every GitHub commit with AI and send it here for your approval.\n\n"
            f"{self.EMOJI['commit']} *Step 1 of 5 — Repository*\n\n"
            "Send your GitHub repo:\n`owner/repository-name`\n\n"
            "_Example: `torvalds/linux`_",
        )

    async def handle_message(self, chat_id: str, text: str) -> bool:
        """Returns True if message was consumed by the wizard."""
        user = db.get_user(chat_id)
        if not user:
            return False

        step = user.get("onboard_step", "done")
        if step == "done":
            return False

        # ── Step 1: repo ──────────────────────────────────────────────────────
        if step == "await_repo":
            if "/" not in text or text.count("/") != 1:
                await self.send_message(
                    chat_id,
                    f"{self.EMOJI['warning']} Please use the format `owner/repo`.\n"
                    "_Example: `torvalds/linux`_",
                )
                return True
            owner, repo = text.strip().split("/", 1)
            db.upsert_user(chat_id, owner=owner.strip(), repo=repo.strip(), onboard_step="await_branch")
            await self.send_message(
                chat_id,
                f"{self.EMOJI['check']} Repo: `{owner}/{repo}`\n\n"
                f"{self.EMOJI['commit']} *Step 2 of 5 — Branch*\n\n"
                "Which branch to monitor?\n_(Type name or send `main`)_",
            )
            return True

        # ── Step 2: branch ────────────────────────────────────────────────────
        if step == "await_branch":
            branch = text.strip() or "main"
            db.upsert_user(chat_id, branch=branch, onboard_step="await_token")
            await self.send_message(
                chat_id,
                f"{self.EMOJI['check']} Branch: `{branch}`\n\n"
                f"{self.EMOJI['key']} *Step 3 of 5 — GitHub Personal Access Token*\n\n"
                "I need a token to read commits and perform rollbacks.\n\n"
                "📋 *How to create one:*\n"
                "1. GitHub → avatar (top-right) → *Settings*\n"
                "2. Left sidebar → *Developer settings*\n"
                "3. *Personal access tokens* → *Tokens (classic)*\n"
                "4. *Generate new token (classic)*\n"
                "5. Name it `CommitGuardian`, set expiry (90 days)\n"
                "6. Check these boxes:\n"
                "   ☑️ `repo` — full repo access\n"
                "   ☑️ `read:user` — read your username\n"
                "7. *Generate token* — copy it immediately!\n\n"
                "⚠️ _GitHub only shows it once._\n\n"
                "Paste your token:",
            )
            return True

        # ── Step 3: token ─────────────────────────────────────────────────────
        if step == "await_token":
            token = text.strip()
            if not (token.startswith("ghp_") or token.startswith("github_pat_")):
                await self.send_message(
                    chat_id,
                    f"{self.EMOJI['warning']} Doesn't look right "
                    "(should start with `ghp_` or `github_pat_`).\nTry again:",
                )
                return True
            db.upsert_user(chat_id, github_token=token, onboard_step="await_timeout_hours")
            await self.send_message(
                chat_id,
                f"{self.EMOJI['check']} Token saved.\n\n"
                f"{self.EMOJI['timeout']} *Step 4 of 5 — Review Timeout*\n\n"
                "If you don't respond to a review, how many hours before I auto-act?\n\n"
                "Common choices:\n"
                "• `6` — half a day\n"
                "• `24` — one day _(recommended)_\n"
                "• `48` — two days\n"
                "• `0` — never auto-act\n\n"
                "Send a number:",
            )
            return True

        # ── Step 4: timeout hours ─────────────────────────────────────────────
        if step == "await_timeout_hours":
            try:
                hours = int(text.strip())
                if hours < 0:
                    raise ValueError
            except ValueError:
                await self.send_message(
                    chat_id,
                    f"{self.EMOJI['warning']} Please send a whole number (e.g. `24`). "
                    "Send `0` to disable auto-action.",
                )
                return True

            db.upsert_user(chat_id, timeout_hours=hours, onboard_step="await_timeout_action")

            if hours == 0:
                # Skip action step — auto-action is disabled
                db.upsert_user(chat_id, timeout_action="none", onboard_step="done")
                await self._finish_onboarding(chat_id)
                return True

            await self.send_message(
                chat_id,
                f"{self.EMOJI['check']} Timeout: `{hours}` hours\n\n"
                f"{self.EMOJI['timeout']} *Step 5 of 5 — Timeout Action*\n\n"
                f"After `{hours}` hours with no response, what should I do?\n\n"
                "• Send `accept` — keep the commit in the repo _(safer, won't disrupt teammates)_\n"
                "• Send `decline` — automatically roll it back _(stricter, security-first)_",
            )
            return True

        # ── Step 5: timeout action ────────────────────────────────────────────
        if step == "await_timeout_action":
            action = text.strip().lower()
            if action not in ("accept", "decline"):
                await self.send_message(
                    chat_id,
                    f"{self.EMOJI['warning']} Please send exactly `accept` or `decline`.",
                )
                return True

            db.upsert_user(chat_id, timeout_action=action, onboard_step="done")
            await self._finish_onboarding(chat_id)
            return True

        return False

    async def _finish_onboarding(self, chat_id: str) -> None:
        user           = db.get_user(chat_id)
        webhook_secret = user["webhook_secret"]
        owner          = user["owner"]
        repo           = user["repo"]
        branch         = user["branch"]
        timeout_hours  = user["timeout_hours"]
        timeout_action = user["timeout_action"]
        webhook_url    = f"{CONFIG.public_url}/webhook/github/{chat_id}"

        if timeout_action == "none" or timeout_hours == 0:
            timeout_summary = "Disabled — reviews never auto-expire"
        else:
            timeout_summary = f"Auto-*{timeout_action}* after `{timeout_hours}` hours"

        await self.send_message(
            chat_id,
            f"{self.EMOJI['rocket']} *You're all set!*\n\n"
            f"*Summary:*\n"
            f"• Repo: `{owner}/{repo}`\n"
            f"• Branch: `{branch}`\n"
            f"• Timeout: {timeout_summary}\n\n"
            f"───────────────────────────\n"
            f"{self.EMOJI['link']} *Add the webhook to GitHub:*\n\n"
            f"1. Go to `{owner}/{repo}` → *Settings* → *Webhooks* → *Add webhook*\n"
            f"2. Fill in:\n"
            f"   • *Payload URL:* `{webhook_url}`\n"
            f"   • *Content type:* `application/json`\n"
            f"   • *Secret:* `{webhook_secret}`\n"
            f"   • *Which events?* → *Just the push event*\n"
            f"   • *Active* ✅\n"
            f"3. Click *Add webhook*\n\n"
            f"⚠️ _Copy the secret above now and delete this message after saving it._\n\nEvery push to `{branch}` will now appear here for review. 🎉\n\n"
            f"_Commands: /status · /settings · /start (reconfigure)_",
        )

    # ── /settings command ─────────────────────────────────────────────────────

    async def handle_settings(self, chat_id: str) -> None:
        """Show current settings with quick-change options."""
        user = db.get_user(chat_id)
        if not user or not db.is_setup_complete(chat_id):
            await self.send_message(chat_id, "You're not set up yet. Send /start to begin.")
            return

        timeout_action = user.get("timeout_action", "accept")
        timeout_hours  = user.get("timeout_hours", 24)

        if timeout_action == "none" or timeout_hours == 0:
            timeout_str = "Disabled"
        else:
            timeout_str = f"Auto-{timeout_action} after {timeout_hours}h"

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "⏰ Change timeout",        "callback_data": "cfg:timeout"},
                    {"text": "🔄 Full reconfigure",      "callback_data": "cfg:restart"},
                ],
            ]
        }

        await self.send_message(
            chat_id,
            f"⚙️ *Your Settings*\n\n"
            f"• Repo: `{user.get('owner')}/{user.get('repo')}`\n"
            f"• Branch: `{user.get('branch','main')}`\n"
            f"• Timeout: {timeout_str}\n\n"
            f"Webhook URL:\n`{CONFIG.public_url}/webhook/github/{chat_id}`",
            reply_markup=keyboard,
        )

    # ── Review request ────────────────────────────────────────────────────────

    async def send_review_request(
        self,
        chat_id: str,
        commit_metadata: Dict[str, Any],
        decision: CommitDecision,
    ) -> int:
        """Returns message_id or -1 if skipped."""
        if self._is_revert_commit(commit_metadata):
            reverted = self._extract_reverted_sha(commit_metadata)
            await self.send_message(
                chat_id,
                f"{self.EMOJI['rollback']} *Rollback confirmed*\n\n"
                f"Revert commit `{commit_metadata.get('sha','')[:7]}` applied.\n"
                f"_This reverted commit `{reverted}`. No review needed._",
            )
            return -1

        user          = db.get_user(chat_id) or {}
        timeout_hours = user.get("timeout_hours", 24)
        timeout_action = user.get("timeout_action", "accept")
        sha           = commit_metadata.get("sha", "unknown")
        files         = commit_metadata.get("files", [])
        stats         = commit_metadata.get("stats", {})

        files_summary = "\n".join(
            f"  `{f['filename']}` ({f['status']}, +{f['additions']}/-{f['deletions']})"
            for f in files[:15]
        )
        if len(files) > 15:
            files_summary += f"\n  … and {len(files) - 15} more"

        concerns  = "\n".join(f"  • {c}" for c in decision.concerns[:5])       or "  None"
        positives = "\n".join(f"  • {p}" for p in decision.positive_aspects[:5]) or "  None"

        if timeout_hours == 0 or timeout_action == "none":
            timeout_notice = "_No auto-action — this review won't expire._"
        else:
            timeout_notice = (
                f"_{self.EMOJI['timeout']} No response in *{timeout_hours}h* → "
                f"will auto-*{timeout_action}*._"
            )

        text = (
            f"{self.EMOJI['commit']} *New Commit Requires Review*\n\n"
            f"{self.EMOJI['user']} *Author:* {commit_metadata.get('author_name','Unknown')}"
            f" (`{commit_metadata.get('author_email','N/A')}`)\n"
            f"{self.EMOJI['clock']} *Committed:* {commit_metadata.get('committed_at','N/A')}\n"
            f"{self.EMOJI['commit']} *SHA:* `{sha[:7]}`\n"
            f"{self.EMOJI['files']} *Files:* {len(files)} changed  "
            f"(+{stats.get('additions',0)} / -{stats.get('deletions',0)} lines)\n\n"
            f"💬 *Message:*\n```\n{commit_metadata.get('message','')[:300]}\n```\n\n"
            f"———\n\n"
            f"{self.EMOJI['ai']} *AI Assessment*\n\n"
            f"{self._risk_emoji(decision.risk_level)} *Risk:* {decision.risk_level.upper()}\n"
            f"🎯 *Confidence:* {decision.confidence_score:.0%}\n"
            f"📋 *Suggestion:* {decision.suggested_action}\n\n"
            f"📝 *Summary:* {decision.summary}\n\n"
            f"⚠️ *Concerns:*\n{concerns}\n\n"
            f"✨ *Positives:*\n{positives}\n\n"
            f"———\n*Changed Files:*\n{files_summary}\n\n"
            f"{timeout_notice}\n\n"
            f"_Select an action:_"
        )

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": f"{self.EMOJI['accept']} Accept",               "callback_data": f"acc:{sha}"},
                    {"text": f"{self.EMOJI['decline']} Decline & Rollback",  "callback_data": f"dec:{sha}"},
                ],
                [
                    {"text": f"{self.EMOJI['report']} Transparency Report",  "callback_data": f"rep:{sha}"},
                ],
            ]
        }
        # Bug 3 fix: Telegram hard limit is 4096 chars. Truncate the body (not
        # the buttons) so the card always sends even on large commits.
        MAX_TELEGRAM = 4000  # leave headroom for markup overhead
        if len(text) > MAX_TELEGRAM:
            text = text[:MAX_TELEGRAM] + "\n\n_…message truncated_"

        return await self.send_message(chat_id, text, reply_markup=keyboard)

    # ── Button action handlers ─────────────────────────────────────────────────

    async def handle_accept(
        self, chat_id, callback_query_id, message_id,
        commit_metadata, decision, on_accept,
    ) -> None:
        sha = commit_metadata.get("sha", "N/A")[:7]
        await self.answer_callback(callback_query_id, f"✅ Commit {sha} accepted!")
        proc = await self.send_processing(chat_id, "✅ _Accepting commit…_")
        try:
            await on_accept()
        except Exception as exc:
            logger.error("Accept callback failed: %s", exc)
        finally:
            await self.delete_message(chat_id, proc)
        await self.edit_message(
            chat_id, message_id,
            f"✅ *COMMIT ACCEPTED*\n\n"
            f"`{sha}` — {commit_metadata.get('message','')[:80]}\n"
            f"by {commit_metadata.get('author_name','Unknown')}\n\n"
            f"{self._risk_emoji(decision.risk_level)} Risk: {decision.risk_level.upper()}\n"
            f"✅ Commit kept in repository\n\n"
            f"_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}_",
            reply_markup={"inline_keyboard": []},
        )

    async def handle_decline(
        self, chat_id, callback_query_id, message_id,
        commit_metadata, decision, on_decline,
    ) -> None:
        sha = commit_metadata.get("sha", "N/A")[:7]
        await self.answer_callback(callback_query_id, f"⏪ Rolling back {sha}…")
        proc = await self.send_processing(chat_id, f"⏪ _Rolling back `{sha}`…_")
        result: Dict[str, Any] = {}
        error_msg = None
        try:
            result = await on_decline()
        except Exception as exc:
            error_msg = str(exc)
        finally:
            await self.delete_message(chat_id, proc)

        if result.get("success"):
            text = (
                f"❌ *COMMIT DECLINED & ROLLED BACK*\n\n"
                f"`{sha}` — {commit_metadata.get('message','')[:80]}\n"
                f"by {commit_metadata.get('author_name','Unknown')}\n\n"
                f"{self._risk_emoji(decision.risk_level)} Risk: {decision.risk_level.upper()}\n"
                f"⏪ Strategy: {result.get('strategy','unknown')}\n✅ Rolled back\n"
            )
            if result.get("revert_sha"):
                text += f"📝 Revert SHA: `{result['revert_sha'][:7]}`\n"
        else:
            text = (
                f"❌ *DECLINED — ROLLBACK FAILED*\n\n"
                f"`{sha}` — {commit_metadata.get('message','')[:80]}\n\n"
                f"❌ Error: {error_msg or result.get('message','Unknown')}\n\n"
                f"⚠️ Please rollback manually.\n{commit_metadata.get('url','')}"
            )
        await self.edit_message(chat_id, message_id, text, reply_markup={"inline_keyboard": []})

    async def handle_report(
        self, chat_id, callback_query_id, commit_metadata, decision,
    ) -> None:
        sha = commit_metadata.get("sha", "N/A")[:7]
        await self.answer_callback(callback_query_id, "Generating report…")
        proc = await self.send_processing(chat_id, f"📊 _Generating report for `{sha}`…_")

        try:
            repo_full    = commit_metadata.get("_repo_full_name", "")
            owner, repo  = repo_full.split("/", 1)
            user         = db.get_user(chat_id)
            from github_service import GitHubService
            gh           = GitHubService(token=user["github_token"]) if user else None
            repo_context = await gh.fetch_repo_context(owner, repo) if gh else {}
            report_text  = await ai_service.generate_transparency_report(commit_metadata, repo_context, decision)
            docx_path    = await self._build_report_docx(commit_metadata, decision, report_text)
            await self.delete_message(chat_id, proc)
            await self.send_document(
                chat_id, docx_path,
                caption=f"📊 Transparency Report — `{sha}` | {decision.risk_level.upper()} risk",
                filename=f"report_{sha}.docx",
            )
            try:
                os.unlink(docx_path)
            except OSError:
                pass
            if gh:
                await gh.close()
        except Exception as exc:
            logger.error("Report failed: %s", exc)
            await self.delete_message(chat_id, proc)
            await self.send_message(
                chat_id,
                f"⚠️ *Report Error*\n\n`{exc}`\n\n"
                f"*Basic:* {decision.decision.upper()} | {decision.risk_level.upper()} | "
                f"{decision.confidence_score:.0%}\n\n{decision.summary}",
            )

    # ── Word report builder ───────────────────────────────────────────────────

    async def _build_report_docx(self, commit_metadata, decision, report_text) -> str:
        """
        Build a .docx transparency report using python-docx (pure Python, no Node.js).
        Runs in a thread executor so it doesn't block the event loop.
        """
        loop = asyncio.get_event_loop()
        out_path = await loop.run_in_executor(
            None,
            lambda: self._build_report_docx_sync(commit_metadata, decision, report_text),
        )
        return out_path

    def _build_report_docx_sync(self, commit_metadata, decision, report_text) -> str:
        """Synchronous docx builder — called from a thread executor."""
        sha       = commit_metadata.get("sha", "unknown")[:7]
        author    = commit_metadata.get("author_name", "Unknown")
        email     = commit_metadata.get("author_email", "N/A")
        committed = commit_metadata.get("committed_at", "N/A")
        msg       = commit_metadata.get("message", "")[:300]
        files     = commit_metadata.get("files", [])[:20]
        generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        # ── Colour palette ────────────────────────────────────────────────────
        BLUE      = RGBColor(0x1F, 0x49, 0x7D)   # heading 1
        MID_BLUE  = RGBColor(0x2E, 0x75, 0xB6)   # heading 2
        GREY      = RGBColor(0x88, 0x88, 0x88)   # subtitle
        BLACK     = RGBColor(0x00, 0x00, 0x00)

        RISK_COLOUR = {
            "critical": RGBColor(0xC0, 0x00, 0x00),
            "high":     RGBColor(0xC5, 0x5A, 0x11),
            "medium":   RGBColor(0xBF, 0x8F, 0x00),
            "low":      RGBColor(0x37, 0x86, 0x10),
        }

        doc = Document()

        # ── Page margins (1 inch all round) ───────────────────────────────────
        for section in doc.sections:
            section.top_margin    = Inches(1)
            section.bottom_margin = Inches(1)
            section.left_margin   = Inches(1)
            section.right_margin  = Inches(1)

        # ── Default body style ────────────────────────────────────────────────
        style = doc.styles["Normal"]
        style.font.name = "Arial"
        style.font.size = Pt(11)

        # ── Helpers ───────────────────────────────────────────────────────────
        def h1(text: str):
            p = doc.add_heading(text, level=1)
            p.runs[0].font.color.rgb = BLUE
            p.runs[0].font.name      = "Arial"
            p.runs[0].font.size      = Pt(16)

        def h2(text: str):
            p = doc.add_heading(text, level=2)
            p.runs[0].font.color.rgb = MID_BLUE
            p.runs[0].font.name      = "Arial"
            p.runs[0].font.size      = Pt(13)

        def kv(key: str, value: str, value_colour: RGBColor = BLACK):
            """Bold key followed by plain value on the same line."""
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(4)
            run_k = p.add_run(f"{key}: ")
            run_k.bold           = True
            run_k.font.name      = "Arial"
            run_k.font.size      = Pt(11)
            run_v = p.add_run(value)
            run_v.font.name      = "Arial"
            run_v.font.size      = Pt(11)
            run_v.font.color.rgb = value_colour

        def bullet(text: str):
            p = doc.add_paragraph(text, style="List Bullet")
            p.runs[0].font.name = "Arial"
            p.runs[0].font.size = Pt(11)

        def body(text: str):
            p = doc.add_paragraph(text)
            if p.runs:
                p.runs[0].font.name = "Arial"
                p.runs[0].font.size = Pt(11)
            p.paragraph_format.space_after = Pt(6)

        def spacer():
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(4)

        # ── Title block ───────────────────────────────────────────────────────
        title_p = doc.add_paragraph()
        title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title_run = title_p.add_run("Commit Guardian — Transparency Report")
        title_run.bold           = True
        title_run.font.name      = "Arial"
        title_run.font.size      = Pt(20)
        title_run.font.color.rgb = BLUE

        sub_p = doc.add_paragraph()
        sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sub_run = sub_p.add_run(f"Generated {generated}")
        sub_run.italic           = True
        sub_run.font.name        = "Arial"
        sub_run.font.size        = Pt(9)
        sub_run.font.color.rgb   = GREY
        spacer()

        # ── Commit Details ────────────────────────────────────────────────────
        h1("Commit Details")
        kv("SHA",       sha)
        kv("Author",    f"{author} <{email}>")
        kv("Committed", committed)
        kv("Message",   msg)
        spacer()

        # ── AI Assessment ─────────────────────────────────────────────────────
        h1("AI Assessment")
        risk_col = RISK_COLOUR.get(decision.risk_level.lower(), BLACK)
        kv("Decision",   decision.decision.upper())
        kv("Risk Level", decision.risk_level.upper(), value_colour=risk_col)
        kv("Confidence", f"{decision.confidence_score:.0%}")
        kv("Summary",    decision.summary)
        spacer()

        # ── Concerns ──────────────────────────────────────────────────────────
        if decision.concerns:
            h2("Concerns")
            for c in decision.concerns[:10]:
                bullet(c)
            spacer()

        # ── Positive Aspects ──────────────────────────────────────────────────
        if decision.positive_aspects:
            h2("Positive Aspects")
            for p_item in decision.positive_aspects[:10]:
                bullet(p_item)
            spacer()

        # ── Recommendations ───────────────────────────────────────────────────
        if decision.recommendations:
            h2("Recommendations")
            for r in decision.recommendations[:10]:
                bullet(r)
            spacer()

        # ── Changed Files ─────────────────────────────────────────────────────
        h1("Changed Files")
        for f in files:
            fname   = f.get("filename", "unknown")
            status  = f.get("status", "")
            adds    = f.get("additions", 0)
            dels    = f.get("deletions", 0)
            body(f"{fname}  |  {status}  |  +{adds} / -{dels}")
        spacer()

        # ── Detailed Analysis ─────────────────────────────────────────────────
        h1("Detailed Analysis")
        for line in report_text.split("\n"):
            if line.strip():
                body(line)

        # ── Save ──────────────────────────────────────────────────────────────
        fd, out_path = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        doc.save(out_path)
        return out_path


telegram_service = TelegramService()
