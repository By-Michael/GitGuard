"""
Deterministic risk guardrails — Phase 4 scoring fix.

Why this exists
----------------
Before this module, EVERY risk judgment (risk_level, confidence_score) was
produced entirely by the LLM, from a *truncated* diff (patch capped at
2000 chars per file) and a *truncated* view of the repo. That has two
concrete, reported failure modes:

1. The model has no deterministic anchor. A structurally significant but
   textually tiny change — e.g. deleting the `?` from a `<?php` opening
   tag, which corrupts every line after it — reads to an LLM as "one
   character changed" and gets scored near the "trivial edit" end of the
   distribution (confidence/"safety" ~80/100) unless the model happens to
   reason carefully about PHP tag semantics from a truncated diff.
2. "confidence_score" is the model's confidence in ITS OWN verdict, not a
   risk/safety score — but it's the only number shown to the user, so a
   low-risk-but-uncertain commit and a high-risk-but-confidently-wrong
   commit can render identically.

This module runs cheap, deterministic pattern checks against the actual
diff text (not a summary of it) and produces a `GuardrailResult` that:
  - can only ever push risk UP, never down (it never overrides the model
    toward "safer" — false negatives from the model are corrected, false
    positives from this module just add an extra concern for a human to
    read)
  - is combined with the model's risk_level to compute a single
    `safety_score` that is *consistent* with risk_level by construction
    (see risk_level_to_band), instead of the model's free-floating
    confidence_score being shown as if it were a safety score.

This is intentionally simple regex/heuristic-based analysis, not a linter.
It exists to catch the class of "small diff, big blast radius" changes
that a token-truncated LLM call can miss, not to replace static analysis.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

# risk_level -> inclusive (low, high) score band. Bands are ordered and
# non-overlapping so that risk_level alone determines which band a commit
# falls in — the AI's confidence_score only moves the score *within* the
# band assigned to the (possibly guardrail-escalated) risk_level. This is
# what fixes the "score doesn't reflect the significance of the change"
# complaint: the score can no longer land in the 80s while risk_level says
# something scarier, because the band for a given risk_level caps it.
_RISK_BANDS: Dict[str, Tuple[int, int]] = {
    "low": (80, 100),
    "medium": (55, 79),
    "high": (25, 54),
    "critical": (0, 24),
}

_RISK_ORDER = ["low", "medium", "high", "critical"]


def risk_level_to_band(risk_level: str) -> Tuple[int, int]:
    return _RISK_BANDS.get((risk_level or "medium").lower().strip(), _RISK_BANDS["medium"])


def _escalate(current: str, candidate: str) -> str:
    """Return whichever of current/candidate is higher severity."""
    cur_i = _RISK_ORDER.index(current) if current in _RISK_ORDER else 1
    cand_i = _RISK_ORDER.index(candidate) if candidate in _RISK_ORDER else 1
    return _RISK_ORDER[max(cur_i, cand_i)]


@dataclass
class GuardrailFlag:
    severity: str  # "medium" | "high" | "critical"
    file: str
    message: str


@dataclass
class GuardrailResult:
    flags: List[GuardrailFlag] = field(default_factory=list)
    min_risk_level: str = "low"
    force_decline: bool = False

    @property
    def triggered(self) -> bool:
        return bool(self.flags)

    def concern_lines(self) -> List[str]:
        return [f"[guardrail:{f.severity}] {f.file}: {f.message}" for f in self.flags]


# ── Critical file / path patterns ───────────────────────────────────────────

_CRITICAL_PATH_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r"\.github/workflows/",
        r"(^|/)auth[a-z_]*\.(py|js|ts|php|go|rb|java)$",
        r"(^|/)permissions?\.(py|js|ts|php|go|rb|java)$",
        r"(^|/)secrets?\.(py|js|ts|php|go|rb|java|ya?ml|env)$",
        r"(^|/)payment",
        r"(^|/)migrations?/",
        r"(^|/)\.env($|\.)",
        r"(^|/)dockerfile$",
        r"(^|/)docker-compose\.ya?ml$",
    ]
]

# ── Structural / syntax-corruption signals in the raw patch text ───────────
# Each entry: (regex over the removed (`-`) line, description). These look
# for a *removed* line that opens/introduces a construct whose matching
# counterpart is NOT also being removed in the same patch — i.e. the edit
# is leaving something structurally unbalanced.
_TAG_CORRUPTION_CHECKS = [
    (re.compile(r"^-\s*<\?php\b"), "A PHP opening tag `<?php` was removed/altered — "
        "if the matching pattern wasn't re-added, this can corrupt every line "
        "after it (PHP fails to parse or leaks raw source to the client)."),
    (re.compile(r"^-.*\?>\s*$"), "A PHP closing tag `?>` was removed — verify the file "
        "still parses; a dangling opening tag can leak source or crash rendering."),
]

_SECRET_PATTERNS = [
    re.compile(r"(?i)aws_secret_access_key\s*=\s*['\"][A-Za-z0-9/+=]{20,}"),
    re.compile(r"(?i)-----BEGIN (RSA|EC|OPENSSH|DSA) PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"),
]

_SECURITY_DOWNGRADE_PATTERNS = [
    re.compile(r"^\+.*verify\s*=\s*False", re.IGNORECASE),
    re.compile(r"^\+.*DEBUG\s*=\s*True"),
    re.compile(r"^\+.*ssl_verify\s*[:=]\s*(False|0|off)", re.IGNORECASE),
    re.compile(r"^\+.*(disable|skip).{0,20}(auth|verification|signature)", re.IGNORECASE),
]


def _brace_delta(patch: str) -> Dict[str, int]:
    """
    Net change in bracket balance across the patch. A large non-zero delta
    on a small patch is a cheap signal that the edit may leave the file
    syntactically unbalanced — not proof (diffs are context-limited), just
    a nudge for the human reviewer.
    """
    delta = {"()": 0, "{}": 0, "[]": 0}
    for line in patch.splitlines():
        if not line or line[0] not in "+-":
            continue
        sign = 1 if line[0] == "+" else -1
        delta["()"] += sign * (line.count("(") - line.count(")"))
        delta["{}"] += sign * (line.count("{") - line.count("}"))
        delta["[]"] += sign * (line.count("[") - line.count("]"))
    return delta


def _scan_patch(filename: str, patch: str) -> List[GuardrailFlag]:
    flags: List[GuardrailFlag] = []
    if not patch:
        return flags

    for pattern in _CRITICAL_PATH_PATTERNS:
        if pattern.search(filename):
            flags.append(GuardrailFlag(
                severity="high", file=filename,
                message="Change touches a security/deploy-critical path.",
            ))
            break

    for line in patch.splitlines():
        for pattern, desc in _TAG_CORRUPTION_CHECKS:
            if pattern.match(line):
                flags.append(GuardrailFlag(severity="critical", file=filename, message=desc))
        for pattern in _SECRET_PATTERNS:
            if line.startswith("+") and pattern.search(line):
                flags.append(GuardrailFlag(
                    severity="critical", file=filename,
                    message="Line added that looks like a hardcoded credential/secret.",
                ))
        for pattern in _SECURITY_DOWNGRADE_PATTERNS:
            if pattern.match(line):
                flags.append(GuardrailFlag(
                    severity="high", file=filename,
                    message=f"Security control appears to be weakened/disabled: {line.strip()[:120]}",
                ))

    delta = _brace_delta(patch)
    for kind, val in delta.items():
        if abs(val) >= 3:
            flags.append(GuardrailFlag(
                severity="medium", file=filename,
                message=f"Patch leaves a net {kind} imbalance of {val} within the visible diff — "
                        "double-check the file still parses.",
            ))

    return flags


def scan_commit(commit_metadata: Dict[str, Any]) -> GuardrailResult:
    """Run guardrail checks over every file's patch in a commit."""
    result = GuardrailResult()
    for f in commit_metadata.get("files", []) or []:
        flags = _scan_patch(f.get("filename", "?"), f.get("patch", "") or "")
        result.flags.extend(flags)

    for flag in result.flags:
        if flag.severity == "critical":
            result.min_risk_level = _escalate(result.min_risk_level, "critical")
            result.force_decline = True
        elif flag.severity == "high":
            result.min_risk_level = _escalate(result.min_risk_level, "high")
        elif flag.severity == "medium":
            result.min_risk_level = _escalate(result.min_risk_level, "medium")

    return result


