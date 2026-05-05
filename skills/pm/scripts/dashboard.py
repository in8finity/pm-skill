#!/usr/bin/env python3
"""pm dashboard — minimal HTTP server showing planning-board state.

Reads via the same MCP client / store helpers the rest of pm uses.
Auto-refresh in the browser; no JS dependency, no external libs.

Usage:
  pm dashboard [--port 38418] [--bind 127.0.0.1] [--refresh 5]

Endpoints:
  /                   HTML dashboard (auto-refresh; supports filters via query params)
  /task/<sha>         HTML detail view for one task — full status / report / heartbeat history
  /api/state          JSON snapshot (workdirs → queues → task tree + status)
  /api/task/<sha>     JSON detail for one task (chains, body, attributes)
  /healthz            liveness probe ("ok")

Exit:
  Ctrl+C
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlparse, parse_qs

import re

import mcp_client
import store


# ---- minimal markdown renderer (stdlib-only) -----------------------------
# Covers: ATX headings, fenced/indented code blocks, ordered/unordered lists,
# blockquotes, paragraphs, bold/italic, inline code, links, autolinks.
# Anything fancier (tables, footnotes, etc.) falls through as plain text.

_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_BOLD        = re.compile(r"\*\*([^*\n]+)\*\*")
_MD_ITALIC      = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\w)")
_MD_LINK        = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_AUTOLINK    = re.compile(r"(?<![\w(])((?:https?://|mailto:)[^\s<>()]+)")


def _md_inline(text: str) -> str:
    """Inline markdown → HTML. Input is RAW; output is HTML-safe."""
    placeholders: list[str] = []

    def stash(html: str) -> str:
        placeholders.append(html)
        return f"\x00{len(placeholders)-1}\x00"

    # Pull inline code first (its content must not be re-processed).
    text = _MD_INLINE_CODE.sub(lambda m: stash(f"<code>{escape(m.group(1))}</code>"), text)
    # Links before autolinks so [text](url) wins.
    text = _MD_LINK.sub(
        lambda m: stash(f'<a href="{escape(m.group(2), quote=True)}">{escape(m.group(1))}</a>'),
        text,
    )
    text = _MD_AUTOLINK.sub(
        lambda m: stash(f'<a href="{escape(m.group(1), quote=True)}">{escape(m.group(1))}</a>'),
        text,
    )
    # Now escape everything else.
    text = escape(text)
    text = _MD_BOLD.sub(r"<strong>\1</strong>", text)
    text = _MD_ITALIC.sub(r"<em>\1</em>", text)
    # Restore placeholders.
    text = re.sub(r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], text)
    return text


def render_markdown(text: str) -> str:
    """Render a small subset of CommonMark to HTML. Safe for untrusted input
    only insofar as everything not matched is escaped; href values are also
    escaped, but full XSS hardening (URL scheme allowlist, etc.) is out of
    scope for this dashboard which serves localhost only."""
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    in_list: str | None = None  # 'ul' or 'ol' or None
    in_quote = False

    def close_list():
        nonlocal in_list
        if in_list:
            out.append(f"</{in_list}>")
            in_list = None

    def close_quote():
        nonlocal in_quote
        if in_quote:
            out.append("</blockquote>")
            in_quote = False

    while i < len(lines):
        line = lines[i]

        # Fenced code block.
        m = re.match(r"^( {0,3})(`{3,}|~{3,})\s*([\w+-]*)\s*$", line)
        if m:
            close_list(); close_quote()
            fence = m.group(2)
            lang = m.group(3) or ""
            i += 1
            buf = []
            while i < len(lines) and not re.match(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            cls = f' class="lang-{escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{escape(chr(10).join(buf))}</code></pre>")
            continue

        # ATX heading.
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            close_list(); close_quote()
            level = len(m.group(1))
            out.append(f"<h{level}>{_md_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # Blockquote.
        m = re.match(r"^>\s?(.*)$", line)
        if m:
            close_list()
            if not in_quote:
                out.append("<blockquote>")
                in_quote = True
            out.append(f"<p>{_md_inline(m.group(1))}</p>")
            i += 1
            continue

        # Ordered list.
        m = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if m:
            close_quote()
            if in_list != "ol":
                close_list()
                out.append("<ol>")
                in_list = "ol"
            out.append(f"<li>{_md_inline(m.group(1))}</li>")
            i += 1
            continue

        # Unordered list.
        m = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if m:
            close_quote()
            if in_list != "ul":
                close_list()
                out.append("<ul>")
                in_list = "ul"
            out.append(f"<li>{_md_inline(m.group(1))}</li>")
            i += 1
            continue

        # GFM table: header row + separator row + zero-or-more body rows.
        # Detect by looking ahead — current line has '|', next line is the
        # separator (cells of dashes with optional :align: markers).
        if "|" in line and i + 1 < len(lines) and re.match(
            r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", lines[i + 1]
        ):
            close_list(); close_quote()

            def _cells(row: str) -> list[str]:
                row = row.strip()
                if row.startswith("|"): row = row[1:]
                if row.endswith("|"):   row = row[:-1]
                return [c.strip() for c in row.split("|")]

            headers = _cells(line)
            sep_cells = _cells(lines[i + 1])
            aligns = []
            for c in sep_cells:
                left = c.startswith(":")
                right = c.endswith(":")
                aligns.append("center" if left and right else "right" if right else "left" if left else "")
            i += 2
            body_rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip() and "|" in lines[i] and not re.match(
                r"^(#{1,6}\s|>\s|\s*[-*+]\s|\s*\d+\.\s|`{3,}|~{3,})", lines[i]
            ):
                body_rows.append(_cells(lines[i]))
                i += 1

            def _td(tag: str, content: str, align: str) -> str:
                style = f' style="text-align:{align}"' if align else ''
                return f"<{tag}{style}>{_md_inline(content)}</{tag}>"

            parts = ["<table><thead><tr>"]
            for idx, h in enumerate(headers):
                parts.append(_td("th", h, aligns[idx] if idx < len(aligns) else ""))
            parts.append("</tr></thead><tbody>")
            for row in body_rows:
                parts.append("<tr>")
                for idx in range(len(headers)):
                    cell = row[idx] if idx < len(row) else ""
                    parts.append(_td("td", cell, aligns[idx] if idx < len(aligns) else ""))
                parts.append("</tr>")
            parts.append("</tbody></table>")
            out.append("".join(parts))
            continue

        # Horizontal rule.
        if re.match(r"^\s*([-*_])(\s*\1){2,}\s*$", line):
            close_list(); close_quote()
            out.append("<hr>")
            i += 1
            continue

        # Blank line.
        if not line.strip():
            close_list(); close_quote()
            i += 1
            continue

        # Paragraph: gather contiguous non-empty, non-special lines.
        close_list(); close_quote()
        buf = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
            r"^(#{1,6}\s|>\s|\s*[-*+]\s|\s*\d+\.\s|`{3,}|~{3,})", lines[i]
        ):
            # Stop if we're about to enter a table (current line has '|' and
            # the next is a separator row).
            if "|" in lines[i] and i + 1 < len(lines) and re.match(
                r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", lines[i + 1]
            ):
                break
            buf.append(lines[i])
            i += 1
        out.append(f"<p>{_md_inline(' '.join(buf))}</p>")

    close_list(); close_quote()
    return "\n".join(out)


def _unwrap_items(res):
    if not res:
        return []
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        return res.get("items") or []
    return []


def fetch_state() -> dict:
    """Return all tasks grouped by (workdir, queue), each with status + tree info.

    Performance: two MCP round trips total (one for Tasks, one for
    TaskStatuses), regardless of how many tasks exist. The previous
    implementation made N+1 round trips by calling `find_tip` per task;
    that scaled linearly with queue size and made the dashboard feel
    sluggish (~50ms/task over localhost). The DB query itself is
    indexed on (work_package_id, type, created_at) so each `find_tip`
    is fast individually — the cost was per-call HTTP round-trip
    overhead, which bulk-fetch eliminates by computing the per-task
    latest status in client memory.
    """
    raw = mcp_client.tool("find_items", {"type": "Task", "limit": 10000})
    tasks = _unwrap_items(raw)

    # record_sha256 → text_sha256 lookup for resolving parentTask links.
    record_to_text = {
        t["record_sha256"]: t["text_sha256"]
        for t in tasks
        if "record_sha256" in t and "text_sha256" in t
    }

    # Bulk-tips fetch: one MCP call returns the latest TaskStatus for
    # every task's work_package_id. Server uses an index-aided
    # `DISTINCT ON (work_package_id) ... ORDER BY created_at DESC`
    # query, so the response carries only the N tips, not the full
    # status history. See system-models/reports/proposal-hashharness-
    # bulk-tips.md.
    wp_ids = [store.task_wp(t["text_sha256"]) for t in tasks if t.get("text_sha256")]
    bulk = mcp_client.tool(
        "find_tips_bulk",
        {"work_package_ids": wp_ids, "type": "TaskStatus"},
    )
    latest_by_wp: dict[str, dict] = {
        wp: tip for wp, tip in (bulk.get("tips") or {}).items() if tip is not None
    }

    workdirs: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    by_status: dict[str, int] = defaultdict(int)
    total = 0

    for t in tasks:
        attrs = t.get("attributes") or {}
        sha = t.get("text_sha256")
        if not sha:
            continue
        workdir = attrs.get("workdir") or "<no-workdir>"
        queue = attrs.get("queue") or "default"

        latest = latest_by_wp.get(store.task_wp(sha))
        status = store.status_value(latest) or "?"

        links = t.get("links") or {}
        parent_record = links.get("parentTask")
        parent_text = record_to_text.get(parent_record) if parent_record else None
        deps = links.get("dependsOn") or []

        owner = (latest.get("attributes") or {}).get("agent") if isinstance(latest, dict) else None
        ctx = (latest.get("attributes") or {}).get("context_id") if isinstance(latest, dict) else None

        workdirs[workdir][queue].append({
            "sha": sha,
            "short_sha": sha[:12],
            "slug": attrs.get("slug", "?"),
            "title": t.get("title", "") or "",
            "status": status,
            "queue": queue,
            "workdir": workdir,
            "sticky": bool(attrs.get("sticky")),
            "verifier": attrs.get("verifier") or "",
            "parent": parent_text,
            "deps_count": len(deps),
            "created_at": t.get("created_at"),
            "owner": owner or "",
            "context_id": ctx[:8] if ctx else "",
            "wp_id": t.get("work_package_id") or "",
        })
        by_status[status] += 1
        total += 1

    return {
        "workdirs": {wd: dict(qs) for wd, qs in workdirs.items()},
        "totals": {"tasks": total, "by_status": dict(by_status)},
    }


def _chain_history(task_sha: str, type_name: str) -> list[dict]:
    """Return all items of `type_name` for this task, oldest first.

    The chain is enforced by `chain_predecessor` so created_at order
    equals chain order. Walking the prevX link explicitly would be
    rigorous but adds a round-trip per item; for a dashboard, sorted
    works fine and is dramatically cheaper for long chains.
    """
    res = mcp_client.tool("get_work_package", {
        "work_package_id": store.task_wp(task_sha),
        "type": type_name,
    })
    items = _unwrap_items(res)
    items.sort(key=lambda it: it.get("created_at") or "")
    return items


def fetch_task_detail(task_sha: str) -> dict | None:
    """Return everything needed to render a per-task view: the Task itself,
    and its three chains (status, report, heartbeat) in chronological order.
    """
    task = store.get_task(task_sha)
    if task is None:
        return None
    statuses    = _chain_history(task_sha, "TaskStatus")
    reports     = _chain_history(task_sha, "TaskReport")
    heartbeats  = _chain_history(task_sha, "TaskHeartbeat")

    # Build a unified timeline interleaving status/report/heartbeat events.
    events = []
    for s in statuses:
        attrs = s.get("attributes") or {}
        events.append({
            "kind": "status",
            "created_at": s.get("created_at") or "",
            "key": attrs.get("status") or "?",
            "agent": attrs.get("agent") or "",
            "context_id": attrs.get("context_id") or "",
            "verifier": attrs.get("verifier") or "",
            "verifier_exit": attrs.get("verifier_exit"),
            "verifier_summary": attrs.get("verifier_summary") or "",
            "reclaimed": attrs.get("reclaimed") or False,
            "reclaimer": attrs.get("reclaimer") or "",
            "cancelled": attrs.get("cancelled") or False,
            "cancelled_by": attrs.get("cancelled_by") or "",
            "cancel_reason": attrs.get("cancel_reason") or "",
            "replanned": attrs.get("replanned") or False,
            "superseded_by": attrs.get("superseded_by") or "",
            "text_sha256": s.get("text_sha256") or "",
            "note": (s.get("text") or "").split("\n#nonce:")[0],
        })
    for r in reports:
        events.append({
            "kind": "report",
            "created_at": r.get("created_at") or "",
            "title": r.get("title") or "",
            "body": (r.get("text") or "").split("\n#nonce:")[0],
            "text_sha256": r.get("text_sha256") or "",
        })
    for h in heartbeats:
        attrs = h.get("attributes") or {}
        events.append({
            "kind": "heartbeat",
            "created_at": h.get("created_at") or "",
            "agent": attrs.get("agent") or "",
            "preempt": attrs.get("preempt") or False,
            "text_sha256": h.get("text_sha256") or "",
        })
    events.sort(key=lambda e: e["created_at"])

    return {
        "task": task,
        "statuses": statuses,
        "reports": reports,
        "heartbeats": heartbeats,
        "events": events,
    }


HTML_HEAD = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>pm dashboard</title>
<meta http-equiv="refresh" content="{refresh}">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", monospace; margin: 1em; background: #fafafa; color: #222; }}
h1 {{ font-size: 1.3em; margin: 0 0 0.5em; }}
h2 {{ font-size: 1.1em; margin: 0 0 0.4em; color: #444; }}
h3 {{ font-size: 1em; margin: 0.4em 0 0.3em; color: #555; }}
.workdir {{ background: #fff; border: 1px solid #ddd; padding: 0.8em 1em; margin-bottom: 1em; border-radius: 4px; }}
.queue {{ margin-left: 0.5em; padding-left: 0.8em; border-left: 3px solid #ccc; margin-bottom: 0.6em; }}
.task {{ padding: 0.2em 0; line-height: 1.4; }}
.task[data-depth="1"] {{ margin-left: 1.5em; }}
.task[data-depth="2"] {{ margin-left: 3em; }}
.task[data-depth="3"] {{ margin-left: 4.5em; }}
.task[data-depth="4"] {{ margin-left: 6em; }}
.status {{ display: inline-block; min-width: 4.5em; text-align: center; padding: 0.05em 0.4em; border-radius: 3px; font-size: 0.78em; font-weight: 600; margin-right: 0.4em; }}
.status-new        {{ background: #e3f2fd; color: #0d47a1; }}
.status-working    {{ background: #fff3e0; color: #e65100; }}
.status-done       {{ background: #e8f5e9; color: #1b5e20; }}
.status-rejected   {{ background: #ffebee; color: #c62828; }}
.status-superseded {{ background: #f3e5f5; color: #6a1b9a; }}
.status-\\?         {{ background: #eee; color: #555; }}
.sha {{ color: #999; font-family: monospace; font-size: 0.82em; }}
.slug {{ font-weight: 600; }}
.title {{ color: #555; margin-left: 0.5em; }}
.tag {{ background: #f0f0f0; color: #333; padding: 0 0.4em; border-radius: 3px; font-size: 0.75em; margin-left: 0.3em; vertical-align: middle; }}
.tag-sticky {{ background: #fff8e1; color: #6a5400; }}
.tag-verifier {{ background: #e1f5fe; color: #014361; }}
.tag-deps {{ background: #f3e5f5; color: #491b75; }}
.tag-owner {{ background: #f5f5f5; color: #333; font-family: monospace; }}
.tag-ctx {{ background: #fff8e1; color: #6a5400; font-family: monospace; }}
.filters {{ background: #fff; border: 1px solid #ddd; padding: 0.5em 0.8em; border-radius: 4px; margin-bottom: 0.6em; display: flex; flex-wrap: wrap; gap: 0.6em; align-items: center; }}
.filters label {{ display: flex; align-items: center; gap: 0.3em; font-size: 0.88em; color: #444; }}
.filters select, .filters input {{ font-family: inherit; font-size: 0.88em; padding: 0.15em 0.3em; border: 1px solid #ccc; border-radius: 3px; background: #fafafa; }}
.filters select {{ max-width: 28em; }}
.filters button {{ padding: 0.2em 0.8em; border: 1px solid #1976d2; background: #1976d2; color: white; border-radius: 3px; cursor: pointer; font-size: 0.88em; }}
.filters button:hover {{ background: #1565c0; }}
.filters .clear {{ font-size: 0.85em; color: #888; margin-left: 0.4em; }}
.totals {{ background: #fff; border: 1px solid #ddd; padding: 0.5em 0.8em; border-radius: 4px; margin-bottom: 1em; }}
.totals .status {{ margin-right: 0.4em; }}
.empty {{ color: #888; font-style: italic; }}
.foot {{ margin-top: 1.5em; color: #999; font-size: 0.8em; }}
a {{ color: #1976d2; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.timeline {{ background: #fff; border: 1px solid #ddd; border-radius: 4px; padding: 0.4em 0.8em; }}
.event {{ padding: 0.5em 0.6em; border-left: 4px solid #ccc; margin: 0.4em 0; background: #fafafa; border-radius: 0 3px 3px 0; }}
.event-status    {{ border-left-color: #1976d2; }}
.event-report    {{ border-left-color: #388e3c; background: #f1f8e9; }}
.event-heartbeat {{ border-left-color: #fb8c00; background: #fff8e1; }}
.event .when {{ color: #777; font-size: 0.8em; font-family: monospace; }}
.event .kind {{ display: inline-block; min-width: 5em; padding: 0 0.4em; font-size: 0.75em; font-weight: 600; border-radius: 3px; margin-right: 0.5em; background: #eee; color: #444; }}
.event .body {{ font-size: 0.9em; color: #222; margin-top: 0.3em; padding: 0.4em 0.8em; background: #fff; border: 1px solid #eee; border-radius: 3px; max-height: 30em; overflow-y: auto; }}
.event .body.report-body {{ background: #fff; max-height: none; }}
.event .body p {{ margin: 0.4em 0; line-height: 1.45; }}
.event .body h1, .event .body h2, .event .body h3, .event .body h4 {{ margin: 0.6em 0 0.3em; font-weight: 600; color: #222; }}
.event .body h1 {{ font-size: 1.15em; }}
.event .body h2 {{ font-size: 1.05em; }}
.event .body h3 {{ font-size: 0.98em; }}
.event .body h4 {{ font-size: 0.92em; color: #444; }}
.event .body ul, .event .body ol {{ margin: 0.3em 0 0.3em 1.4em; padding: 0; }}
.event .body li {{ margin: 0.15em 0; line-height: 1.4; }}
.event .body code {{ background: #f3f3f3; padding: 0 0.3em; border-radius: 2px; font-family: ui-monospace, SFMono-Regular, monospace; font-size: 0.88em; }}
.event .body pre {{ background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 3px; padding: 0.5em 0.8em; overflow-x: auto; margin: 0.4em 0; }}
.event .body pre code {{ background: transparent; padding: 0; font-size: 0.85em; }}
.event .body blockquote {{ border-left: 3px solid #ddd; padding: 0.1em 0.8em; color: #555; margin: 0.4em 0; }}
.event .body a {{ color: #1976d2; }}
.event .body hr {{ border: 0; border-top: 1px solid #eee; margin: 0.6em 0; }}
.event .body strong {{ font-weight: 600; }}
.event .body em {{ font-style: italic; }}
.event .body table {{ border-collapse: collapse; margin: 0.6em 0; font-size: 0.88em; }}
.event .body th, .event .body td {{ border: 1px solid #ddd; padding: 0.3em 0.6em; text-align: left; vertical-align: top; }}
.event .body th {{ background: #f5f5f5; font-weight: 600; }}
.event .body tr:nth-child(even) td {{ background: #fafafa; }}
.event .meta {{ color: #555; font-size: 0.85em; margin-top: 0.2em; }}
.task-header {{ background: #fff; border: 1px solid #ddd; padding: 0.8em 1em; border-radius: 4px; margin-bottom: 0.8em; }}
.task-header h2 {{ margin: 0 0 0.3em; }}
.task-header dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 0.2em 0.8em; margin: 0.4em 0 0; font-size: 0.88em; }}
.task-header dt {{ color: #666; }}
.task-header dd {{ margin: 0; font-family: monospace; word-break: break-all; }}
</style>
</head><body>
<h1>pm dashboard <span class="sha">— auto-refresh {refresh}s · <a href="/api/state">json</a></span></h1>
"""


