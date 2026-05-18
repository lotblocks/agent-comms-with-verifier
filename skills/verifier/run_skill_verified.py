#!/usr/bin/env python3
"""run_skill_verified.py — orchestration layer for the verifier primitive.

This is the v1 "tool wrapper" — it sits between the caller and the existing
skill execution flow, runs the builder, hands the output to a verifier, and
loops with structured remediation up to max_attempts.

The verifier is pluggable. v1 uses the deterministic mock in mock_verifier.py;
the real LLM verifier will be a drop-in replacement that implements the same
verify() signature.

Usage:
  python3 run_skill_verified.py \\
    --target-script /path/to/builder.py \\
    --target-args 'arg1 arg2' \\
    --skill-name my-skill \\
    --skill-doc "what the skill is supposed to do" \\
    --intent "the user's plain-language goal" \\
    --max-attempts 2 \\
    --strictness medium

Output: a JSON RunSkillVerifiedResult on stdout. Use --pretty for indented JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from typing import Any

# Ensure relative imports work whether run as a script or a module.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schemas import (
    Claim,
    GapReport,
    Improvement,
    RunSkillVerifiedResult,
    VerificationRecord,
)
import remediation


def _load_verifier(backend: str):
    """Pluggable verifier backend selection.

    Both backends implement the same verify() signature defined in schemas.
    Defaults to mock for deterministic tests; switch to llm for real claim
    decomposition. The CLI takes precedence; env var is the fallback.
    """
    if backend == "llm":
        import llm_verifier
        return llm_verifier
    if backend == "mock":
        import mock_verifier
        return mock_verifier
    raise ValueError(f"Unknown verifier backend: {backend!r} (expected 'mock' or 'llm')")


# ---------- subprocess: run the builder ----------

def run_target(
    target_script: str,
    target_args: str,
    remediation_prompt: str = "",
    timeout_sec: int = 60,
) -> str:
    """Run the target builder script as a subprocess and return its stdout.

    If a remediation prompt is supplied, it is passed via the REMEDIATION_PROMPT
    environment variable. Builders that opt into remediation should read this
    variable and adjust behavior accordingly.
    """
    env = os.environ.copy()
    if remediation_prompt:
        env["REMEDIATION_PROMPT"] = remediation_prompt
    else:
        env.pop("REMEDIATION_PROMPT", None)

    cmd = ["python3", target_script]
    if target_args:
        cmd.extend(target_args.split())

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return json.dumps({
            "_orchestrator_error": "target_script_timeout",
            "timeout_sec": timeout_sec,
        })

    if result.returncode != 0:
        return json.dumps({
            "_orchestrator_error": "target_script_failed",
            "returncode": result.returncode,
            "stderr": result.stderr.strip(),
        })

    return result.stdout.strip()


# ---------- gap report: aggregate across attempts ----------

def merge_gap_reports(
    skill_name: str,
    attempt_history: list[VerificationRecord],
) -> GapReport | None:
    """Dedup and merge per-attempt gap reports into a session-wide one.

    Why: even if the builder fixes an unverifiable on attempt 2 by including
    the right field, the underlying documentation gap is real and worth
    surfacing. The user might want to make the doc improvement permanent so
    the next caller doesn't rely on lucky remediation.
    """
    seen_claim_ids: set[str] = set()
    merged_claims: list[Claim] = []
    merged_improvements: list[Improvement] = []

    for record in attempt_history:
        if record.gap_report is None:
            continue
        for c in record.gap_report.unverifiable_claims:
            if c.id in seen_claim_ids:
                continue
            seen_claim_ids.add(c.id)
            merged_claims.append(c)
        for imp in record.gap_report.proposed_improvements:
            if imp.claim_id in seen_claim_ids:
                # Only include improvements for claims we kept; dedup on claim_id.
                if not any(existing.claim_id == imp.claim_id for existing in merged_improvements):
                    merged_improvements.append(imp)

    if not merged_claims:
        return None

    return GapReport(
        skill_id=skill_name,
        unverifiable_claims=merged_claims,
        proposed_improvements=merged_improvements,
        summary=(
            f"{len(merged_claims)} claim(s) were unverifiable across "
            f"{len(attempt_history)} attempt(s). Proposed documentation "
            "improvements would prevent future runs from depending on lucky "
            "remediation."
        ),
    )


# ---------- main orchestration ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Run a skill with verification.")
    parser.add_argument("--target-script", required=True, help="path to the builder script")
    parser.add_argument("--target-args", default="", help="args passed to the builder")
    parser.add_argument("--skill-name", required=True, help="name of the skill being verified")
    parser.add_argument("--skill-doc", default="", help="skill documentation (for claim decomposition)")
    parser.add_argument("--intent", required=True, help="user's plain-language goal")
    parser.add_argument("--max-attempts", type=int, default=2, help="max builder invocations (default 2)")
    parser.add_argument("--strictness", default="medium", choices=["low", "medium", "high"])
    parser.add_argument("--budget-usd", type=float, default=0.50, help="per-attempt verifier budget")
    parser.add_argument("--timeout-sec", type=int, default=120, help="per-attempt timeout")
    parser.add_argument(
        "--backend",
        default=os.environ.get("VERIFIER_BACKEND", "mock"),
        choices=["mock", "llm"],
        help="verifier backend (default: mock; env VERIFIER_BACKEND overrides)",
    )
    parser.add_argument("--pretty", action="store_true", help="pretty-print the result")
    args = parser.parse_args()

    verifier = _load_verifier(args.backend)

    overall_start_ms = int(time.time() * 1000)
    attempts_used = 0
    last_output: Any = None
    attempt_history: list[VerificationRecord] = []
    remediation_prompt = ""
    total_cost = 0.0

    # The remediation loop. Bounded by max_attempts.
    for attempt in range(1, args.max_attempts + 1):
        attempts_used = attempt

        builder_output = run_target(
            target_script=args.target_script,
            target_args=args.target_args,
            remediation_prompt=remediation_prompt,
            timeout_sec=args.timeout_sec,
        )
        last_output = builder_output

        verification = verifier.verify(
            skill_name=args.skill_name,
            skill_documentation=args.skill_doc,
            intent=args.intent,
            builder_output=builder_output,
            attempt=attempt,
            strictness=args.strictness,
        )
        attempt_history.append(verification)
        total_cost += verification.cost_usd

        if verification.status == "verified":
            break
        if verification.status == "partial":
            # No failures, just unverifiable claims. Nothing for the builder to
            # remediate; the gap report carries the signal to the user.
            break

        # status == "failed" — build remediation prompt if more attempts available.
        if attempt < args.max_attempts:
            failed = [c for c in verification.claims if c.verdict == "fail"]
            unv = [c for c in verification.claims if c.verdict == "unverifiable"]
            remediation_prompt = remediation.build_remediation_prompt(
                original_command=f"{args.target_script} {args.target_args}".strip(),
                original_intent=args.intent,
                failed_claims=failed,
                unverifiable_claims=unv,
            )

    # Final verification is the last one we ran.
    final_verification = attempt_history[-1]

    # Promote a session-wide gap report into the final verification's slot.
    # Captures unverifiable claims even from earlier attempts that later passed.
    session_gap = merge_gap_reports(args.skill_name, attempt_history)
    if session_gap is not None:
        final_verification.gap_report = session_gap

    total_duration_ms = int(time.time() * 1000) - overall_start_ms

    result = RunSkillVerifiedResult(
        result=last_output,
        attempts=attempts_used,
        verification=final_verification,
        attempt_history=attempt_history,
        total_duration_ms=total_duration_ms,
        total_cost_usd=total_cost,
    )

    payload = asdict(result)
    if args.pretty:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(json.dumps(payload, default=str))

    # Exit code communicates verdict to callers that want to gate on it.
    #   0  → verified or partial (output is usable; user may want to review gaps)
    #   1  → failed (builder did not converge within max_attempts)
    return 0 if final_verification.status in ("verified", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
