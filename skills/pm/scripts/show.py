#!/usr/bin/env python3
"""Print everything about one task in a CLI-friendly form.

Usage:
  show.py --task SHA [--json]

Without --json, prints a human-readable summary:
  - Task header (slug, queue, workdir, verifier, sticky, body, attrs)
  - Status chain (every TaskStatus, oldest first, with timestamps,
    agent/context_id where set, and reclaim/cancel/replan flags)
  - Reports (every TaskReport, oldest first, with title + body)
  - Heartbeats (count + most recent timestamp; full list elided unless
    you really want it via --json)

With --json, prints the same data as a single JSON object — same shape
as the dashboard's /api/task/<sha> endpoint, but without spinning up
the HTTP server.

Exit codes:
  0  printed
  6  task not found
"""
from __future__ import annotations

import argparse
import json
import sys

import mcp_client
import store


def _unwrap_items(res):
    if not res:
        return []
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        return res.get("items") or []
    return []


def _chain(task_sha: str, type_name: str) -> list[dict]:
    res = mcp_client.tool("get_work_package", {
        "work_package_id": store.task_wp(task_sha),
        "type": type_name,
    })
    items = _unwrap_items(res)
    items.sort(key=lambda it: it.get("created_at") or "")
    return items


def fetch_detail(task_sha: str) -> dict | None:
    task = store.get_task(task_sha)
    if task is None:
        return None
    return {
        "task":       task,
        "statuses":   _chain(task_sha, "TaskStatus"),
        "reports":    _chain(task_sha, "TaskReport"),
        "heartbeats": _chain(task_sha, "TaskHeartbeat"),
    }


def _strip_nonce(s: str) -> str:
    return (s or "").split("\n#nonce:")[0]


def render_text(detail: dict) -> str:
    task = detail["task"]
    attrs = task.get("attributes") or {}
    sha = task.get("text_sha256") or ""
    out: list[str] = []

    out.append(f"Task: {attrs.get('slug') or '?':<40} {sha[:12]}")
    if title := task.get("title"):
        out.append(f"  title:    {title}")
    out.append(f"  queue:    {attrs.get('queue') or '?'}")
    if workdir := attrs.get("workdir"):
        out.append(f"  workdir:  {workdir}")
    if verifier := attrs.get("verifier"):
        out.append(f"  verifier: {verifier}")
    if attrs.get("sticky"):
        out.append(f"  sticky:   true")
    if (body := attrs.get("body")) is not None:
        out.append("")
        out.append("  ---- body ----")
        for line in str(body).splitlines() or [""]:
            out.append(f"  {line}")
        out.append("  --------------")
    out.append("")

    statuses = detail["statuses"]
    out.append(f"Status chain ({len(statuses)} entries):")
    for s in statuses:
        st_attrs = s.get("attributes") or {}
        status = st_attrs.get("status") or "?"
        ts = s.get("created_at") or ""
        flags = []
        if (a := st_attrs.get("agent")):       flags.append(f"agent={a}")
        if (c := st_attrs.get("context_id")):  flags.append(f"ctx={c[:12]}")
        if st_attrs.get("reclaimed"):          flags.append(f"reclaimed by {st_attrs.get('reclaimer','?')}")
        if st_attrs.get("cancelled"):          flags.append(f"cancelled by {st_attrs.get('cancelled_by','?')}")
        if st_attrs.get("replanned"):          flags.append("replanned")
        if (sb := st_attrs.get("superseded_by")): flags.append(f"superseded→{sb[:12]}")
        if "verifier_exit" in st_attrs:        flags.append(f"verifier_exit={st_attrs['verifier_exit']}")
        flags_str = "  [" + ", ".join(flags) + "]" if flags else ""
        out.append(f"  {ts}  {status:<11}{flags_str}")
        note = _strip_nonce(s.get("text") or "")
        if note and note != f"claimed by {st_attrs.get('agent','')}":
            for line in note.splitlines():
                out.append(f"    | {line}")
    out.append("")

    reports = detail["reports"]
    out.append(f"Reports ({len(reports)} entries):")
    for r in reports:
        ts = r.get("created_at") or ""
        title = r.get("title") or ""
        body = _strip_nonce(r.get("text") or "")
        out.append(f"  {ts}  {title}")
        for line in body.splitlines():
            out.append(f"    | {line}")
    out.append("")

    heartbeats = detail["heartbeats"]
    if heartbeats:
        last = heartbeats[-1]
        last_ts = last.get("created_at") or ""
        last_agent = (last.get("attributes") or {}).get("agent") or ""
        out.append(f"Heartbeats: {len(heartbeats)} total · "
                   f"most recent {last_ts} from {last_agent}")
    else:
        out.append("Heartbeats: 0")

    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--task", required=True, help="task text_sha256")
    p.add_argument("--json", action="store_true",
                   help="emit JSON instead of human-readable text")
    args = p.parse_args()

    detail = fetch_detail(args.task)
    if detail is None:
        sys.stderr.write(f"task {args.task[:12]} not found\n")
        return 6

    if args.json:
        print(json.dumps(detail, indent=2, default=str))
    else:
        print(render_text(detail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
