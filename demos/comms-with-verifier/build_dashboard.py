#!/usr/bin/env python3
"""Static HTML observability dashboard for the SQLite bus.

Reads the bus database and emits a single self-contained HTML page showing:
  - Live agents with heartbeat status
  - Conversations grouped by id, with full message timeline
  - Per-message verification status (color-coded)
  - Gap reports highlighted at the bottom

Usage:
    python3 build_dashboard.py [--db PATH] [--out PATH]

Defaults: reads _demo_bus.sqlite in this directory, writes dashboard.html
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone


HERE = os.path.dirname(os.path.abspath(__file__))


def fetch_agents(conn) -> list[dict]:
    now = time.time()
    rows = conn.execute(
        "SELECT id, name, role, subscriptions, last_heartbeat FROM agents"
    ).fetchall()
    agents = []
    for r in rows:
        age = now - r[4]
        agents.append({
            "id": r[0], "name": r[1], "role": r[2],
            "subscriptions": json.loads(r[3]),
            "last_heartbeat": r[4],
            "age_sec": age,
            "is_alive": age < 90,
        })
    agents.sort(key=lambda a: a["name"])
    return agents


def fetch_messages(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, conversation_id, from_agent, to_agent, topic, msg_type, "
        "reply_to, hop_count, ttl_seconds, created_at, payload FROM messages "
        "ORDER BY created_at ASC"
    ).fetchall()
    return [{
        "id": r[0], "conversation_id": r[1], "from_agent": r[2],
        "to_agent": r[3], "topic": r[4], "msg_type": r[5],
        "reply_to": r[6], "hop_count": r[7], "ttl_seconds": r[8],
        "created_at": r[9], "payload": json.loads(r[10]),
    } for r in rows]


def group_by_conversation(messages: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for m in messages:
        groups.setdefault(m["conversation_id"], []).append(m)
    return groups


def build_chains(messages: list[dict]) -> list[dict]:
    """Group conversations into parent/child chains using parent_conversation_id.

    Returns a list of chain dicts:
        {
            "root_conv_id": str,
            "conv_ids": [root, child, grandchild, ...],  # depth-first order
            "messages_by_conv": {cid: [msgs, ...]},
            "depth": int,
            "first_message_at": float,
            "last_message_at": float,
            "chain_summary": dict | None,    # from final response's chain_summary
        }
    Top-level conversations (parent_conversation_id is NULL) are roots.
    """
    by_conv = group_by_conversation(messages)

    # parent_id → list of child conv_ids
    children: dict[str, list[str]] = {}
    parents: dict[str, str | None] = {}
    for cid, msgs in by_conv.items():
        parent = None
        for m in msgs:
            if m.get("parent_conversation_id"):
                parent = m["parent_conversation_id"]
                break
        parents[cid] = parent
        if parent:
            children.setdefault(parent, []).append(cid)

    roots = [cid for cid, p in parents.items() if not p]

    chains = []
    for root in sorted(roots, key=lambda c: min(m["created_at"] for m in by_conv[c])):
        ordered_cids: list[str] = []
        def _dfs(cid: str, depth: int) -> int:
            ordered_cids.append(cid)
            max_depth = depth
            for child in sorted(children.get(cid, []),
                                key=lambda c: min(m["created_at"] for m in by_conv[c])):
                max_depth = max(max_depth, _dfs(child, depth + 1))
            return max_depth
        max_depth = _dfs(root, 0)

        all_msgs = [m for cid in ordered_cids for m in by_conv[cid]]
        # chain_summary: pull from the latest response in the root conversation
        chain_summary = None
        for m in reversed(by_conv[root]):
            if m["msg_type"] == "response" and isinstance(m["payload"], dict):
                if m["payload"].get("chain_summary"):
                    chain_summary = m["payload"]["chain_summary"]
                    break

        chains.append({
            "root_conv_id": root,
            "conv_ids": ordered_cids,
            "messages_by_conv": {cid: by_conv[cid] for cid in ordered_cids},
            "depth": max_depth,
            "first_message_at": min(m["created_at"] for m in all_msgs),
            "last_message_at": max(m["created_at"] for m in all_msgs),
            "chain_summary": chain_summary,
        })
    return chains


def extract_verification(payload: dict) -> dict | None:
    """If the payload includes a verification record, surface it."""
    if isinstance(payload, dict) and "verification" in payload:
        return payload["verification"]
    return None


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3] + "Z"


def fmt_age(sec: float) -> str:
    if sec < 1:
        return f"{int(sec * 1000)}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    return f"{int(sec // 60)}m {int(sec % 60)}s"


# ---------- HTML emission ----------

CSS = """
:root {
  --bg: #FAFAF7; --bg-card: #FFFFFF; --bg-subtle: #F2EFE8;
  --ink: #16140F; --ink-soft: #4A463E; --ink-mute: #8A8578;
  --rule: #E8E3D6; --accent: #B8482E; --accent-soft: #F4E4DA;
  --green: #2D5A1F; --green-bg: #E8F0E5;
  --amber: #8B6914; --amber-bg: #F4ECDA;
  --red: #8B2914; --red-bg: #F4DAD4;
  --blue: #2B4E7A; --blue-bg: #E0E8F0;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--ink);
  font-family: 'Inter', system-ui, sans-serif; font-size: 15px;
  line-height: 1.6; -webkit-font-smoothing: antialiased;
}
.container { max-width: 1080px; margin: 0 auto; padding: 56px 32px; }

