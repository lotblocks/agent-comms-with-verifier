#!/usr/bin/env python3
"""Toy builder for end-to-end testing of the verifier orchestrator.

Behavior:
- If REMEDIATION_PROMPT env var is empty: produce a deliberately incomplete
  output (missing the "amount" and "timestamp" fields that the mock verifier
  expects).
- If REMEDIATION_PROMPT is present: read it, infer what was missing, and
  produce a complete output.

This is the "builder agent" stand-in. In production, this would be replaced
by a call to RunWithCredentials against a real Hyperagent skill.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def main() -> None:
    remediation = os.environ.get("REMEDIATION_PROMPT", "").strip()

    if not remediation:
        # First attempt — deliberately incomplete to exercise the loop.
        output = {
            "status": "test",
            "report": "transaction processed",
        }
    else:
        # Remediated — include the missing fields the verifier flagged.
        # A real builder would parse the remediation prompt and reason about
        # what to change. For the toy, we just include everything plausible.
        output = {
            "status": "test",
            "report": "transaction processed",
            "amount": 100.00,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "_remediation_acknowledged": True,
        }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
