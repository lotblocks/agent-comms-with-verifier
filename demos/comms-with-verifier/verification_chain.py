"""verification_chain.py — walks a multi-hop verification chain and emits a
single end-to-end summary.

When a worker (Gamma) calls another worker (Beta) to fulfill a request, the
final response to the caller (Alpha) contains nested verification records:

    response.payload = {
        "verification":          { ... Gamma's own work ... },
        "upstream_verification": { ... Beta's verification ... }
                                 # which itself may have upstream_verification
    }

This module:
  - Walks the chain via `upstream_verification` keys
  - Computes end-to-end aggregates:
      * chain_status — weakest link rule (fail > partial > verified)
      * total_cost_usd — sum across all hops
      * total_duration_ms — sum across all hops
      * total_attempts — sum across all hops
      * merged_gap_report — deduped union of all gap_reports
      * hop_count — number of verifications in the chain
  - Returns a structured ChainSummary dict that can be embedded in a response
    payload for downstream consumers.
"""
from __future__ import annotations

from typing import Any


def walk_chain(verification: dict | None) -> list[dict]:
    """Return the verification records in the chain, root first, in the order
    walked from leaf (most-recent worker) to root (deepest upstream).

    The convention: index 0 is the local worker's own verification; subsequent
    entries are upstream, deeper-and-deeper.
    """
    chain: list[dict] = []
    cursor = verification
    seen_ids: set[int] = set()  # python object id to defend against cycles
    while isinstance(cursor, dict):
        if id(cursor) in seen_ids:
            break
        seen_ids.add(id(cursor))
        chain.append(cursor)
        cursor = cursor.get("upstream_verification")
    return chain


def aggregate_status(records: list[dict]) -> str:
    """Weakest-link rule across the chain.

    failed beats partial beats verified beats unknown.
    """
    rank = {"verified": 0, "partial": 1, "failed": 2}
    worst = "verified"
    for r in records:
        s = r.get("status", "verified")
        if rank.get(s, 99) > rank.get(worst, 99):
            worst = s
    return worst


def aggregate_cost(records: list[dict]) -> float:
    return sum(float(r.get("cost_usd", 0.0)) for r in records)


def aggregate_duration(records: list[dict]) -> int:
    return sum(int(r.get("duration_ms", 0)) for r in records)


def merged_gap_report(records: list[dict], skill_id_fallback: str = "chain") -> dict | None:
    """Dedupe gap reports across the chain by (skill_id, claim_id) so the same
    documentation gap doesn't appear twice. Returns None if no gaps anywhere.
    """
    seen: set[tuple[str, str]] = set()
    merged_claims: list[dict] = []
    merged_improvements: list[dict] = []
    skill_ids: list[str] = []

    for record in records:
        gap = record.get("gap_report")
        if not gap:
            continue
        skill_id = gap.get("skill_id", skill_id_fallback)
        if skill_id not in skill_ids:
            skill_ids.append(skill_id)
        for c in gap.get("unverifiable_claims", []):
            key = (skill_id, c.get("id", ""))
            if key in seen:
                continue
            seen.add(key)
            c_copy = dict(c)
            c_copy["_skill_id"] = skill_id   # annotate so consumers know origin
            merged_claims.append(c_copy)
        for imp in gap.get("proposed_improvements", []):
            key = (skill_id, imp.get("claim_id", ""))
            # Keep one improvement per (skill, claim).
            if any((existing.get("_skill_id"), existing.get("claim_id")) == key
                   for existing in merged_improvements):
                continue
            imp_copy = dict(imp)
            imp_copy["_skill_id"] = skill_id
            merged_improvements.append(imp_copy)

    if not merged_claims:
        return None

    return {
        "skill_ids": skill_ids,
        "unverifiable_claims": merged_claims,
        "proposed_improvements": merged_improvements,
        "summary": (
            f"{len(merged_claims)} unverifiable claim(s) across "
            f"{len(records)} verification hop(s) spanning skill(s): "
            f"{', '.join(skill_ids)}."
        ),
    }


def build_chain_summary(verification: dict | None, *,
                        skill_id_fallback: str = "chain") -> dict:
    """Build the end-to-end chain summary embedded in a response payload.

    Schema:
        {
            "chain_status": "verified" | "partial" | "failed",
            "hop_count": int,
            "total_cost_usd": float,
            "total_duration_ms": int,
            "total_attempts": int,
            "per_hop": [
                {"verifier_model": ..., "status": ..., "duration_ms": ..., "cost_usd": ...},
                ...  # root first, leaf last (i.e. deepest upstream is index 0)
            ],
            "merged_gap_report": GapReport | None
        }
    """
    if not isinstance(verification, dict):
        return {
            "chain_status": "verified",
            "hop_count": 0,
            "total_cost_usd": 0.0,
            "total_duration_ms": 0,
            "total_attempts": 0,
            "per_hop": [],
            "merged_gap_report": None,
        }

    chain = walk_chain(verification)
    # Reverse so root (deepest upstream) is index 0 — easier to read in transcripts.
    ordered = list(reversed(chain))

    per_hop = [{
        "verifier_model": r.get("verifier_model", "?"),
        "status": r.get("status", "verified"),
        "duration_ms": int(r.get("duration_ms", 0)),
        "cost_usd": float(r.get("cost_usd", 0.0)),
    } for r in ordered]

    return {
        "chain_status": aggregate_status(chain),
        "hop_count": len(chain),
        "total_cost_usd": aggregate_cost(chain),
        "total_duration_ms": aggregate_duration(chain),
        "total_attempts": 0,  # attempts isn't in VerificationRecord; populated by callers
        "per_hop": per_hop,
        "merged_gap_report": merged_gap_report(chain, skill_id_fallback=skill_id_fallback),
    }
