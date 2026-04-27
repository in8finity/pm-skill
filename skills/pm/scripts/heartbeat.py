#!/usr/bin/env python3
"""Append a TaskHeartbeat for a task currently in `working` phase.

A worker calls this periodically to signal "I'm still alive on this task".
The supervisor (sweep.py) uses heartbeat freshness to detect dead claimants.

Usage:
  heartbeat.py --task SHA [--agent ID]

Refuses if the task's current TaskStatus is not `working`.

Exit codes:
  0  heartbeat appended
  6  task not in `working` phase — heartbeat is meaningless
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

import store


def default_agent_id() -> str:
    return os.environ.get("PM_AGENT_ID") or f"{socket.gethostname()}-{os.getpid()}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task", required=True)
    p.add_argument("--agent", default=default_agent_id(),
                   help="agent identifier (default: $PM_AGENT_ID or hostname-pid)")
    args = p.parse_args()

    latest = store.latest_status(args.task)
    if not latest or store.status_value(latest) != "working":
        sys.stderr.write(
            f"refusing: task {args.task[:12]} is not in 'working' phase\n"
        )
        return 6

    hb = store.append_heartbeat(args.task, args.agent, latest["text_sha256"])
    print(json.dumps(hb, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
