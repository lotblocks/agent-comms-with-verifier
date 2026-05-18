"""agent_memory.py — durable memory for the agent system.

Each verification produces signal. Gap reports name documentation gaps,
failed claims name brittleness patterns, remediations name what worked.
This module persists those signals so the system gets smarter over time.

Three memory scopes:
  - global         → shared facts (e.g., known-bad input patterns)
  - skill.<name>   → per-skill learning (gaps, failure patterns, remediations)
  - agent.<id>     → per-agent reputation (verification track record)

The orchestrator consults memories at the start of each run and stores new
ones at the end. The mock verifier doesn't read memories (deterministic by
design), but the LLM verifier does — they're injected into the system prompt
as additional `claim_guidelines`.

Storage is a separate SQLite file so memories survive even when the bus DB
is wiped between demos. Path is configurable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Optional


DEFAULT_MEMORY_DB = "_agent_memory.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    category TEXT NOT NULL,
    subject TEXT,
    content TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.5,
    created_at REAL NOT NULL,
    last_used_at REAL,
    use_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);
CREATE INDEX IF NOT EXISTS idx_memories_scope_category ON memories(scope, category);
CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject);
"""


class MemoryStore:
    """SQLite-backed memory store with scope-based organization.

    Scope conventions:
      - "global"           — broadly applicable lessons
      - "skill.<name>"     — per-skill knowledge (gaps, patterns)
      - "agent.<id>"       — per-agent reputation, preferences

    Category conventions:
      - "gap"              — documentation gap surfaced by a verifier
      - "failure_pattern"  — a claim that has failed before
      - "remediation"      — what remediation prompt fixed a past failure
      - "reputation"       — running success/failure counts for an agent
      - "fact"             — a general known fact
    """

    def __init__(self, db_path: str = DEFAULT_MEMORY_DB):
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            for stmt in SCHEMA.strip().split(";"):
                if stmt.strip():
                    c.execute(stmt)

    # ---------- generic ----------

    def store(
        self,
        *,
        scope: str,
        category: str,
        content: Any,
        subject: Optional[str] = None,
        importance: float = 0.5,
    ) -> str:
        """Store a memory. Returns the memory id."""
        mid = "mem_" + uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute(
                "INSERT INTO memories(id, scope, category, subject, content, "
                "importance, created_at, last_used_at, use_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0)",
                (mid, scope, category, subject,
                 json.dumps(content) if not isinstance(content, str) else content,
                 importance, time.time()),
            )
        return mid

    def recall(
        self,
        *,
        scope: str,
        category: Optional[str] = None,
        subject: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Recall memories matching scope (and optional filters).

        Marks each returned memory as 'used' (increments use_count, updates
        last_used_at). Ordered by importance desc then created_at desc.
        """
        sql = "SELECT id, scope, category, subject, content, importance, created_at FROM memories WHERE scope = ?"
        params: list[Any] = [scope]
        if category is not None:
            sql += " AND category = ?"
            params.append(category)
        if subject is not None:
            sql += " AND subject = ?"
            params.append(subject)
        sql += " ORDER BY importance DESC, created_at DESC LIMIT ?"
        params.append(limit)

        with self._conn() as c:
            rows = c.execute(sql, params).fetchall()
            result_ids = [r[0] for r in rows]
            if result_ids:
                placeholders = ",".join("?" * len(result_ids))
                c.execute(
                    f"UPDATE memories SET use_count = use_count + 1, "
                    f"last_used_at = ? WHERE id IN ({placeholders})",
                    [time.time(), *result_ids],
                )

        return [{
            "id": r[0], "scope": r[1], "category": r[2], "subject": r[3],
            "content": _maybe_json(r[4]),
            "importance": r[5], "created_at": r[6],
        } for r in rows]

    def count(self, scope: Optional[str] = None) -> int:
        with self._conn() as c:
            if scope:
                row = c.execute(
                    "SELECT COUNT(*) FROM memories WHERE scope = ?", (scope,)
                ).fetchone()
            else:
                row = c.execute("SELECT COUNT(*) FROM memories").fetchone()
        return row[0]

    def all_scopes(self) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT scope FROM memories ORDER BY scope"
            ).fetchall()
        return [r[0] for r in rows]

    # ---------- high-level helpers ----------

    def store_gap(self, skill_name: str, gap_report: dict) -> list[str]:
        """Store unverifiable claims from a gap report as per-skill memories.

        Deduplication: if a memory with the same (scope, category, subject)
        and the same statement already exists, we bump its importance and
        use_count rather than creating a duplicate row. This keeps the
        memory store from bloating on repeated runs of the same scenario.
        """
        ids: list[str] = []
        scope = f"skill.{skill_name}"
        for c in gap_report.get("unverifiable_claims", []):
            subject = c.get("id", "")
            statement = c.get("statement", "")
            existing = self._find_existing_gap(scope, subject, statement)
            if existing is not None:
                self._reinforce(existing)
                ids.append(existing)
                continue
            mid = self.store(
                scope=scope,
                category="gap",
                subject=subject,
                content={
                    "claim_id": c.get("id"),
                    "type": c.get("type"),
                    "statement": statement,
                    "evidence_required": c.get("evidence_required"),
                    "reasoning": c.get("reasoning"),
                },
                importance=0.7,
            )
            ids.append(mid)
        return ids

    def _find_existing_gap(self, scope: str, subject: str, statement: str) -> Optional[str]:
        """Return the id of an existing gap memory with matching statement, or None."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, content FROM memories "
                "WHERE scope = ? AND category = 'gap' AND subject = ?",
                (scope, subject),
            ).fetchall()
        for mid, content_str in rows:
            try:
                content = json.loads(content_str)
            except (json.JSONDecodeError, TypeError):
                continue
            if content.get("statement") == statement:
                return mid
        return None

    def _reinforce(self, memory_id: str, *, importance_bump: float = 0.05) -> None:
        """A repeated observation makes a memory more important (capped at 1.0)."""
        with self._conn() as c:
            c.execute(
                "UPDATE memories SET use_count = use_count + 1, "
                "last_used_at = ?, "
                "importance = MIN(1.0, importance + ?) "
                "WHERE id = ?",
                (time.time(), importance_bump, memory_id),
            )

    def store_failure_pattern(
        self,
        *,
        skill_name: str,
        claim_id: str,
        statement: str,
        why_failed: str,
        remediation_summary: Optional[str] = None,
    ) -> str:
        """Store a record that a particular claim has failed before, with
        what remediation worked (if any).
        """
        return self.store(
            scope=f"skill.{skill_name}",
            category="failure_pattern",
            subject=claim_id,
            content={
                "claim_id": claim_id,
                "statement": statement,
                "why_failed": why_failed,
                "remediation_summary": remediation_summary,
            },
            importance=0.8,
        )

    def store_reputation(self, agent_id: str, success: bool) -> str:
        """Bump an agent's reputation score. Simple running counter."""
        return self.store(
            scope=f"agent.{agent_id}",
            category="reputation",
            subject="verification_outcome",
            content={"success": success, "ts": time.time()},
            importance=0.3,
        )

    def reputation_summary(self, agent_id: str) -> dict:
        """Return success/total counts for an agent."""
        rows = self.recall(
            scope=f"agent.{agent_id}", category="reputation", limit=10000
        )
        total = len(rows)
        successes = sum(1 for r in rows if r["content"].get("success"))
        return {
            "agent_id": agent_id,
            "successes": successes,
            "total": total,
            "success_rate": (successes / total) if total else None,
        }

    def claim_guidelines_for_skill(self, skill_name: str, limit: int = 5) -> str:
        """Render skill-scoped memories as guidelines string the verifier can
        append to its system prompt. The mock verifier ignores this; the LLM
        verifier uses it to bias claim production.
        """
        gaps = self.recall(scope=f"skill.{skill_name}", category="gap", limit=limit)
        fails = self.recall(scope=f"skill.{skill_name}", category="failure_pattern", limit=limit)
        if not gaps and not fails:
            return ""

        lines = []
        if gaps:
            lines.append("KNOWN DOCUMENTATION GAPS FOR THIS SKILL:")
            for g in gaps:
                stmt = g["content"].get("statement", "")
                why = g["content"].get("reasoning", "")
                lines.append(f"  - {stmt}  (why unverifiable: {why})")
        if fails:
            lines.append("KNOWN FAILURE PATTERNS FOR THIS SKILL:")
            for f in fails:
                stmt = f["content"].get("statement", "")
                why = f["content"].get("why_failed", "")
                rem = f["content"].get("remediation_summary") or "(no recorded fix)"
                lines.append(f"  - {stmt}  · failed because: {why}  · fixed by: {rem}")
        lines.append(
            "Use these to inform claim production. If a known gap or failure "
            "applies, produce a corresponding claim with explicit evidence_required."
        )
        return "\n".join(lines)


def _maybe_json(s: str) -> Any:
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s
