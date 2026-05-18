# Verifier — v1 (stubbed)

In-band claim validation for Hyperagent skill execution. Wraps a builder skill
with a verifier subagent that decomposes the output into atomic claims and
either accepts the result or re-prompts the builder with structured feedback.

**Status: v1 with mock verifier.** The orchestration layer is real and tested.
The verifier itself returns deterministic mock claims so the loop can be
exercised end-to-end. The next milestone is wiring up a real LLM-driven
verifier behind the same `verify()` signature — the orchestrator does not
need to change.

See Reference No. 02 (the design spec) for the full architecture.

---

## Files

| File | Purpose |
|------|---------|
| `run_skill_verified.py` | Main orchestration entry point. Runs the builder, hands the output to the verifier, loops with remediation up to `max_attempts`. Exits 0 on verified/partial, 1 on failed. |
| `mock_verifier.py` | Deterministic stubbed verifier. Returns mixed verdicts on attempt 1 (1 fail + 1 unverifiable + 3 pass) and all-pass on attempt 2. Replace with the real verifier without changing the orchestrator. |
| `schemas.py` | Data model: `Claim`, `Improvement`, `GapReport`, `VerificationRecord`, `RunSkillVerifiedResult`. The contract between every component. |
| `remediation.py` | Builds the structured remediation prompt from failed/unverifiable claims. Passed to the builder via `REMEDIATION_PROMPT` env var. |
| `_toy_builder.py` | Toy builder script for end-to-end testing. Produces an incomplete output unless `REMEDIATION_PROMPT` is set, in which case it includes the missing fields. |
| `test_smoke.py` | Assertion-based smoke test of the full flow. 20 assertions cover orchestration, loop behavior, gap report, and remediation. |

---

## Usage

```bash
python3 run_skill_verified.py \
  --target-script /path/to/builder.py \
  --target-args 'arg1 arg2' \
  --skill-name my-skill \
  --skill-doc "what the skill is supposed to do" \
  --intent "the user's plain-language goal" \
  --max-attempts 2 \
  --strictness medium \
  --pretty
```

Output is a JSON `RunSkillVerifiedResult` on stdout. Exit code:
- `0` → status is `verified` or `partial` (output usable; check gap report)
- `1` → status is `failed` (builder did not converge within `max_attempts`)

### Parameters

| Flag | Type | Default | Notes |
|------|------|---------|-------|
| `--target-script` | path | required | builder script (any executable Python file) |
| `--target-args` | string | "" | extra args passed to the builder |
| `--skill-name` | string | required | used as `skill_id` in gap reports |
| `--skill-doc` | string | "" | fed to verifier for claim decomposition |
| `--intent` | string | required | the user's plain-language goal |
| `--max-attempts` | int | 2 | total builder invocations (1 = no remediation) |
| `--strictness` | low/medium/high | medium | how many claims to produce per pass |
| `--budget-usd` | float | 0.50 | per-attempt verifier budget cap |
| `--timeout-sec` | int | 120 | per-attempt wall-clock timeout |
| `--pretty` | flag | off | indent the output JSON |

### Builder contract

Builders that opt into remediation should:
1. Read `REMEDIATION_PROMPT` from the environment.
2. If empty → first attempt; produce best-effort output.
3. If present → adjust behavior to address the failed/unverifiable claims listed.
4. Always print final output to stdout as JSON.

Builders that do not read the env var simply repeat the same output and the
loop will exhaust `max_attempts` without progress. That is a useful signal —
it tells you the builder isn't capable of incorporating verifier feedback.

---

## Behavior verified by the smoke test

Running `python3 test_smoke.py` exercises:

- Orchestrator shape: result, attempts, verification, attempt_history fields all present
- Loop bounds: exactly 2 attempts used when configured for 2
- Remediation triggers: attempt 1 fails, the remediation prompt is generated, the toy builder reads it, attempt 2 produces a complete output
- Aggregate verdict: attempt 1 is `failed`, attempt 2 is `verified`
- Gap report accumulation: even after attempt 2 passes the previously-unverifiable claim, the session-wide gap report still surfaces it as a documentation improvement candidate
- Deduplication: the same claim id appears only once in the merged gap report

---

## How to swap in the real verifier

The whole verification step happens inside `mock_verifier.verify()`. Its signature is the contract:

```python
def verify(
    *,
    skill_name: str,
    skill_documentation: str,
    intent: str,
    builder_output: Any,
    attempt: int,
    strictness: str = "medium",
) -> VerificationRecord:
    ...
```

A real verifier implements the same signature, calls out to an LLM with the
verifier system prompt (see spec §05), parses the LLM's structured output into
`Claim` instances, and returns a `VerificationRecord`. No other file needs
to change. The orchestrator, remediation builder, and gap-report merge logic
are all verifier-agnostic.

To swap:
1. Create `llm_verifier.py` exporting `verify(...)` with the same signature.
2. Update the one import in `run_skill_verified.py`: `import mock_verifier as verifier` → `import llm_verifier as verifier`.
3. Add the verifier model's API key to the skill's credential schema.

---

## What v1 deliberately does NOT do

(See spec §09 for the full list.)

- No real LLM verification — the verifier is a stub
- No multi-skill chain verification — wraps a single builder
- No human-in-the-loop claims — fully automated
- No streaming verification — end-of-skill only
- No rubric integration — verifications don't yet feed eval data
- No recursive verification — verifier cannot itself call `RunSkillVerified`

These are deferred until v1 proves the primitive in production.
