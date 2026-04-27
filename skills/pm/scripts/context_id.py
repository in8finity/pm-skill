#!/usr/bin/env python3
"""Generate a fresh context UUID for a worker session.

Sticky tasks are bound to a context at first claim. The agent that
claimed must produce the same context id on every subsequent call
(heartbeat, report, finished) for the lifetime of the binding.
Reclaim strips the binding; sweep reclaim does the same.

Usage:
  pm context-id            # bare UUID — for capturing into env
  pm context-id --export   # `export PM_CONTEXT_ID=<uuid>` — eval-friendly

Typical worker setup:
  export PM_CONTEXT_ID=$(pm context-id)
  # or
  eval "$(pm context-id --export)"
"""
from __future__ import annotations

import argparse
import sys
import uuid


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--export", action="store_true",
                   help="emit `export PM_CONTEXT_ID=...` for shell eval")
    args = p.parse_args()
    cid = str(uuid.uuid4())
    print(f"export PM_CONTEXT_ID={cid}" if args.export else cid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
