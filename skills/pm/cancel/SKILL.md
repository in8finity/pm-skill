---
name: pm-cancel
description: >
  Cancel a task as a supervisor or planner override. Writes a synthetic
  TaskReport carrying the cancel reason, then appends a terminal
  TaskStatus(rejected, cancelled=true) linked to that proof. Use when a
  task should be stopped regardless of current ownership, or when a parent
  task's cancellation should cascade into unfinished subtasks.
---

# pm:cancel — supervisor cancel with proof

## Procedure

`../scripts/pm cancel --task <task-sha> [--reason "..."] [--cancelled-by ID] [--no-cascade]`

- Reads the latest TaskStatus for `<task-sha>`. Refuses with exit code 6
  if the task's current status is `done`, `rejected`, or `superseded`
  (any absorbing status — see "Why superseded is absorbing too" below).
- Appends a synthetic TaskReport containing the cancel reason.
- Appends a TaskStatus with:
  - `attributes.status = "rejected"`
  - `attributes.cancelled = true`
  - `attributes.cancelled_by = <id>`
  - `attributes.cancel_reason = <reason>`
  - `links.task = <task-sha>`
  - `links.prevStatus = <latest-status-sha>`
  - `links.proof = <synthetic-report-sha>`
- **Cascade is the default** (since v0.3): the cancel recursively visits
  every unfinished descendant via `parentTask` reverse-links and rejects
  them with the same reason. This avoids orphaned grandchildren — the
  failure mode where rejecting a parent left its subtree running, with
  no automatic mechanism to pull them down.
- Pass `--no-cascade` to opt out (legacy behavior). Use only when you
  genuinely want orphan children to keep running, which is rare; the
  alternative — a sibling group cancelled but their grandchildren still
  busy — is almost always a bug. `--cascade` is also accepted as a
  no-op for back-compat with scripts that pass it explicitly.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Task cancelled (and cascade completed if requested) |
| 6 | Task already absorbing (`done` / `rejected` / `superseded`); cancellation refused |

## Cascade properties (formally verified)

The `--cascade` walk is `parentTask`-reverse, depth-first, with a
visited set to break any cycles. Six properties are verified in
`system-models/planning_cancel_cascade.als`:

- **CC1 NoDescendantLeftUndone** — every undone descendant of the
  cancelled root ends up `rejected`.
- **CC2 PreviousTerminalUntouched** — descendants already in `done` or
  `rejected` are NOT re-closed; they stay where they were.
- **CC3 CascadeOnlyTransitionsNonTerminal** — the cascade only flips
  `new`/`working` → `rejected`; never disturbs an already-finished
  task.
- **CC4 CascadeIsParentTransitive** — if A is parent of B and B is
  parent of C, cancelling A reaches C.
- **CC5 NonDescendantUntouched** — tasks NOT in the parent⁻¹ closure
  are not touched. Sibling subtrees are isolated.
- **CC6 CascadeRefusesAbsorbingRoot** — cancel on a root that's
  already `done`/`rejected`/`superseded` exits 6 without entering the
  cascade DFS.

**Boundary**: cascade stops at absorbing intermediates. If a
descendant chain runs A→B(superseded)→C, cancelling A visits B
(refused per R4), does NOT recurse, and leaves C untouched. In
practice supersede creates a NEW task; the OLD task's children are
unusual after supersede. If you need to wipe a chain that includes
superseded intermediates, cancel each undone subtree explicitly.

## Why cancellation writes proof

Cancellation is not a silent state flip. The synthetic TaskReport records
who cancelled the task and why, and the rejected TaskStatus links `proof`
to that report. This preserves the same audit property as ordinary finish:
every terminal status carries evidence.

## Ownership note

Unlike `pm:finished`, cancellation does **not** require the current
worker to be the closer. It is a supervisor/planner override by design.

## Why superseded is absorbing too

`pm replan --text/--verifier` marks the original task `superseded` and
creates a new clone. From the audit chain's perspective, the original
is already closed — its successor carries the work forward. Allowing
`pm cancel` to then append `rejected` on top would falsify the
`SupersededIsAbsorbing` property (verified in
`system-models/planning_replan.als`) and pollute the chain with a
misleading second close. If you want to stop the chain, cancel the
SUCCESSOR (the `-r<N>` clone) instead.
