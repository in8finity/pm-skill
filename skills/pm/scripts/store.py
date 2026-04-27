#!/usr/bin/env python3
"""Shared helpers for planning task storage on hashharness."""
from __future__ import annotations

import hashlib
import secrets
from typing import Any, Iterable

import mcp_client
from now_iso import now_iso

VALID_STATUSES = ("new", "working", "done", "rejected", "superseded")


class SlugTaken(Exception):
    """Raised when create_task fails because the (queue, slug) is already claimed."""
    def __init__(self, queue: str, slug: str, mcp_error: dict[str, Any] | None = None) -> None:
        self.queue = queue
        self.slug = slug
        self.mcp_error = mcp_error
        super().__init__(f"slug '{slug}' already exists in queue '{queue}'")


class ClaimLost(Exception):
    """Raised when append_claim fails because another agent claimed off the same prev-tip.

    The deterministic claim text (``claim:<task>:<prev>``) makes two parallel
    claimants targeting the same prev-tip collide on text_sha256; hashharness
    rejects the second.
    """
    def __init__(self, task_sha: str, prev_status_sha: str, mcp_error: dict[str, Any] | None = None) -> None:
        self.task_sha = task_sha
        self.prev_status_sha = prev_status_sha
        self.mcp_error = mcp_error
        super().__init__(
            f"claim race lost on task {task_sha[:12]} (prev={prev_status_sha[:12]})"
        )


def queue_wp(queue: str) -> str:
    return f"planning:{queue}"


def task_wp(task_sha: str) -> str:
    return f"planning:task:{task_sha}"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unwrap_items(res: Any) -> list[dict[str, Any]]:
    """Normalize MCP list-returning responses to a plain list of items.

    `find_items` / `get_work_package` wrap results as ``{"items": [...]}``
    (sometimes with ``item_count``). Older shapes returned a bare list.
    """
    if not res:
        return []
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        return res.get("items") or []
    return []


def find_task_by_slug(queue: str, slug: str) -> dict[str, Any] | None:
    """Locate a Task by (queue, slug) via its canonical content hash.

    Tasks are content-addressed on ``text = task:<queue>/<slug>`` (see
    ``task_identity_text``), so the (queue, slug) pair maps to exactly
    one ``text_sha256``. We look up by that hash directly — this is
    both faster than a query and avoids relying on the ``find_items``
    work-package-id filter (which has been observed to match across
    queues).
    """
    import hashlib
    sha = hashlib.sha256(task_identity_text(queue, slug).encode()).hexdigest()
    item = mcp_client.tool("get_item_by_hash", {"text_sha256": sha})
    if isinstance(item, dict) and item.get("type") == "Task":
        return item
    return None


def list_tasks(queue: str) -> list[dict[str, Any]]:
    return _unwrap_items(mcp_client.tool(
        "get_work_package",
        {"work_package_id": queue_wp(queue), "type": "Task"},
    ))


def _normalize_tip(res: Any) -> dict[str, Any] | None:
    """find_tip returns a minimal projection (no `attributes` / `links`),
    a list, or a string error message ("No items found ...") when the
    work_package is empty. We re-hydrate to a full record so callers can
    inspect attributes.status etc."""
    if not res:
        return None
    if isinstance(res, list):
        if not res:
            return None
        res = res[0]
    if not isinstance(res, dict):
        return None
    sha = res.get("text_sha256")
    if not sha:
        return res
    full = mcp_client.tool("get_item_by_hash", {"text_sha256": sha})
    if isinstance(full, dict) and full.get("type"):
        return full
    return res


def latest_status(task_sha: str) -> dict[str, Any] | None:
    return _normalize_tip(mcp_client.tool(
        "find_tip",
        {"work_package_id": task_wp(task_sha), "type": "TaskStatus"},
    ))


def latest_report(task_sha: str) -> dict[str, Any] | None:
    return _normalize_tip(mcp_client.tool(
        "find_tip",
        {"work_package_id": task_wp(task_sha), "type": "TaskReport"},
    ))


def status_value(status_item: dict[str, Any] | None) -> str | None:
    if not status_item:
        return None
    return (status_item.get("attributes") or {}).get("status")