def _render_task(task: dict, children_of: dict, depth: int = 0) -> list[str]:
    out = []
    tags = []
    if task["sticky"]:
        tags.append('<span class="tag tag-sticky">sticky</span>')
    if task["verifier"]:
        tags.append(f'<span class="tag tag-verifier">verifier:{escape(task["verifier"])[:30]}</span>')
    if task["deps_count"]:
        tags.append(f'<span class="tag tag-deps">deps:{task["deps_count"]}</span>')
    if task["owner"]:
        tags.append(f'<span class="tag tag-owner">@{escape(task["owner"])[:24]}</span>')
    if task["context_id"]:
        tags.append(f'<span class="tag tag-ctx">ctx:{escape(task["context_id"])}</span>')

    title_part = f'<span class="title">— {escape(task["title"])[:80]}</span>' if task["title"] else ""

    detail_url = f'/task/{escape(task["sha"])}'
    out.append(
        f'<div class="task" data-depth="{depth}">'
        f'<span class="status status-{escape(task["status"])}">{escape(task["status"])}</span>'
        f'<a class="slug" href="{detail_url}">{escape(task["slug"])}</a> '
        f'<a class="sha" href="{detail_url}">{escape(task["short_sha"])}</a>'
        f'{title_part}'
        f'{"".join(tags)}'
        f'</div>'
    )

    children = sorted(children_of.get(task["sha"], []), key=lambda x: x.get("created_at") or "")
    for c in children:
        out.extend(_render_task(c, children_of, depth + 1))
    return out


