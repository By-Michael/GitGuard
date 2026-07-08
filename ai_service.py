"""
AI Analysis Service using Groq's Llama API.
Performs contextual risk assessment of GitHub commits with full project awareness.
Provides transparent, explainable decisions with detailed reporting.
"""

import asyncio
import json
import os
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from config import CONFIG, logger
from http_utils import DEFAULT_LIMITS, LONG_READ_TIMEOUT, request_with_retry
from exceptions import AIServiceError, AIAnalysisError  # noqa: F401 (Phase 4 shared hierarchy)
from risk_guardrails import scan_commit, scan_repo_files, risk_level_to_band, apply_guardrails


@dataclass
class CommitDecision:
    """Structured decision from the AI analysis."""
    
    decision: str = "review"  # "accept", "decline", or "review"
    confidence_score: float = 0.5  # 0.0 to 1.0
    risk_level: str = "medium"  # "low", "medium", "high", "critical"
    summary: str = ""
    reasoning: List[str] = field(default_factory=list)
    concerns: List[str] = field(default_factory=list)
    positive_aspects: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    suggested_action: str = ""
    transparency_report: str = ""  # Detailed explanation for end users
    raw_response: str = ""  # Full raw AI response for debugging

    # Phase 4 scoring fix: confidence_score is the MODEL'S confidence in its
    # own verdict — it is not a safety/risk score and was previously the
    # only number shown to users, which is what produced misleading reads
    # like "80/100" on a structurally dangerous one-character change.
    # safety_score is deterministic-anchored (see risk_guardrails.py): its
    # band is fixed by risk_level, so it can never disagree with the risk
    # label shown next to it.
    safety_score: int = 50
    guardrail_triggered: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "confidence_score": self.confidence_score,
            "safety_score": self.safety_score,
            "guardrail_triggered": self.guardrail_triggered,
            "risk_level": self.risk_level,
            "summary": self.summary,
            "reasoning": self.reasoning,
            "concerns": self.concerns,
            "positive_aspects": self.positive_aspects,
            "recommendations": self.recommendations,
            "suggested_action": self.suggested_action,
        }