# ===== Sticky-context helpers =====

def task_is_sticky(task: dict[str, Any] | None) -> bool:
    if not task:
        return False
    return bool((task.get("attributes") or {}).get("sticky"))


def task_workdir(task: dict[str, Any] | None) -> str | None:
    """Return the ``workdir`` recorded on a Task, or None for legacy tasks."""
    if not task:
        return None
    return (task.get("attributes") or {}).get("workdir")


def status_context_id(status_item: dict[str, Any] | None) -> str | None:
    """Return the ``context_id`` recorded on a TaskStatus, or None."""
    if not status_item:
        return None
    return (status_item.get("attributes") or {}).get("context_id")


def task_context_id(task_sha: str) -> str | None:
    """Latest context binding for a task — the context_id on its latest
    TaskStatus, or None if unbound (e.g., after reclaim)."""
    return status_context_id(latest_status(task_sha))


class StickyContextMismatch(Exception):
    """Raised when an agent's PM_CONTEXT_ID doesn't match a sticky task's
    bound context (or one of its sticky parent / dependency contexts)."""
    def __init__(self, task_sha: str, expected: str | None,
                 found: str | None, where: str = "task"):
        self.task_sha = task_sha
        self.expected = expected
        self.found = found
        self.where = where
        super().__init__(
            f"sticky-context mismatch on {where} {task_sha[:12]}: "
            f"expected {(expected or '<unbound>')[:12]}, "
            f"got {(found or '<unset>')[:12]}"
        )


class StickyContextConflict(Exception):
    """Raised when a sticky task has parents/deps bound to two distinct
    contexts — no single agent can satisfy both."""
    def __init__(self, task_sha: str, contexts: set[str]):
        self.task_sha = task_sha
        self.contexts = contexts
        super().__init__(
            f"sticky-context conflict on {task_sha[:12]}: "
            f"ancestors/deps span {len(contexts)} distinct contexts"
        )


def collect_required_contexts(task_sha: str) -> set[str]:
    """Walk the task's connected sticky chain in both directions:

    - upward via parent + dependsOn (sticky ancestors)
    - downward via reverse-parent walk over the queue (sticky descendants)

    Collects every distinct context_id of sticky tasks bound to a context.
    The downward walk catches the case where a sticky child was claimed
    before the sticky parent — without it, the parent claim would succeed
    with a different context, breaking sticky-chain coherence (formally
    verified by ``StickyChainCoherence`` in planning.als).
    """
    contexts: set[str] = set()
    seen: set[str] = set()

    def visit_up(t_sha: str) -> None:
        if t_sha in seen:
            return
        seen.add(t_sha)
        t = get_task(t_sha)
        if t is None:
            return
        if task_is_sticky(t):
            cid = task_context_id(t_sha)
            if cid:
                contexts.add(cid)
        parent = (t.get("links") or {}).get("parentTask")
        if parent:
            visit_up(parent)
        for d in (t.get("links") or {}).get("dependsOn") or []:
            visit_up(d)

    me = get_task(task_sha)
    if me is None:
        return contexts
    queue = (me.get("attributes") or {}).get("queue", "default")
    parent = (me.get("links") or {}).get("parentTask")
    if parent:
        visit_up(parent)
    for d in (me.get("links") or {}).get("dependsOn") or []:
        visit_up(d)

    # Downward: any sticky descendant currently bound to a context.
    # Walk via parent reverse-links (find_undone_subtasks recursively).
    descendant_seen: set[str] = set()
    def visit_down(t_sha: str) -> None:
        for child in find_undone_subtasks(t_sha, queue):
            csha = child["text_sha256"]
            if csha in descendant_seen:
                continue
            descendant_seen.add(csha)
            if task_is_sticky(child):
                cid = task_context_id(csha)
                if cid:
                    contexts.add(cid)
            visit_down(csha)
    visit_down(task_sha)

    return contexts


