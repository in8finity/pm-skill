#!/usr/bin/env python3
"""Print current UTC time as ISO 8601 (e.g. 2026-04-27T01:35:00.123Z)."""
from datetime import datetime, timezone


def now_iso() -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return now.replace("+00:00", "Z")


if __name__ == "__main__":
    print(now_iso())