STATUS_OPTIONS = ["new", "working", "done", "rejected", "superseded", "?"]


def parse_filters(query: str) -> dict:
    """Parse ?wd=...&q=...&status=...&search=... into a filter spec."""
    qs = parse_qs(query, keep_blank_values=False)
    return {
        "workdir": (qs.get("wd") or [""])[0].strip(),
        "queue":   (qs.get("q")  or [""])[0].strip(),
        "status":  (qs.get("status") or [""])[0].strip(),
        "search":  (qs.get("search") or [""])[0].strip(),
    }


def task_matches(task: dict, filters: dict) -> bool:
    """Test if a task survives the filter spec. Empty filter values match all."""
    if filters["status"] and task["status"] != filters["status"]:
        return False
    if filters["search"]:
        needle = filters["search"].lower()
        haystack = (
            task["slug"].lower()
            + " " + (task.get("title") or "").lower()
            + " " + task["sha"].lower()
            + " " + (task.get("verifier") or "").lower()
            + " " + (task.get("owner") or "").lower()
        )
        if needle not in haystack:
            return False
    return True


def apply_filters(state: dict, filters: dict) -> dict:
    """Return a new state with workdirs/queues/tasks filtered. Preserves
    structure so the render path stays identical."""
    out_workdirs: dict[str, dict[str, list[dict]]] = {}
    by_status: dict[str, int] = defaultdict(int)
    total = 0
    for wd, queues in state["workdirs"].items():
        if filters["workdir"] and wd != filters["workdir"]:
            continue
        kept_queues: dict[str, list[dict]] = {}
        for q, tasks in queues.items():
            if filters["queue"] and q != filters["queue"]:
                continue
            kept = [t for t in tasks if task_matches(t, filters)]
            if kept:
                kept_queues[q] = kept
                for t in kept:
                    by_status[t["status"]] += 1
                    total += 1
        if kept_queues:
            out_workdirs[wd] = kept_queues
    return {
        "workdirs": out_workdirs,
        "totals": {"tasks": total, "by_status": dict(by_status)},
    }