def check_sticky_eligibility(task_sha: str, agent_context: str | None) -> None:
    """Raise StickyContext{Mismatch,Conflict} if the agent's context can't
    legally claim/heartbeat/report/finish a sticky task."""
    task = get_task(task_sha)
    if not task_is_sticky(task):
        return
    required = collect_required_contexts(task_sha)
    if len(required) > 1:
        raise StickyContextConflict(task_sha, required)
    if required:
        expected = next(iter(required))
        if agent_context != expected:
            raise StickyContextMismatch(task_sha, expected, agent_context,
                                        where="ancestor/dep")
    own = task_context_id(task_sha)
    if own and agent_context != own:
        raise StickyContextMismatch(task_sha, own, agent_context,
                                    where="task")


def task_identity_text(queue: str, slug: str) -> str:
    """Canonical text for a Task record — fully determined by (queue, slug).

    Identity is content-addressed via sha256(text); making the text deterministic
    from queue+slug means hashharness rejects duplicate slug-creates by
    construction. The user's free-form body lives in `attributes.body`.
    """
    return f"task:{queue}/{slug}"


def create_task(
    queue: str,
    title: str,
    text: str,
    slug: str,
    *,
    parent_task_sha: str | None = None,
    spawned_at_status_sha: str | None = None,
    depends_on: Iterable[str] | None = None,
    verifier: str | None = None,
    sticky: bool = False,
    workdir: str | None = None,
) -> dict[str, Any]:
    links: dict[str, Any] = {}
    if parent_task_sha:
        links["parentTask"] = parent_task_sha
    if spawned_at_status_sha:
        links["spawnedAt"] = spawned_at_status_sha
    deps = list(depends_on or [])
    if deps:
        links["dependsOn"] = deps
    # `text` (the caller's free-form body) moves to attributes.body so the
    # record's `text_sha256` is determined solely by (queue, slug). Two parallel
    # plan() calls with the same slug now produce the same text_sha256 and
    # hashharness rejects the second — see system-models/planning_plan_race.als.
    attributes: dict[str, Any] = {"slug": slug, "queue": queue, "body": text}
    if verifier:
        attributes["verifier"] = verifier
    if sticky:
        attributes["sticky"] = True
    if workdir:
        attributes["workdir"] = workdir
    result, err = mcp_client.tool_safe(
        "create_item",
        {
            "type": "Task",
            "work_package_id": queue_wp(queue),
            "created_at": now_iso(),
            "title": title,
            "text": task_identity_text(queue, slug),
            "attributes": attributes,
            "links": links,
            "return": "full",
        },
    )
    if err is not None:
        # hashharness rejects on duplicate text_sha256 — that's the slug-uniqueness gate.
        msg = str(err.get("message", err)).lower()
        if "already exists" in msg or "cannot be updated" in msg:
            raise SlugTaken(queue, slug, mcp_error=err)
        raise RuntimeError(f"create_item failed: {err}")
    return result


def append_status(
    task_sha: str,
    status: str,
    *,
    note: str = "",
    proof_report_sha: str | None = None,
    extra_attrs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}; expected one of {VALID_STATUSES}")
    prev = latest_status(task_sha)
    links: dict[str, Any] = {"task": task_sha}
    if prev:
        links["prevStatus"] = prev["text_sha256"]
    if proof_report_sha:
        links["proof"] = proof_report_sha
    body = note or status
    # `#nonce:` is purely a hash-uniqueness footer (sha256(text) is the
    # record id, so two appends with identical bodies would otherwise
    # collide). Auditable facts live elsewhere: timestamp in `created_at`,
    # prev-edge in `links.prevStatus`, status in `attributes.status`.
    text = f"{body}\n#nonce:{secrets.token_hex(8)}"
    attributes: dict[str, Any] = {"status": status}
    if extra_attrs:
        attributes.update(extra_attrs)
    return mcp_client.tool(
        "create_item",
        {
            "type": "TaskStatus",
            "work_package_id": task_wp(task_sha),
            "created_at": now_iso(),
            "title": f"{status}: {task_sha[:8]}",
            "text": text,
            "attributes": attributes,
            "links": links,
            "return": "full",
        },
    )


def get_task(task_sha: str) -> dict[str, Any] | None:
    """Fetch a Task record by its text_sha256."""
    res = mcp_client.tool("get_item_by_hash", {"text_sha256": task_sha})
    if isinstance(res, dict) and res.get("type") == "Task":
        return res
    return None


