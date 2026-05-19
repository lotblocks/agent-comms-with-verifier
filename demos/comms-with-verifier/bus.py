"""SQLite-backed message bus for peer-to-peer agent communication.

A minimal implementation of the architecture from Reference No. 01:
- Each agent has a stable ID and a friendly name
- Messages carry the envelope from the design spec (from, to, topic, ttl,
  hop_count, conversation_id, etc.)
- Loop prevention is enforced AT THE BUS, not at the agents (TTL + hop limit)
- Supports both topic-based broadcast AND direct addressing

SQLite is used so the demo runs anywhere with no infrastructure. For a real
deployment, swap to Postgres LISTEN/NOTIFY or Redis pub/sub — the public
function signatures are stable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Optional


DEFAULT_TTL_SECONDS = 300
MAX_HOP_COUNT = 8
HEARTBEAT_STALE_SECONDS = 90
ROLE_TOPIC_PREFIX = "role."   # workers subscribe to ROLE_TOPIC_PREFIX + role


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    subscriptions TEXT NOT NULL,     -- JSON array of topics
    last_heartbeat REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    parent_conversation_id TEXT,     -- NULL for top-level, links nested sub-conversations
    from_agent TEXT NOT NULL,
    to_agent TEXT,                   -- NULL for broadcast
    topic TEXT,                      -- NULL for direct-only
    msg_type TEXT NOT NULL,          -- request | response | event
    reply_to TEXT,
    hop_count INTEGER NOT NULL DEFAULT 0,
    ttl_seconds INTEGER NOT NULL DEFAULT 300,
    created_at REAL NOT NULL,
    payload TEXT NOT NULL,           -- JSON
    claimed_by TEXT,
    claimed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_messages_inbox ON messages(to_agent, claimed_by);
CREATE INDEX IF NOT EXISTS idx_messages_topic ON messages(topic, claimed_by);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_conversation_id);
"""

# Idempotent migration for databases created before parent_conversation_id existed.
def _migrate_add_parent_conv(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "parent_conversation_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN parent_conversation_id TEXT")


