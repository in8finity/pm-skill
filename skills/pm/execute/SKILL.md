---
name: planning-execute
description: >
  Run N worker agents in parallel that drain the planning queue. Each worker
  loops: pull next task -> claim it (executing) -> do the work -> submit a
  report -> finish the task. Use when the user says "execute the plan",
  "run the queue", "spawn workers", or asks for parallel execution with a
  specific agent count.
---

# planning:execute — run N agents against the queue

## Inputs

- `--agents N` — number of parallel workers (required)
- `--queue Q` — board to drain (default `default`)

## Procedure (driven by Claude, not a single script)

This skill orchestrates concurrency by spawning `N` Agent calls **in a
single message** so they run in parallel. Each agent is given the worker
loop below as its prompt; they share no state except the hashharness
board.

### Worker prompt (template for each spawned Agent)

```
You are planning worker. Repeat until the queue is empty:

1. Run: <skills>/pm/scripts/pm next --queue <Q>
   - If output is "null", stop and report "queue empty".
   - Otherwise parse the JSON; record task.text_sha256 as TASK.
2. Claim it: <skills>/pm/scripts/pm executing --task TASK
   - If exit code 6, the task was not `new` — go to step 1.
   - If exit code 8, post-append race lost — go to step 1.
   - Only proceed on exit 0.
3. Read `task.attributes.body` (the description) and execute the work.
   - If you decide to spawn a subtask, run `pm plan` with --parent TASK so
     the new task is chained to your current TaskStatus.
   - **Heartbeat between chunks**: at every natural checkpoint (between
     file edits, before long tool calls, after a sub-step finishes), run
     `<skills>/pm/scripts/pm heartbeat --task TASK`.
     If you go silent on the chain for longer than the queue TTL
     (default 300s), `pm sweep` will treat your claim as a zombie and
     reclaim the task — your work would be lost. Cheap to tick; do it
     liberally.
4. **If `task.attributes.verifier` starts with `skill:` or `prompt:`**,
   you MUST apply that verifier to your own work BEFORE submitting the
   report, and append a `## Verifier Attestation` section to the report
   body in this exact form:

   ```
   ## Verifier Attestation

   verifier: <verbatim copy of task.attributes.verifier>
   verdict: PASS         # or: FAIL: <one short reason>
   evidence:
     <free-form, multi-line OK; describe what you ran and what passed>
   ```

   `pm finished` will reject the close with exit 9 if the block is
   missing, the `verifier:` line doesn't match verbatim, or `verdict:`
   isn't PASS. (`verify-skill:` / `verify-prompt:` and shell-path
   verifiers do NOT require an attestation block — `pm finished` runs
   them itself.)
5. Submit proof: <skills>/pm/scripts/pm report --task TASK \
        --title "<short>" --text-file <path-to-output>
6. Close: <skills>/pm/scripts/pm finished --task TASK
   (use --rejected if the work cannot be completed)
   - If exit code 9, the verifier for this task failed. Do not mark the
     task done; update the work, submit a fresh report (with a fresh
     attestation block, if applicable), and retry or reject it
     explicitly.
7. Loop.
```

### Liveness / zombie recovery

Workers signal liveness via two channels:

- **Implicit**: any chain write (status, report, heartbeat) on the task
  resets its staleness clock.
- **Explicit**: `pm heartbeat --task TASK` writes a `TaskHeartbeat`
  record specifically for ticking the lease.

A supervisor process (cron or on-demand) runs:
```
pm sweep --queue <Q> --ttl 300
```
which finds any task in `working` whose last activity is older than the
TTL and reclaims it (appends `TaskStatus(new, reclaimed=true)`). The
reclaimed task is immediately runnable again via `pm next` for a fresh
worker.

Per-task supervisor override:
```
pm reclaim --task <sha> --reason "stuck — manual reset"
```

This is the runtime version of the formal model in
`system-models/planning_lease.als` (the `Crash` + `Reclaim` transitions).

### Spawning rule

Send one assistant message that contains `N` `Agent` tool calls; do **not**
spawn them sequentially — that defeats the purpose. Use `subagent_type:
"general-purpose"` (or a more specialized agent if the task type calls for
it).

### When to stop

Workers stop when `pm next` returns `null`. The orchestrator (you) waits
for all `N` agents to complete and then summarizes: tasks done, tasks
rejected, tasks left (should be zero or blocked-on-deps).
