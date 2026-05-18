#!/usr/bin/env python3
"""Test the LLM verifier's response-parsing and aggregation logic without
making a real API call. Validates everything from the HTTP boundary inward.

Run from the verifier/ directory:
    python3 test_llm_parsing.py

For an end-to-end test against the real Anthropic API, set a valid
ANTHROPIC_API_KEY and run:
    python3 run_skill_verified.py --backend llm ...
"""
from __future__ import annotations

import json
import sys
from unittest.mock import patch

import llm_verifier


# Realistic-shaped synthetic responses the LLM might produce.
RAW_RESPONSE_CLEAN_JSON = {
    "content": [{"type": "text", "text": json.dumps({
        "claims": [
            {
                "id": "claim_001",
                "type": "structural",
                "statement": "the output is valid JSON with a 'status' field",
                "evidence_required": "JSON parses; 'status' key present",
                "evidence_collected": {"parseable": True, "has_status": True},
                "verdict": "pass",
                "confidence": 0.95,
                "reasoning": "output parses cleanly and contains the required 'status' field",
            },
            {
                "id": "claim_002",
                "type": "semantic",
                "statement": "the output addresses the user's intent for a payment of 100",
                "evidence_required": "amount field present and equals 100",
                "evidence_collected": {"amount_field_present": False},
                "verdict": "fail",
                "confidence": 0.90,
                "reasoning": "the user asked for a payment of 100 but no amount field appears in the output",
            },
            {
                "id": "claim_003",
                "type": "behavioral",
                "statement": "the payment would be processed by Stripe in test mode",
                "evidence_required": "executing the skill against Stripe's test API",
                "evidence_collected": None,
                "verdict": "unverifiable",
                "confidence": 0.5,
                "reasoning": "v1 verifier cannot execute the skill; this requires actually calling Stripe",
            },
        ]
    })}],
    "usage": {"input_tokens": 1200, "output_tokens": 450},
}

RAW_RESPONSE_WITH_FENCE = {
    "content": [{"type": "text", "text": "```json\n" + json.dumps({
        "claims": [
            {"id": "c1", "type": "structural", "statement": "...", "evidence_required": "...",
             "verdict": "pass", "confidence": 0.9, "reasoning": "..."},
        ]
    }) + "\n```"}],
    "usage": {"input_tokens": 100, "output_tokens": 50},
}

RAW_RESPONSE_WITH_PROSE = {
    "content": [{"type": "text", "text": "Here are the claims:\n\n" + json.dumps({
        "claims": [
            {"id": "c1", "type": "semantic", "statement": "...", "evidence_required": "...",
             "verdict": "unverifiable", "confidence": 0.5, "reasoning": "..."},
        ]
    }) + "\n\nThat is my analysis."}],
    "usage": {"input_tokens": 100, "output_tokens": 100},
}


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  ({detail})" if detail else ""))
    if not condition:
        raise AssertionError(label)


def test_clean_json_response():
    print("\nTest 1: Clean JSON response")
    with patch.object(llm_verifier, "_call_anthropic", return_value=RAW_RESPONSE_CLEAN_JSON):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            record = llm_verifier.verify(
                skill_name="stripe-payment",
                skill_documentation="Creates a Stripe payment intent.",
                intent="create a payment for 100 in test mode",
                builder_output='{"status": "ok"}',
                attempt=1,
                strictness="medium",
            )
    check("3 claims parsed", len(record.claims) == 3, f"got {len(record.claims)}")
    check("status == failed (1 fail present)", record.status == "failed", f"got {record.status}")
    check("verifier_model is recorded", bool(record.verifier_model))
    check("cost > 0", record.cost_usd > 0, f"${record.cost_usd:.4f}")
    check("gap_report present (1 unverifiable)", record.gap_report is not None)
    check(
        "gap report has 1 unverifiable claim",
        len(record.gap_report.unverifiable_claims) == 1,
    )
    check(
        "improvement targets documentation",
        record.gap_report.proposed_improvements[0].target == "documentation",
    )


def test_markdown_fence_response():
    print("\nTest 2: Response wrapped in ```json ... ```")
    with patch.object(llm_verifier, "_call_anthropic", return_value=RAW_RESPONSE_WITH_FENCE):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            record = llm_verifier.verify(
                skill_name="x", skill_documentation="", intent="x",
                builder_output="x", attempt=1,
            )
    check("fence stripped, 1 claim parsed", len(record.claims) == 1)
    check("status == verified (all pass)", record.status == "verified")


def test_prose_around_json_response():
    print("\nTest 3: JSON object embedded in prose")
    with patch.object(llm_verifier, "_call_anthropic", return_value=RAW_RESPONSE_WITH_PROSE):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            record = llm_verifier.verify(
                skill_name="x", skill_documentation="", intent="x",
                builder_output="x", attempt=1,
            )
    check("inner JSON object extracted", len(record.claims) == 1)
    check("status == partial (1 unverifiable, no fail)", record.status == "partial")


def test_missing_api_key():
    print("\nTest 4: Missing API key raises clear error")
    with patch.dict("os.environ", {}, clear=True):
        try:
            llm_verifier.verify(
                skill_name="x", skill_documentation="", intent="x",
                builder_output="x", attempt=1,
            )
            raised = False
        except RuntimeError as e:
            raised = True
            msg = str(e)
    check("RuntimeError raised", raised)
    check("error message mentions ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY" in msg)
    check("error message mentions mock fallback", "mock" in msg.lower())


def test_unparseable_response():
    print("\nTest 5: Non-JSON response raises clear error")
    garbage = {"content": [{"type": "text", "text": "I cannot help with that."}], "usage": {}}
    with patch.object(llm_verifier, "_call_anthropic", return_value=garbage):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            try:
                llm_verifier.verify(
                    skill_name="x", skill_documentation="", intent="x",
                    builder_output="x", attempt=1,
                )
                raised = False
            except RuntimeError as e:
                raised = True
                msg = str(e)
    check("RuntimeError raised on non-JSON", raised)
    check("error mentions parseable JSON", "JSON" in msg or "json" in msg)


def test_confidence_clamping():
    print("\nTest 6: Out-of-range confidence values are clamped")
    bad_conf = {
        "content": [{"type": "text", "text": json.dumps({
            "claims": [
                {"id": "c1", "type": "semantic", "statement": "x", "evidence_required": "x",
                 "verdict": "pass", "confidence": 1.5, "reasoning": "x"},
                {"id": "c2", "type": "semantic", "statement": "x", "evidence_required": "x",
                 "verdict": "pass", "confidence": -0.2, "reasoning": "x"},
                {"id": "c3", "type": "semantic", "statement": "x", "evidence_required": "x",
                 "verdict": "pass", "confidence": "not a number", "reasoning": "x"},
            ]
        })}],
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }
    with patch.object(llm_verifier, "_call_anthropic", return_value=bad_conf):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            record = llm_verifier.verify(
                skill_name="x", skill_documentation="", intent="x",
                builder_output="x", attempt=1,
            )
    check("confidence > 1 clamped to 1.0", record.claims[0].confidence == 1.0)
    check("confidence < 0 clamped to 0.0", record.claims[1].confidence == 0.0)
    check("non-numeric defaults to 0.5", record.claims[2].confidence == 0.5)


def main() -> int:
    print("LLM verifier — synthetic parse + aggregate tests")

    test_clean_json_response()
    test_markdown_fence_response()
    test_prose_around_json_response()
    test_missing_api_key()
    test_unparseable_response()
    test_confidence_clamping()

    print("\nAll LLM verifier parse tests passed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
