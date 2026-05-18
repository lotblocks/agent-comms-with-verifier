"""Real LLM verifier — calls Anthropic Claude to verify skill output.

Implements the same `verify()` signature as mock_verifier.py. Swapping the
backend is one import change in run_skill_verified.py.

Credentials:
    ANTHROPIC_API_KEY (required) — Anthropic API key for the verifier model.

Optional environment:
    VERIFIER_MODEL — model ID (default: claude-sonnet-4-5-20250929)

Limitations of single-call LLM verifier (deferred to a future agentic verifier):
    - Cannot make tool calls; evidence collection is limited to inspecting the
      builder output text. Behavioral claims that would require execution are
      marked as `unverifiable` with explicit reasoning.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from schemas import Claim, GapReport, Improvement, VerificationRecord


ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
DEFAULT_MODEL = "claude-sonnet-4-5-20250929"
API_URL = "https://api.anthropic.com/v1/messages"

# Approximate per-million-token pricing for cost estimation. Update as needed.
COST_TABLE_USD = {
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-opus-4-5-20251014":  {"input": 15.0, "output": 75.0},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
}

STRICTNESS_COUNT_TARGET = {"low": "4", "medium": "7", "high": "12"}

VERIFIER_SYSTEM_PROMPT = """\
You are the verifier. You did not run the skill.

You have received:
- the skill's documentation
- the user's plain-language intent
- the skill's output

Your job: decompose what should be true into atomic claims and render a
verdict for each. You are not trying to be helpful. You are trying to be RIGHT.

CLAIM PRODUCTION RULES
- Claims live at the INTERSECTION of (a) what the docs say the skill does, AND
  (b) what the user's intent says they wanted. Claims outside both are noise;
  do not produce them.
- Each claim must be independently testable from the output text alone.
- Behavioral claims that would require running code, hitting an external API,
  or executing the skill again must be marked `unverifiable` with reasoning
  that the verifier cannot execute (this is a v1 limitation, not a bug).
- For each claim: state it precisely, state what evidence would prove it,
  state what evidence you actually observed (or null), and render a verdict.

CLAIM TYPES (pick the one that fits best):
- existential — asserts a thing exists in the output
- structural — asserts the output has a specific shape, field, or schema
- behavioral — asserts the output would do something when used (often
  unverifiable in v1)
- factual — asserts something is true about the world (date, name, value)
- semantic — asserts the output means what was asked / addresses the intent
- negative — asserts something did NOT happen (no PII leak, no token, etc.)

VERDICTS
- pass — evidence in the output supports the claim
- fail — evidence in the output contradicts the claim
- unverifiable — cannot determine from the output alone; this is a FINDING,
  not a failure. When marking unverifiable, state WHY and WHAT the skill
  documentation or output schema would need so the claim is verifiable next time.

STRICTNESS: {STRICTNESS} — produce {COUNT_TARGET} claims (plus or minus 2).

OUTPUT FORMAT
Return ONLY a JSON object matching this exact shape. No prose. No markdown
code fences. JSON object only:

