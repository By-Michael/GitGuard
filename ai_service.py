"""
AI Analysis Service using Groq's Llama API.
Performs contextual risk assessment of GitHub commits with full project awareness.
Provides transparent, explainable decisions with detailed reporting.
"""

import json
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from config import CONFIG, logger


class AIServiceError(Exception):
    """Base exception for AI service errors."""
    pass


class AIAnalysisError(AIServiceError):
    """Raised when the AI analysis API call fails."""
    pass


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "confidence_score": self.confidence_score,
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

    def __init__(self) -> None:
        self.api_key = CONFIG.groq_api_key
        self.model = CONFIG.groq_model
        self.max_tokens = CONFIG.groq_max_tokens
        self.temperature = CONFIG.groq_temperature
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
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

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{self.GROQ_API_BASE}/chat/completions",
                    headers=self.headers,
                    json=payload,
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

            logger.info(
                "AI analysis complete — decision: %s, risk: %s, confidence: %.0f%%",
                decision.decision,
                decision.risk_level,
                decision.confidence_score * 100,
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
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{self.GROQ_API_BASE}/chat/completions",
                    headers=self.headers,
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


# Singleton instance
ai_service = AIService()