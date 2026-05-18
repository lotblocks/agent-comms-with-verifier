#!/usr/bin/env python3
"""Learning demo — run the three-agent flow twice, observe memories accumulate.

The MemoryStore lives in its own SQLite file outside the bus, so memories
survive across runs even when the bus DB is wiped. Each Beta/Gamma:
  - On startup, prints the current memory count
  - Before invoking the verifier, recalls per-skill memories and appends them
    as "MEMORY HINTS" to skill_doc
  - After verification, stores any gap reports as per-skill memories
  - Records its own success/failure as per-agent reputation

What you should see:
  Run 1: memory starts empty; gap memories stored at the end
  Run 2: memory starts non-empty; verifier sees the hints in its system prompt
         (mock verifier ignores them, LLM verifier uses them)

Run from this directory:
    python3 run_learning_demo.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bus import Bus
from agent_memory import MemoryStore


HERE = os.path.dirname(os.path.abspath(__file__))
BUS_DB = os.path.join(HERE, "_learning_bus.sqlite")
MEMORY_DB = os.path.join(HERE, "_learning_memory.sqlite")


def cleanup(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)


def memory_snapshot(label: str) -> None:
    """Pretty-print the current state of the memory DB."""
    store = MemoryStore(MEMORY_DB)
    total = store.count()
    print(f"  {label}: {total} memories total")
    for scope in store.all_scopes():
        scope_count = store.count(scope)
        print(f"    {scope}: {scope_count}")
        if scope_count > 0 and scope_count <= 5:
            for mem in store.recall(scope=scope, limit=5):
                content_preview = str(mem["content"])[:80]
                if len(str(mem["content"])) > 80:
                    content_preview += "..."
                print(f"      · [{mem['category']}] subject={mem['subject']!r} "
                      f"importance={mem['importance']}")
                print(f"        {content_preview}")


def run_one_pass(run_number: int) -> int:
    """Run one full three-agent flow. Bus DB is wiped; memory DB persists."""
    cleanup(BUS_DB)  # fresh bus state each pass

    env = os.environ.copy()
    env["AGENT_MEMORY_DB"] = MEMORY_DB   # all agents will use this memory file

    print(f"\n{'=' * 70}")
    print(f"RUN {run_number}")
    print('=' * 70)
    memory_snapshot(f"  Memory BEFORE run {run_number}")
    print()

    beta = subprocess.Popen(
        ["python3", os.path.join(HERE, "agent_beta.py"),
         "--db", BUS_DB, "--max-requests", "1", "--idle-timeout-sec", "30"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    gamma = subprocess.Popen(
        ["python3", os.path.join(HERE, "agent_gamma.py"),
         "--db", BUS_DB, "--max-requests", "1", "--idle-timeout-sec", "45"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    time.sleep(1.5)

    alpha = subprocess.run(
        ["python3", os.path.join(HERE, "agent_alpha.py"),
         "--db", BUS_DB,
         "--my-id", f"agent_alpha_run{run_number}",
         "--target-role", "writer",
         "--task", "write_report",
         "--intent", f"write a report paragraph (run {run_number})",
         "--wait-sec", "60"],
        env=env, capture_output=True, text=True, timeout=90,
    )

    for p in (beta, gamma):
        try:
            out, _ = p.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            p.kill(); out, _ = p.communicate()
        # Print just the meaningful lines (skip the noise)
        for line in out.rstrip().splitlines():
            if any(tok in line for tok in (
                "registered", "memory:", "received", "chain_status",
                "stored", "→ ", "← ", "TIMEOUT", "done", "error",
            )):
                print(f"    {line}")

    # Alpha's verdict line
    verdict = next(
        (l for l in reversed(alpha.stdout.splitlines()) if "chain is" in l),
        "(no result)",
    )
    print(f"    alpha · rc={alpha.returncode} · {verdict.strip()}")
    print()
    memory_snapshot(f"  Memory AFTER run {run_number}")

    return alpha.returncode


def main() -> int:
    print("=" * 70)
    print("LEARNING DEMO")
    print("Same task, two runs, shared memory DB — watch memories accumulate")
    print("=" * 70)

    # Wipe memory DB so the demo starts fresh.
    cleanup(MEMORY_DB)
    # Initialize empty store so .count() works.
    MemoryStore(MEMORY_DB)

    rc1 = run_one_pass(1)
    rc2 = run_one_pass(2)

    print(f"\n{'=' * 70}")
    print("DEMO COMPLETE")
    print('=' * 70)
    print(f"Run 1 exit: {rc1}")
    print(f"Run 2 exit: {rc2}")

    store = MemoryStore(MEMORY_DB)
    print()
    print("Final memory state by scope:")
    for scope in store.all_scopes():
        print(f"  {scope}: {store.count(scope)} memories")

    # Show what claim_guidelines look like — what the LLM verifier would see
    # on the next run.
    print()
    print("Rendered claim_guidelines for skill 'demo-compute-total' "
          "(what the LLM verifier would receive on the next run):")
    print()
    hints = store.claim_guidelines_for_skill("demo-compute-total")
    if hints:
        for line in hints.splitlines():
            print(f"    {line}")
    else:
        print("    (none)")

    return 0 if (rc1 == 0 and rc2 == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
