"""
Verifier tool for the LangGraph agent-comms-with-verifier port.

MAPPING FROM ORIGINAL SYSTEM
=============================

Original: run_skill_verified.py
  - Wraps any builder with claim validation, remediation, and gap reporting
  - Extracts claims from builder output
  - Validates each claim against skill docs and intent
  - Attempts remediation for failed claims
  - Produces a VerificationRecord with gap report

LangGraph port:
  - This module provides `verify_output` as a LangGraph-compatible tool
  - It can be called directly by nodes (Beta, Gamma) after their builder step
  - Supports mock backend (default) for demo/testing, real LLM backends via env
  - Preserves the full claim taxonomy from the original system

CLAIM TAXONOMY
==============

The original verifier recognizes six claim types, each with different
validation strategies:

1. EXISTENTIAL  - "X exists" / "X is present"
   Validated by checking the output contains evidence of X.

2. STRUCTURAL   - "X has property Y" / "X contains Z"
   Validated by inspecting structure/schema of the output.

3. BEHAVIORAL   - "When X happens, Y results"
   Validated by tracing causal chains in the output.

4. FACTUAL      - "X equals 42" / "The date is 2024-01-01"
   Validated against skill docs or external reference.

5. SEMANTIC     - "X means Y" / "X is equivalent to Z"
   Validated by checking semantic equivalence.

6. NEGATIVE     - "X does not contain Y" / "X never Z"
   Validated by confirming absence — these are the hardest to verify
   and most likely to produce gap reports.

pip install: langgraph langchain-core
"""

from __future__ import annotations

import hashlib
import os
import time
from enum import Enum
from typing import Any

from state import VerificationRecord


# ---------------------------------------------------------------------------
# Claim types — preserved from the original system
# ---------------------------------------------------------------------------

class ClaimType(str, Enum):
    """The six claim types recognized by the verifier.

    Original: Defined in run_skill_verified.py's claim extraction phase.
    Each type has a different validation strategy and failure mode.
    """
    EXISTENTIAL = "existential"
    STRUCTURAL = "structural"
    BEHAVIORAL = "behavioral"
    FACTUAL = "factual"
    SEMANTIC = "semantic"
    NEGATIVE = "negative"


# ---------------------------------------------------------------------------
# Claim dataclass
# ---------------------------------------------------------------------------

class Claim:
    """A single claim extracted from builder output.

    Original: Internal structure in run_skill_verified.py's pipeline.
    """

    def __init__(
        self,
        text: str,
        claim_type: ClaimType,
        source_span: str = "",
        confidence: float = 1.0,
    ):
        self.text = text
        self.claim_type = claim_type
        self.source_span = source_span
        self.confidence = confidence
        self.status: str = "pending"  # pending | passed | failed | remediated
        self.failure_reason: str = ""
        self.remediation_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "claim_type": self.claim_type.value,
            "source_span": self.source_span,
            "confidence": self.confidence,
            "status": self.status,
            "failure_reason": self.failure_reason,
            "remediation_note": self.remediation_note,
        }


# ---------------------------------------------------------------------------
# Mock verifier backend
# ---------------------------------------------------------------------------

def _mock_extract_claims(output: str, intent: str) -> list[Claim]:
    """Extract claims from builder output using mock heuristics.

    Original: run_skill_verified.py calls an LLM to extract claims.
    This mock version uses simple text heuristics for demo purposes.

    In a real deployment, this would call the verifier LLM (ideally a
    different model family than the builder) with a prompt like:

        "Extract all verifiable claims from the following output.
         Classify each as existential, structural, behavioral,
         factual, semantic, or negative."
    """
    claims: list[Claim] = []

    # Simulate claim extraction based on output content
    sentences = [s.strip() for s in output.split(".") if s.strip()]
    for i, sentence in enumerate(sentences):
        lower = sentence.lower()

        # Classify by simple keyword heuristics
        if any(w in lower for w in ["exists", "present", "found", "available"]):
            ct = ClaimType.EXISTENTIAL
        elif any(w in lower for w in ["contains", "has", "includes", "property"]):
            ct = ClaimType.STRUCTURAL
        elif any(w in lower for w in ["when", "causes", "results", "triggers"]):
            ct = ClaimType.BEHAVIORAL
        elif any(w in lower for w in ["equals", "is", "was", "date", "number"]):
            ct = ClaimType.FACTUAL
        elif any(w in lower for w in ["means", "equivalent", "same as", "implies"]):
            ct = ClaimType.SEMANTIC
        elif any(w in lower for w in ["not", "never", "without", "absent", "no "]):
            ct = ClaimType.NEGATIVE
        else:
            ct = ClaimType.FACTUAL  # default

        claims.append(Claim(
            text=sentence,
            claim_type=ct,
            source_span=f"sentence_{i}",
            confidence=0.85 + (i % 3) * 0.05,  # mock confidence spread
        ))

    # Always produce at least one claim
    if not claims:
        claims.append(Claim(
            text=f"Output addresses intent: {intent}",
            claim_type=ClaimType.EXISTENTIAL,
            source_span="full_output",
            confidence=0.9,
        ))

    return claims