/* Header */
.eyebrow {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  letter-spacing: 0.14em; text-transform: uppercase; color: var(--accent);
  margin-bottom: 20px;
}
h1 {
  font-family: 'Fraunces', Georgia, serif; font-weight: 400;
  font-size: 52px; line-height: 1.05; letter-spacing: -0.02em;
  margin-bottom: 16px;
}
h1 em { font-style: italic; color: var(--accent); font-weight: 300; }
.subtitle {
  font-family: 'Fraunces', serif; font-size: 18px; color: var(--ink-soft);
  font-style: italic; margin-bottom: 32px;
}
.meta-row {
  display: flex; gap: 32px; flex-wrap: wrap;
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-mute); letter-spacing: 0.04em; padding: 16px 0;
  border-top: 1px solid var(--rule); border-bottom: 1px solid var(--rule);
}
.meta-row strong { color: var(--ink-soft); font-weight: 500; }

/* Section heads */
section { margin-top: 56px; }
h2 {
  font-family: 'Fraunces', serif; font-weight: 500; font-size: 28px;
  letter-spacing: -0.015em; margin-bottom: 8px;
}
h2 .num {
  display: block; font-family: 'JetBrains Mono', monospace; font-size: 12px;
  color: var(--accent); letter-spacing: 0.1em; margin-bottom: 8px;
}
h2 .count {
  font-family: 'JetBrains Mono', monospace; font-size: 14px;
  color: var(--ink-mute); margin-left: 8px;
}
.sect-desc {
  font-size: 14px; color: var(--ink-mute); margin-bottom: 24px;
  font-style: italic;
}

/* Stats strip */
.stats {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px;
  margin: 24px 0;
}
.stat {
  background: var(--bg-card); border: 1px solid var(--rule);
  border-radius: 4px; padding: 18px 20px;
}
.stat .label {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.08em; color: var(--ink-mute);
  margin-bottom: 8px;
}
.stat .value {
  font-family: 'Fraunces', serif; font-size: 32px; color: var(--ink);
  font-weight: 500; line-height: 1;
}
.stat .sub {
  font-size: 11px; color: var(--ink-mute); margin-top: 4px;
  font-family: 'JetBrains Mono', monospace;
}

/* Agents */
.agents { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.agent {
  background: var(--bg-card); border: 1px solid var(--rule);
  border-radius: 4px; padding: 20px;
  display: grid; grid-template-columns: 1fr auto; gap: 12px;
  align-items: start;
}
.agent .id-line {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-mute); margin-bottom: 4px; letter-spacing: 0.04em;
}
.agent h3 {
  font-family: 'Fraunces', serif; font-size: 20px; font-weight: 500;
  color: var(--ink); margin: 0 0 4px;
}
.agent .role {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--accent); letter-spacing: 0.06em; text-transform: uppercase;
  margin-bottom: 10px;
}
.agent .subs {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-soft);
}
.status-pill {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.1em;
  padding: 4px 10px; border-radius: 11px; white-space: nowrap;
}
.status-pill.alive { background: var(--green-bg); color: var(--green); }
.status-pill.stale { background: var(--amber-bg); color: var(--amber); }
.heartbeat-age {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  color: var(--ink-mute); margin-top: 6px;
}

