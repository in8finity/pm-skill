#!/usr/bin/env python3
"""Pull and claim the next runnable task in one shot.

Usage:
  pull.py [--queue Q] [--max-retries N] [--regex P] [--context-id ID]
          [--agent ID] [--json]

Behavior:
  1. Call next() to get a runnable task.
  2. Claim it via append_status('working'). The chain's native
     ``chain_predecessor`` CAS is the race-resolution primitive; on
     race-loss (HeadMoved → ClaimLost), the contended task is added to
     a per-invocation skip-set and the loop retries against a DIFFERENT
     candidate. Retries up to --max-retries races (default 5).
  3. Mirrors `pm executing`'s preconditions (parity with the diagnostic
     split form):
       - explicit pre-claim check that the freshly-read tip's
         ``status == "new"``; if not, skip this candidate without
         burning a retry attempt — closes the TOCTOU window where the
         chain CAS would otherwise let a second worker append `working`
         chained off another `working`.
       - sticky-context enforcement: if the task is sticky, refuses to
         claim without an agent context_id, calls
         ``check_sticky_eligibility``, and passes ``context_id`` to
         ``append_claim`` so the binding is recorded on the chain.
         Tasks the agent is ineligible for are skipped, not claimed.
  4. On success print three shell-eval-able lines to stdout:
        TASK=<text_sha256>
        IDEA_PATH=<extracted from task.text via regex; empty if no match>
        SLUG=<task.attributes.slug>
     Caller in bash: ``eval "$(pm pull)"`` and then check ``[ -n "$TASK" ]``.
     With ``--json``, prints a single JSON object instead — friendlier
     to restricted sandboxes that block ``eval`` and ``$(...)`` chains:
        {"task": "...", "idea_path": "...", "slug": "...", "agent": "..."}
     On queue-empty with ``--json``, prints ``null``.
  5. On queue-empty (no --json): exit 0 with NO output (TASK ends up empty).
  6. On all races lost: exit 8 with stdout line ``RETRIES_LOST=1`` (or
     JSON ``{"retries_lost": true}`` under --json) and a stderr note.
     The non-zero exit lets a worker that captures ``$?`` distinguish
     "queue genuinely empty, stop" (exit 0, no TASK) from "still
     contended, back off and retry" (exit 8, RETRIES_LOST set).
     The legacy ``eval "$(pm pull)"`` pattern still works — it sets
     ``RETRIES_LOST=1`` in the worker shell, ``TASK`` stays empty, so
     workers that don't check ``$?`` behave exactly as before.

  The claim's ``agent`` attribute is derived from --agent (if given),
  else $PM_AGENT_ID, else ``worker-<context-id[:12]>`` when a
  context-id is in play, else literal "pull". This keeps subsequent
  ``pm heartbeat --context-id <CID>`` calls from failing with
  exit 12 (lease lost) — they would otherwise see ``agent=pull`` on
  the working status and refuse, defeating the point of heartbeats
  for pull-claimed tasks.

The default --regex extracts the path that follows
``Run /qualify-idea-toulmin `` on line 1 of task.text. Override with
``--regex`` (Python regex; first capture group is used) for other workflows.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys

import store
from now_iso import now_iso


def shell_quote(v: str) -> str:
    return "'" + v.replace("'", "'\\''") + "'"


def default_agent_id(context_id: str | None = None) -> str:
    """Mirrors executing.py / heartbeat.py so a worker that pulls,
    then heartbeats, then reports against the same task uses the same
    agent identifier on every chain write — avoiding the lease-lost
    (exit 12) false-positive on heartbeat."""
    if env := os.environ.get("PM_AGENT_ID"):
        return env
    ctx = context_id or os.environ.get("PM_CONTEXT_ID")
    if ctx:
        return f"worker-{ctx[:12]}"
    return "pull"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", default="default")
    p.add_argument("--max-retries", type=int, default=5)
    p.add_argument(
        "--regex",
        default=r"Run /qualify-idea-toulmin (\S+)",
        help="Python regex to extract a path-or-arg from task.text; group 1.",
    )
    p.add_argument("--context-id", default=None,
                   help="sticky context id (overrides $PM_CONTEXT_ID). "
                        "Same selection semantics as `pm next --context-id`: "
                        "context-affinitive tasks come first, then FIFO.")
    p.add_argument("--agent", default=None,
                   help="agent identifier written to the claim's "
                        "`agent` attribute. Defaults to $PM_AGENT_ID, else "
                        "`worker-<context-id[:12]>` when a context-id is in "
                        "play, else literal 'pull'. Matches the resolution "
                        "in executing.py/heartbeat.py so subsequent "
                        "heartbeats from the same worker don't false-fail.")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of shell-eval-able assignments. "
                        "Friendlier to restricted sandboxes that block "
                        "`eval` or `$(...)` command substitution.")
    args = p.parse_args()

    pat = re.compile(args.regex)
    agent_context = args.context_id or os.environ.get("PM_CONTEXT_ID") or None
    agent_id = args.agent or default_agent_id(args.context_id)

    # Per-invocation skip-set: tasks that lost a CAS race or failed a
    # precondition (status moved, sticky-ineligible) within this pull
    # call. Without this, retries kept re-picking the same first-by-sort
    # candidate and either repeatedly lost races or repeatedly hit the
    # same skipped state, exhausting --max-retries while sibling tasks
    # sat idle. Retries now advance to the next candidate.
    skip: set[str] = set()

    for attempt in range(args.max_retries + 1):
        # Re-implement next.py logic inline to avoid double round-trip.
        tasks = store.list_tasks(args.queue)

        # record→text translation matches next.py so the runnable-set
        # here is identical to what `pm next` would return.
        record_to_text = {t["record_sha256"]: t["text_sha256"] for t in tasks}

        status_cache: dict[str, str | None] = {}
        ctx_cache: dict[str, set[str]] = {}

        def status_of(sha: str) -> str | None:
            if sha not in status_cache:
                status_cache[sha] = store.status_value(store.latest_status(sha))
            return status_cache[sha]

        def required_ctx(sha: str) -> set[str]:
            if sha not in ctx_cache:
                ctx_cache[sha] = store.collect_required_contexts(sha)
            return ctx_cache[sha]

        def context_priority(t: dict) -> int:
            """Same shape as next.py's context_priority — see that file
            for the rationale. Mirrors so pull's selection order matches
            what `pm next` would return."""
            if agent_context is None:
                return 1
            sha = t["text_sha256"]
            if store.task_context_id(sha) == agent_context:
                return 0
            return 0 if agent_context in required_ctx(sha) else 1

        tasks.sort(key=lambda t: (context_priority(t), t.get("created_at", "")))

        def dep_done(d_record: str) -> bool:
            d_text = record_to_text.get(d_record)
            return d_text is not None and status_of(d_text) == "done"

        def parent_claimed(t: dict) -> bool:
            parent_record = (t.get("links") or {}).get("parentTask")
            if not parent_record:
                return True
            parent_text = record_to_text.get(parent_record)
            if parent_text is None:
                return True
            return status_of(parent_text) != "new"

        candidate = None
        for t in tasks:
            sha = t["text_sha256"]
            if sha in skip:
                continue
            if status_of(sha) != "new":
                continue
            deps = (t.get("links") or {}).get("dependsOn") or []
            if not all(dep_done(d) for d in deps):
                continue
            # See next.py for the rationale on the parent-claim gate.
            if not parent_claimed(t):
                continue
            # Sticky-context gate (mirrors executing.py:135-149). Skip,
            # don't refuse — pull is a worker contract, so the right
            # outcome for "this task isn't for me" is to try the next
            # sibling rather than crash the whole pull call.
            if store.task_is_sticky(t):
                if not agent_context:
                    skip.add(sha)
                    continue
                try:
                    store.check_sticky_eligibility(sha, agent_context)
                except (store.StickyContextMismatch, store.StickyContextConflict):
                    skip.add(sha)
                    continue
            candidate = t
            break

        if candidate is None:
            if args.json:
                print("null")
            return 0  # queue empty (or every runnable filtered out)

        sha = candidate["text_sha256"]
        is_sticky = store.task_is_sticky(candidate)

        # Pre-claim status recheck (mirrors executing.py:79-103). The
        # chain's chain_predecessor CAS guarantees "no two records
        # share the same prev"; it does NOT guarantee "you can't append
        # `working` chained off another `working`". If between the
        # bulk listing and this point the head moved from `new` to
        # `working`, append_status would happily append a SECOND
        # `working` chained off the first. The fresh status check
        # below closes that TOCTOU; on miss, we skip the candidate
        # without burning a retry attempt.
        latest_before = store.latest_status(sha)
        if latest_before is None or store.status_value(latest_before) != "new":
            skip.add(sha)
            continue
        prev_record_sha = latest_before["record_sha256"]
        try:
            store.append_claim(
                sha, agent_id, prev_record_sha,
                context_id=agent_context if is_sticky else None,
            )
        except store.ClaimLost:
            sys.stderr.write(f"pull: race lost on {sha[:16]} (attempt {attempt+1}/{args.max_retries+1})\n")
            skip.add(sha)
            continue

        attrs = candidate.get("attributes") or {}
        slug = attrs.get("slug") or ""
        # Match against attributes.body — that's where the human-written
        # body lives. Task.text is the canonical content-address
        # (`task:<queue>/<slug>`) and never contains the workflow
        # command. Falls back to task.text for legacy tasks created
        # before the body migration.
        text = attrs.get("body") or candidate.get("text") or ""
        m = pat.search(text)
        idea_path = m.group(1) if m else ""

        if args.json:
            print(json.dumps({
                "task": sha,
                "idea_path": idea_path,
                "slug": slug,
                "agent": agent_id,
            }))
        else:
            print(f"TASK={shell_quote(sha)}")
            print(f"IDEA_PATH={shell_quote(idea_path)}")
            print(f"SLUG={shell_quote(slug)}")
        return 0

    sys.stderr.write(f"pull: all {args.max_retries+1} attempts lost races\n")
    if args.json:
        print(json.dumps({"retries_lost": True}))
    else:
        print("RETRIES_LOST=1")
    return 8


if __name__ == "__main__":
    sys.exit(main())
