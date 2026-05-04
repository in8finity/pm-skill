#!/usr/bin/env python3
"""Append a TaskHeartbeat for a task currently in `working` phase.

A worker calls this periodically to signal "I'm still alive on this task".
The supervisor (sweep.py) uses heartbeat freshness to detect dead claimants.

Usage:
  heartbeat.py --task SHA [--agent ID] [--context-id ID]

Refuses if:
  * the task's current TaskStatus is not `working` (exit 6) — heartbeat
    is meaningless on a non-working task; OR
  * a sticky chain disagrees with the agent's PM_CONTEXT_ID (exit 10)
    — a heartbeat from a context outside the sticky binding would
    falsely keep the chain "alive" for a worker that doesn't own it; OR
  * the current working status is owned by a different agent (exit 12) —
    a zombie heartbeat from a process that lost its lease (e.g. via
    sweep+reclaim then re-claim by another worker) would otherwise
    falsely keep the new owner's task "fresh" and prevent legitimate
    reclamation if that new owner dies.

Exit codes:
  0   heartbeat appended
  6   task not in `working` phase — heartbeat is meaningless
  10  sticky-context refusal (chain bound to a different PM_CONTEXT_ID)
  12  current working status is owned by a different agent (lease lost)
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

import store


def default_agent_id() -> str:
    """Agent identifier for the heartbeat. Resolution order:
        1. ``$PM_AGENT_ID`` (explicit override)
        2. ``worker-<PM_CONTEXT_ID[:12]>`` if a context_id is set —
           STABLE across subshells of the same session, which prevents
           the lease-lost-on-PID-change footgun a worker hits when
           heartbeating from a fresh subshell of the original claimer.
        3. ``hostname-pid`` (legacy fallback for non-sticky one-shots)
    """
    if env := os.environ.get("PM_AGENT_ID"):
        return env
    if ctx := os.environ.get("PM_CONTEXT_ID"):
        return f"worker-{ctx[:12]}"
    return f"{socket.gethostname()}-{os.getpid()}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task", required=True)
    p.add_argument("--agent", default=default_agent_id(),
                   help="agent identifier (default: $PM_AGENT_ID, else "
                        "worker-<PM_CONTEXT_ID[:12]>, else hostname-pid)")
    p.add_argument("--context-id", default=None,
                   help="sticky context id (overrides $PM_CONTEXT_ID)")
    args = p.parse_args()

    latest = store.latest_status(args.task)
    if not latest or store.status_value(latest) != "working":
        sys.stderr.write(
            f"refusing: task {args.task[:12]} is not in 'working' phase\n"
        )
        return 6

    # Sticky-chain check (matches executing/report/finished). Closes
    # the gap where a heartbeat from outside the sticky binding would
    # falsely keep the chain "alive" for a worker without ownership.
    agent_context = args.context_id or os.environ.get("PM_CONTEXT_ID") or None
    try:
        store.check_sticky_eligibility(args.task, agent_context)
    except (store.StickyContextMismatch, store.StickyContextConflict) as e:
        sys.stderr.write(f"refusing: {e}\n")
        return 10

    owner = (latest.get("attributes") or {}).get("agent")
    if owner and owner != args.agent:
        sys.stderr.write(
            f"refusing: task {args.task[:12]} working status is owned by "
            f"'{owner}', not '{args.agent}' — lease lost (zombie heartbeat)\n"
        )
        return 12

    hb = store.append_heartbeat(args.task, args.agent, latest["record_sha256"])
    print(json.dumps(hb, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