class AIService:
    """
    Groq Llama API integration for contextual commit analysis.
    
    The AI receives:
    - Full commit metadata (who, when, what changed, diff)
    - Entire project context (file tree, README, key files)
    - Instructions to assess risk with full codebase awareness
    """

    GROQ_API_BASE = "https://api.groq.com/openai/v1"

    # Fix #8: cap concurrent Groq calls to avoid hitting rate limits when many
    # users push simultaneously.
    _semaphore = asyncio.Semaphore(5)

    def __init__(self) -> None:
        self.api_key = CONFIG.groq_api_key
        self.model = CONFIG.groq_model
        self.max_tokens = CONFIG.groq_max_tokens
        self.temperature = CONFIG.groq_temperature
        # Fix (Phase 2): reuse one pooled client across all Groq calls instead
        # of opening a fresh TLS connection per request. Every call site below
        # used to do `async with httpx.AsyncClient(...) as client:` — under
        # concurrent commit processing that meant N simultaneous TCP+TLS
        # handshakes to the same host instead of N requests sharing a
        # keep-alive pool.
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=LONG_READ_TIMEOUT, limits=DEFAULT_LIMITS)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    def _get_headers(self) -> Dict[str, str]:
        """
        Fix #24: re-read the key from the environment on every request so a
        rotated key takes effect without restarting the server.
        """
        key = os.getenv("GROQ_API_KEY", self.api_key)
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def _build_system_prompt(self) -> str:
        """Build the system prompt that defines the AI's role and expected output format."""
        return textwrap.dedent("""\
            You are CommitGuardian, an expert code review AI that analyzes GitHub commits for risk assessment.
            
            YOUR CAPABILITIES:
            - You understand the ENTIRE project context including its architecture, dependencies, and code patterns
            - You analyze diffs with full awareness of how they fit into the broader codebase
            - You assess security risks, breaking changes, code quality, and architectural alignment
            - You provide transparent, explainable decisions that developers can trust
            
            RISK ASSESSMENT CRITERIA:
            - LOW: Minor changes (docs, comments, formatting), obvious fixes, test additions
            - MEDIUM: Refactoring, dependency updates, moderate feature additions
            - HIGH: Security-sensitive code, authentication/authorization changes, database migrations, API changes
            - CRITICAL: Changes to CI/CD, secrets management, permission systems, or anything that could cause data loss/security breaches
            
            DECISION GUIDELINES:
            - "accept": The commit is clearly safe and beneficial
            - "decline": The commit has critical issues, security risks, or fundamentally breaks the project
            - "review": Uncertain — needs human review (always err on the side of caution)
            
            YOU MUST RESPOND WITH VALID JSON ONLY. No markdown, no explanations outside the JSON.
            Use this exact structure:
            {
                "decision": "accept|decline|review",
                "confidence_score": 0.0-1.0,
                "risk_level": "low|medium|high|critical",
                "summary": "One-sentence summary of the commit's nature and impact",
                "reasoning": ["Point 1", "Point 2", ...],
                "concerns": ["Concern 1", "Concern 2", ...],
                "positive_aspects": ["Good thing 1", ...],
                "recommendations": ["Suggestion 1", ...],
                "suggested_action": "What should be done with this commit (accept/decline/review)?",
                "transparency_report": "A detailed, easy-to-understand explanation of this commit for a non-technical or busy stakeholder. Explain what changed, why it matters, what the risks are, and what the recommended action is. Use clear language, avoid jargon where possible, and be specific about what files were touched and what could go wrong. Write 3-5 paragraphs."
            }
            
            IMPORTANT RULES:
            1. Always base your analysis on BOTH the commit changes AND the project context
            2. Consider how the changes interact with existing code patterns
            3. Flag any changes to security-critical files (auth, secrets, payment, user data)
            4. Consider the author's history if available — new contributors need more scrutiny
            5. Be transparent about WHY you made your decision — never be vague
            6. The transparency_report must be written for a human who wants to understand the commit deeply
            7. If you see anything suspicious (backdoors, credential leaks, malicious code), set decision to "decline" and risk_level to "critical"
        """)

    def _build_analysis_prompt(
        self, commit_metadata: Dict[str, Any], repo_context: Dict[str, Any]
    ) -> str:
        """Build the user prompt containing all commit and repository data."""
        
        # Format commit info
        files_info = []
        for f in commit_metadata.get("files", []):
            file_entry = (
                f"File: {f['filename']} | Status: {f['status']} | "
                f"+{f['additions']}/-{f['deletions']} lines"
            )
            if f.get("patch"):
                patch = f["patch"][:2000]  # Limit patch size per file
                if len(f["patch"]) > 2000:
                    patch += "\n... [truncated]"
                file_entry += f"\n```diff\n{patch}\n```"
            files_info.append(file_entry)

        # Format repo context
        key_files_info = []
        for file_data in repo_context.get("files", [])[:10]:  # Top 10 most relevant files
            content_preview = file_data.get("content", "")[:1500]
            if len(file_data.get("content", "")) > 1500:
                content_preview += "\n... [truncated]"
            key_files_info.append(
                f"=== {file_data['path']} ===\n{content_preview}"
            )

        prompt = textwrap.dedent(f"""\
            # COMMIT ANALYSIS REQUEST

            ## COMMIT METADATA
            - SHA: {commit_metadata.get('sha', 'N/A')}
            - Author: {commit_metadata.get('author_name', 'Unknown')} ({commit_metadata.get('author_email', 'N/A')})
            - GitHub Username: {commit_metadata.get('author_username') or 'N/A'}
            - Committed At: {commit_metadata.get('committed_at', 'N/A')}
            - Push Time: {commit_metadata.get('pushed_at', 'N/A')}
            - Commit Message: {commit_metadata.get('message', 'No message')}
            - Total Changes: +{commit_metadata.get('stats', {}).get('additions', 0)} / -{commit_metadata.get('stats', {}).get('deletions', 0)} lines
            - URL: {commit_metadata.get('url', 'N/A')}

            ## CHANGED FILES ({len(commit_metadata.get('files', []))} files)
            {"\n\n".join(files_info) if files_info else "No file details available."}

            ## PROJECT CONTEXT
            - Repository: {repo_context.get('repository', 'N/A')}
            - Description: {repo_context.get('description') or 'N/A'}
            - Topics: {', '.join(repo_context.get('topics', [])) or 'N/A'}
            - Primary Language: {repo_context.get('language') or 'N/A'}
            - Languages: {json.dumps(repo_context.get('languages', {}), indent=2)}
            - Visibility: {repo_context.get('visibility', 'N/A')}

            ## PROJECT FILE TREE ({len(repo_context.get('tree', []))} files)
            {"\n".join(repo_context.get('tree', [])[:100])}

            ## KEY PROJECT FILES
            {"\n\n".join(key_files_info) if key_files_info else "No key files fetched."}

            ## README
            {repo_context.get('readme') or 'No README available.'}

            ---

            Analyze this commit with full awareness of the project context above.
            Consider the commit's changes in relation to the existing codebase.
            Respond ONLY with the JSON format specified in your instructions.
        """)

        return prompt

    async def analyze_commit(
        self,
        commit_metadata: Dict[str, Any],
        repo_context: Dict[str, Any],
    ) -> CommitDecision:
        """
        Send commit and repo context to Groq Llama for risk analysis.
        
        Args:
            commit_metadata: Dictionary from GitHubService.fetch_commit_metadata()
            repo_context: Dictionary from GitHubService.fetch_repo_context()
            
        Returns:
            CommitDecision with structured analysis results
        """
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_analysis_prompt(commit_metadata, repo_context)

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "response_format": {"type": "json_object"},
        }

        logger.info(
            "Sending analysis request to Groq — model: %s, prompt_tokens_est: ~%d",
            self.model,
            len(system_prompt) + len(user_prompt),
        )

        # Fix #8: limit concurrent Groq calls to avoid hammering the single
        # shared API key when many users push at once. Transient failures
        # (429 / 5xx / connection errors) are retried by request_with_retry;
        # any other HTTP error (4xx besides 429) fails immediately.
        async with self._semaphore:
            client = await self._get_client()
            try:
                response = await request_with_retry(
                    client, "POST", f"{self.GROQ_API_BASE}/chat/completions",
                    headers=self._get_headers(), json=payload,
                    max_attempts=3,
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                error_detail = ""
                try:
                    err_json = exc.response.json()
                    if isinstance(err_json, dict):
                        error_detail = err_json.get("error", {}).get("message", exc.response.text)
                    else:
                        error_detail = exc.response.text[:500]
                except Exception:
                    error_detail = exc.response.text[:500]
                raise AIAnalysisError(
                    f"Groq API error (HTTP {exc.response.status_code}): {error_detail}"
                ) from exc
            except httpx.RequestError as exc:
                raise AIAnalysisError(f"Network error connecting to Groq API: {exc}") from exc

        # Parse response
        try:
            response_data = response.json()
            raw_content = response_data["choices"][0]["message"]["content"]
            
            # Parse JSON from response
            try:
                parsed = json.loads(raw_content)
            except json.JSONDecodeError:
                # Try to extract JSON from markdown code block
                import re
                json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw_content, re.DOTALL)
                if json_match:
                    parsed = json.loads(json_match.group(1))
                else:
                    raise AIAnalysisError(f"AI returned non-JSON response: {raw_content[:500]}")

            # Some models wrap the response in a list even with json_object format — unwrap it
            if isinstance(parsed, list):
                logger.warning("AI returned a JSON list instead of object — unwrapping first element")
                parsed = parsed[0] if parsed else {}
            if not isinstance(parsed, dict):
                raise AIAnalysisError(f"AI returned unexpected JSON type: {type(parsed).__name__}")

            decision = CommitDecision(
                decision=parsed.get("decision", "review").lower().strip(),
                confidence_score=float(parsed.get("confidence_score", 0.5)),
                risk_level=parsed.get("risk_level", "medium").lower().strip(),
                summary=parsed.get("summary", "No summary provided"),
                reasoning=parsed.get("reasoning", []),
                concerns=parsed.get("concerns", []),
                positive_aspects=parsed.get("positive_aspects", []),
                recommendations=parsed.get("recommendations", []),
                suggested_action=parsed.get("suggested_action", "Manual review recommended"),
                transparency_report=parsed.get("transparency_report", ""),
                raw_response=raw_content,
            )

            # Validate decision value
            if decision.decision not in ("accept", "decline", "review"):
                logger.warning("AI returned unexpected decision '%s', defaulting to 'review'", decision.decision)
                decision.decision = "review"

            # Clamp confidence score
            decision.confidence_score = max(0.0, min(1.0, decision.confidence_score))

            # Phase 4 scoring fix: run deterministic guardrails over the raw
            # patches (not the model's summary of them) and recompute a
            # safety_score whose band is locked to risk_level. This can only
            # escalate risk/decline, never soften what the model said.
            apply_guardrails(decision, commit_metadata)
            if decision.guardrail_triggered:
                logger.warning(
                    "Guardrails escalated commit %s — risk now %s, safety_score %d",
                    commit_metadata.get("sha", "?")[:7], decision.risk_level, decision.safety_score,
                )

            logger.info(
                "AI analysis complete — decision: %s, risk: %s, confidence: %.0f%%, safety_score: %d",
                decision.decision,
                decision.risk_level,
                decision.confidence_score * 100,
                decision.safety_score,
            )
            return decision

        except (KeyError, json.JSONDecodeError, ValueError) as exc:
            raise AIAnalysisError(f"Failed to parse AI response: {exc}") from exc

    async def generate_transparency_report(
        self,
        commit_metadata: Dict[str, Any],
        repo_context: Dict[str, Any],
        original_decision: CommitDecision,
    ) -> str:
        """
        Generate an expanded, user-friendly transparency report on demand.
        This is called when the user clicks the "Transparency Report" button.
        """
        # If the original analysis already has a good report, return it expanded
        if original_decision.transparency_report and len(original_decision.transparency_report) > 200:
            base_report = original_decision.transparency_report
        else:
            base_report = "No previous transparency data available."

        # Build a focused prompt for an even more detailed report
        files_changed = "\n".join(
            f"- {f['filename']} ({f['status']}, +{f['additions']}/-{f['deletions']})"
            for f in commit_metadata.get("files", [])
        )

        prompt = textwrap.dedent(f"""\
            Provide a comprehensive, detailed transparency report for this GitHub commit.
            Write it as if explaining to a tech-savvy project manager or senior developer
            who wants to deeply understand the implications of this change.

            COMMIT: {commit_metadata.get('sha', 'N/A')[:7]}
            Author: {commit_metadata.get('author_name', 'Unknown')} ({commit_metadata.get('author_email', 'N/A')})
            When: {commit_metadata.get('committed_at', 'N/A')}
            Message: {commit_metadata.get('message', 'No message')}

            FILES CHANGED:
            {files_changed}

            AI INITIAL ASSESSMENT:
            - Decision: {original_decision.decision.upper()}
            - Risk Level: {original_decision.risk_level.upper()}
            - Confidence: {original_decision.confidence_score:.0%}
            - Summary: {original_decision.summary}

            CONCERNS: {"; ".join(original_decision.concerns) or "None identified"}
            POSITIVE ASPECTS: {"; ".join(original_decision.positive_aspects) or "None identified"}

            Write a thorough 5-8 paragraph report covering:
            1. What exactly this commit does (in plain English)
            2. Why it matters to the project
            3. Specific risks and how they could manifest
            4. What would happen if this commit is accepted vs declined
            5. Code quality observations
            6. Final recommendation with clear justification
            
            Be specific, cite filenames, and be honest about uncertainties.
        """)

        try:
            client = await self._get_client()
            response = await request_with_retry(
                client, "POST", f"{self.GROQ_API_BASE}/chat/completions",
                headers=self._get_headers(),
                json={
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are an expert technical writer specializing in clear, honest commit analysis reports. You explain complex code changes in accessible but technically accurate language. You never sugarcoat risks and always back claims with specific references to files or code patterns.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": self.max_tokens,
                    "temperature": 0.4,
                },
            )
            response.raise_for_status()

            report = response.json()["choices"][0]["message"]["content"]
            logger.info("Generated expanded transparency report (~%d chars)", len(report))
            return report

        except Exception as exc:
            logger.error("Failed to generate expanded transparency report: %s", exc)
            # Return the base report as fallback
            return (
                f"**TRANSPARENCY REPORT**\n\n"
                f"{base_report}\n\n"
                f"---\n"
                f"*Note: Expanded report generation encountered an error: {exc}*\n\n"
                f"**AI Decision:** {original_decision.decision.upper()}\n"
                f"**Risk Level:** {original_decision.risk_level.upper()}\n"
                f"**Confidence:** {original_decision.confidence_score:.0%}\n\n"
                f"**Reasoning:**\n"
                + "\n".join(f"- {r}" for r in original_decision.reasoning)
            )


    # ── Full codebase analysis ─────────────────────────────────────────────────

    async def analyze_full_codebase(
        self,
        repo_context: Dict[str, Any],
        reviews: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """
        Analyse the entire codebase for security, quality, and progress.
        Accepts a repo_context dict and all historical review records.
        Returns a structured dict with sections ready for docx generation.

        Token-budget strategy:
        - File tree: max 200 paths, each trimmed to basename for structure overview
        - File contents: up to 10 files, 1 500 chars each (already pre-trimmed by fetch_repo_context)
        - Review history: last 30 entries, decision + risk only (no raw diffs)
        - README: 3 000 chars max
        """

        # -- summarise historical reviews with tiny footprint --
        # Also tally authoritative counts locally (Phase 4 fix): previously
        # the "progress" numbers in the report were whatever the LLM chose
        # to restate from this same text, which it can miscount or round —
        # especially past ~15-20 line items. We now compute them here and
        # overwrite the model's numbers after the call, so the report's
        # figures are always exactly right regardless of what the model says.
        review_summary_lines: List[str] = []
        local_progress = {"total_commits_reviewed": 0, "accepted": 0, "declined": 0,
                           "pending": 0, "high_risk_commits": 0}
        for r in (reviews or [])[-30:]:
            try:
                dec = json.loads(r.get("decision_json") or "{}")
            except Exception:
                dec = {}
            status = r.get("status", "?")
            risk = (dec.get("risk_level") or "").lower()
            local_progress["total_commits_reviewed"] += 1
            if status == "accepted":
                local_progress["accepted"] += 1
            elif status == "declined":
                local_progress["declined"] += 1
            else:
                local_progress["pending"] += 1
            if risk in ("high", "critical"):
                local_progress["high_risk_commits"] += 1
            review_summary_lines.append(
                f"SHA:{(r.get('commit_sha') or '')[:7]} "
                f"status:{status} "
                f"risk:{risk or '?'} "
                f"decision:{dec.get('decision','?')}"
            )

        # -- build compact file-tree (paths only, max 200) --
        tree_snippet = "\n".join((repo_context.get("tree") or [])[:200])

        # -- key file contents (already ≤8 000 chars each, take 10 files at 1 500 chars) --
        files_snippet_parts: List[str] = []
        for fd in (repo_context.get("files") or [])[:10]:
            content = (fd.get("content") or "")[:1500]
            files_snippet_parts.append(f"=== {fd.get('path','?')} ===\n{content}")
        files_snippet = "\n\n".join(files_snippet_parts)

        readme_snippet = (repo_context.get("readme") or "")[:3000]

        prompt = textwrap.dedent(f"""\
            You are auditing a software project. Respond ONLY with a JSON object using
            the exact schema below — no markdown, no preamble.

            REPOSITORY: {repo_context.get('repository','N/A')}
            LANGUAGE: {repo_context.get('language','N/A')}
            DESCRIPTION: {repo_context.get('description') or 'N/A'}
            TOPICS: {', '.join(repo_context.get('topics',[]))}

            FILE TREE (up to 200 paths):
            {tree_snippet}

            KEY FILE CONTENTS (up to 10 files, 1500 chars each):
            {files_snippet}

            README (first 3000 chars):
            {readme_snippet}

            COMMIT REVIEW HISTORY (last 30 commits — status / risk / decision only):
            {chr(10).join(review_summary_lines) or 'No history yet.'}

            Produce a JSON object with these exact keys:
            {{
              "overall_health": "excellent|good|fair|poor|critical",
              "security": {{
                "score": 0-100,
                "summary": "2-3 sentence overview",
                "findings": ["finding 1", ...],
                "recommendations": ["rec 1", ...]
              }},
              "code_quality": {{
                "score": 0-100,
                "summary": "2-3 sentence overview",
                "strengths": ["...", ...],
                "weaknesses": ["...", ...]
              }},
              "architecture": {{
                "summary": "2-3 sentence overview",
                "patterns_detected": ["...", ...],
                "concerns": ["...", ...]
              }},
              "progress": {{
                "summary": "2-3 sentence overview",
                "total_commits_reviewed": <int>,
                "accepted": <int>,
                "declined": <int>,
                "pending": <int>,
                "high_risk_commits": <int>,
                "trend": "improving|stable|declining|insufficient_data"
              }},
              "dependencies": {{
                "summary": "1-2 sentences",
                "notable": ["...", ...]
              }},
              "executive_summary": "3-5 sentence non-technical overview for management",
              "top_recommendations": ["priority rec 1", "priority rec 2", "priority rec 3"]
            }}
        """)

        try:
            async with self._semaphore:
                client = await self._get_client()
                response = await request_with_retry(
                    client, "POST", f"{self.GROQ_API_BASE}/chat/completions",
                    headers=self._get_headers(),
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": "You are a senior software auditor. Always respond with valid JSON only, no markdown."},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 2000,
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
            result = json.loads(raw)

            # Phase 4 fix: numeric progress fields come from our local tally,
            # not the model's restatement of the same input.
            result.setdefault("progress", {})
            result["progress"].update(local_progress)
            result["progress"].setdefault("trend", "insufficient_data" if local_progress["total_commits_reviewed"] < 5 else "stable")

            # Phase 4 fix: deterministic secret scan over the actual file
            # contents, merged into the security section so a hardcoded
            # credential can't be missed just because the model didn't
            # flag it from a 1500-char preview.
            guardrail_flags = scan_repo_files(repo_context.get("files") or [])
            if guardrail_flags:
                result.setdefault("security", {})
                findings = list(result["security"].get("findings") or [])
                for gf in guardrail_flags:
                    findings.append(f"[guardrail] {gf.file}: {gf.message}")
                result["security"]["findings"] = findings
                # A confirmed hardcoded secret caps the security score —
                # the model's own score can't be higher than this.
                result["security"]["score"] = min(int(result["security"].get("score", 100) or 100), 20)
                if result.get("overall_health") in ("excellent", "good"):
                    result["overall_health"] = "poor"

            logger.info("Full codebase analysis complete — health: %s", result.get("overall_health"))
            return result
        except Exception as exc:
            logger.error("Full codebase analysis failed: %s", exc)
            raise AIAnalysisError(f"Codebase analysis failed: {exc}") from exc

    # ── Author performance analysis ────────────────────────────────────────────

    async def analyze_authors(
        self,
        reviews: List[Dict[str, Any]],
        repo_name: str,
    ) -> Dict[str, Any]:
        """
        Analyse per-author commit quality from stored review records.
        Builds minimal summary per author (no raw code) to stay within token limits.
        Returns structured dict ready for docx generation.
        """
        from collections import defaultdict

        # -- aggregate per-author stats locally (no extra API calls needed) --
        stats: Dict[str, Dict] = defaultdict(lambda: {
            "total": 0, "accepted": 0, "declined": 0, "pending": 0,
            "high_risk": 0, "critical_risk": 0, "low_risk": 0,
            "concerns_sample": [], "positives_sample": [],
            "messages": [],
        })

        for r in reviews:
            try:
                meta = json.loads(r.get("commit_meta_json") or "{}")
                dec  = json.loads(r.get("decision_json") or "{}")
            except Exception:
                continue
            name   = meta.get("author_name") or meta.get("_pusher_name") or "Unknown"
            email  = meta.get("author_email") or ""
            author_key = f"{name} <{email}>" if email else name

            s = stats[author_key]
            s["total"] += 1
            status = r.get("status", "pending")
            if status == "accepted":
                s["accepted"] += 1
            elif status == "declined":
                s["declined"] += 1
            else:
                s["pending"] += 1

            risk = (dec.get("risk_level") or "").lower()
            if risk in ("high", "critical"):
                s["high_risk"] += 1
            if risk == "critical":
                s["critical_risk"] += 1
            if risk == "low":
                s["low_risk"] += 1

            # sample at most 3 concerns and 3 positives per author
            for c in (dec.get("concerns") or [])[:2]:
                if len(s["concerns_sample"]) < 6:
                    s["concerns_sample"].append(c)
            for p in (dec.get("positive_aspects") or [])[:2]:
                if len(s["positives_sample"]) < 6:
                    s["positives_sample"].append(p)

            msg = (meta.get("message") or "")[:80]
            if len(s["messages"]) < 5:
                s["messages"].append(msg)

        if not stats:
            return {
                "repo": repo_name,
                "authors": [],
                "team_summary": "No commit history found to analyse.",
                "mvp": None,
                "needs_attention": None,
            }

        # -- build compact prompt --
        author_lines: List[str] = []
        for author, s in stats.items():
            decline_rate = round(s["declined"] / s["total"] * 100) if s["total"] else 0
            author_lines.append(
                f"AUTHOR: {author}\n"
                f"  total={s['total']} accepted={s['accepted']} declined={s['declined']} pending={s['pending']}\n"
                f"  high_risk_commits={s['high_risk']} critical={s['critical_risk']} low_risk={s['low_risk']}\n"
                f"  decline_rate={decline_rate}%\n"
                f"  concern_samples={s['concerns_sample'][:3]}\n"
                f"  positive_samples={s['positives_sample'][:3]}\n"
                f"  recent_messages={s['messages'][:3]}"
            )

        prompt = textwrap.dedent(f"""\
            You are evaluating developer performance based on commit review data for repo: {repo_name}.
            Respond ONLY with a JSON object using the exact schema below.

            RAW STATS PER AUTHOR:
            {chr(10).join(author_lines)}

            Produce a JSON object:
            {{
              "repo": "{repo_name}",
              "team_summary": "2-3 sentence team-wide overview",
              "mvp": "author name of top performer (or null)",
              "needs_attention": "author name who needs most improvement (or null)",
              "authors": [
                {{
                  "name": "Full Name <email>",
                  "total_commits": <int>,
                  "accepted": <int>,
                  "declined": <int>,
                  "pending": <int>,
                  "high_risk_commits": <int>,
                  "decline_rate_pct": <int>,
                  "performance_rating": "excellent|good|average|below_average|poor",
                  "strengths": ["...", ...],
                  "concerns": ["...", ...],
                  "verdict": "1-2 sentence honest assessment"
                }},
                ...
              ]
            }}

            Rules:
            - Be honest and specific — this is an internal team review.
            - performance_rating should reflect decline_rate AND commit quality signals.
            - A high decline_rate (>40%) = at minimum 'below_average'.
            - Authors with 0 declined commits and good quality signals = 'excellent' or 'good'.
            - Include ALL authors from the stats above.
        """)

        try:
            async with self._semaphore:
                client = await self._get_client()
                response = await request_with_retry(
                    client, "POST", f"{self.GROQ_API_BASE}/chat/completions",
                    headers=self._get_headers(),
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": "You are a senior engineering manager. Respond with valid JSON only, no markdown."},
                            {"role": "user", "content": prompt},
                        ],
                        "max_tokens": 2000,
                        "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
            result = json.loads(raw)

            # Phase 4 fix: the model restates total/accepted/declined/etc.
            # per author from the same stats we already computed exactly —
            # it can transpose or miscount, especially with several authors
            # in one prompt. Overwrite its numbers with the authoritative
            # local tally so the report can never show wrong figures; only
            # the qualitative fields (rating, strengths, concerns, verdict)
            # stay AI-generated.
            for author_entry in result.get("authors", []):
                s = stats.get(author_entry.get("name"))
                if not s:
                    # try loose match in case the model normalised the name slightly
                    s = next((v for k, v in stats.items() if k.split(" <")[0] == (author_entry.get("name") or "").split(" <")[0]), None)
                if not s:
                    continue
                decline_rate = round(s["declined"] / s["total"] * 100) if s["total"] else 0
                author_entry["total_commits"] = s["total"]
                author_entry["accepted"] = s["accepted"]
                author_entry["declined"] = s["declined"]
                author_entry["pending"] = s["pending"]
                author_entry["high_risk_commits"] = s["high_risk"]
                author_entry["decline_rate_pct"] = decline_rate
                # keep the model's qualitative rating in sync with the hard rule
                # stated in the prompt, in case it didn't apply it consistently
                if decline_rate > 40 and author_entry.get("performance_rating") in ("excellent", "good", "average"):
                    author_entry["performance_rating"] = "below_average"

            # Attach local raw stats for the docx builder to use
            result["_raw_stats"] = dict(stats)
            logger.info("Author analysis complete — %d authors", len(result.get("authors", [])))
            return result
        except Exception as exc:
            logger.error("Author analysis failed: %s", exc)
            raise AIAnalysisError(f"Author analysis failed: {exc}") from exc


# Singleton instance
ai_service = AIService()