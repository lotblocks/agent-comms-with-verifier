"""Real LLM verifier backed by Replicate's API.

Implements the same `verify()` signature as mock_verifier and llm_verifier.
Designed as a third pluggable backend so users can pick whichever provider
they have a key for.

Credentials:
    REPLICATE_API_TOKEN (required) — set as a skill credential.

Optional environment:
    VERIFIER_REPLICATE_MODEL — default "meta/meta-llama-3-8b-instruct"

Why Replicate as an option:
  - Cheap free-tier-ish for small models (~$0.0002/call for Llama-3-8b)
  - Many models available (Llama, Mistral, Mixtral, Gemma, etc.)
  - Different failure modes than Anthropic — useful for cross-family pairings
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from schemas import Claim, GapReport, Improvement, VerificationRecord


REPLICATE_API_TOKEN_ENV = "REPLICATE_API_TOKEN"
DEFAULT_MODEL = "meta/meta-llama-3-8b-instruct"
API_BASE = "https://api.replicate.com/v1/models"


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
  (b) what the user's intent says they wanted. Claims outside both are noise.
- Each claim must be independently testable from the output text alone.
- Behavioral claims that would require running code must be marked
  `unverifiable` with reasoning that the verifier cannot execute (v1 limitation).
- For each claim: state it precisely, state what evidence would prove it,
  state what evidence you actually observed (or null), and render a verdict.

CLAIM TYPES (pick the one that fits best):
- existential — asserts a thing exists in the output
- structural — asserts the output has a specific shape, field, or schema
- behavioral — asserts the output would do something when used
- factual — asserts something is true about the world
- semantic — asserts the output means what was asked
- negative — asserts something did NOT happen (no PII leak, no token, etc.)

VERDICTS
- pass — evidence in the output supports the claim
- fail — evidence in the output contradicts the claim
- unverifiable — cannot determine from the output alone

STRICTNESS: {STRICTNESS} — produce {COUNT_TARGET} claims (plus or minus 2).

OUTPUT FORMAT
Return ONLY a JSON object matching this exact shape. No prose. No markdown
code fences. JSON object only:

{
  "claims": [
    {
      "id": "claim_001",
      "type": "structural",
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
    """Real LLM verification via Replicate. Same signature as mock/llm verifiers."""
    api_token = os.environ.get(REPLICATE_API_TOKEN_ENV)
    if not api_token:
        raise RuntimeError(
            f"{REPLICATE_API_TOKEN_ENV} is not set. Configure it as a skill "
            "credential, or use the mock backend via VERIFIER_BACKEND=mock."
        )

    start_ms = int(time.time() * 1000)
    model = os.environ.get("VERIFIER_REPLICATE_MODEL", DEFAULT_MODEL)
    count_target = STRICTNESS_COUNT_TARGET.get(strictness, "7")

    system_prompt = (
        VERIFIER_SYSTEM_PROMPT
        .replace("{STRICTNESS}", strictness.upper())
        .replace("{COUNT_TARGET}", count_target)
    )
    user_message = json.dumps({
        "skill_name": skill_name,
        "skill_documentation": skill_documentation or "(no documentation provided)",
        "user_intent": intent,
        "attempt_number": attempt,
        "builder_output": str(builder_output),
    }, indent=2)

    # Replicate's chat-style models use a "prompt" field. Llama-3 family also
    # supports a system prompt via Replicate's API.
    full_prompt = (
        f"<SYSTEM>\n{system_prompt}\n</SYSTEM>\n\n<INPUT>\n{user_message}\n</INPUT>"
    )

    raw_output = _call_replicate(
        api_token=api_token,
        model=model,
        input_data={
            "prompt": full_prompt,
            "max_tokens": 2048,
            "temperature": 0,
            "system_prompt": system_prompt,
        },
    )

    parsed = _extract_json(raw_output)
    if parsed is None or "claims" not in parsed:
        raise RuntimeError(
            "Verifier LLM did not return parseable JSON with a 'claims' array.\n"
            f"Raw response (first 500): {raw_output[:500]}"
        )

    claims = _build_claims(parsed["claims"])
    status = _aggregate_status(claims)
    gap_report = _build_gap_report(skill_name, claims)

    duration_ms = max(1, int(time.time() * 1000) - start_ms)

    return VerificationRecord(
        status=status,
        claims=claims,
        verifier_model=model,
        duration_ms=duration_ms,
        cost_usd=0.0,  # Replicate doesn't return per-call cost; calculate offline if needed
        gap_report=gap_report,
    )


# ---------- HTTP call ----------

def _call_replicate(*, api_token: str, model: str, input_data: dict,
                    poll_interval_sec: float = 1.0,
                    max_wait_sec: float = 120.0) -> str:
    """Create a prediction with synchronous wait. Returns the model's text output.

    Replicate's chat-style models return output as a list of tokens; we join them.
    """
    url = f"{API_BASE}/{model}/predictions"
    body = json.dumps({"input": input_data}).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "Prefer": "wait=60",  # synchronous; up to 60s
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=80) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Replicate API HTTP {e.code}: {body_text[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error reaching Replicate: {e}")

    # If still running, poll the prediction until terminal.
    status = response.get("status")
    pid = response.get("id")
    deadline = time.time() + max_wait_sec
    while status in ("starting", "processing") and time.time() < deadline:
        time.sleep(poll_interval_sec)
        get_url = f"https://api.replicate.com/v1/predictions/{pid}"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    get_url,
                    headers={"Authorization": f"Bearer {api_token}"},
                ),
                timeout=20,
            ) as r:
                response = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Replicate poll HTTP {e.code}: {body_text[:500]}")
        status = response.get("status")

    if status != "succeeded":
        raise RuntimeError(
            f"Replicate prediction did not succeed: status={status}, "
            f"error={response.get('error')}"
        )

    output = response.get("output")
    if isinstance(output, list):
        return "".join(str(x) for x in output)
    return str(output) if output is not None else ""


# ---------- response parsing ----------

def _extract_json(text: str) -> dict | None:
    """Tolerate markdown fences and trailing prose around the JSON object."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[len("```json"):].lstrip()
    elif text.startswith("```"):
        text = text[len("```"):].lstrip()
    if text.endswith("```"):
        text = text[: -len("```")].rstrip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(text[start: end + 1])
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
            "alone. Proposed documentation changes would make these verifiable next time."
        ),
    )