/* Chains (one chain = root + nested children) */
.chain {
  background: var(--bg-card); border: 1px solid var(--rule);
  border-radius: 4px; padding: 24px 28px; margin-bottom: 24px;
}
.chain .chain-head {
  display: flex; justify-content: space-between; align-items: start;
  padding-bottom: 16px; border-bottom: 1px solid var(--rule);
  margin-bottom: 16px; gap: 24px;
}
.chain .chain-id {
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
  color: var(--ink-soft); letter-spacing: 0.04em;
}
.chain .chain-title {
  font-family: 'Fraunces', serif; font-size: 18px;
  color: var(--ink); font-weight: 500; margin-top: 4px;
}
.chain-stats {
  display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px;
  margin-bottom: 20px;
}
.chain-stat {
  background: var(--bg-subtle); padding: 12px 14px; border-radius: 3px;
}
.chain-stat .l {
  font-family: 'JetBrains Mono', monospace; font-size: 9px;
  text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--ink-mute); margin-bottom: 4px;
}
.chain-stat .v {
  font-family: 'Fraunces', serif; font-size: 20px; color: var(--ink);
  font-weight: 500;
}
.chain-status-pill {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  text-transform: uppercase; letter-spacing: 0.1em;
  padding: 6px 14px; border-radius: 14px;
}
.chain-status-pill.verified { background: var(--green-bg); color: var(--green); }
.chain-status-pill.partial { background: var(--amber-bg); color: var(--amber); }
.chain-status-pill.failed { background: var(--red-bg); color: var(--red); }

.conversation {
  margin-top: 14px; padding-top: 14px;
  border-top: 1px dashed var(--rule);
}
.conversation:first-of-type { border-top: none; padding-top: 0; margin-top: 0; }
.conversation.nested {
  margin-left: 24px; padding-left: 16px;
  border-left: 2px solid var(--accent-soft);
}
.conversation .head {
  display: flex; justify-content: space-between; align-items: center;
  padding-bottom: 10px; margin-bottom: 12px;
}
.conversation .cid {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-soft); letter-spacing: 0.04em;
}
.conversation .cid .nested-tag {
  background: var(--accent-soft); color: var(--accent);
  padding: 2px 8px; border-radius: 2px; margin-left: 8px;
  font-size: 9px; text-transform: uppercase; letter-spacing: 0.08em;
}
.conversation .meta {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  color: var(--ink-mute);
}

.message {
  display: grid; grid-template-columns: 110px 1fr;
  gap: 16px; padding: 16px 0; border-bottom: 1px dashed var(--rule);
}
.message:last-child { border-bottom: none; }
.message .meta {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  color: var(--ink-mute); line-height: 1.5;
}
.message .meta strong { color: var(--ink-soft); }
.message .body {
  font-size: 14px;
}
.message .route {
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
  color: var(--ink); margin-bottom: 6px;
}
.message .arrow { color: var(--accent); margin: 0 6px; }
.message .type-pill {
  font-family: 'JetBrains Mono', monospace; font-size: 9px;
  text-transform: uppercase; letter-spacing: 0.08em;
  padding: 2px 8px; border-radius: 2px;
  background: var(--bg-subtle); color: var(--ink-soft);
  margin-left: 8px;
}
.message .type-pill.request { background: var(--blue-bg); color: var(--blue); }
.message .type-pill.response { background: var(--green-bg); color: var(--green); }
.message .type-pill.event { background: var(--amber-bg); color: var(--amber); }

.payload-preview {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-soft); margin-top: 4px;
  background: var(--bg-subtle); padding: 8px 12px; border-radius: 3px;
  white-space: pre-wrap; word-break: break-word;
}