def scan_repo_files(files: List[Dict[str, Any]]) -> List[GuardrailFlag]:
    """
    Same idea applied to whole-file snapshots (used by Full Code Analysis,
    which sees file *contents*, not diffs) — here we just check for
    committed secrets, since there's no "removed line" concept for a
    snapshot.
    """
    flags: List[GuardrailFlag] = []
    for fd in files or []:
        content = fd.get("content", "") or ""
        path = fd.get("path", "?")
        for pattern in _SECRET_PATTERNS:
            if pattern.search(content):
                flags.append(GuardrailFlag(
                    severity="critical", file=path,
                    message="File appears to contain a hardcoded credential/secret.",
                ))
    return flags


def compute_safety_score(risk_level: str, ai_confidence: float) -> int:
    """
    Blend the (possibly guardrail-escalated) risk_level with the model's
    confidence into a single 0-100 score whose band is fixed by
    risk_level, so the number always agrees with the label shown next to
    it. `ai_confidence` only decides where within the band it lands.
    """
    low, high = risk_level_to_band(risk_level)
    ai_confidence = max(0.0, min(1.0, ai_confidence))
    return int(round(low + ai_confidence * (high - low)))


def apply_guardrails(decision, commit_metadata: Dict[str, Any]):
    """
    Mutates and returns `decision` (an ai_service.CommitDecision) after
    running guardrails and recomputing a consistent safety_score.
    Kept as a free function (rather than a CommitDecision method) so
    risk_guardrails.py has no dependency on ai_service.py — avoids a
    circular import since ai_service.py imports this module.
    """
    guardrail_result = scan_commit(commit_metadata)

    if guardrail_result.triggered:
        original_risk = decision.risk_level
        decision.risk_level = _escalate(decision.risk_level, guardrail_result.min_risk_level)
        decision.concerns = list(decision.concerns) + guardrail_result.concern_lines()
        if guardrail_result.force_decline and decision.decision == "accept":
            decision.decision = "decline"
        if decision.risk_level != original_risk:
            decision.reasoning = list(decision.reasoning) + [
                f"Risk level escalated from {original_risk} to {decision.risk_level} "
                f"by deterministic guardrail checks (see concerns)."
            ]

    decision.safety_score = compute_safety_score(decision.risk_level, decision.confidence_score)
    decision.guardrail_triggered = guardrail_result.triggered
    return decision
