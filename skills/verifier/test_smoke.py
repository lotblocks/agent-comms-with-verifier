#!/usr/bin/env python3
"""Smoke test: runs the full orchestrator end-to-end and asserts the key
behaviors that v1 must support.

Run from the verifier/ directory:
    python3 test_smoke.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
ORCH = os.path.join(HERE, "run_skill_verified.py")
TOY = os.path.join(HERE, "_toy_builder.py")


def run() -> tuple[int, dict]:
    cmd = [
        "python3", ORCH,
        "--target-script", TOY,
        "--skill-name", "toy-payment-skill",
        "--skill-doc", "Processes a test transaction and returns a structured result.",
        "--intent", "create a test payment for amount 100 and report success",
        "--max-attempts", "2",
        "--strictness", "medium",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=HERE)
    return result.returncode, json.loads(result.stdout)


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        raise AssertionError(label)


def main() -> int:
    print("Running smoke test against the verifier orchestrator...")
    print()
    exit_code, payload = run()

    print("Assertions:")

    # Orchestration shape
    check("orchestrator exits 0", exit_code == 0, f"got {exit_code}")
    check("result is present", "result" in payload)
    check("attempts field present", "attempts" in payload)
    check("verification field present", "verification" in payload)
    check("attempt_history field present", "attempt_history" in payload)

    # Loop behavior
    check(
        "used exactly 2 attempts",
        payload["attempts"] == 2,
        f"got {payload['attempts']}",
    )
    check(
        "attempt_history has 2 records",
        len(payload["attempt_history"]) == 2,
        f"got {len(payload['attempt_history'])}",
    )

    # Attempt 1 must fail (the toy builder produces incomplete output first)
    attempt_1 = payload["attempt_history"][0]
    check(
        "attempt 1 status == failed",
        attempt_1["status"] == "failed",
        f"got {attempt_1['status']}",
    )
    a1_fails = [c for c in attempt_1["claims"] if c["verdict"] == "fail"]
    check(
        "attempt 1 has at least one failed claim",
        len(a1_fails) >= 1,
        f"got {len(a1_fails)} failed",
    )
    a1_unv = [c for c in attempt_1["claims"] if c["verdict"] == "unverifiable"]
    check(
        "attempt 1 has at least one unverifiable claim",
        len(a1_unv) >= 1,
        f"got {len(a1_unv)} unverifiable",
    )

    # Attempt 2 must verify (the toy builder reads the remediation prompt and
    # produces a complete output)
    attempt_2 = payload["attempt_history"][1]
    check(
        "attempt 2 status == verified",
        attempt_2["status"] == "verified",
        f"got {attempt_2['status']}",
    )
    check(
        "attempt 2 has zero failed claims",
        all(c["verdict"] != "fail" for c in attempt_2["claims"]),
    )

    # Final state
    final = payload["verification"]
    check(
        "final status == verified",
        final["status"] == "verified",
        f"got {final['status']}",
    )

    # Session-wide gap report must survive even though attempt 2 passed
    check(
        "session gap report present",
        final.get("gap_report") is not None,
    )
    if final.get("gap_report") is not None:
        gap = final["gap_report"]
        check(
            "gap report has >= 1 unverifiable claim",
            len(gap["unverifiable_claims"]) >= 1,
            f"got {len(gap['unverifiable_claims'])}",
        )
        check(
            "gap report has >= 1 proposed improvement",
            len(gap["proposed_improvements"]) >= 1,
            f"got {len(gap['proposed_improvements'])}",
        )
        # Dedup: claim_004 should appear only once even though it surfaced on
        # attempt 1 (and was technically resolved on attempt 2).
        claim_ids = [c["id"] for c in gap["unverifiable_claims"]]
        check(
            "gap report dedups claim ids",
            len(claim_ids) == len(set(claim_ids)),
            f"ids: {claim_ids}",
        )

    # Final builder output should be JSON and include the remediated fields
    result_str = payload["result"]
    try:
        final_output = json.loads(result_str)
    except json.JSONDecodeError:
        final_output = None
    check("final output is valid JSON", final_output is not None)
    if final_output is not None:
        check(
            "final output includes amount (remediation worked)",
            "amount" in final_output,
            f"keys: {list(final_output.keys())}",
        )
        check(
            "final output includes timestamp (remediation worked)",
            "timestamp" in final_output,
        )

    print()
    print("All assertions passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print()
        print(f"FAILED: {e}")
        sys.exit(1)
