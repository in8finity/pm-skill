#!/usr/bin/env python3
"""Shared helpers for planning task storage on hashharness."""
from __future__ import annotations

import hashlib
import secrets
from typing import Any, Iterable

import mcp_client

VALID_STATUSES = ("new", "working", "done", "rejected", "superseded")


def _link_record_sha_for(text_sha: str) -> str:
    """Resolve a Task's content-addressed identity (text_sha256) to the stored
    record's ``record_sha256`` for use as a link value.

    Inter-item links now reference ``record_sha256`` (hashharness 3a6cd18) so
    a target's full record (text + meta + links) is pinned, not just its text.
    Callers still hold text_sha256 as the canonical Task identity (it's the
    work-package namespace key and the slug-uniqueness gate); this helper
    bridges the two at link-write time.
    """
    item = mcp_client.tool("get_item_by_hash", {"text_sha256": text_sha})
    if not isinstance(item, dict) or "record_sha256" not in item:
        raise RuntimeError(
            f"cannot resolve record_sha256 for text_sha256={text_sha[:12]}"
        )
    return item["record_sha256"]


class SlugTaken(Exception):
    """Raised when create_task fails because the (queue, slug) is already claimed."""
    def __init__(self, queue: str, slug: str, mcp_error: dict[str, Any] | None = None) -> None:
        self.queue = queue
        self.slug = slug
        self.mcp_error = mcp_error
        super().__init__(f"slug '{slug}' already exists in queue '{queue}'")


class HeadMoved(Exception):
    """Raised when an append targeting a stale chain-head is rejected.

    Hashharness enforces ``chain_predecessor`` natively (schema 2026-05+):
    ``prevStatus`` / ``prevReport`` / ``prevHeartbeat`` must equal the
    current head record_sha256 for (work_package_id, type), else
    ``create_item`` rejects with 'head moved'. This exception surfaces
    that condition to callers that want to retry against the new tip.
    """
    def __init__(self, work_package_id: str, type_name: str,
                 mcp_error: dict[str, Any] | None = None) -> None:
        self.work_package_id = work_package_id
        self.type_name = type_name
        self.mcp_error = mcp_error
        super().__init__(
            f"head moved on ({work_package_id}, {type_name})"
        )


class WorkerStillAlive(Exception):
    """Raised when a reclaim attempt's preempt-heartbeat is rejected by
    `chain_predecessor` — a worker's heartbeat raced the sweeper's
    freshness snapshot. The sweep should abort the reclaim and leave the
    task to its (still-live) owner.

    Closes the TTL-window race where the sweeper observes age > TTL,
    then a heartbeat arrives, then the sweeper appends the reclaim
    status — wrongly evicting a live worker.
    """
    def __init__(self, task_sha: str,
                 expected_prev_heartbeat_sha: str | None,
                 mcp_error: dict[str, Any] | None = None) -> None:
        self.task_sha = task_sha
        self.expected_prev_heartbeat_sha = expected_prev_heartbeat_sha
        self.mcp_error = mcp_error
        super().__init__(
            f"worker still alive on {task_sha[:12]} — "
            f"heartbeat raced sweeper's snapshot "
            f"(expected_prev={(expected_prev_heartbeat_sha or '<none>')[:12]})"
        )


class ClaimLost(Exception):
    """Raised when append_claim fails because another agent claimed off the
    same prev-tip first. Now a thin re-classification of HeadMoved on the
    TaskStatus chain — the storage layer's compare-and-swap on the head
    pointer is the actual race-resolution primitive.
    """
    def __init__(self, task_sha: str, prev_status_sha: str, mcp_error: dict[str, Any] | None = None) -> None:
        self.task_sha = task_sha
        self.prev_status_sha = prev_status_sha
        self.mcp_error = mcp_error
        super().__init__(
            f"claim race lost on task {task_sha[:12]} (prev={prev_status_sha[:12]})"
        )


