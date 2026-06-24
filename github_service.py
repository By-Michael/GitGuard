"""
GitHub service — multi-user edition.

GitHubService now takes a per-user token in its constructor instead of
reading from a global config. Everything else stays the same.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from config import CONFIG, logger


class GitHubServiceError(Exception):
    pass

class WebhookVerificationError(GitHubServiceError):
    pass

class GitHubAPIError(GitHubServiceError):
    pass

class RollbackError(GitHubServiceError):
    pass


class GitHubService:
    """Per-user GitHub API client."""

    GITHUB_API_BASE = "https://api.github.com"

    def __init__(self, token: str) -> None:
        self.token = token
        self.headers = {
            "Authorization": f"token {self.token}",
            "Accept":        "application/vnd.github.v3+json",
            "User-Agent":    "CommitGuardian/2.0",
        }
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(headers=self.headers, timeout=30.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ── Webhook verification ──────────────────────────────────────────────────

    @staticmethod
    def verify_webhook_signature(payload: bytes, signature_header: Optional[str], secret: str) -> bool:
        if not secret:
            logger.warning("No webhook secret configured — skipping verification")
            return True
        if not signature_header:
            raise WebhookVerificationError("X-Hub-Signature-256 header missing")
        expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        provided = signature_header.removeprefix("sha256=") if signature_header.startswith("sha256=") else signature_header
        if not hmac.compare_digest(f"sha256={expected}", f"sha256={provided}"):
            raise WebhookVerificationError("Webhook signature verification failed")
        return True

    # ── Commit metadata ───────────────────────────────────────────────────────

    async def fetch_commit_metadata(self, owner: str, repo: str, commit_sha: str) -> Dict[str, Any]:
        client = await self._get_client()
        url = f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{commit_sha}"
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GitHubAPIError(f"Failed to fetch commit {commit_sha}: {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise GitHubAPIError(f"Network error: {exc}") from exc

        data         = response.json()
        commit_data  = data.get("commit", {})
        author_info  = commit_data.get("author", {})
        gh_author    = data.get("author") or {}
        files        = data.get("files", [])

        return {
            "sha":              data.get("sha", commit_sha),
            "message":          commit_data.get("message", "No message"),
            "author_name":      author_info.get("name", "Unknown"),
            "author_email":     author_info.get("email", "unknown@example.com"),
            "author_username":  gh_author.get("login"),
            "committed_at":     commit_data.get("committer", {}).get("date"),
            "pushed_at":        datetime.now(timezone.utc).isoformat(),
            "url":              data.get("html_url", ""),
            "stats":            data.get("stats", {"additions": 0, "deletions": 0, "total": 0}),
            "files": [
                {
                    "filename":          f.get("filename"),
                    "status":            f.get("status"),
                    "additions":         f.get("additions", 0),
                    "deletions":         f.get("deletions", 0),
                    "patch":             f.get("patch", ""),
                    "previous_filename": f.get("previous_filename"),
                }
                for f in files
            ],
        }

    # ── Repo context ──────────────────────────────────────────────────────────

    async def fetch_repo_context(self, owner: str, repo: str, default_branch: str = "main") -> Dict[str, Any]:
        client  = await self._get_client()
        context: Dict[str, Any] = {
            "repository": f"{owner}/{repo}", "files": [], "tree": [],
            "readme": None, "languages": {}, "description": None, "topics": [],
        }

        try:
            r = await client.get(f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}")
            if r.status_code == 200:
                d = r.json()
                context.update({
                    "description":    d.get("description"),
                    "topics":         d.get("topics", []),
                    "default_branch": d.get("default_branch", default_branch),
                    "language":       d.get("language"),
                    "visibility":     d.get("visibility"),
                })
        except Exception as exc:
            logger.warning("Could not fetch repo metadata: %s", exc)

        try:
            r = await client.get(f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/languages")
            if r.status_code == 200:
                context["languages"] = r.json()
        except Exception:
            pass

        branch = context.get("default_branch", default_branch)
        try:
            r = await client.get(
                f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{branch}",
                params={"recursive": "1"},
            )
            if r.status_code == 200:
                all_files     = [i for i in r.json().get("tree", []) if i.get("type") == "blob"]
                filtered      = self._filter_files(all_files)[:CONFIG.max_context_files]
                context["tree"]  = [f.get("path") for f in filtered]
                context["files"] = await self._fetch_key_file_contents(client, owner, repo, filtered)
        except Exception as exc:
            logger.warning("Could not fetch repo tree: %s", exc)

        for name in ["README.md", "README.rst", "README.txt", "README"]:
            try:
                r = await client.get(f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{name}")
                if r.status_code == 200 and "content" in r.json():
                    context["readme"] = base64.b64decode(r.json()["content"]).decode("utf-8", errors="replace")[:10000]
                    break
            except Exception:
                continue

        return context

    def _filter_files(self, files: List[Dict]) -> List[Dict]:
        return [
            f for f in files
            if not any(p.strip() in f.get("path", "") for p in CONFIG.excluded_patterns)
            and f.get("size", 0) <= CONFIG.max_file_size_bytes
        ]

    async def _fetch_single_file(self, client, owner, repo, path) -> Optional[Dict]:
        try:
            r = await client.get(
                f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/contents/{path}",
                headers={**self.headers, "Accept": "application/vnd.github.v3.raw"},
            )
            if r.status_code == 200:
                content = r.text
                if len(content) > 8000:
                    content = content[:8000] + "\n… [truncated]"
                return {"path": path, "content": content}
        except Exception:
            pass
        return None

    async def _fetch_key_file_contents(self, client, owner, repo, files) -> List[Dict]:
        priority = [r"README", r"package\.json", r"requirements\.txt", r"pyproject\.toml",
                    r"Dockerfile", r"config", r"main\.", r"app\.", r"index\.", r"settings\."]
        scored   = sorted(files, key=lambda f: sum(1 for p in priority if re.search(p, f.get("path",""), re.I)), reverse=True)
        results  = await asyncio.gather(*[
            self._fetch_single_file(client, owner, repo, f.get("path",""))
            for f in scored[:20]
        ])
        return [r for r in results if r]

    async def fetch_changed_file_contents(self, owner: str, repo: str, files_changed: List[Dict]) -> List[Dict]:
        """Always fetch full content of files touched by the commit, regardless of name heuristics."""
        client = await self._get_client()
        results = await asyncio.gather(*[
            self._fetch_single_file(client, owner, repo, f["filename"])
            for f in files_changed[:10]
        ])
        return [r for r in results if r]

    # ── Rollback ──────────────────────────────────────────────────────────────

    async def rollback_commit(self, owner: str, repo: str, commit_sha: str, branch: str = "main") -> Dict[str, Any]:
        strategy = CONFIG.rollback_strategy
        if strategy == "revert":
            return await self._revert_commit(owner, repo, commit_sha, branch)
        elif strategy == "force_push":
            return await self._force_push_remove(owner, repo, commit_sha)
        else:
            raise RollbackError(f"Unknown strategy: {strategy}")

    async def _revert_commit(self, owner, repo, commit_sha, branch="main") -> Dict[str, Any]:
        client = await self._get_client()

        try:
            r = await client.get(f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/git/commits/{commit_sha}")
            r.raise_for_status()
            parents = r.json().get("parents", [])
            if not parents:
                raise RollbackError(f"Commit {commit_sha[:7]} has no parents — cannot revert.")
            parent_sha = parents[0]["sha"]
        except httpx.HTTPStatusError as exc:
            raise RollbackError(f"Failed to fetch commit: {exc.response.status_code}") from exc

        try:
            r = await client.get(f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/git/commits/{parent_sha}")
            r.raise_for_status()
            parent_tree_sha = r.json()["tree"]["sha"]
        except httpx.HTTPStatusError as exc:
            raise RollbackError(f"Failed to fetch parent tree: {exc.response.status_code}") from exc

        try:
            r = await client.get(f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs/heads/{branch}")
            r.raise_for_status()
            head_sha = r.json()["object"]["sha"]
        except httpx.HTTPStatusError as exc:
            raise RollbackError(f"Failed to fetch branch HEAD: {exc.response.status_code}") from exc

        try:
            r = await client.post(
                f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/git/commits",
                json={
                    "message": (
                        f"revert: rollback commit {commit_sha[:7]}\n\n"
                        "This commit was automatically rejected and reverted by CommitGuardian."
                    ),
                    "tree":    parent_tree_sha,
                    "parents": [head_sha],
                },
            )
            r.raise_for_status()
            new_sha = r.json()["sha"]
        except httpx.HTTPStatusError as exc:
            raise RollbackError(f"Failed to create revert commit: {exc.response.status_code} — {exc.response.text[:200]}") from exc

        try:
            r = await client.patch(
                f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs/heads/{branch}",
                json={"sha": new_sha, "force": False},
            )
            if r.status_code == 422:
                r = await client.patch(
                    f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs/heads/{branch}",
                    json={"sha": new_sha, "force": True},
                )
            r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RollbackError(f"Failed to update branch: {exc.response.status_code}") from exc

        return {"success": True, "strategy": "revert", "revert_sha": new_sha}

    async def _force_push_remove(self, owner, repo, commit_sha) -> Dict[str, Any]:
        client = await self._get_client()
        try:
            r = await client.get(f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/git/commits/{commit_sha}")
            r.raise_for_status()
            parents = r.json().get("parents", [])
            if not parents:
                raise RollbackError("No parents — cannot remove initial commit")
            parent_sha = parents[0]["sha"]
        except httpx.HTTPError as exc:
            raise RollbackError(f"Failed to fetch commit: {exc}") from exc

        try:
            r = await client.get(f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}")
            r.raise_for_status()
            default_branch = r.json().get("default_branch", "main")
        except httpx.HTTPError as exc:
            raise RollbackError(f"Failed to fetch repo: {exc}") from exc

        try:
            r = await client.patch(
                f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/git/refs/heads/{default_branch}",
                json={"sha": parent_sha, "force": True},
            )
            r.raise_for_status()
            return {"success": True, "strategy": "force_push", "new_sha": parent_sha}
        except httpx.HTTPStatusError as exc:
            raise RollbackError(f"Force-push failed: {exc.response.status_code}") from exc