def render_filter_form(state: dict, filters: dict) -> str:
    """The filter bar at the top. Dropdowns populated from live state."""
    workdirs = sorted(state["workdirs"].keys())
    queues_set: set[str] = set()
    for q_map in state["workdirs"].values():
        queues_set.update(q_map.keys())
    queues = sorted(queues_set)

    def opt(value: str, current: str) -> str:
        sel = ' selected' if value == current else ''
        label = escape(value) if value else "(any)"
        return f'<option value="{escape(value)}"{sel}>{label}</option>'

    wd_opts = opt("", filters["workdir"]) + "".join(
        opt(wd, filters["workdir"]) for wd in workdirs
    )
    q_opts = opt("", filters["queue"]) + "".join(
        opt(q, filters["queue"]) for q in queues
    )
    st_opts = opt("", filters["status"]) + "".join(
        opt(s, filters["status"]) for s in STATUS_OPTIONS
    )

    search_val = escape(filters["search"])
    return f'''<form class="filters" method="get" action="/">
  <label>workdir <select name="wd">{wd_opts}</select></label>
  <label>queue <select name="q">{q_opts}</select></label>
  <label>status <select name="status">{st_opts}</select></label>
  <label>search <input type="text" name="search" value="{search_val}" placeholder="slug / title / sha / verifier / owner" size="32"></label>
  <button type="submit">apply</button>
  <a href="/" class="clear">clear</a>
</form>'''


