"""Verifier primitive — data model.

Mirrors the design spec (Reference No. 02). These dataclasses are the contract
between the orchestrator (run_skill_verified.py) and any verifier implementation
(mock_verifier.py today, real LLM verifier later).

Keeping them in one place means: swapping the verifier is a drop-in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Literal, Optional


# Type aliases (documentation only — Python doesn't enforce at runtime).
ClaimType = Literal[
    "existential",   # asserts a thing exists
    "structural",    # asserts a thing has a shape
    "behavioral",    # asserts a thing does something
    "factual",       # asserts something is true about the world
    "semantic",      # asserts the output means what was asked
    "negative",      # asserts something did NOT happen
]
Verdict = Literal["pass", "fail", "unverifiable"]
RunStatus = Literal["verified", "failed", "partial"]
Strictness = Literal["low", "medium", "high"]
ImprovementTarget = Literal["documentation", "output_schema", "script"]


@dataclass
class Claim:
    """A single, independently testable assertion about the output."""
    id: str
    type: str                # ClaimType
    statement: str           # human-readable assertion
    evidence_required: str   # what would prove this true
    verdict: str             # Verdict
    confidence: float        # 0.0 - 1.0
    reasoning: str           # why the verifier chose this verdict
    evidence_collected: Optional[Any] = None


@dataclass
class Improvement:
    """A proposed change to the skill, derived from an unverifiable claim."""
    claim_id: str
    target: str              # ImprovementTarget
    proposed_text: str
    rationale: str
    confidence: float
    current_text: Optional[str] = None


@dataclass
class GapReport:
    """Packaged improvements emitted when claims are unverifiable.

    Flows into the existing UpdateSkillAndScripts draft-card UI for user review.
    """
    skill_id: str
    unverifiable_claims: List[Claim]
    proposed_improvements: List[Improvement]
    summary: str             # 1-2 sentences for the user


@dataclass
class VerificationRecord:
    """The verifier's output for a single verification pass."""
    status: str              # RunStatus
    claims: List[Claim]
    verifier_model: str
    duration_ms: int
    cost_usd: float
    gap_report: Optional[GapReport] = None


@dataclass
class RunSkillVerifiedResult:
    """The final result of a verified skill run, returned to the caller."""
    result: Any                          # builder's final output
    attempts: int                        # builder invocations used
    verification: VerificationRecord     # the LAST verification (final state)
    attempt_history: List[VerificationRecord] = field(default_factory=list)
    total_duration_ms: int = 0
    total_cost_usd: float = 0.0
