---
name: pm-executing
description: >
  Claim a task for execution. Appends a TaskStatus(working) chained to the
  previous status of that task. Refuses if the task is not currently in
  state "new" (prevents two workers claiming the same task). Use when an
  agent picks up a task returned by pm:next.
---

# pm:executing — mark a task as working

## Procedure

`../scripts/pm executing --task <task-sha> [--agent ID]`

- Reads the latest TaskStatus for `<task-sha>`. Refuses with exit code 6
  unless that status is `new` (**pre-claim** check).
- Appends a new TaskStatus with `attributes.status = "working"`,
  `attributes.agent = <agent-id>`, `links.task = <task-sha>`,
  `links.prevStatus = <previous-status-sha>`.
- `prevStatus` is declared `chain_predecessor` in the schema, so
  hashharness compare-and-swaps the TaskStatus head for this task on
  every append: two claimants racing off the same previous tip both
  submit the same `prevStatus`, exactly one append wins, the other is
  rejected with 'head moved' which the script surfaces as exit 8
  (**claim race lost**) — caller MUST drop the task.
- On success (exit 0), prints the created TaskStatus as JSON.

## Sticky-context binding

If the task's `attributes.sticky` is set, the claim is a **binding** event:
the agent's `$PM_CONTEXT_ID` (or `--context-id`) is recorded on the
working TaskStatus and becomes the only context allowed to advance
this task's chain (heartbeat, report, finished) — and the only context
allowed to claim any sticky descendant via `parentTask` or `dependsOn`.

The check runs in `store.check_sticky_eligibility` at every chain-
advancing call site (`pm executing` / `pm heartbeat` / `pm report` /
`pm finished`). A mismatch — wrong context, missing context when sticky
is required, or two distinct contexts already bound across the sticky
chain — refuses with **exit 10** ("sticky-context refusal").

A sticky binding is established only at claim time and cleared only by
reclaim. After reclaim the task is `new` with no `context_id`, so any
agent context can rebind it (subject to the chain coherence rule).
**Done sticky ancestors continue to require their original context** —
the binding persists on the terminal TaskStatus for audit, and
`task_context_id()` reads the latest status.

See `system-models/planning.als` (`StickyChainCoherence`,
`StickyBindingOnlyAtClaim`) and `system-models/planning_sticky_rebinding.als`
(SR1–SR5).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Claim won; this agent owns the task |
| 6 | Pre-claim refusal: task was not in state `new` |
| 8 | Claim race lost: another agent claimed off the same prev-tip first |
| 10 | Sticky-context refusal: bound context mismatch or chain conflict |

## Concurrency note

The TaskStatus type declares `prevStatus` as a `chain_predecessor` link
in the planning schema. On every `create_item`, hashharness atomically
checks that `prevStatus` equals the current head record_sha256 for this
task's TaskStatus chain, advances the head if so, and rejects with
'head moved' otherwise. Two parallel claimants observing the same tip
therefore both submit the same `prevStatus`; exactly one append wins.

This race-safety is **structurally enforced** by the store plus script
(exit 8). It matches the formal model's `commitClaim` / `abortClaim`
split — see `system-models/planning.als` and the `NoDoubleCommit`
assertion.
