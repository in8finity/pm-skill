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


def _format_explain_human(e: dict) -> str:
    """Human-readable stderr summary for ``--explain`` without ``--json``.
    Distinguishes 'queue genuinely drained' (every counter is 0) from
    'work remains, gated to other contexts' (sticky_mismatch > 0)."""
    lines = [
        f"pm pull: no runnable task in queue '{e['queue']}'",
        f"  candidates (status=new):       {e['candidates_new']}",
        f"  blocked by dep:                {e['blocked_by_dep']}",
        f"  blocked by parent unclaimed:   {e['blocked_by_parent_unclaimed']}",
        f"  sticky, no worker context:     {e['sticky_no_context']}",
        f"  sticky, owned by other ctx:    {e['sticky_mismatch']}",
    ]
    owners = e.get("sticky_owner_contexts") or []
    if owners:
        head = ", ".join(o[:12] for o in owners[:4])
        more = f" (+{len(owners) - 4} more)" if len(owners) > 4 else ""
        lines.append(f"  owner contexts:                {head}{more}")
    if e["sticky_mismatch"] > 0 or e["blocked_by_dep"] > 0 or e["blocked_by_parent_unclaimed"] > 0:
        lines.append("  -> work remains; gated to other workers/contexts, not drained")
    else:
        lines.append("  -> queue genuinely drained for this worker")
    return "\n".join(lines) + "\n"


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
    p.add_argument("--explain", action="store_true",
                   help="when no task can be claimed, emit a diagnostic "
                        "breakdown distinguishing 'queue genuinely drained' "
                        "from 'work remains but is gated to other contexts': "
                        "candidate count, sticky-ineligible count, "
                        "dep/parent-blocked count, and the set of context_ids "
                        "currently owning sticky-bound tasks. Under --json "
                        "the breakdown is attached to the JSON payload as "
                        "`explain`; otherwise it's printed to stderr.")
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

        # O(open) candidate discovery via the tip-attribute index — same
        # shape as next.py. Re-primed each retry attempt because a racing
        # worker may have moved a tip since the last read.
        new_shas = store.open_task_shas(args.queue, status="new", tasks=tasks)
        status_cache: dict[str, str | None] = {sha: "new" for sha in new_shas}

        need_status: set[str] = set()
        for t in tasks:
            if t["text_sha256"] not in new_shas:
                continue
            for d in (t.get("links") or {}).get("dependsOn") or []:
                dt = record_to_text.get(d)
                if dt and dt not in status_cache:
                    need_status.add(dt)
            p = (t.get("links") or {}).get("parentTask")
            if p:
                pt = record_to_text.get(p)
                if pt and pt not in status_cache:
                    need_status.add(pt)
        if need_status:
            status_cache.update(store.bulk_status_values(list(need_status)))

        # Restrict the iteration set to candidate `new` tasks for the
        # same reason as next.py: terminal/working tasks can never be
        # claimed, so paying any status fallback for them is waste.
        tasks = [t for t in tasks if t["text_sha256"] in new_shas]
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

        # Per-iteration cache for cross-queue parent lookups. Re-built
        # each retry attempt because the parent's status can change
        # (claim, finish, reclaim) between attempts.
        xq_parent_status_cache: dict[str, str | None] = {}

        def parent_claimed(t: dict) -> bool:
            """Cross-queue parent-claim gate. Mirrors next.py:parent_claimed.
            See planning_parent_gate.als#ChildBlockedUntilParentClaimed and
            #CrossQueueChildBlockedUntilParentClaimed."""
            parent_record = (t.get("links") or {}).get("parentTask")
            if not parent_record:
                return True
            parent_text = record_to_text.get(parent_record)
            if parent_text is not None:
                return status_of(parent_text) != "new"
            # Cross-queue parent fallback — one global record-sha
            # lookup, cached per pull invocation.
            if parent_record not in xq_parent_status_cache:
                xq_parent_status_cache[parent_record] = \
                    store.task_status_by_record_sha(parent_record)
            cached = xq_parent_status_cache[parent_record]
            if cached is None:
                return True  # dangling parent link
            return cached != "new"

        # Diagnostic counters for `--explain`. Reset per attempt — the
        # LAST attempt's numbers are what gets surfaced when we return
        # null. A worker that wants to log "queue is drained vs. gated
        # to other contexts" reads these from --explain.
        gated_by_dep = 0
        gated_by_parent = 0
        sticky_no_context = 0
        sticky_mismatch = 0
        sticky_owner_contexts: set[str] = set()

        candidate = None
        for t in tasks:
            sha = t["text_sha256"]
            if sha in skip:
                continue
            if status_of(sha) != "new":
                continue
            deps = (t.get("links") or {}).get("dependsOn") or []
            if not all(dep_done(d) for d in deps):
                gated_by_dep += 1
                continue
            # See next.py for the rationale on the parent-claim gate.
            if not parent_claimed(t):
                gated_by_parent += 1
                continue
            # Sticky-context gate (mirrors executing.py:135-149). Skip,
            # don't refuse — pull is a worker contract, so the right
            # outcome for "this task isn't for me" is to try the next
            # sibling rather than crash the whole pull call.
            if store.task_is_sticky(t):
                if not agent_context:
                    sticky_no_context += 1
                    skip.add(sha)
                    continue
                try:
                    store.check_sticky_eligibility(sha, agent_context)
                except (store.StickyContextMismatch, store.StickyContextConflict):
                    sticky_mismatch += 1
                    # Record which OTHER context already owns this
                    # cluster so the operator can see why their worker
                    # was routed away.
                    owner = store.task_context_id(sha)
                    if owner:
                        sticky_owner_contexts.add(owner)
                    else:
                        # No own context_id — must be bound via an
                        # ancestor (parent/dep chain). Walk required_ctx.
                        for ctx in required_ctx(sha):
                            sticky_owner_contexts.add(ctx)
                    skip.add(sha)
                    continue
            candidate = t
            break

        if candidate is None:
            # No runnable task this attempt. Build the explain payload —
            # cheap, always available; only emitted when --explain.
            explain = {
                "queue": args.queue,
                "candidates_new": len(new_shas),
                "agent_context": agent_context,
                "blocked_by_dep": gated_by_dep,
                "blocked_by_parent_unclaimed": gated_by_parent,
                "sticky_no_context": sticky_no_context,
                "sticky_mismatch": sticky_mismatch,
                "sticky_owner_contexts": sorted(sticky_owner_contexts),
            }
            if args.json:
                if args.explain:
                    print(json.dumps({"task": None, "explain": explain}))
                else:
                    print("null")
            if args.explain and not args.json:
                sys.stderr.write(_format_explain_human(explain))
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
        # One-round-trip recheck via find_tip's where_attributes filter:
        # we only proceed if the tip is still `new` and we need only the
        # CAS prev-link. The old path took two round trips (find_tip
        # minimal + get_item_by_hash rehydrate for attributes.status).
        matched_tip = store.latest_status_if_new(sha)
        if matched_tip is None:
            skip.add(sha)
            continue
        prev_record_sha = matched_tip["record_sha256"]
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
