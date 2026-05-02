---
name: pm-replan
description: >
  Replan a task — restart it (and by default all its dependency-chain
  ancestors) so a fresh worker can pick the chain up from the start.
  Use when a task was interrupted, rejected, or completed-but-wrong,
  especially when the user says "replan", "restart from", "redo this",
  "start over", or "the chain was broken — try again". Supports
  body/verifier adjustments to the target via --text / --verifier.
---

# pm:replan — restart a task and its upstream chain

## Procedure

`../scripts/pm replan --task <sha> [--text "..."] [--verifier "..."] [--no-cascade-up] [--note "..."]`

## Two modes

- **In-place reset** (no `--text`/`--verifier`): appends
  `TaskStatus(new, replanned=true)` to the target. The Task record
  itself is immutable; only the status chain advances. A worker that
  pulls the queue will see the task as `new` and pick it up again.
- **Body adjustment** (`--text` and/or `--verifier` given): creates a
  NEW Task in the same queue with the adjusted body. The new task gets
  slug `<original-slug>-r<N>` (next free) and inherits the original's
  parent / spawnedAt / dependsOn / sticky / workdir. The audit link
  back to the original is on the new task's **genesis TaskStatus**
  (not on the Task itself): `genesis_status.attributes.replan_of =
  <original-sha>`. The original task is appended
  `TaskStatus(superseded, superseded_by=<new-sha>)` — an absorbing
  status that excludes it from `pm next` forever and is also refused
  by `pm cancel` (see `pm-cancel/SKILL.md`).

## Cascade behavior

By default `replan` walks `links.dependsOn` upstream from the target
and resets every ancestor that is currently `done` or `rejected` back
to `new` (in-place — ancestor body adjustments are not supported via
this command; replan each ancestor separately if you need to edit
them). Ancestors already in `new` or `working` are left alone — they're
already pending or in flight.

`--no-cascade-up` limits the operation to the target only.

## Why no dependency rewiring

Tasks are immutable, so `dependsOn` is fixed at plan time. In-place
reset doesn't change a task's sha; the new target task (when adjusted)
keeps the same `dependsOn` list as the original. Since the ancestors'
shas are unchanged (just their statuses go back to `new`), the new
target will become runnable once they're done again. No graph surgery
required.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Replan succeeded (target reset or cloned, ancestors processed) |
| 6 | Target task not found, or already in `superseded` status |

## Notes

- `superseded` is a new terminal status (alongside `done` / `rejected`).
  `pm next` skips superseded tasks via the standard "latest status must
  be `new`" check.
- The original Task body is preserved on the chain — replan never
  rewrites or deletes records, it only appends.
- For wholesale "kill this subtree" semantics (cancel + cascade DOWN
  via `parentTask`), use `pm cancel --cascade`. Replan walks UP via
  `dependsOn`; cancel walks DOWN via `parentTask`. Different graphs,
  different operations.
