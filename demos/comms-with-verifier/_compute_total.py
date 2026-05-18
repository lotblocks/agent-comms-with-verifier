#!/usr/bin/env python3
"""Toy builder for the demo: computes a total of line items and returns JSON.

Behavior matches the mock verifier's expectations so the demo exercises the
full loop deterministically:
- First attempt: returns the total but omits timestamp / amount metadata.
- With REMEDIATION_PROMPT set: returns the full schema.

In a real deployment, this would be replaced by an actual data-fetching or
computation skill invoked via RunWithCredentials.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def main() -> None:
    # Hard-coded line items for the demo; in reality these would come from args.
    line_items = [
        {"item": "design audit", "cost": 40.0},
        {"item": "implementation",  "cost": 60.0},
    ]
    total = sum(li["cost"] for li in line_items)

    remediation = os.environ.get("REMEDIATION_PROMPT", "").strip()

    if not remediation:
        # First attempt — minimal output, missing fields the verifier expects.
        output = {
            "status": "test",
            "report": "totals computed",
            "items": line_items,
        }
    else:
        # Remediated — include all fields the verifier flagged.
        output = {
            "status": "test",
            "report": "totals computed",
            "items": line_items,
            "amount": total,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "_remediation_acknowledged": True,
        }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