{
  "claims": [
    {
      "id": "claim_001",
      "type": "existential" | "structural" | "behavioral" | "factual" | "semantic" | "negative",
      "statement": "...",
      "evidence_required": "...",
      "evidence_collected": <object or null>,
      "verdict": "pass" | "fail" | "unverifiable",
      "confidence": 0.0 to 1.0,
      "reasoning": "..."
    }
  ]
}
"""


def verify(
    *,
    skill_name: str,
    skill_documentation: str,
    intent: str,
    builder_output: Any,
    attempt: int,
    strictness: str = "medium",
) -> VerificationRecord:
    """Run a real LLM-driven verification pass.

    Same signature as mock_verifier.verify — drop-in replacement.

    Raises:
        RuntimeError: if the API key is missing, the network call fails, or
        the LLM returns output that cannot be parsed as the expected schema.
    """
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"{ANTHROPIC_API_KEY_ENV} is not set. The real LLM verifier requires "
            "an Anthropic API key. Configure it as a skill credential, or use "
            "the mock backend via VERIFIER_BACKEND=mock for offline testing."
        )

    start_ms = int(time.time() * 1000)

    model = os.environ.get("VERIFIER_MODEL", DEFAULT_MODEL)
    count_target = STRICTNESS_COUNT_TARGET.get(strictness, "7")
    system_prompt = (
        VERIFIER_SYSTEM_PROMPT
        .replace("{STRICTNESS}", strictness.upper())
        .replace("{COUNT_TARGET}", count_target)
    )

    user_message = json.dumps(
        {
            "skill_name": skill_name,
            "skill_documentation": skill_documentation or "(no documentation provided)",
            "user_intent": intent,
            "attempt_number": attempt,
            "builder_output": str(builder_output),
        },
        indent=2,
    )

    response_data = _call_anthropic(
        api_key=api_key,
        model=model,
        system=system_prompt,
        user_message=user_message,
    )

    full_text = _extract_text(response_data)
    parsed = _extract_json(full_text)
    if parsed is None or "claims" not in parsed:
        raise RuntimeError(
            "Verifier LLM did not return parseable JSON with a 'claims' array.\n"
            f"Raw response: {full_text[:500]}"
        )

    claims = _build_claims(parsed["claims"])
    status = _aggregate_status(claims)
    gap_report = _build_gap_report(skill_name, claims)
    cost_usd = _estimate_cost(model, response_data.get("usage", {}))

    duration_ms = max(1, int(time.time() * 1000) - start_ms)

    return VerificationRecord(
        status=status,
        claims=claims,
        verifier_model=model,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
        gap_report=gap_report,
    )


# ---------- HTTP call ----------

def _call_anthropic(*, api_key: str, model: str, system: str, user_message: str) -> dict:
    body = {
        "model": model,
        "max_tokens": 4096,
        "system": system,
        "messages": [{"role": "user", "content": user_message}],
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Anthropic API HTTP {e.code}: {body[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error reaching Anthropic API: {e}")


def _extract_text(response: dict) -> str:
    blocks = response.get("content", [])
    parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return "\n".join(parts).strip()


# ---------- response parsing ----------

def _extract_json(text: str) -> dict | None:
    """Tolerate markdown fences and trailing prose around the JSON object."""
    text = text.strip()
    # Strip leading markdown code fence
    if text.startswith("```json"):
        text = text[len("```json"):].lstrip()
    elif text.startswith("```"):
        text = text[len("```"):].lstrip()
    # Strip trailing fence
    if text.endswith("```"):
        text = text[: -len("```")].rstrip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last resort: find the outermost {...}
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def _build_claims(raw_claims: list) -> list[Claim]:
    claims: list[Claim] = []
    for i, c in enumerate(raw_claims, start=1):
        claims.append(
            Claim(
                id=str(c.get("id", f"claim_{i:03d}")),
                type=str(c.get("type", "semantic")),
                statement=str(c.get("statement", "")),
                evidence_required=str(c.get("evidence_required", "")),
                evidence_collected=c.get("evidence_collected"),
                verdict=str(c.get("verdict", "unverifiable")),
                confidence=_safe_float(c.get("confidence"), default=0.5),
                reasoning=str(c.get("reasoning", "")),
            )
        )
    return claims


def _safe_float(value, *, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, v))


# ---------- aggregation (same rules as mock_verifier) ----------

def _aggregate_status(claims: list[Claim]) -> str:
    if any(c.verdict == "fail" for c in claims):
        return "failed"
    if any(c.verdict == "unverifiable" for c in claims):
        return "partial"
    return "verified"


def _build_gap_report(skill_name: str, claims: list[Claim]) -> GapReport | None:
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
                    f"Document the expected evidence for: \"{c.statement}\". "
                    f"Verifier reasoning: {c.reasoning}"
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
            f"{len(unverifiable)} claim(s) could not be verified from the output "
            "alone. Proposed documentation changes would make these verifiable "
            "on the next run."
        ),
    )


def _estimate_cost(model: str, usage: dict) -> float:
    rates = COST_TABLE_USD.get(model)
    if rates is None:
        return 0.0
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    return (
        input_tokens / 1_000_000 * rates["input"]
        + output_tokens / 1_000_000 * rates["output"]
    )