def _mock_validate_claim(
    claim: Claim,
    skill_docs: str,
    memory_hints: list[str],
) -> None:
    """Validate a single claim against skill docs and memory hints.

    Original: run_skill_verified.py validates each claim by calling
    the verifier LLM with the claim, skill docs, and any prior gap
    guidelines from memory.

    This mock version uses a deterministic hash to simulate pass/fail,
    with negative claims more likely to fail (matching real behavior).
    """
    # Use hash for deterministic mock results
    claim_hash = int(hashlib.md5(claim.text.encode()).hexdigest()[:8], 16)

    # Negative claims fail more often (realistic: absence is hard to verify)
    if claim.claim_type == ClaimType.NEGATIVE:
        fail_threshold = 0.4  # 40% of negative claims fail
    else:
        fail_threshold = 0.15  # 15% of other claims fail

    normalized = (claim_hash % 1000) / 1000.0

    # Check if any memory hint specifically flags this claim area
    hint_flagged = any(
        hint.lower() in claim.text.lower()
        for hint in memory_hints
    )
    if hint_flagged:
        fail_threshold *= 2  # Double failure rate for historically problematic areas

    if normalized < fail_threshold:
        claim.status = "failed"
        claim.failure_reason = (
            f"Mock validation failed for {claim.claim_type.value} claim. "
            f"In production, the verifier LLM would explain specifically "
            f"why this claim could not be confirmed against the skill docs."
        )
    else:
        claim.status = "passed"


