#!/usr/bin/env python3
"""List every task on a queue with its current status and key metadata.

Usage:
  list_tasks.py [--queue Q] [--state new|working|done|rejected|orphan]
                [--json] [--limit N]

Fills the gap between `pm next` (returns one runnable task) and
`pm show --task SHA` (full record of one task). Useful for:
  * orchestrator audits: "what's actually queued on Q right now?"
  * batch checkpoints: "are all my 13 children done yet?"
  * CI gates: `pm list --queue release --state working --json | jq length`

Text output (one task per line, columns aligned):
  STATE      SHA12        DEPS  SLUG
  new        e23ba74fae84   -   smoke
  working    7f1d09c2e3b1   2/3 ingest-walton-corpus
  done       6e047b4e389b   -   warmup

  - `DEPS` shows `done/total` for tasks with dependencies, else `-`.
  - For working tasks the owning agent appears in parentheses after
    the slug.
  - Sticky tasks get a trailing `[ctx=…]` marker.

JSON output: list of objects. Shape:
  [
    {"sha": "...", "slug": "...", "state": "...", "agent": null,
     "context_id": null, "parent_sha": null, "deps_done": 0,
     "deps_total": 0, "created_at": "..."},
    ...
  ]

Uses one bulk `find_tips_bulk` call (the same primitive dashboard.py
uses) so the round-trip cost is O(1) MCP calls regardless of queue size.

Exit codes:
  0  list emitted (possibly empty)
  2  usage error
"""
from __future__ import annotations

import argparse
import json
import sys

import mcp_client
import store


VALID_STATES = ("new", "working", "done", "rejected", "orphan")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--queue", default="default")
    p.add_argument("--state", default=None, choices=VALID_STATES,
                   help="filter to one state; default: all states.")
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=None,
                   help="cap the number of rows printed (after filtering).")
    args = p.parse_args()

    tasks = store.list_tasks(args.queue)
    record_to_text = {
        t["record_sha256"]: t["text_sha256"]
        for t in tasks
        if "record_sha256" in t and "text_sha256" in t
    }

    # Bulk fetch latest TaskStatus per task — one MCP call, O(N) tips.
    wp_ids = [store.task_wp(t["text_sha256"]) for t in tasks if t.get("text_sha256")]
    bulk = mcp_client.tool(
        "find_tips_bulk",
        {"work_package_ids": wp_ids, "type": "TaskStatus"},
    )
    latest_by_wp: dict[str, dict] = {
        wp: tip for wp, tip in (bulk.get("tips") or {}).items() if tip is not None
    }

    # Resolve dep counts up front using the same status map.
    def state_of_text_sha(sha: str) -> str:
        tip = latest_by_wp.get(store.task_wp(sha))
        return store.status_value(tip) or "orphan"

    rows: list[dict] = []
    for t in tasks:
        sha = t.get("text_sha256")
        if not sha:
            continue
        attrs = t.get("attributes") or {}
        links = t.get("links") or {}
        tip = latest_by_wp.get(store.task_wp(sha))
        if tip is None:
            state = "orphan"
            agent = None
            ctx_id = None
        else:
            state = store.status_value(tip) or "?"
            tip_attrs = tip.get("attributes") or {}
            agent = tip_attrs.get("agent")
            ctx_id = tip_attrs.get("context_id")

        deps = links.get("dependsOn") or []
        deps_total = len(deps)
        deps_done = sum(
            1 for d in deps
            if state_of_text_sha(record_to_text.get(d, "")) == "done"
        ) if deps else 0

        parent_record = links.get("parentTask")
        parent_sha = record_to_text.get(parent_record) if parent_record else None

        rows.append({
            "sha": sha,
            "slug": attrs.get("slug") or t.get("title", ""),
            "state": state,
            "agent": agent,
            "context_id": ctx_id,
            "parent_sha": parent_sha,
            "deps_done": deps_done,
            "deps_total": deps_total,
            "created_at": t.get("created_at"),
        })

    if args.state:
        rows = [r for r in rows if r["state"] == args.state]

    rows.sort(key=lambda r: (r["created_at"] or ""))

    if args.limit is not None:
        rows = rows[: args.limit]

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return 0

    if not rows:
        return 0

    print(f"{'STATE':<9}  {'SHA12':<14}{'DEPS':<6}SLUG")
    for r in rows:
        deps = f"{r['deps_done']}/{r['deps_total']}" if r["deps_total"] else "-"
        suffix = ""
        if r["state"] == "working" and r["agent"]:
            suffix += f"  ({r['agent']})"
        if r["context_id"]:
            suffix += f"  [ctx={r['context_id'][:8]}]"
        print(f"{r['state']:<9}  {r['sha'][:12]:<14}{deps:<6}{r['slug']}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
