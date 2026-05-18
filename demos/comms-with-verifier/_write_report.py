#!/usr/bin/env python3
"""Toy builder for Gamma the writer.

Reads two env vars:
  - DATA_INPUT — JSON string from Beta's verified output (Gamma passes it through)
  - REMEDIATION_PROMPT — set by the verifier if a previous attempt failed

Behavior matches the mock verifier's expectations so the demo exercises the
full loop deterministically:
  - First attempt: returns a paragraph but omits the timestamp / amount fields
                   the verifier wants to see in the output payload
  - With REMEDIATION_PROMPT set: returns the complete schema
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone


def main() -> None:
    data_input = os.environ.get("DATA_INPUT", "{}")
    try:
        data = json.loads(data_input)
    except json.JSONDecodeError:
        data = {}

    items = data.get("items", [])
    total = data.get("amount", "?")
    item_count = len(items)

    paragraph = (
        f"Project total: ${total}. Across {item_count} line items, "
        f"this represents a balanced allocation between {items[0]['item']!r} "
        f"and {items[1]['item']!r}." if item_count >= 2
        else f"Project total: ${total}."
    )

    remediation = os.environ.get("REMEDIATION_PROMPT", "").strip()

    if not remediation:
        # First attempt — minimal output
        output = {
            "status": "test",
            "report": paragraph,
        }
    else:
        # Remediated — include the full schema the verifier expects
        output = {
            "status": "test",
            "report": paragraph,
            "amount": total,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "_remediation_acknowledged": True,
            "_based_on_data": data,
        }

    print(json.dumps(output))


if __name__ == "__main__":
    main()