.verification-banner {
  display: inline-flex; align-items: center; gap: 8px;
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  padding: 6px 12px; border-radius: 3px; margin-top: 8px;
  letter-spacing: 0.04em;
}
.verification-banner.verified { background: var(--green-bg); color: var(--green); }
.verification-banner.partial { background: var(--amber-bg); color: var(--amber); }
.verification-banner.failed { background: var(--red-bg); color: var(--red); }
.verification-banner strong { font-weight: 600; }
.verification-banner .sep { color: var(--ink-mute); margin: 0 4px; }

.claims-mini {
  display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap;
}
.claim-chip {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  padding: 3px 8px; border-radius: 10px; letter-spacing: 0.03em;
}
.claim-chip.pass { background: var(--green-bg); color: var(--green); }
.claim-chip.fail { background: var(--red-bg); color: var(--red); }
.claim-chip.unverifiable { background: var(--amber-bg); color: var(--amber); }

/* Gap reports */
.gap-report {
  background: var(--bg-card); border-left: 3px solid var(--accent);
  border-radius: 0 4px 4px 0;
  padding: 20px 24px; margin-bottom: 16px;
}
.gap-report .src {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  color: var(--ink-mute); margin-bottom: 6px; letter-spacing: 0.05em;
}
.gap-report h3 {
  font-family: 'Fraunces', serif; font-size: 18px; font-weight: 500;
  margin-bottom: 8px;
}
.gap-report .summary {
  font-size: 14px; color: var(--ink-soft); margin-bottom: 16px;
  font-style: italic;
}
.improvement {
  border-top: 1px solid var(--rule); padding-top: 12px; margin-top: 12px;
}
.improvement .claim-statement {
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-soft); margin-bottom: 6px;
}
.improvement .proposed {
  font-size: 13px; line-height: 1.55; color: var(--ink);
  padding: 10px 14px; background: var(--accent-soft); border-radius: 3px;
}
.improvement .proposed strong {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  text-transform: uppercase; letter-spacing: 0.08em; color: var(--accent);
  display: block; margin-bottom: 4px;
}
.improvement .conf {
  font-family: 'JetBrains Mono', monospace; font-size: 10px;
  color: var(--ink-mute); margin-top: 6px;
}

/* Empty */
.empty {
  background: var(--bg-card); border: 1px dashed var(--rule);
  border-radius: 4px; padding: 32px; text-align: center;
  color: var(--ink-mute); font-style: italic; font-size: 14px;
}

footer {
  margin-top: 96px; padding: 32px 0; border-top: 1px solid var(--rule);
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
  color: var(--ink-mute); text-align: center; letter-spacing: 0.04em;
}

