"""Remediation prompt builder.

Converts failed and unverifiable claims into a structured prompt that the
builder receives on its next attempt. Format follows the design spec §06.
"""
from __future__ import annotations

from typing import List

from schemas import Claim


def build_remediation_prompt(
    *,
    original_command: str,
    original_intent: str,
    failed_claims: List[Claim],
    unverifiable_claims: List[Claim],
) -> str:
    """Build a structured remediation prompt from claim verdicts.

    The output is a multi-line string. The builder is expected to read it via
    the REMEDIATION_PROMPT environment variable and use it to guide the next
    attempt.
    """
    lines: List[str] = [
        "Your previous output was reviewed by an independent verifier.",
        "",
    ]

    if failed_claims:
        lines.append("The following claims FAILED:")
        lines.append("")
        for c in failed_claims:
            lines.append(f"  - {c.statement}")
            lines.append(f"    Why it failed: {c.reasoning}")
            if c.evidence_collected is not None:
                lines.append(f"    Evidence the verifier saw: {c.evidence_collected}")
            lines.append("")

    if unverifiable_claims:
        lines.append("The following claims could NOT be verified:")
        lines.append("")
        for c in unverifiable_claims:
            lines.append(f"  - {c.statement}")
            lines.append(f"    Why not: {c.reasoning}")
            lines.append(
                "    To make this verifiable next time, include in the output the "
                "evidence required: " + c.evidence_required
            )
            lines.append("")

    lines.extend([
        "Run the skill again. Do not change the user's original request.",
        "Address each failed claim. For unverifiable claims, include the evidence",
        "the verifier would need to validate them.",
        "",
        f"Original command: {original_command}",
        f"Original intent: {original_intent}",
    ])

    return "\n".join(lines)