def append_claim(task_sha: str, agent: str, prev_status_sha: str,
                 *, context_id: str | None = None) -> dict[str, Any]:
    """Atomically claim a task — deterministic-text TaskStatus(working).

    The text is fully determined by (task_sha, prev_status_sha) and carries
    no nonce, so two concurrent agents observing the same prev-tip produce
    identical ``text_sha256``. With hashharness's atomic check-then-insert
    in ``create_item`` (cache_lock held continuously), only one such
    create succeeds; the second raises ``ClaimLost``. Agent identity is
    in ``attributes.agent`` (not in text), so it doesn't break the
    collision.

    Caller is responsible for the pre-claim check that ``prev_status_sha``
    is the current latest tip with ``status == "new"``.
    """
    text = f"claim:{task_sha[:16]}/{prev_status_sha[:16]}"
    attributes: dict[str, Any] = {"status": "working", "agent": agent}
    if context_id:
        attributes["context_id"] = context_id
    result, err = mcp_client.tool_safe(
        "create_item",
        {
            "type": "TaskStatus",
            "work_package_id": task_wp(task_sha),
            "created_at": now_iso(),
            "title": f"working: {task_sha[:8]}",
            "text": text,
            "attributes": attributes,
            "links": {"task": task_sha, "prevStatus": prev_status_sha},
            "return": "full",
        },
    )
    if err is not None:
        msg = str(err.get("message", err)).lower()
        if "already exists" in msg or "cannot be updated" in msg:
            raise ClaimLost(task_sha, prev_status_sha, mcp_error=err)
        raise RuntimeError(f"create_item failed: {err}")
    return result


def latest_heartbeat(task_sha: str) -> dict[str, Any] | None:
    return _normalize_tip(mcp_client.tool(
        "find_tip",
        {"work_package_id": task_wp(task_sha), "type": "TaskHeartbeat"},
    ))


def append_heartbeat(task_sha: str, agent: str, claim_status_sha: str) -> dict[str, Any]:
    """Append a TaskHeartbeat record proving the agent is still alive on this task.

    `claim_status_sha` should be the TaskStatus(working) the heartbeat is for —
    it lets a sweeper distinguish heartbeats from a dead claim cycle from those
    of a fresh one.
    """
    prev = latest_heartbeat(task_sha)
    links: dict[str, Any] = {"task": task_sha, "claimStatus": claim_status_sha}
    if prev:
        links["prevHeartbeat"] = prev["text_sha256"]
    text = f"hb:{task_sha[:8]}:{agent}\n#nonce:{secrets.token_hex(8)}"
    return mcp_client.tool(
        "create_item",
        {
            "type": "TaskHeartbeat",
            "work_package_id": task_wp(task_sha),
            "created_at": now_iso(),
            "title": f"hb {task_sha[:8]} {agent}",
            "text": text,
            "attributes": {"agent": agent},
            "links": links,
            "return": "full",
        },
    )


def last_activity_at(task_sha: str) -> str | None:
    """Return the most recent ``created_at`` across status / report / heartbeat
    chains for this task — the supervisor's notion of "is anything happening"."""
    candidates: list[str] = []
    for fetch in (latest_status, latest_report, latest_heartbeat):
        item = fetch(task_sha)
        if item and item.get("created_at"):
            candidates.append(item["created_at"])
    if not candidates:
        return None
    return max(candidates)