def render_html(state: dict, refresh: int, filters: dict | None = None,
                full_state: dict | None = None) -> str:
    """`state` is the (possibly filtered) snapshot rendered as the body.
    `full_state` is the unfiltered snapshot used to populate filter dropdowns.
    `filters` carries the current filter values (for form pre-population)."""
    filters = filters or {"workdir": "", "queue": "", "status": "", "search": ""}
    full_state = full_state or state

    pieces = [HTML_HEAD.format(refresh=refresh)]

    # Filter form.
    pieces.append(render_filter_form(full_state, filters))

    # Totals bar — reflects the filtered view.
    total = state["totals"]["tasks"]
    full_total = full_state["totals"]["tasks"]
    parts = [f"<strong>Showing:</strong> {total}"]
    if total != full_total:
        parts[0] += f' <span class="sha">of {full_total}</span>'
    for st, n in sorted(state["totals"]["by_status"].items()):
        parts.append(f'<span class="status status-{escape(st)}">{escape(st)}: {n}</span>')
    pieces.append(f'<div class="totals">{" ".join(parts)}</div>')

    if not state["workdirs"]:
        pieces.append('<p class="empty">(no tasks match the current filter)</p>')

    for workdir in sorted(state["workdirs"].keys()):
        queues = state["workdirs"][workdir]
        pieces.append(f'<div class="workdir"><h2>📂 {escape(workdir)}</h2>')
        for queue in sorted(queues.keys()):
            tasks = queues[queue]
            pieces.append(f'<div class="queue"><h3>queue: <code>{escape(queue)}</code> &nbsp;<span class="sha">({len(tasks)} tasks)</span></h3>')

            # Build parent index.
            by_sha = {t["sha"]: t for t in tasks}
            children_of: dict = defaultdict(list)
            roots = []
            for t in tasks:
                if t["parent"] and t["parent"] in by_sha:
                    children_of[t["parent"]].append(t)
                else:
                    roots.append(t)

            for r in sorted(roots, key=lambda x: x.get("created_at") or ""):
                pieces.extend(_render_task(r, children_of))

            pieces.append('</div>')
        pieces.append('</div>')

    pieces.append(
        '<div class="foot">Powered by <code>pm dashboard</code> reading hashharness MCP. '
        'See <a href="/api/state">/api/state</a> for raw JSON.</div>'
    )
    pieces.append('</body></html>')
    return "\n".join(pieces)


