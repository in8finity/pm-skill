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
  15 — parent-claim refusal: this task has a parentTask in `new`. Claim
       the parent first to bind the subtree's lifecycle. (Also enforced
       at `pm next` / `pm pull` selection — this is the hard claim-time
       gate that catches direct dispatch / build-task-body resume.)
  16 — task exists but has no TaskStatus on the chain. Most likely a
       transient genesis-read race (the TaskStatus(new) write hasn't
       become visible yet); retry once. Persistent → the chain is
       corrupt and needs operator inspection.
  17 — no Task with that text_sha256 exists. Likely a wrong/hallucinated
       sha; check input. Distinct from 16 so retries don't waste cycles
       on a sha that will never resolve.

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


def default_agent_id(context_id: str | None = None) -> str:
    """Agent identifier for the claim. Resolution order:
        1. ``$PM_AGENT_ID`` (explicit override)
        2. ``worker-<context-id[:12]>`` if a context_id is supplied
           (via --context-id flag or $PM_CONTEXT_ID env) — stable across
           subshells of the same session, so the heartbeat that follows
           a claim from a fresh subshell still owns the lease.
        3. ``hostname-pid`` (legacy fallback for non-sticky one-shots)
    """
    if env := os.environ.get("PM_AGENT_ID"):
        return env
    ctx = context_id or os.environ.get("PM_CONTEXT_ID")
    if ctx:
        return f"worker-{ctx[:12]}"
    return f"{socket.gethostname()}-{os.getpid()}"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task", required=True)
    p.add_argument("--agent", default=None,
                   help="agent identifier (default: $PM_AGENT_ID, else "
                        "worker-<context-id[:12]> from --context-id flag or "
                        "$PM_CONTEXT_ID env, else hostname-pid)")
    p.add_argument("--note", default="",
                   help="ignored (legacy compat); use --agent for claimant id")
    p.add_argument("--context-id", default=None,
                   help="sticky context id (overrides $PM_CONTEXT_ID)")
    args = p.parse_args()
    if args.agent is None:
        args.agent = default_agent_id(args.context_id)

    # Fast path: one find_tip with where_attributes={status:"new"} +
    # fields=[record_sha256] gives us the CAS prev-link in a single round
    # trip when the task is claimable. On non-match we fall back to the
    # full latest_status path to surface the right diagnostic exit code
    # (17 missing task / 16 orphan task / 6 wrong status) — those paths
    # are rarer than the common "claim succeeds" path, so paying the
    # extra round trip there is fine.
    matched_tip = store.latest_status_if_new(args.task)
    if matched_tip is not None:
        prev_sha = matched_tip["record_sha256"]
    else:
        latest_before = store.latest_status(args.task)
        if latest_before is None:
            # Distinguish "task doesn't exist" from "task exists but has
            # no genesis TaskStatus yet". The first is a wrong/hallucinated
            # sha (worker should give up and check input); the second is a
            # transient genesis-read race (worker should retry once).
            # Conflating them sends operators down the wrong recovery path.
            if store.get_task(args.task) is None:
                sys.stderr.write(
                    f"refusing: no Task with text_sha256={args.task[:12]} "
                    f"found. Check your sha — typo or hallucinated reference. "
                    f"Use `pm show --task <sha>` or look up by slug instead.\n"
                )
                return 17
            sys.stderr.write(
                f"refusing: task {args.task[:12]} exists but has no "
                f"TaskStatus on the chain — likely a transient genesis-read "
                f"race. Retry once; if it persists the task's chain is "
                f"corrupt and needs operator inspection.\n"
            )
            return 16
        current = store.status_value(latest_before)
        sys.stderr.write(f"refusing: task {args.task[:12]} status is '{current}', expected 'new'\n")
        return 6
    agent_context = args.context_id or os.environ.get("PM_CONTEXT_ID") or None

    task = store.get_task(args.task)
    is_sticky = store.task_is_sticky(task)

    # Parent-claim gate (also enforced at pm next / pm pull selection).
    # The convention is "child can't start until parent's lifecycle has
    # begun"; this hard gate makes it real regardless of how the worker
    # arrived at the sha (direct dispatch, build-task-body, resume, etc.).
    # **Universal across queues** — when the parent lives on a
    # different queue from this task, fall back to a global
    # record-sha lookup rather than skipping the gate. See
    # planning_parent_gate.als#ChildBlockedUntilParentClaimed and
    # #CrossQueueChildBlockedUntilParentClaimed.
    parent_record = (task.get("links") or {}).get("parentTask") if task else None
    if parent_record:
        queue = ((task or {}).get("attributes") or {}).get("queue", "default")
        parent_status: str | None = None
        parent_text: str | None = None
        for sibling in store.list_tasks(queue):
            if sibling.get("record_sha256") == parent_record:
                parent_text = sibling["text_sha256"]
                parent_status = store.status_value(store.latest_status(parent_text))
                break
        if parent_status is None:
            # Parent on a different queue — global lookup. Returns
            # None for truly dangling links; only refuse on `new`.
            parent_status = store.task_status_by_record_sha(parent_record)
        if parent_status == "new":
            label = parent_text[:12] if parent_text else "(cross-queue)"
            sys.stderr.write(
                f"refusing: task {args.task[:12]} has parent "
                f"{label} still in `new` — claim the parent first to "
                f"bind the subtree's lifecycle. See `skills/pm/plan/"
                f"SKILL.md` \"Parents are grouping nodes\".\n"
            )
            return 15

    if is_sticky:
        if not agent_context:
            sys.stderr.write(
                f"refusing: task {args.task[:12]} is sticky but no "
                f"context_id was provided.\n"
                f"  Pass it inline:  pm executing --task {args.task[:12]} --context-id <uuid>\n"
                f"  Or via env:      PM_CONTEXT_ID=<uuid> pm executing --task {args.task[:12]}\n"
                f"  Mint a fresh one with: pm context-id\n"
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