def cancel_task(
    task_sha: str,
    *,
    reason: str = "cancelled",
    cancelled_by: str = "supervisor",
) -> dict[str, Any]:
    """Append a synthetic TaskReport + TaskStatus(rejected, cancelled=true).

    Cancellation is allowed regardless of ownership — the caller is treated
    as a supervisor / planner exercising override authority. The synthetic
    report carries the cancel reason and serves as the `proof` link on the
    rejected status, so the existing ProofRequiredForTerminal invariant
    keeps holding by construction.

    Caller is responsible for refusing to cancel an already-terminal task
    (see cancel.py).
    """
    body = f"cancelled by {cancelled_by}: {reason}"
    report = append_report(task_sha, title=f"cancelled: {task_sha[:8]}", text=f"{body}\n#nonce:{secrets.token_hex(8)}")

    prev = latest_status(task_sha)
    links: dict[str, Any] = {"task": task_sha, "proof": report["text_sha256"]}
    if prev:
        links["prevStatus"] = prev["text_sha256"]
    status_text = f"{body}\n#nonce:{secrets.token_hex(8)}"
    status = mcp_client.tool(
        "create_item",
        {
            "type": "TaskStatus",
            "work_package_id": task_wp(task_sha),
            "created_at": now_iso(),
            "title": f"cancelled: {task_sha[:8]}",
            "text": status_text,
            "attributes": {
                "status": "rejected",
                "cancelled": True,
                "cancelled_by": cancelled_by,
                "cancel_reason": reason,
            },
            "links": links,
            "return": "full",
        },
    )
    return {"report": report, "status": status}


def find_undone_subtasks(parent_sha: str, queue: str) -> list[dict[str, Any]]:
    """Return Tasks whose ``links.parentTask == parent_sha`` and whose
    latest status is not in {done, rejected}. Used by cancel.py --cascade."""
    children: list[dict[str, Any]] = []
    for t in list_tasks(queue):
        if (t.get("links") or {}).get("parentTask") != parent_sha:
            continue
        sha = t["text_sha256"]
        cur = status_value(latest_status(sha))
        if cur in ("done", "rejected"):
            continue
        children.append(t)
    return children


def find_dependency_ancestors(task_sha: str) -> list[str]:
    """Return all ancestor task shas reachable via ``links.dependsOn``.

    Topologically deep-first; the closest ancestor (immediate dep) appears
    first, the most upstream last. Cycles are broken by a visited set.
    Used by replan.py to reset the chain leading up to a stuck task.
    """
    out: list[str] = []
    visited: set[str] = {task_sha}

    def walk(sha: str) -> None:
        task = get_task(sha)
        if task is None:
            return
        deps = (task.get("links") or {}).get("dependsOn") or []
        for d in deps:
            if d in visited:
                continue
            visited.add(d)
            out.append(d)
            walk(d)

    walk(task_sha)
    return out


def reclaim(task_sha: str, *, reason: str = "stale lease", reclaimer: str = "sweeper") -> dict[str, Any]:
    """Append a TaskStatus(new, reclaimed=true) recycling a zombie task.

    Mirrors the Reclaim transition in planning_lease.als: phase' = PNew, no owner.
    The new status carries ``attributes.reclaimed = true`` so it's distinguishable
    from a genesis ``new`` event. Only meaningful when the latest status is
    ``working``; sweep.py enforces that precondition.
    """
    prev = latest_status(task_sha)
    links: dict[str, Any] = {"task": task_sha}
    if prev:
        links["prevStatus"] = prev["text_sha256"]
    body = f"reclaimed by {reclaimer}: {reason}"
    text = f"{body}\n#nonce:{secrets.token_hex(8)}"
    return mcp_client.tool(
        "create_item",
        {
            "type": "TaskStatus",
            "work_package_id": task_wp(task_sha),
            "created_at": now_iso(),
            "title": f"reclaimed: {task_sha[:8]}",
            "text": text,
            "attributes": {"status": "new", "reclaimed": True, "reclaimer": reclaimer},
            "links": links,
            "return": "full",
        },
    )


def append_report(task_sha: str, title: str, text: str) -> dict[str, Any]:
    prev = latest_report(task_sha)
    links: dict[str, Any] = {"task": task_sha}
    if prev:
        links["prevReport"] = prev["text_sha256"]
    # Two reports with the same body bytes (across any task) would
    # otherwise collide on `text_sha256` and hashharness rejects the
    # duplicate. The `#nonce:` footer mirrors the trick used in
    # append_status; auditable facts live in `created_at` and `links`.
    body = f"{text}\n#nonce:{secrets.token_hex(8)}"
    return mcp_client.tool(
        "create_item",
        {
            "type": "TaskReport",
            "work_package_id": task_wp(task_sha),
            "created_at": now_iso(),
            "title": title,
            "text": body,
            "links": links,
            "return": "full",
        },
    )