def render_task_detail_html(detail: dict, refresh: int) -> str:
    """Render the per-task detail page: header + chronological event timeline."""
    task = detail["task"]
    attrs = task.get("attributes") or {}
    sha = task.get("text_sha256") or ""
    short = sha[:12]
    slug = attrs.get("slug") or "?"
    title = task.get("title") or ""
    body = (task.get("text") or "").split("\n#nonce:")[0]

    pieces = [HTML_HEAD.format(refresh=refresh)]
    pieces.append(f'<p><a href="/">← back to dashboard</a></p>')

    # Header.
    rows = []
    def row(k, v):
        if v is None or v == "":
            return
        rows.append(f'<dt>{escape(str(k))}</dt><dd>{escape(str(v))}</dd>')
    row("sha", sha)
    row("slug", slug)
    row("queue", attrs.get("queue"))
    row("workdir", attrs.get("workdir"))
    row("verifier", attrs.get("verifier"))
    row("sticky", attrs.get("sticky"))
    row("created_at", task.get("created_at"))
    row("work_package_id", task.get("work_package_id"))

    body_html = f'<div class="event"><div class="body">{render_markdown(body)}</div></div>' if body else ""
    pieces.append(
        f'<div class="task-header">'
        f'<h2>{escape(slug)} <span class="sha">{escape(short)}</span></h2>'
        + (f'<div>{escape(title)}</div>' if title else '')
        + f'<dl>{"".join(rows)}</dl>'
        + body_html
        + '</div>'
    )

    # Event timeline.
    pieces.append(f'<h2>Timeline ({len(detail["events"])} events)</h2>')
    pieces.append('<div class="timeline">')
    if not detail["events"]:
        pieces.append('<p class="empty">(no events yet)</p>')
    for ev in detail["events"]:
        kind = ev["kind"]
        when = escape(ev.get("created_at") or "")
        if kind == "status":
            status = ev["key"]
            badges = []
            if ev.get("agent"):
                badges.append(f'<span class="tag tag-owner">@{escape(ev["agent"])[:32]}</span>')
            if ev.get("context_id"):
                badges.append(f'<span class="tag tag-ctx">ctx:{escape(ev["context_id"][:8])}</span>')
            if ev.get("verifier"):
                vtxt = f'verifier:{escape(ev["verifier"])[:30]}'
                if ev.get("verifier_exit") is not None:
                    vtxt += f' exit={ev["verifier_exit"]}'
                badges.append(f'<span class="tag tag-verifier">{vtxt}</span>')
            if ev.get("reclaimed"):
                badges.append(f'<span class="tag" style="background:#ffe0b2;color:#bf360c">reclaimed by {escape(ev.get("reclaimer") or "")}</span>')
            if ev.get("cancelled"):
                badges.append(f'<span class="tag" style="background:#ffcdd2;color:#b71c1c">cancelled by {escape(ev.get("cancelled_by") or "")}</span>')
            if ev.get("replanned"):
                badges.append(f'<span class="tag" style="background:#e1bee7;color:#4a148c">replanned</span>')
            if ev.get("superseded_by"):
                badges.append(f'<span class="tag">superseded → {escape(ev["superseded_by"][:12])}</span>')
            note_html = ""
            if ev.get("verifier_summary"):
                note_html += f'<div class="body">{render_markdown(ev["verifier_summary"])}</div>'
            if ev.get("cancel_reason"):
                note_html += f'<div class="meta">reason: {escape(ev["cancel_reason"])}</div>'
            if ev.get("note") and ev["note"] != f'claimed by {ev.get("agent","")}':
                note_html += f'<div class="meta">{escape(ev["note"])}</div>'
            pieces.append(
                f'<div class="event event-status">'
                f'<span class="kind">status</span>'
                f'<span class="status status-{escape(status)}">{escape(status)}</span>'
                f' {"".join(badges)} <span class="when">{when}</span>'
                f'{note_html}'
                f'</div>'
            )
        elif kind == "report":
            body_text = ev.get("body") or ""
            title_text = ev.get("title") or ""
            pieces.append(
                f'<div class="event event-report">'
                f'<span class="kind">report</span>'
                f'<strong>{escape(title_text)}</strong> <span class="when">{when}</span>'
                f'<div class="body report-body">{render_markdown(body_text)}</div>'
                f'</div>'
            )
        elif kind == "heartbeat":
            preempt = ' <span class="tag" style="background:#ffe0b2;color:#bf360c">preempt</span>' if ev.get("preempt") else ''
            agent = escape(ev.get("agent") or "")
            pieces.append(
                f'<div class="event event-heartbeat">'
                f'<span class="kind">heartbeat</span>'
                f'<span class="tag tag-owner">@{agent[:32]}</span>{preempt}'
                f' <span class="when">{when}</span>'
                f'</div>'
            )
    pieces.append('</div>')

    pieces.append(f'<div class="foot">JSON: <a href="/api/task/{escape(sha)}">/api/task/{escape(short)}</a></div>')
    pieces.append('</body></html>')
    return "\n".join(pieces)


