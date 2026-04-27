---
name: planning-cancel
description: >
  Cancel a task as a supervisor or planner override. Writes a synthetic
  TaskReport carrying the cancel reason, then appends a terminal
  TaskStatus(rejected, cancelled=true) linked to that proof. Use when a
  task should be stopped regardless of current ownership, or when a parent
  task's cancellation should cascade into unfinished subtasks.
---

# planning:cancel — supervisor cancel with proof

## Procedure

`../scripts/pm cancel --task <task-sha> [--reason "..."] [--cancelled-by ID] [--cascade]`

- Reads the latest TaskStatus for `<task-sha>`. Refuses with exit code 6
  if the task is already `done` or `rejected` (terminal-absorbing).
- Appends a synthetic TaskReport containing the cancel reason.
- Appends a TaskStatus with:
  - `attributes.status = "rejected"`
  - `attributes.cancelled = true`
  - `attributes.cancelled_by = <id>`
  - `attributes.cancel_reason = <reason>`
  - `links.task = <task-sha>`
  - `links.prevStatus = <latest-status-sha>`
  - `links.proof = <synthetic-report-sha>`
- If `--cascade` is set, recursively cancels unfinished subtasks linked by
  `parentTask`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Task cancelled (and cascade completed if requested) |
| 6 | Task already terminal; cancellation refused |

## Why cancellation writes proof

Cancellation is not a silent state flip. The synthetic TaskReport records
who cancelled the task and why, and the rejected TaskStatus links `proof`
to that report. This preserves the same audit property as ordinary finish:
every terminal status carries evidence.

## Ownership note

Unlike `planning:finished`, cancellation does **not** require the current
worker to be the closer. It is a supervisor/planner override by design.
