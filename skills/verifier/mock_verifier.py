"""Stubbed verifier — v1 of the verifier primitive.

Returns deterministic mock claims so the orchestration layer can be tested
end-to-end. Designed to exercise both the remediation loop AND the gap-report
flow:

  - attempt 1: 5 claims, 3 pass + 1 fail + 1 unverifiable
                → triggers remediation AND emits a gap report
  - attempt 2: 5 claims, all pass
                → loop converges, returns "verified"

The real verifier (next milestone) will replace this module without changing
the public function signature. The orchestration layer is verifier-agnostic.
"""
from __future__ import annotations

import time
from typing import Any

from schemas import Claim, GapReport, Improvement, VerificationRecord


VERIFIER_MODEL = "mock-verifier-v1"


def verify(
    *,
    skill_name: str,
    skill_documentation: str,
    intent: str,
    builder_output: Any,
    attempt: int,
    strictness: str = "medium",
) -> VerificationRecord:
    """Run a verification pass.

    Public signature must remain stable — the real verifier will implement
    exactly this function and the orchestrator will not need to change.

    Args:
        skill_name: name of the skill being verified (used as skill_id in reports)
        skill_documentation: the skill's docs (for claim decomposition)
        intent: the user's plain-language goal
        builder_output: whatever the builder skill produced
        attempt: which builder invocation this is (1-indexed)
        strictness: low / medium / high — governs how many claims are emitted

    Returns:
        VerificationRecord — claims, aggregate status, optional gap report.
    """
    start_ms = int(time.time() * 1000)

    if attempt == 1:
        claims = _initial_claims(skill_name, intent, builder_output)
    else:
        claims = _remediated_claims(skill_name, intent, builder_output)

    status = _aggregate_status(claims)
    gap_report = _build_gap_report(skill_name, claims)

    duration_ms = max(1, int(time.time() * 1000) - start_ms)

    return VerificationRecord(
        status=status,
        claims=claims,
        verifier_model=VERIFIER_MODEL,
        duration_ms=duration_ms,
        cost_usd=0.0,
        gap_report=gap_report,
    )


# ---------- internal: claim generation ----------

def _initial_claims(skill_name: str, intent: str, builder_output: Any) -> list[Claim]:
    """Attempt 1 — deliberately mixed verdicts to exercise the remediation loop."""
    output_str = str(builder_output)
    return [
        Claim(
            id="claim_001",
            type="existential",
            statement=f"the output of {skill_name} is non-empty",
            evidence_required="output payload contains data",
            evidence_collected={"length": len(output_str)},
            verdict="pass",
            confidence=0.95,
            reasoning="output is a non-empty string",
        ),
        Claim(
            id="claim_002",
            type="structural",
            statement="the output is valid JSON",
            evidence_required="json.loads succeeds on the payload",
            evidence_collected={"parseable": True},
            verdict="pass",
            confidence=0.98,
            reasoning="output parses as JSON",
        ),
        Claim(
            id="claim_003",
            type="semantic",
            statement=f"the output addresses the user's intent: {intent}",
            evidence_required="output content reflects the intent's key entities",
            evidence_collected={
                "intent_keywords_found": ["test", "report"],
                "intent_keywords_missing": ["amount"],
            },
            verdict="fail",
            confidence=0.75,
            reasoning=(
                "the user's intent references an amount, but the output does not "
                "include any amount-related field"
            ),
        ),
        Claim(
            id="claim_004",
            type="factual",
            statement="the output is timestamped from today",
            evidence_required="timestamp field present and recent",
            evidence_collected=None,
            verdict="unverifiable",
            confidence=0.5,
            reasoning=(
                "the output has no timestamp field and the skill documentation "
                "does not specify whether one should be present"
            ),
        ),
        Claim(
            id="claim_005",
            type="negative",
            statement="no credentials or secrets appear in the output",
            evidence_required="scan for token-shaped strings; none found",
            evidence_collected={"tokens_found": 0},
            verdict="pass",
            confidence=0.99,
            reasoning="no API keys, tokens, or secret patterns detected",
        ),
    ]


def _remediated_claims(skill_name: str, intent: str, builder_output: Any) -> list[Claim]:
    """Attempt 2+ — the builder has remediated; all claims now pass."""
    output_str = str(builder_output)
    return [
        Claim(
            id="claim_001",
            type="existential",
            statement=f"the output of {skill_name} is non-empty",
            evidence_required="output payload contains data",
            evidence_collected={"length": len(output_str)},
            verdict="pass",
            confidence=0.95,
            reasoning="output is a non-empty string",
        ),
        Claim(
            id="claim_002",
            type="structural",
            statement="the output is valid JSON",
            evidence_required="json.loads succeeds on the payload",
            evidence_collected={"parseable": True},
            verdict="pass",
            confidence=0.98,
            reasoning="output parses as JSON",
        ),
        Claim(
            id="claim_003",
            type="semantic",
            statement=f"the output addresses the user's intent: {intent}",
            evidence_required="output content reflects the intent's key entities",
            evidence_collected={
                "intent_keywords_found": ["test", "report", "amount"],
            },
            verdict="pass",
            confidence=0.90,
            reasoning="the output now includes the amount value the intent requires",
        ),
        Claim(
            id="claim_004",
            type="factual",
            statement="the output is timestamped from today",
            evidence_required="timestamp field present and recent",
            evidence_collected={"timestamp_present": True, "is_today": True},
            verdict="pass",
            confidence=0.92,
            reasoning="timestamp field is present and within today's date range",
        ),
        Claim(
            id="claim_005",
            type="negative",
            statement="no credentials or secrets appear in the output",
            evidence_required="scan for token-shaped strings; none found",
            evidence_collected={"tokens_found": 0},
            verdict="pass",
            confidence=0.99,
            reasoning="no API keys, tokens, or secret patterns detected",
        ),
    ]


# ---------- internal: aggregation & gap report ----------

def _aggregate_status(claims: list[Claim]) -> str:
    """Apply the conservative aggregate rule from the design spec.

    verified  → all claims pass
    failed    → at least one claim fails
    partial   → no failures, but at least one unverifiable
    """
    if any(c.verdict == "fail" for c in claims):
        return "failed"
    if any(c.verdict == "unverifiable" for c in claims):
        return "partial"
    return "verified"


def _build_gap_report(skill_name: str, claims: list[Claim]) -> GapReport | None:
    """Build a gap report from any unverifiable claims in this pass.

    Note: gap reports fire whenever there are unverifiable claims, regardless
    of the aggregate status. Even on a "failed" run, the unverifiable claims
    represent real documentation gaps the user might want to fix.
    """
    unverifiable = [c for c in claims if c.verdict == "unverifiable"]
    if not unverifiable:
        return None

    improvements: list[Improvement] = []
    for c in unverifiable:
        improvements.append(
            Improvement(
                claim_id=c.id,
                target="documentation",
                proposed_text=(
                    f"Specify whether the output of {skill_name} includes a "
                    f"{c.type} field for: \"{c.statement}\". If yes, document "
                    "the field name and format. If no, document that it is "
                    "intentionally absent."
                ),
                rationale=c.reasoning,
                confidence=0.7,
            )
        )

    return GapReport(
        skill_id=skill_name,
        unverifiable_claims=unverifiable,
        proposed_improvements=improvements,
        summary=(
            f"{len(unverifiable)} claim(s) could not be verified because the "
            "skill documentation does not specify expectations. The proposed "
            "improvements below would make these verifiable on the next run."
        ),
    )