def _is_head_moved(err: dict[str, Any] | None) -> bool:
    """Detect hashharness's chain_predecessor head-mismatch rejection.

    Two phrasings are emitted depending on the rejection branch:
      * "head moved" / "head has moved" — when our supplied prev no
         longer matches the live head;
      * "must equal current head" — when prev is supplied stale, or
         omitted while a head exists.
    """
    if not err:
        return False
    msg = str(err.get("message", err)).lower()
    return (
        "head moved" in msg
        or "head has moved" in msg
        or "must equal current head" in msg
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
        links["parentTask"] = _link_record_sha_for(parent_task_sha)
    if spawned_at_status_sha:
        links["spawnedAt"] = _link_record_sha_for(spawned_at_status_sha)
    deps = list(depends_on or [])
    if deps:
        links["dependsOn"] = [_link_record_sha_for(d) for d in deps]
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
    """Append a TaskStatus chained off the current tip.

    ``task_sha`` is the Task's text_sha256 (canonical content-addressed
    identity). ``proof_report_sha`` is the proof-target's record_sha256
    (already-resolved link value — see hashharness 3a6cd18).
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status}; expected one of {VALID_STATUSES}")
    prev = latest_status(task_sha)
    links: dict[str, Any] = {"task": _link_record_sha_for(task_sha)}
    if prev:
        links["prevStatus"] = prev["record_sha256"]
    if proof_report_sha:
        links["proof"] = proof_report_sha
    body = note or status
    # `#nonce:` is a hash-uniqueness footer; concurrent appends are gated
    # by hashharness's native `chain_predecessor` check on ``prevStatus``,
    # not by text-collision. Auditable facts live in `created_at`,
    # `links.prevStatus`, and `attributes.status`.
    text = f"{body}\n#nonce:{secrets.token_hex(8)}"
    attributes: dict[str, Any] = {"status": status}
    if extra_attrs:
        attributes.update(extra_attrs)
    wp = task_wp(task_sha)
    result, err = mcp_client.tool_safe(
        "create_item",
        {
            "type": "TaskStatus",
            "work_package_id": wp,
            "title": f"{status}: {task_sha[:8]}",
            "text": text,
            "attributes": attributes,
            "links": links,
            "return": "full",
        },
    )
    if err is not None:
        if _is_head_moved(err):
            raise HeadMoved(wp, "TaskStatus", mcp_error=err)
        raise RuntimeError(f"create_item failed: {err}")
    return result


def get_task(task_sha: str) -> dict[str, Any] | None:
    """Fetch a Task record by its text_sha256."""
    res = mcp_client.tool("get_item_by_hash", {"text_sha256": task_sha})
    if isinstance(res, dict) and res.get("type") == "Task":
        return res
    return None


def append_claim(task_sha: str, agent: str, prev_status_sha: str,
                 *, context_id: str | None = None) -> dict[str, Any]:
    """Atomically claim a task by appending TaskStatus(working).

    Race-safety is provided by hashharness's native ``chain_predecessor``
    check on ``prevStatus``: two concurrent claimants observing the same
    tip both submit ``prevStatus = prev_status_sha``; the storage layer
    compare-and-swaps the head, one append succeeds, the other is
    rejected with 'head moved' which we surface as ``ClaimLost``.

    ``prev_status_sha`` must be the current tip's ``record_sha256``.
    Caller is responsible for the pre-claim check that the tip's
    ``status == "new"``.
    """
    extra: dict[str, Any] = {"agent": agent}
    if context_id:
        extra["context_id"] = context_id
    try:
        return append_status(
            task_sha,
            "working",
            note=f"claimed by {agent}",
            extra_attrs=extra,
        )
    except HeadMoved as e:
        raise ClaimLost(task_sha, prev_status_sha, mcp_error=e.mcp_error) from e


def latest_heartbeat(task_sha: str) -> dict[str, Any] | None:
    return _normalize_tip(mcp_client.tool(
        "find_tip",
        {"work_package_id": task_wp(task_sha), "type": "TaskHeartbeat"},
    ))


def append_heartbeat(task_sha: str, agent: str, claim_status_sha: str) -> dict[str, Any]:
    """Append a TaskHeartbeat record proving the agent is still alive on this task.

    `claim_status_sha` should be the TaskStatus(working) the heartbeat is for —
    it lets a sweeper distinguish heartbeats from a dead claim cycle from those
    of a fresh one. Concurrent heartbeats race-resolve via hashharness's
    ``chain_predecessor`` on ``prevHeartbeat``.
    """
    prev = latest_heartbeat(task_sha)
    links: dict[str, Any] = {
        "task": _link_record_sha_for(task_sha),
        "claimStatus": claim_status_sha,
    }
    if prev:
        links["prevHeartbeat"] = prev["record_sha256"]
    text = f"hb:{task_sha[:8]}:{agent}\n#nonce:{secrets.token_hex(8)}"
    wp = task_wp(task_sha)
    result, err = mcp_client.tool_safe(
        "create_item",
        {
            "type": "TaskHeartbeat",
            "work_package_id": wp,
            "title": f"hb {task_sha[:8]} {agent}",
            "text": text,
            "attributes": {"agent": agent},
            "links": links,
            "return": "full",
        },
    )
    if err is not None:
        if _is_head_moved(err):
            raise HeadMoved(wp, "TaskHeartbeat", mcp_error=err)
        raise RuntimeError(f"create_item failed: {err}")
    return result


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
    report = append_report(task_sha, title=f"cancelled: {task_sha[:8]}", text=body)
    status = append_status(
        task_sha,
        "rejected",
        note=body,
        proof_report_sha=report["record_sha256"],
        extra_attrs={
            "cancelled": True,
            "cancelled_by": cancelled_by,
            "cancel_reason": reason,
        },
    )
    return {"report": report, "status": status}


def find_undone_subtasks(parent_sha: str, queue: str) -> list[dict[str, Any]]:
    """Return Tasks whose ``links.parentTask`` points at ``parent_sha`` and
    whose latest status is not in {done, rejected}. Used by cancel.py
    --cascade.

    Link values are ``record_sha256`` (hashharness link contract), so we
    resolve the parent's record_sha256 once and compare against that.
    """
    parent = get_task(parent_sha)
    if parent is None:
        return []
    parent_record_sha = parent["record_sha256"]
    children: list[dict[str, Any]] = []
    for t in list_tasks(queue):
        if (t.get("links") or {}).get("parentTask") != parent_record_sha:
            continue
        sha = t["text_sha256"]
        cur = status_value(latest_status(sha))
        if cur in ("done", "rejected"):
            continue
        children.append(t)
    return children


def find_dependency_ancestors(task_sha: str) -> list[str]:
    """Return all ancestor task ``text_sha256`` values reachable via
    ``links.dependsOn``.

    Link values are ``record_sha256`` (hashharness link contract); we
    walk the originating queue once to build a record_sha → text_sha
    lookup so the returned shas are usable with ``get_task`` /
    ``latest_status`` (which key off text_sha256).

    Topologically deep-first; closest ancestor first, most upstream
    last. Cycles are broken by a visited set. Used by replan.py to
    reset the chain leading up to a stuck task.
    """
    root = get_task(task_sha)
    if root is None:
        return []
    queue = (root.get("attributes") or {}).get("queue", "default")
    record_to_text = {
        t["record_sha256"]: t["text_sha256"] for t in list_tasks(queue)
    }
    out: list[str] = []
    visited: set[str] = {task_sha}

    def walk(sha: str) -> None:
        task = get_task(sha)
        if task is None:
            return
        deps = (task.get("links") or {}).get("dependsOn") or []
        for d_record in deps:
            d = record_to_text.get(d_record)
            if d is None or d in visited:
                continue
            visited.add(d)
            out.append(d)
            walk(d)

    walk(task_sha)
    return out


def reclaim(
    task_sha: str,
    *,
    reason: str = "stale lease",
    reclaimer: str = "sweeper",
    preempt_heartbeat: bool = False,
    preempt_prev_heartbeat_sha: str | None = None,
) -> dict[str, Any]:
    """Append a TaskStatus(new, reclaimed=true) recycling a zombie task.

    Mirrors the Reclaim transition in planning_lease.als: phase' = PNew, no owner.
    The new status carries ``attributes.reclaimed = true`` so it's distinguishable
    from a genesis ``new`` event. Only meaningful when the latest status is
    ``working``; sweep.py enforces that precondition.

    Race-safety against live workers (sweep usage):
      Pass ``preempt_heartbeat=True`` along with the heartbeat tip's
      ``record_sha256`` (or None if no prior heartbeat) that the sweeper
      observed at freshness-snapshot time. Reclaim then appends a
      "preempt" TaskHeartbeat first, with ``prevHeartbeat`` set to the
      observed tip. If a worker raced and committed a heartbeat in
      between, hashharness's ``chain_predecessor`` on ``prevHeartbeat``
      rejects the preempt with 'head moved' — surfaced as
      ``WorkerStillAlive`` so the sweeper aborts cleanly. If no race,
      the preempt commits and the reclaim status follows.

      Without ``preempt_heartbeat=True`` (default), reclaim is
      unconditional — used by supervisor flows that don't need to
      respect liveness (e.g. ``cancel.py --cascade``).
    """
    if preempt_heartbeat:
        preempt_text = (
            f"preempt:{task_sha[:8]}:{reclaimer}\n#nonce:{secrets.token_hex(8)}"
        )
        links: dict[str, Any] = {
            "task": _link_record_sha_for(task_sha),
        }
        if preempt_prev_heartbeat_sha is not None:
            links["prevHeartbeat"] = preempt_prev_heartbeat_sha
        wp = task_wp(task_sha)
        # `claimStatus` is required by the schema (single-link), so we
        # point it at the current working status — same one whose lease
        # we're about to break. If a worker is mid-claim and a fresh
        # `working` has just been committed (re-claim race), the link
        # target is still valid but the freshness snapshot would have
        # noticed the new status's age was within TTL anyway.
        cur_status = latest_status(task_sha)
        if cur_status is not None:
            links["claimStatus"] = cur_status["record_sha256"]
        result, err = mcp_client.tool_safe(
            "create_item",
            {
                "type": "TaskHeartbeat",
                "work_package_id": wp,
                "title": f"sweep-preempt {task_sha[:8]}",
                "text": preempt_text,
                "attributes": {"agent": reclaimer, "preempt": True},
                "links": links,
                "return": "full",
            },
        )
        if err is not None:
            if _is_head_moved(err):
                raise WorkerStillAlive(
                    task_sha, preempt_prev_heartbeat_sha, mcp_error=err
                )
            raise RuntimeError(f"preempt-heartbeat create_item failed: {err}")

    body = f"reclaimed by {reclaimer}: {reason}"
    return append_status(
        task_sha,
        "new",
        note=body,
        extra_attrs={"reclaimed": True, "reclaimer": reclaimer},
    )


def append_report(task_sha: str, title: str, text: str) -> dict[str, Any]:
    prev = latest_report(task_sha)
    links: dict[str, Any] = {"task": _link_record_sha_for(task_sha)}
    if prev:
        links["prevReport"] = prev["record_sha256"]
    # `#nonce:` footer keeps `text_sha256` unique when two reports share
    # the same body bytes. Concurrent appends race-resolve via
    # hashharness's `chain_predecessor` on `prevReport`.
    body = f"{text}\n#nonce:{secrets.token_hex(8)}"
    wp = task_wp(task_sha)
    result, err = mcp_client.tool_safe(
        "create_item",
        {
            "type": "TaskReport",
            "work_package_id": wp,
            "title": title,
            "text": body,
            "links": links,
            "return": "full",
        },
    )
    if err is not None:
        if _is_head_moved(err):
            raise HeadMoved(wp, "TaskReport", mcp_error=err)
        raise RuntimeError(f"create_item failed: {err}")
    return result