@media (max-width: 720px) {
  .agents, .stats { grid-template-columns: 1fr; }
  h1 { font-size: 36px; }
  .message { grid-template-columns: 1fr; }
}
"""


def render(*, db_path: str, agents: list[dict], messages: list[dict],
           snapshot_at: float) -> str:
    convos = group_by_conversation(messages)
    snapshot_str = datetime.fromtimestamp(snapshot_at, tz=timezone.utc).strftime(
        "%Y-%m-%d · %H:%M:%S UTC"
    )

    # Collect gap reports — prefer chain_summary.merged_gap_report (already
    # deduped across the chain) over per-message verification.gap_report.
    gap_reports: list[tuple[dict, str]] = []  # (gap_report, source_message_id)
    seen_sources = set()
    for m in messages:
        if not isinstance(m["payload"], dict):
            continue
        cs = m["payload"].get("chain_summary")
        if cs and cs.get("merged_gap_report"):
            gap_reports.append((cs["merged_gap_report"], m["id"]))
            seen_sources.add(m["id"])
            continue
        # Fallback: per-message gap report (only for messages not already covered
        # by a chain-merged report).
        v = extract_verification(m["payload"])
        if v and v.get("gap_report") and m["id"] not in seen_sources:
            gap_reports.append((v["gap_report"], m["id"]))

    # Aggregate stats
    alive = sum(1 for a in agents if a["is_alive"])
    response_verdicts: dict[str, int] = {}
    for m in messages:
        v = extract_verification(m["payload"])
        if v:
            response_verdicts[v["status"]] = response_verdicts.get(v["status"], 0) + 1

    out = ["<!DOCTYPE html>", '<html lang="en">', "<head>",
           '<meta charset="UTF-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
           "<title>Bus Observability Dashboard</title>",
           '<link rel="preconnect" href="https://fonts.googleapis.com">',
           '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
           '<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,400;9..144,500;9..144,600&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">',
           "<style>", CSS, "</style>",
           "</head>", "<body>", '<div class="container">']

    # Header
    out += [
        '<div class="eyebrow">Bus snapshot · Reference No. 03 series</div>',
        '<h1>The conversation, <em>visible.</em></h1>',
        '<div class="subtitle">A static read of the multi-agent bus — every agent, every message, every verdict.</div>',
        '<div class="meta-row">',
        f'<span><strong>SNAPSHOT</strong> &nbsp;{snapshot_str}</span>',
        f'<span><strong>DATABASE</strong> &nbsp;{html.escape(os.path.basename(db_path))}</span>',
        f'<span><strong>AGENTS</strong> &nbsp;{len(agents)} ({alive} alive)</span>',
        f'<span><strong>CONVERSATIONS</strong> &nbsp;{len(convos)}</span>',
        f'<span><strong>MESSAGES</strong> &nbsp;{len(messages)}</span>',
        '</div>',
    ]

    # Stats strip
    verified_count = response_verdicts.get("verified", 0)
    partial_count = response_verdicts.get("partial", 0)
    failed_count = response_verdicts.get("failed", 0)
    out += [
        '<div class="stats">',
        f'<div class="stat"><div class="label">Verified replies</div>'
        f'<div class="value">{verified_count}</div>'
        f'<div class="sub">all claims pass</div></div>',
        f'<div class="stat"><div class="label">Partial replies</div>'
        f'<div class="value">{partial_count}</div>'
        f'<div class="sub">no fails, ≥1 unverifiable</div></div>',
        f'<div class="stat"><div class="label">Failed replies</div>'
        f'<div class="value">{failed_count}</div>'
        f'<div class="sub">at least 1 claim failed</div></div>',
        f'<div class="stat"><div class="label">Gap reports</div>'
        f'<div class="value">{len(gap_reports)}</div>'
        f'<div class="sub">doc improvement candidates</div></div>',
        '</div>',
    ]

    # Agents section
    out += [
        '<section>',
        f'<h2><span class="num">§ 01 — Registry</span>Live agents<span class="count">· {len(agents)}</span></h2>',
        '<div class="sect-desc">Heartbeat-based liveness. Agents that miss more than 90 seconds become stale.</div>',
    ]
    if agents:
        out.append('<div class="agents">')
        for a in agents:
            subs = ", ".join(a["subscriptions"]) if a["subscriptions"] else "—"
            status_cls = "alive" if a["is_alive"] else "stale"
            status_lbl = "ALIVE" if a["is_alive"] else "STALE"
            out += [
                '<div class="agent">',
                '<div>',
                f'<div class="id-line">{html.escape(a["id"])}</div>',
                f'<h3>{html.escape(a["name"])}</h3>',
                f'<div class="role">{html.escape(a["role"])}</div>',
                f'<div class="subs">subs: {html.escape(subs)}</div>',
                '</div>',
                f'<div>'
                f'<div class="status-pill {status_cls}">{status_lbl}</div>'
                f'<div class="heartbeat-age">last beat: {fmt_age(a["age_sec"])} ago</div>'
                f'</div>',
                '</div>',
            ]
        out.append('</div>')
    else:
        out.append('<div class="empty">No agents registered.</div>')
    out.append('</section>')

    # Chains section — group parent/child conversations together
    chains = build_chains(messages)
    out += [
        '<section>',
        f'<h2><span class="num">§ 02 — Chains</span>End-to-end conversations<span class="count">· {len(chains)}</span></h2>',
        '<div class="sect-desc">Each chain is a root conversation plus any nested sub-conversations a worker started to fulfill it. Stats are end-to-end (weakest-link trust, total cost, total duration).</div>',
    ]
    if chains:
        for chain in chains:
            cs = chain.get("chain_summary") or {}
            chain_status = cs.get("chain_status", "—")
            duration_sec = chain["last_message_at"] - chain["first_message_at"]
            n_msgs = sum(len(msgs) for msgs in chain["messages_by_conv"].values())
            out += [
                '<div class="chain">',
                '<div class="chain-head">',
                '<div>',
                f'<div class="chain-id">root: {html.escape(chain["root_conv_id"])}</div>',
                f'<div class="chain-title">'
                f'{len(chain["conv_ids"])} conversation(s) · {n_msgs} message(s) · depth {chain["depth"]}'
                f'</div>',
                '</div>',
                f'<div class="chain-status-pill {chain_status}">'
                f'chain: {chain_status}</div>',
                '</div>',
            ]
            if cs:
                out += [
                    '<div class="chain-stats">',
                    f'<div class="chain-stat"><div class="l">Hops</div>'
                    f'<div class="v">{cs.get("hop_count", "?")}</div></div>',
                    f'<div class="chain-stat"><div class="l">Total attempts</div>'
                    f'<div class="v">{cs.get("total_attempts", "?")}</div></div>',
                    f'<div class="chain-stat"><div class="l">Total cost</div>'
                    f'<div class="v">${cs.get("total_cost_usd", 0):.4f}</div></div>',
                    f'<div class="chain-stat"><div class="l">Wall duration</div>'
                    f'<div class="v">{duration_sec:.2f}s</div></div>',
                    '</div>',
                ]

            # Render each conversation in the chain.
            for idx, cid in enumerate(chain["conv_ids"]):
                msgs = chain["messages_by_conv"][cid]
                is_nested = idx > 0
                conv_dur = msgs[-1]["created_at"] - msgs[0]["created_at"]
                nested_tag = '<span class="nested-tag">SUB</span>' if is_nested else ''
                cls = "conversation nested" if is_nested else "conversation"
                out += [
                    f'<div class="{cls}">',
                    '<div class="head">',
                    f'<div class="cid">{html.escape(cid)}{nested_tag}</div>',
                    f'<div class="meta">{len(msgs)} msgs · {conv_dur:.2f}s</div>',
                    '</div>',
                ]
                for m in msgs:
                    target = m["to_agent"] if m["to_agent"] else f'#{m["topic"]}'
                    v = extract_verification(m["payload"])
                    payload_preview = _summarize_payload(m["payload"])
                    out += [
                        '<div class="message">',
                        f'<div class="meta">'
                        f'<div><strong>{fmt_time(m["created_at"])}</strong></div>'
                        f'<div>hop={m["hop_count"]} ttl={m["ttl_seconds"]}s</div>'
                        f'<div>{html.escape(m["id"])}</div>'
                        f'</div>',
                        '<div class="body">',
                        f'<div class="route">'
                        f'<span>{html.escape(m["from_agent"])}</span>'
                        f'<span class="arrow">→</span>'
                        f'<span>{html.escape(target)}</span>'
                        f'<span class="type-pill {m["msg_type"]}">{m["msg_type"]}</span>'
                        f'</div>',
                        f'<div class="payload-preview">{html.escape(payload_preview)}</div>',
                    ]
                    if v:
                        status = v["status"]
                        claims = v.get("claims", [])
                        passes = sum(1 for c in claims if c["verdict"] == "pass")
                        fails = sum(1 for c in claims if c["verdict"] == "fail")
                        unv = sum(1 for c in claims if c["verdict"] == "unverifiable")
                        cost = v.get("cost_usd", 0.0)
                        out.append(
                            f'<div class="verification-banner {status}">'
                            f'<strong>verification:</strong> {status} '
                            f'<span class="sep">·</span> '
                            f'{len(claims)} claims '
                            f'<span class="sep">·</span> '
                            f'{v.get("verifier_model","?")} '
                            f'<span class="sep">·</span> '
                            f'${cost:.4f}'
                            f'</div>'
                        )
                        chips = []
                        if passes:
                            chips.append(f'<span class="claim-chip pass">{passes} pass</span>')
                        if fails:
                            chips.append(f'<span class="claim-chip fail">{fails} fail</span>')
                        if unv:
                            chips.append(f'<span class="claim-chip unverifiable">{unv} unverifiable</span>')
                        if chips:
                            out.append('<div class="claims-mini">' + "".join(chips) + '</div>')
                    out += ['</div>', '</div>']
                out.append('</div>')
            out.append('</div>')
    else:
        out.append('<div class="empty">No conversations on the bus.</div>')
    out.append('</section>')

    # Gap reports section
    out += [
        '<section>',
        f'<h2><span class="num">§ 03 — Gap reports</span>Doc improvement candidates<span class="count">· {len(gap_reports)}</span></h2>',
        '<div class="sect-desc">When the verifier marks a claim unverifiable, it proposes a documentation change that would make the claim verifiable next time. These are the suggestions awaiting a draft-card review.</div>',
    ]
    if gap_reports:
        for gap, src_mid in gap_reports:
            # Merged reports use skill_ids (plural); per-message reports use skill_id.
            skill_label = (
                ", ".join(gap.get("skill_ids", []))
                if gap.get("skill_ids")
                else gap.get("skill_id", "?")
            )
            is_merged = bool(gap.get("skill_ids"))
            merged_tag = ' · MERGED ACROSS CHAIN' if is_merged else ''
            out += [
                '<div class="gap-report">',
                f'<div class="src">FROM MESSAGE {html.escape(src_mid)} · '
                f'SKILL(S) {html.escape(skill_label)}{merged_tag}</div>',
                f'<h3>{len(gap.get("unverifiable_claims", []))} unverifiable claim(s)</h3>',
                f'<div class="summary">{html.escape(gap.get("summary", ""))}</div>',
            ]
            for imp in gap.get("proposed_improvements", []):
                # find the claim text
                claim_text = ""
                for c in gap.get("unverifiable_claims", []):
                    if c["id"] == imp["claim_id"]:
                        claim_text = c["statement"]
                        break
                origin = imp.get("_skill_id")
                origin_tag = f" · origin: {origin}" if origin else ""
                out += [
                    '<div class="improvement">',
                    f'<div class="claim-statement">claim: {html.escape(claim_text)}{html.escape(origin_tag)}</div>',
                    '<div class="proposed">',
                    f'<strong>PROPOSED ({imp.get("target","documentation").upper()})</strong>',
                    html.escape(imp.get("proposed_text", "")),
                    '</div>',
                    f'<div class="conf">confidence: {imp.get("confidence", 0):.0%}</div>',
                    '</div>',
                ]
            out.append('</div>')
    else:
        out.append('<div class="empty">No gap reports in this snapshot. Either no verifier ran, or every claim was verifiable.</div>')
    out.append('</section>')

    out.append('<footer>Bus snapshot · generated by build_dashboard.py · '
               'reload by re-running the demo and the generator</footer>')
    out += ['</div>', '</body>', '</html>']
    return "\n".join(out)


def _summarize_payload(payload: dict) -> str:
    """Concise one-line summary of a payload."""
    if not isinstance(payload, dict):
        return json.dumps(payload)[:200]
    # Drop heavy fields for the preview
    light = {k: v for k, v in payload.items() if k not in ("verification", "result")}
    if "result" in payload:
        result_val = payload["result"]
        if isinstance(result_val, dict):
            light["result"] = "(object: " + ", ".join(result_val.keys()) + ")"
        else:
            light["result"] = str(result_val)[:80]
    s = json.dumps(light, ensure_ascii=False)
    return s if len(s) < 240 else s[:237] + "..."


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(HERE, "_demo_bus.sqlite"))
    parser.add_argument("--out", default=os.path.join(HERE, "dashboard.html"))
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"DB not found: {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    try:
        agents = fetch_agents(conn)
        messages = fetch_messages(conn)
    finally:
        conn.close()

    html_out = render(
        db_path=args.db,
        agents=agents,
        messages=messages,
        snapshot_at=time.time(),
    )

    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"Wrote {args.out}")
    print(f"  agents={len(agents)}  messages={len(messages)}  "
          f"conversations={len({m['conversation_id'] for m in messages})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
