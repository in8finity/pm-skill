#!/usr/bin/env python3
"""Stop hook — refuse turn-end if this agent left a task in `working`.

Why: an agent that says "done!" in chat without calling `pm finished`
or `pm cancel` leaves the queue with a stuck claim. Sweep eventually
reclaims it (per the heartbeat-vs-reclaim race protocol), but that's
slow, costs another worker's cycles to redo, and doesn't surface the
honest mistake. This hook makes the mistake loud at the moment of
turn-end — the agent reads the refusal reason and finishes / cancels
the dangling task before the user gets a "I'm done" they shouldn't.

Identity resolution mirrors executing.py / heartbeat.py:
  1. $PM_AGENT_ID            (explicit override)
  2. worker-<PM_CONTEXT_ID[:12]>  (stable per session)
  3. <hostname>-<pid>        (legacy fallback)

Configured via:
  {
    "hooks": {
      "Stop": [
        {"hooks": [{
          "type": "command",
          "command": "$CLAUDE_PROJECT_DIR/skills/pm/hooks/stop_with_open_task.py"
        }]}
      ]
    }
  }

Override:
  PM_HOOK_ALLOW_OPEN_TASKS=1   skip the check entirely
  PM_HOOK_QUEUES=q1,q2,*       only scan these queues (default: all queues
                                visible via find_items, which can be slow on
                                a large backend; restrict if needed)
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

# Make the pm scripts importable. The hook lives at
#   skills/pm/hooks/stop_with_open_task.py
# scripts at
#   skills/pm/scripts/
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


def default_agent_id() -> str:
    if env := os.environ.get("PM_AGENT_ID"):
        return env
    if ctx := os.environ.get("PM_CONTEXT_ID"):
        return f"worker-{ctx[:12]}"
    return f"{socket.gethostname()}-{os.getpid()}"


def emit_block(reason: str) -> None:
    """JSON output that tells Claude Code to refuse the Stop."""
    print(json.dumps({
        "decision": "block",
        "reason": reason,
    }))


def main() -> int:
    if os.environ.get("PM_HOOK_ALLOW_OPEN_TASKS") == "1":
        return 0

    # Best-effort: if the env isn't wired (no MCP URL) we can't query —
    # silently pass. The SessionStart hook surfaces the missing-env case;
    # don't double-fire.
    if not os.environ.get("HASHHARNESS_MCP_URL"):
        return 0

    try:
        import mcp_client
        import store
    except Exception as e:
        # Pm scripts not importable — likely the hook is wired in a
        # non-pm project. Pass silently.
        sys.stderr.write(f"[stop_with_open_task] pm scripts not found: {e}\n")
        return 0

    me = default_agent_id()

    # Discover all Tasks. Optionally restrict to specific queues.
    raw = mcp_client.tool("find_items", {"type": "Task", "limit": 10000})
    items = raw if isinstance(raw, list) else (raw.get("items") if isinstance(raw, dict) else [])
    if not items:
        return 0

    queue_filter = os.environ.get("PM_HOOK_QUEUES", "").strip()
    allowed_queues: set[str] | None = None
    if queue_filter and queue_filter != "*":
        allowed_queues = {q.strip() for q in queue_filter.split(",") if q.strip()}

    open_tasks: list[dict] = []
    for t in items:
        attrs = t.get("attributes") or {}
        sha = t.get("text_sha256") or ""
        if not sha:
            continue
        queue = attrs.get("queue") or "default"
        if allowed_queues and queue not in allowed_queues:
            continue

        latest = store.latest_status(sha)
        if not latest:
            continue
        if store.status_value(latest) != "working":
            continue
        owner = (latest.get("attributes") or {}).get("agent") or ""
        if owner != me:
            continue

        open_tasks.append({
            "sha": sha[:12],
            "slug": attrs.get("slug") or "?",
            "queue": queue,
        })

    if not open_tasks:
        return 0

    n = len(open_tasks)
    summary = "\n".join(
        f"  - {t['queue']} / {t['slug']} ({t['sha']})" for t in open_tasks[:10]
    )
    overflow = f"\n  ... and {n - 10} more" if n > 10 else ""

    emit_block(
        f"Refusing turn-end: this agent ({me}) has {n} task(s) still in "
        f"`working` state. Close each via `pm finished --task <sha>` (with "
        f"a TaskReport on chain) or `pm cancel --task <sha> --reason \"...\"` "
        f"before stopping. Otherwise the queue is left holding a dead "
        f"claim that only `pm sweep` will eventually reclaim.\n\n"
        f"Open tasks:\n{summary}{overflow}\n\n"
        f"Set PM_HOOK_ALLOW_OPEN_TASKS=1 to disable this check."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