def _mock_attempt_remediation(claim: Claim, skill_docs: str) -> bool:
    """Attempt to remediate a failed claim.

    Original: run_skill_verified.py asks the verifier LLM to suggest
    a correction, then re-validates. If the correction passes, the
    claim is marked as remediated.

    This mock version remediates ~60% of failed claims.
    """
    claim_hash = int(hashlib.md5(claim.text.encode()).hexdigest()[:8], 16)
    if (claim_hash % 10) < 6:  # 60% remediation success
        claim.status = "remediated"
        claim.remediation_note = (
            "Mock remediation applied. In production, the verifier "
            "would provide the specific correction made."
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Main verifier function
# ---------------------------------------------------------------------------

def verify_output(
    builder_output: str,
    skill: str,
    skill_docs: str,
    intent: str,
    memory_hints: list[str] | None = None,
    builder_model: str = "mock-builder",
    verifier_model: str | None = None,
) -> VerificationRecord:
    """Run verification on builder output and return a VerificationRecord.

    MAPPING FROM ORIGINAL:
    ======================

    This is the LangGraph equivalent of run_skill_verified.py's main
    entry point. The original function:

    1. Calls the builder to produce output
    2. Extracts claims from the output
    3. Validates each claim against skill docs
    4. Attempts remediation for failed claims
    5. Compiles gap report for unresolvable failures
    6. Returns VerificationRecord

    In this port, step 1 (calling the builder) happens in the node
    function before calling verify_output. This function handles
    steps 2-6.

    Args:
        builder_output: The text output from the builder LLM.
        skill: Name of the skill/builder that produced the output.
        skill_docs: Documentation for the skill, used as reference
                    during claim validation.
        intent: The original task intent, used to scope claim extraction.
        memory_hints: Prior gap guidelines from agent_memory.py's
                     claim_guidelines_for_skill(). These focus the
                     verifier on historically problematic areas.
        builder_model: Which model produced the output (for the record).
        verifier_model: Which model to use for verification. If None,
                       uses mock backend. Set VERIFIER_MODEL env var
                       for real deployments.

    Returns:
        VerificationRecord with full claim breakdown, gap report,
        cost, and duration.
    """
    if memory_hints is None:
        memory_hints = []

    # Resolve verifier model
    effective_verifier = verifier_model or os.environ.get(
        "VERIFIER_MODEL", "mock-verifier"
    )

    start_ms = int(time.time() * 1000)

    # --- Step 2: Extract claims ---
    # Original: LLM call to extract and classify claims
    if effective_verifier == "mock-verifier":
        claims = _mock_extract_claims(builder_output, intent)
    else:
        # In a real deployment, this would call the verifier LLM:
        #   from langchain_openai import ChatOpenAI  # cross-family
        #   verifier_llm = ChatOpenAI(model=effective_verifier)
        #   response = verifier_llm.invoke(extraction_prompt)
        #   claims = parse_claims(response)
        raise NotImplementedError(
            f"Real verifier backend '{effective_verifier}' requires "
            f"langchain_openai or langchain_anthropic. Set "
            f"VERIFIER_MODEL=mock-verifier for demo mode."
        )

    # --- Step 3: Validate each claim ---
    for claim in claims:
        _mock_validate_claim(claim, skill_docs, memory_hints)

    # --- Step 4: Remediation for failed claims ---
    failed_claims = [c for c in claims if c.status == "failed"]
    remediation_applied = False
    for claim in failed_claims:
        if _mock_attempt_remediation(claim, skill_docs):
            remediation_applied = True

    # --- Step 5: Compile gap report ---
    # Gaps are claims that failed AND could not be remediated.
    # These become memory hints for future verifications.
    gap_report: list[str] = []
    for claim in claims:
        if claim.status == "failed":
            gap_report.append(
                f"[{claim.claim_type.value}] {claim.text}: "
                f"{claim.failure_reason}"
            )

    # --- Step 6: Build VerificationRecord ---
    end_ms = int(time.time() * 1000)
    passed = sum(1 for c in claims if c.status in ("passed", "remediated"))
    failed = sum(1 for c in claims if c.status == "failed")

    # Determine overall status
    if failed == 0:
        status = "verified"
    elif passed > failed:
        status = "partial"
    else:
        status = "failed"

    record = VerificationRecord(
        skill=skill,
        status=status,
        claims_checked=len(claims),
        claims_passed=passed,
        claims_failed=failed,
        claim_details=[c.to_dict() for c in claims],
        remediation_applied=remediation_applied,
        gap_report=gap_report,
        cost_usd=0.0 if effective_verifier == "mock-verifier" else 0.003,
        duration_ms=end_ms - start_ms,
        builder_model=builder_model,
        verifier_model=effective_verifier,
    )

    return record


# ---------------------------------------------------------------------------
# LangGraph tool wrapper (for use with ToolNode or direct invocation)
# ---------------------------------------------------------------------------

def make_verifier_tool():
    """Create a LangGraph-compatible tool from the verifier.

    Usage in a node:
        verifier = make_verifier_tool()
        record = verifier.invoke({
            "builder_output": "...",
            "skill": "summarize",
            "skill_docs": "...",
            "intent": "Summarize the Q4 report",
        })

    Or bind to an LLM for agent-style invocation:
        llm_with_tools = llm.bind_tools([verifier])
    """
    from langchain_core.tools import tool

    @tool
    def run_verifier(
        builder_output: str,
        skill: str,
        skill_docs: str = "",
        intent: str = "",
        memory_hints: list[str] | None = None,
    ) -> dict:
        """Verify builder output by extracting and validating claims.

        Extracts claims from the builder output, validates each against
        the skill documentation and intent, attempts remediation for
        failures, and returns a verification record with gap report.

        Claim types: existential, structural, behavioral, factual,
        semantic, negative.
        """
        return dict(verify_output(
            builder_output=builder_output,
            skill=skill,
            skill_docs=skill_docs,
            intent=intent,
            memory_hints=memory_hints,
        ))

    return run_verifier