class Bus:
    """A simple peer-to-peer message bus over SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    # ---------- internal ----------

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
            _migrate_add_parent_conv(c)

    # ---------- registry ----------

    def register(self, agent_id: str, name: str, role: str, subscriptions: list[str]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO agents(id, name, role, subscriptions, last_heartbeat) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_id, name, role, json.dumps(subscriptions), time.time()),
            )

    def heartbeat(self, agent_id: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE agents SET last_heartbeat = ? WHERE id = ?",
                (time.time(), agent_id),
            )

    def list_agents(self, include_stale: bool = False) -> list[dict]:
        now = time.time()
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, name, role, subscriptions, last_heartbeat FROM agents"
            ).fetchall()
        agents = []
        for r in rows:
            agent = {
                "id": r[0],
                "name": r[1],
                "role": r[2],
                "subscriptions": json.loads(r[3]),
                "last_heartbeat": r[4],
                "is_alive": (now - r[4]) < HEARTBEAT_STALE_SECONDS,
            }
            if include_stale or agent["is_alive"]:
                agents.append(agent)
        return agents

    # ---------- role-based discovery ----------

    def find_agents_by_role(
        self,
        role: str,
        alive_only: bool = True,
        memory_store: Any = None,
        reputation_min: Optional[float] = None,
        min_reputation_samples: int = 3,
    ) -> list[dict]:
        """Return all agents matching the given role.

        With alive_only=True (default), only agents within the heartbeat window
        are returned. Sorted by least-recently-active first for fairness.

        Reputation-aware filtering (optional):
            Pass `memory_store` (a MemoryStore instance) and `reputation_min`
            (a float 0.0-1.0). Agents with success_rate below the threshold
            are filtered out, UNLESS they have fewer than min_reputation_samples
            samples (so new agents get a chance to prove themselves).
        """
        candidates = [a for a in self.list_agents(include_stale=not alive_only)
                      if a["role"] == role]
        if alive_only:
            candidates = [a for a in candidates if a["is_alive"]]

        if memory_store is not None and reputation_min is not None:
            filtered = []
            for a in candidates:
                rep = memory_store.reputation_summary(a["id"])
                # New agents (insufficient samples) get a probation pass.
                if rep["total"] < min_reputation_samples:
                    a = dict(a)
                    a["_reputation_status"] = "probation"
                    filtered.append(a)
                    continue
                if (rep["success_rate"] or 0) >= reputation_min:
                    a = dict(a)
                    a["_reputation_status"] = "trusted"
                    a["_reputation_score"] = rep["success_rate"]
                    filtered.append(a)
                # Else: filtered out for low reputation.
            candidates = filtered

        candidates.sort(key=lambda a: a["last_heartbeat"])
        return candidates

    def pick_agent_by_role(
        self,
        role: str,
        exclude: Optional[set[str]] = None,
        memory_store: Any = None,
        reputation_min: Optional[float] = None,
    ) -> Optional[dict]:
        """Pick one live agent matching a role.

        exclude is an optional set of agent_ids to skip.

        Reputation-aware picks: pass memory_store + reputation_min to skip
        replicas with bad track records (with probation for new agents).

        NOTE: for true worker-pool fanout, prefer publish_to_role() — the bus's
        atomic claim-locking is a better load balancer than caller-side picking.
        """
        exclude = exclude or set()
        for a in self.find_agents_by_role(
            role, alive_only=True,
            memory_store=memory_store, reputation_min=reputation_min,
        ):
            if a["id"] not in exclude:
                return a
        return None

    def publish_to_role(
        self,
        *,
        from_agent: str,
        role: str,
        payload: Any,
        conversation_id: Optional[str] = None,
        parent_conversation_id: Optional[str] = None,
        msg_type: str = "request",
        hop_count: int = 0,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> tuple[str, str]:
        """Publish to the role's shared topic — any subscribed replica may claim.

        This is the right primitive for worker pools: the caller doesn't pick a
        specific replica, the bus does the load balancing through atomic claim-locking
        in receive(). Returns (message_id, topic).
        """
        topic = ROLE_TOPIC_PREFIX + role
        msg_id = self.publish(
            from_agent=from_agent,
            topic=topic,
            payload=payload,
            conversation_id=conversation_id,
            parent_conversation_id=parent_conversation_id,
            msg_type=msg_type,
            hop_count=hop_count,
            ttl_seconds=ttl_seconds,
        )
        return msg_id, topic

    # ---------- send ----------

    def _insert_message(self, msg: dict) -> str:
        with self._conn() as c:
            c.execute(
                "INSERT INTO messages(id, conversation_id, parent_conversation_id, "
                "from_agent, to_agent, topic, msg_type, reply_to, hop_count, "
                "ttl_seconds, created_at, payload) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg["id"],
                    msg["conversation_id"],
                    msg.get("parent_conversation_id"),
                    msg["from_agent"],
                    msg.get("to_agent"),
                    msg.get("topic"),
                    msg["msg_type"],
                    msg.get("reply_to"),
                    msg.get("hop_count", 0),
                    msg.get("ttl_seconds", DEFAULT_TTL_SECONDS),
                    msg["created_at"],
                    json.dumps(msg["payload"]),
                ),
            )
        return msg["id"]

    def publish(
        self,
        *,
        from_agent: str,
        topic: str,
        payload: Any,
        conversation_id: Optional[str] = None,
        parent_conversation_id: Optional[str] = None,
        msg_type: str = "event",
        hop_count: int = 0,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str:
        """Broadcast to a topic. Returns the message id.

        parent_conversation_id is set when this message is part of a nested
        sub-conversation started by a worker to fulfill an outer request.
        """
        if hop_count > MAX_HOP_COUNT:
            raise RuntimeError(f"hop_count {hop_count} exceeds MAX_HOP_COUNT {MAX_HOP_COUNT}")
        msg = {
            "id": "msg_" + uuid.uuid4().hex[:12],
            "conversation_id": conversation_id or "cnv_" + uuid.uuid4().hex[:12],
            "parent_conversation_id": parent_conversation_id,
            "from_agent": from_agent,
            "to_agent": None,
            "topic": topic,
            "msg_type": msg_type,
            "hop_count": hop_count,
            "ttl_seconds": ttl_seconds,
            "created_at": time.time(),
            "payload": payload,
        }
        return self._insert_message(msg)

    def send_direct(
        self,
        *,
        from_agent: str,
        to_agent: str,
        payload: Any,
        conversation_id: Optional[str] = None,
        parent_conversation_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        msg_type: str = "request",
        hop_count: int = 0,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str:
        """Send a directly-addressed message. Returns the message id.

        parent_conversation_id is set when this message is part of a nested
        sub-conversation (e.g. Gamma asking Beta in order to fulfill Alpha's
        request). Audit tools can walk the chain via this field.
        """
        if hop_count > MAX_HOP_COUNT:
            raise RuntimeError(f"hop_count {hop_count} exceeds MAX_HOP_COUNT {MAX_HOP_COUNT}")
        msg = {
            "id": "msg_" + uuid.uuid4().hex[:12],
            "conversation_id": conversation_id or "cnv_" + uuid.uuid4().hex[:12],
            "parent_conversation_id": parent_conversation_id,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "topic": None,
            "msg_type": msg_type,
            "reply_to": reply_to,
            "hop_count": hop_count,
            "ttl_seconds": ttl_seconds,
            "created_at": time.time(),
            "payload": payload,
        }
        return self._insert_message(msg)

    # ---------- receive ----------

    def receive(
        self,
        *,
        agent_id: str,
        subscriptions: list[str],
        max_messages: int = 1,
        wait_sec: float = 0.0,
        poll_interval_sec: float = 0.1,
    ) -> list[dict]:
        """Atomically claim and return messages addressed to this agent or to
        a topic the agent subscribes to. Polls up to wait_sec for new arrivals.

        Drops messages whose TTL has expired.
        """
        deadline = time.time() + wait_sec
        while True:
            claimed = self._claim_messages(agent_id, subscriptions, max_messages)
            if claimed or time.time() >= deadline:
                return claimed
            time.sleep(poll_interval_sec)

    def _claim_messages(
        self, agent_id: str, subscriptions: list[str], max_messages: int
    ) -> list[dict]:
        now = time.time()
        topic_placeholders = ",".join("?" * len(subscriptions)) if subscriptions else "''"

        with self._conn() as c:
            # Lock: find candidate ids, then claim them in a single UPDATE.
            params: list[Any] = [agent_id]
            sql = (
                "SELECT id, created_at, ttl_seconds FROM messages "
                "WHERE claimed_by IS NULL AND (to_agent = ?"
            )
            if subscriptions:
                sql += f" OR (to_agent IS NULL AND topic IN ({topic_placeholders}))"
                params.extend(subscriptions)
            sql += ") ORDER BY created_at ASC LIMIT ?"
            params.append(max_messages * 4)  # over-fetch to skip expired

            candidates = c.execute(sql, params).fetchall()
            usable_ids: list[str] = []
            for row in candidates:
                mid, created_at, ttl = row
                if (now - created_at) > ttl:
                    # TTL expired — sweep these so they don't accumulate.
                    c.execute("DELETE FROM messages WHERE id = ?", (mid,))
                    continue
                usable_ids.append(mid)
                if len(usable_ids) >= max_messages:
                    break
            if not usable_ids:
                return []

            # Claim atomically.
            placeholders = ",".join("?" * len(usable_ids))
            c.execute(
                f"UPDATE messages SET claimed_by = ?, claimed_at = ? "
                f"WHERE id IN ({placeholders}) AND claimed_by IS NULL",
                [agent_id, now, *usable_ids],
            )

            rows = c.execute(
                f"SELECT id, conversation_id, parent_conversation_id, from_agent, "
                f"to_agent, topic, msg_type, reply_to, hop_count, ttl_seconds, "
                f"created_at, payload "
                f"FROM messages WHERE id IN ({placeholders}) AND claimed_by = ?",
                [*usable_ids, agent_id],
            ).fetchall()

        return [self._row_to_message(r) for r in rows]

    def _row_to_message(self, row: tuple) -> dict:
        return {
            "id": row[0],
            "conversation_id": row[1],
            "parent_conversation_id": row[2],
            "from_agent": row[3],
            "to_agent": row[4],
            "topic": row[5],
            "msg_type": row[6],
            "reply_to": row[7],
            "hop_count": row[8],
            "ttl_seconds": row[9],
            "created_at": row[10],
            "payload": json.loads(row[11]),
        }

    # ---------- inspection (for the demo transcript) ----------

    def conversation_log(self, conversation_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, conversation_id, parent_conversation_id, from_agent, "
                "to_agent, topic, msg_type, reply_to, hop_count, ttl_seconds, "
                "created_at, payload "
                "FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def conversation_chain(self, root_conversation_id: str) -> dict[str, list[dict]]:
        """Walk descendants of a root conversation and return every linked
        conversation, keyed by its id, in chronological order within each.

        Returns: { conversation_id: [messages, ...], ... } including the root.
        """
        chain: dict[str, list[dict]] = {}
        to_visit = [root_conversation_id]
        visited: set[str] = set()
        while to_visit:
            cid = to_visit.pop()
            if cid in visited:
                continue
            visited.add(cid)
            chain[cid] = self.conversation_log(cid)
            # Find children: any conversation whose parent_conversation_id == cid
            with self._conn() as c:
                rows = c.execute(
                    "SELECT DISTINCT conversation_id FROM messages "
                    "WHERE parent_conversation_id = ?",
                    (cid,),
                ).fetchall()
            for (child_cid,) in rows:
                if child_cid not in visited:
                    to_visit.append(child_cid)
        return chain

    def find_root_conversation(self, conversation_id: str) -> str:
        """Walk parent links up to find the root conversation_id."""
        current = conversation_id
        seen: set[str] = set()
        while current not in seen:
            seen.add(current)
            with self._conn() as c:
                row = c.execute(
                    "SELECT parent_conversation_id FROM messages "
                    "WHERE conversation_id = ? AND parent_conversation_id IS NOT NULL "
                    "LIMIT 1",
                    (current,),
                ).fetchone()
            if row is None or row[0] is None:
                return current
            current = row[0]
        return current
