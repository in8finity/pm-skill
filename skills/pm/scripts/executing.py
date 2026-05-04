#!/usr/bin/env python3
"""Mark a task as ``working``. Refuses if its current status is not ``new``.

Usage:
  executing.py --task SHA [--agent ID] [--note "..."]

Exit codes:
  0  — claim won, agent owns this task
  6  — pre-claim refusal: task is already not ``new``
  8  — claim race lost: another agent claimed off the same prev-tip first
       (hashharness's `chain_predecessor` compare-and-swap on `prevStatus`
       rejected our append with 'head moved')
  10 — sticky-context refusal: task is sticky and PM_CONTEXT_ID either is
       unset or doesn't match the bound context of an ancestor/dep

Race-safety: native `chain_predecessor` head-move check on the
TaskStatus chain — see ``system-models/reports/planning-enforcement.md``.

Sticky tasks: if ``Task.attributes.sticky`` is set, ``$PM_CONTEXT_ID``
must match (and not conflict with) the contexts bound to any sticky
parent or dep in the task's chain. The claim records ``context_id`` on
the working TaskStatus so subsequent heartbeat/report/finished can
verify the same agent.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys

import store


def default_agent_id() -> str:
    """Agent identifier for the claim. Resolution order:
        1. ``$PM_AGENT_ID`` (explicit override)
        2. ``worker-<PM_CONTEXT_ID[:12]>`` if a context_id is set —
           stable across subshells of the same session, so the
           heartbeat that follows a claim from a fresh subshell still
           owns the lease.
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
    p.add_argument("--agent", default=default_agent_id())
    p.add_argument("--note", default="",
                   help="ignored (legacy compat); use --agent for claimant id")
    p.add_argument("--context-id", default=None,
                   help="sticky context id (overrides $PM_CONTEXT_ID)")
    args = p.parse_args()

    latest_before = store.latest_status(args.task)
    current = store.status_value(latest_before)
    if current != "new":
        sys.stderr.write(f"refusing: task {args.task[:12]} status is '{current}', expected 'new'\n")
        return 6

    prev_sha = latest_before["record_sha256"]
    agent_context = args.context_id or os.environ.get("PM_CONTEXT_ID") or None

    task = store.get_task(args.task)
    is_sticky = store.task_is_sticky(task)

    if is_sticky:
        if not agent_context:
            sys.stderr.write(
                f"refusing: task {args.task[:12]} is sticky but PM_CONTEXT_ID is not set. "
                f"Set it via: export PM_CONTEXT_ID=$(pm context-id)\n"
            )
            return 10
        try:
            store.check_sticky_eligibility(args.task, agent_context)
        except (store.StickyContextMismatch, store.StickyContextConflict) as e:
            sys.stderr.write(f"refusing: {e}\n")
            return 10

    try:
        status = store.append_claim(
            args.task, args.agent, prev_sha,
            context_id=agent_context if is_sticky else None,
        )
    except store.ClaimLost:
        sys.stderr.write(
            f"lost race for task {args.task[:12]} — another agent claimed off the same prev-tip\n"
        )
        return 8

    print(json.dumps(status, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