class Handler(BaseHTTPRequestHandler):
    refresh_seconds = 5

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/state":
                full_state = fetch_state()
                # /api/state honours filters too — useful for scripted scrapes.
                filters = parse_filters(parsed.query)
                state = apply_filters(full_state, filters) if any(filters.values()) else full_state
                payload = {"filters": filters, **state} if any(filters.values()) else state
                body = json.dumps(payload, indent=2, default=str).encode("utf-8")
                self._respond(200, "application/json", body)
            elif path == "/healthz":
                self._respond(200, "text/plain", b"ok")
            elif path.startswith("/task/"):
                sha = path[len("/task/"):].strip("/")
                detail = fetch_task_detail(sha)
                if detail is None:
                    self._respond(404, "text/plain", b"task not found")
                else:
                    body = render_task_detail_html(detail, self.refresh_seconds).encode("utf-8")
                    self._respond(200, "text/html; charset=utf-8", body)
            elif path.startswith("/api/task/"):
                sha = path[len("/api/task/"):].strip("/")
                detail = fetch_task_detail(sha)
                if detail is None:
                    self._respond(404, "application/json", b'{"error":"not found"}')
                else:
                    body = json.dumps(detail, indent=2, default=str).encode("utf-8")
                    self._respond(200, "application/json", body)
            elif path in ("/", "/index.html"):
                full_state = fetch_state()
                filters = parse_filters(parsed.query)
                state = apply_filters(full_state, filters) if any(filters.values()) else full_state
                body = render_html(
                    state, self.refresh_seconds,
                    filters=filters, full_state=full_state,
                ).encode("utf-8")
                self._respond(200, "text/html; charset=utf-8", body)
            else:
                self._respond(404, "text/plain", b"not found")
        except Exception as e:  # pragma: no cover — surface the error
            msg = f"<pre>error: {escape(str(e))}</pre>".encode("utf-8")
            self._respond(500, "text/html; charset=utf-8", msg)

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        # Quieter than default — only log non-200s.
        if args and args[1] not in ("200", "304"):
            sys.stderr.write(f"{self.address_string()} - {format % args}\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=38418)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--refresh", type=int, default=5,
                   help="HTML auto-refresh interval in seconds (default 5)")
    args = p.parse_args()

    Handler.refresh_seconds = args.refresh
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"pm dashboard listening at http://{args.bind}:{args.port}/", file=sys.stderr)
    print(f"  /              auto-refresh every {args.refresh}s", file=sys.stderr)
    print(f"  /api/state     JSON snapshot", file=sys.stderr)
    print(f"  /healthz       liveness probe", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
