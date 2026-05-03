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

`../scripts/pm replan --task <sha> [--text "..."] [--verifier "..."] [--no-cascade] [--cascade-down] [--note "..."]`

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

Three modes — pick by failure shape, not by habit:

- **cascade-up** (default): walk `links.dependsOn` upstream from the
  target and reset every ancestor currently in `done` or `rejected`
  back to `new`. Use when you suspect *upstream output is wrong* — bad
  ideas → bad cross-refs, stale model output → broken downstream
  parsing. The whole upstream chain is re-derived before the target
  runs again. Ancestors already in `new` / `working` / `superseded`
  are left alone.

- **no-cascade** (`--no-cascade`): just reset the target. Use when the
  failure was *transient or environmental* — sandbox died, network
  blip, OOM, agent crashed mid-step. The target's inputs were fine; it
  just didn't get to do its work. **This is the right pick for "I need
  to re-run this one task" — most replans.** The flag's old name
  `--no-cascade-up` still works as a back-compat alias.

- **cascade-down** (`--cascade-down`): also reset every task that
  transitively lists the target in its `dependsOn` (the consumers of
  the target's output). Use when the target's *output is now invalid*
  and downstream artifacts built on it are stale — classic case: you
  fixed a bug in step 3, so steps 4/5/6 (which consumed step 3's
  output) need to redo. Combine with `cascade-up` if both upstream
  re-derivation and downstream invalidation apply.

  Note: cascade-down walks `dependsOn` only, NOT `parentTask`. A
  parent task that rolls up its children (the `--depth ≥1` expansion
  pattern) is NOT auto-invalidated when a child is replanned —
  `system-models/planning_replan_with_parent_gate.als` has a
  `StaleRollupWitness` scenario showing this. Use
  `--cascade-down-parents` (below) to also invalidate the rollup.

- **cascade-down-parents** (`--cascade-down-parents`, implies
  `--cascade-down`): in addition to depends_on consumers, also reset
  every `parentTask` ancestor of the target and of each reset
  descendant. Use when the target lives in a `--depth ≥1` skill
  expansion and the parent's report is a rollup of children's
  outcomes — without this flag, a Done parent stays Done while
  children get redone, leaving the rollup stale. Verified by
  `planning_replan_with_parent_gate.als#P6` (closes the hazard) and
  `#P7` (only affects rollup ancestors + deps descendants, nothing
  else).

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
