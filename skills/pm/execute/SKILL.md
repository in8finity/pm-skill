---
name: pm-execute
description: >
  Run N worker agents in parallel that drain the planning queue. Each worker
  loops: pull next task -> claim it (executing) -> do the work -> submit a
  report -> finish the task. Use when the user says "execute the plan",
  "run the queue", "spawn workers", or asks for parallel execution with a
  specific agent count.
---

# pm:execute — run N agents against the queue

## Inputs

- `--agents N` — number of parallel workers (required)
- `--queue Q` — board to drain (default `default`)
- `--running` — stay alive across rate-limit windows: keep topping the
  queue up to `N` workers, sleep through hourly caps via `ScheduleWakeup`,
  and stop cleanly when the weekly reserve is breached. Without this
  flag, the orchestrator spawns one batch of `N` workers, waits, and
  exits — the legacy single-shot behavior.
- `--reserve-weekly PCT` — used with `--running` only. Leave at least
  this percent of the seven-day token budget untouched. Default `20`
  (i.e. stop spawning once weekly usage crosses 80%). Set `0` to drain
  the bucket.
- `--max-five-hour PCT` — used with `--running` only. Treat the hourly
  bucket as full at this percent and wait until reset. Default `95`.

## Procedure (driven by the host agent, not a single script)

This skill orchestrates concurrency by spawning `N` worker agents **in a
single parallel batch** so they run concurrently. Each worker is given
the loop below as its prompt; they share no state except the
hashharness board.

### Worker prompt (template for each spawned worker)

```
You are a planning worker. Repeat until the queue is empty:

1. Atomically pull-and-claim the next runnable task. Capture both
   stdout (for `eval`) and exit code (to distinguish empty-queue from
   race-exhaustion):

       out=$(skills/pm/scripts/pm pull --queue <Q>); rc=$?
       eval "$out"

   This sets shell vars TASK=<sha>, IDEA_PATH=<...>, SLUG=<...>, and
   on race-exhaustion also RETRIES_LOST=1.

   Exit codes:
     0  + non-empty $TASK              — claimed, proceed to step 2.
     0  + empty $TASK                  — queue genuinely empty: stop
                                          and report "queue empty".
     8  + RETRIES_LOST=1               — every retry inside this pull
                                          call lost its CAS race on a
                                          contended task; the queue
                                          isn't empty, you just kept
                                          picking targets that another
                                          worker beat you to. Back off
                                          briefly and loop:
                                            sleep $((RANDOM % 3 + 1))
                                            continue

   Treating exit 8 as "queue empty" is wrong — sibling tasks may still
   be runnable. The legacy single-line form `eval "$(pm pull)"` (no
   $? capture) is still safe: empty $TASK on retries-lost means the
   worker stops, same as before; only adopt the rc=$? pattern if you
   want the worker to push through transient contention.

   `pm pull` is preferred over the split `pm next + pm executing`
   form below because it eliminates two failure modes structurally:
     - SHA hallucination: the worker never types or rebuilds the sha.
     - Race window: claim is atomic on the chain (chain_predecessor
       on prevStatus, with the verified prev_status_sha threaded all
       the way to create_item — see store.append_status's
       `expected_prev_status_sha` kwarg); the loser is retried
       internally with a per-invocation skip-set so retries advance
       to the next candidate instead of re-attempting the same one.
     - Sticky-context bypass: pull enforces `check_sticky_eligibility`
       and writes `context_id` onto the working TaskStatus. Tasks the
       worker isn't sticky-eligible for are skipped (don't burn a
       retry); the binding is recorded on the chain for sweep/reclaim.

   Use the split form (next then executing) ONLY for diagnostic flows
   where you want to inspect the candidate before claiming, or run
   custom logic between selection and claim. See "Diagnostic split
   form" below.

2. Read `task.attributes.body` (the description) and execute the work.
   Inspect via `skills/pm/scripts/pm show --task "$TASK"`.
   - If you decide to spawn a subtask, run `pm plan` with --parent TASK so
     the new task is chained to your current TaskStatus.
   - **Heartbeat between chunks**: at every natural checkpoint (between
     file edits, before long tool calls, after a sub-step finishes), run
     `skills/pm/scripts/pm heartbeat --task TASK`.
     If you go silent on the chain for longer than the queue TTL
     (default 300s), `pm sweep` will treat your claim as a zombie and
     reclaim the task — your work would be lost. Cheap to tick; do it
     liberally.
3. **If `task.attributes.verifier` starts with `skill:` or `prompt:`**,
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
4. Submit proof: skills/pm/scripts/pm report --task "$TASK" \
        --title "<short>" --text-file <path-to-output>
5. Close: skills/pm/scripts/pm finished --task "$TASK"
   (use --rejected if the work cannot be completed)
   - If exit code 9, the verifier for this task failed. Do not mark the
     task done; update the work, submit a fresh report (with a fresh
     attestation block, if applicable), and retry or reject it
     explicitly.
6. Loop.
```

### Diagnostic split form

When you need to inspect what would be claimed without claiming, or
run custom logic between selection and claim, use the split form:

```
1. JSON=$(skills/pm/scripts/pm next --queue <Q>)
   - If "$JSON" == "null", queue is empty.
   - Otherwise TASK=$(echo "$JSON" | jq -r .text_sha256).
2. skills/pm/scripts/pm executing --task "$TASK"
   - Exit codes (since v0.6.2):
       6  task not in `new` (somebody beat you to claim) — re-loop.
       8  claim race lost between your read and the chain CAS — re-loop.
       10 sticky-context refusal (your context can't claim this task).
       15 parent-claim gate (parent task is still `new` — claim it first).
       16 task exists but no TaskStatus on the chain (transient genesis-
          read race) — retry once.
       17 task sha doesn't exist (typo / hallucinated) — give up.
   - Only proceed on exit 0.
3. Continue at step 2 of the canonical worker loop above (verifier,
   heartbeat, report, finished).
```

The atomic `pm pull` form folds steps 1+2 into one call and retries
race-loss internally — prefer it for production worker loops.

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

Use the platform's agent/delegation primitive to launch `N` workers in
parallel; do **not** spawn them sequentially.

- **Claude Code:** send one assistant message containing `N` `Agent`
  tool calls.
- **Codex:** issue `N` parallel `spawn_agent` calls (or the equivalent
  worker delegation primitive available in that environment), then wait
  on them after the batch is launched.

### When to stop

Workers stop when `pm next` returns `null`. The orchestrator (you) waits
for all `N` agents to complete and then summarizes: tasks done, tasks
rejected, tasks left (should be zero or blocked-on-deps).

### Sequential fallback (sub-agent context, no fan-out primitive)

The "parent fans out via `pm-execute`" pattern only works when the
caller has access to the platform's worker-spawning primitive. In
**Claude Code**, the `Agent` tool is **orchestrator-only**: sub-agents
spawned via `Agent` inherit a tools subset that excludes `Agent`
itself. A sub-agent that claims a parent task and then tries to drive
`pm-execute --agents N` to drain a subqueue cannot spawn the workers.
The same constraint shows up in any environment whose worker-delegation
primitive is gated to top-level sessions.

When the caller lacks the fan-out primitive, fall back to **the worker
becoming its own (sequential) executor** — single-threaded but
correct:

```
while true; do
  out=$(skills/pm/scripts/pm pull --queue <SUBQUEUE> \
        --context-id $PM_CONTEXT_ID --json)
  [[ "$out" == "null" || -z "$out" ]] && break
  TASK=$(printf '%s' "$out" | jq -r .task)
  # ...do the work for $TASK, then:
  skills/pm/scripts/pm report   --task "$TASK" --title "..." --text -
  skills/pm/scripts/pm finished --task "$TASK"
done
```

Detection rule: if the parent task's body says "fan out to subqueue Q
via pm-execute" but the current session is a sub-agent with no `Agent`
tool (or an equivalent constraint elsewhere), switch to the loop above
*without* trying to spawn — the spawn call will silently no-op or
error in a way the orchestrator can't easily catch.

Implications worth flagging in the parent task's body or report:

- Sequential drain is slower (no parallelism inside the subqueue). For
  small subqueues (≤ ~5 children) this is fine; for large ones,
  consider planning the parent so its claim happens at the
  orchestrator level instead, e.g. by leaving the parent unclaimed and
  letting the top-level `pm-execute` pick it up.
- A long sequential drain can hit rate-limit windows mid-loop. Use
  `pm limits --json` checks between iterations the same way
  `--running` mode does at the orchestrator level.
- Heartbeating remains correct (the worker keeps holding the parent's
  claim while iterating children), but the parent's `working` status
  stays open for the entire subqueue drain — surface progress via
  intermediate `pm report` calls so a supervisor can audit without
  reading every child task.

### Claude permission allowlist gotchas (sub-agent invocation)

This section is Claude-specific. Codex workers do not use Claude's
literal `Bash(...)` allowlist matcher, so the command-shape caveats
below do not apply there.

Sub-agents typically run under a permission allowlist that constrains
which Bash commands they can execute. The matcher is a **literal-
string prefix check on the command** — there is no PATH resolution,
no executable normalisation, no $env-var expansion. So `Bash(pm next *)`
matches `pm next --queue Q` but NOT `/abs/path/skills/pm/scripts/pm
next --queue Q`. The allowlist must carry one entry per invocation
form a worker might use.

**The orchestrator-side fix**: when constructing worker prompts, always
invoke pm via the **project-relative path** `skills/pm/scripts/pm
<verb>`, never an absolute path. Workers' cwd defaults to the project
root, so the relative form resolves correctly, and a single allowlist
entry `Bash(skills/pm/scripts/pm *)` covers every verb. Bare `pm
<verb>` works in the orchestrator session (the SessionStart hook
prepends the script dir to PATH) but **does not** work in sub-agents
because they don't inherit `$CLAUDE_ENV_FILE` PATH munging — confirmed
empirically with a probe.

Three patterns reliably break a worker the FIRST time it runs `pm
next`; all are subtle and hard to diagnose from the worker's side
("Permission denied" looks identical to "your environment is broken"):

0. **Absolute path doesn't match bare-name allowlist.** `Bash(pm
   next *)` does NOT match `/Users/.../skills/pm/scripts/pm next ...`
   in a sub-agent. Use the relative form in worker prompts AND
   allowlist `Bash(skills/pm/scripts/pm *)` (already in this project's
   `.claude/settings.json`).

1. **Trailing space-star requires args.** `Bash(pm next *)` matches
   `pm next --queue Q` but NOT bare `pm next` (no args). If your
   workers might call `pm next` with no flags, allowlist BOTH:

       Bash(pm next)
       Bash(pm next *)

   The same holds for any `pm` verb the worker calls without args.

2. **`export X=Y; pm ...` chains don't match `Bash(pm ...)`.** The
   allowlist matches the *literal command shape*. A multi-statement
   chain is a different shape. Two patterns that DO match:

       pm executing --task TASK --context-id "$CTX"           # ← inline flag
       PM_CONTEXT_ID="$CTX" pm executing --task TASK           # ← inline env

   And one that does NOT:

       export PM_CONTEXT_ID="$CTX"; pm executing --task TASK   # ← chain

   For sticky-context work, mint the context once at the orchestrator
   and pass it down as an explicit `--context-id` flag in the worker
   prompt template — on every verb that writes to the chain
   (`executing`, `pull`, `report`, `finished`, `heartbeat`) or
   filters by sticky binding (`next`). Read-only verbs (`show`,
   `list`, `tree`) don't accept it. Never tell the worker to
   `export` it themselves.

A "dry-run permission check" helper would catch this in 1 second; in
the meantime, when a worker dies on the first `pm next` and the error
mentions "Permission to use Bash has been denied", check the allowlist
shape against the actual command form before assuming an env bug.

## Running mode (`--running`)

Single-shot mode spawns one batch of `N` workers, waits, and returns.
That's fine for short queues, but for long runs it hits two problems:

1. **Hourly cap.** When Claude Code's five-hour bucket fills,
   workers start failing mid-task with rate-limit errors. The orchestrator
   reports "done", the queue is half-drained, and the user has to come back
   later to resume manually.
2. **Weekly burn.** With no awareness of the seven-day bucket, an
   ambitious queue can consume the user's entire weekly allotment, leaving
   nothing for unrelated work.

`--running` solves both by turning the orchestrator into a long-lived
controller that sleeps through hourly caps and exits cleanly when a
caller-specified weekly reserve would be breached.

### Rate-limit source

Under Claude, `pm limits` reads the same `rate_limits.five_hour` /
`seven_day` numbers the user sees on their status bar. They're produced
by the Claude Code harness and **only** piped into the statusline hook's
stdin (no other local file or env var exposes them). To make those
numbers readable out-of-band, the skill ships its own capture hook:

    skills/pm/hooks/statusline_capture.sh

It snapshots the harness JSON to `$CLAUDE_CONFIG_DIR/pm-rate-limits.json`
(default `~/.claude/pm-rate-limits.json`) on every render, then either
delegates rendering to the user's prior statusline (set via
`PM_UPSTREAM_STATUSLINE=/path/to/your/statusline.sh`) or emits a
minimal one-line bar of its own. The skill never edits the user's
existing statusline file — installation is a one-line change to
`.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "$CLAUDE_PROJECT_DIR/skills/pm/hooks/statusline_capture.sh"
  }
}
```

Chained form (keep your existing render, just add capture):

```json
{
  "statusLine": {
    "type": "command",
    "command": "PM_UPSTREAM_STATUSLINE=/Users/me/.claude/statusline-command.sh \\
                $CLAUDE_PROJECT_DIR/skills/pm/hooks/statusline_capture.sh"
  }
}
```

Under Codex, no hook is needed: `pm limits` reads the freshest
`token_count` event from the current `~/.codex/sessions/*.jsonl` file
(preferring the session named by `CODEX_THREAD_ID` when present).

If the user opts out of the Claude hook, `pm limits` returns exit 1
(`unknown`) there and the controller falls back to single-shot
behavior — i.e. `--running` degrades gracefully rather than erroring.

`pm limits` parses that cache and emits a decision:

```
$ pm limits --json --reserve-weekly 20 --max-five-hour 95
```

Exit codes (so the orchestrator can branch without parsing JSON):

| code | status    | what it means                                       |
|------|-----------|-----------------------------------------------------|
| 0    | `ok`      | under all caps — safe to spawn                      |
| 1    | `unknown` | cache missing / stale / malformed — caller decides  |
| 2    | `stop`    | seven-day reserve breached — stop the run           |
| 3    | `wait`    | five-hour cap hit — sleep `wait_seconds`            |

If the orchestrator sees `unknown`, the safest call is to proceed with
one batch and re-check after; the cache becomes fresh again on the very
next statusline render (so within seconds of the next tool call).

### Controller loop (orchestrator behavior)

This is the `--running` analog of the one-shot procedure above. It is
intended to be driven by the host agent (you) across multiple turns,
with `ScheduleWakeup` carrying the loop across hourly waits without
burning context on a `sleep`. Use the `loop` skill's dynamic mode
(`<<autonomous-loop-dynamic>>`) or invoke `pm:execute --running` under
`/loop pm:execute --running --agents N ...` so the wake-ups are
re-entries into this same procedure.

Each iteration:

1. **Check limits.**

       OUT=$(skills/pm/scripts/pm limits --json \
             --reserve-weekly <PCT> --max-five-hour <PCT>); rc=$?

   Branch on `rc`:
     - `2` (stop): the weekly reserve is breached. Reclaim any tasks the
       previous batch is still holding (so the next runner — possibly
       tomorrow — picks them up cleanly), summarize what was completed,
       and exit the loop. Do **not** schedule another wake-up.
     - `3` (wait): the hourly bucket is full. Reclaim in-flight tasks
       (the per-task answer to "what should happen to running work" —
       see step 3 below), then `ScheduleWakeup` for `wait_seconds`
       (clamped to `[60, 3600]` by the runtime — for waits longer than
       an hour, the controller will simply re-check, see `wait` again,
       and reschedule). Use the same `--running` prompt verbatim so
       the next firing re-enters this procedure.
     - `1` (unknown): proceed but treat with caution. If two consecutive
       iterations report `unknown`, surface to the user — the statusline
       hook may not be installed.
     - `0` (ok): continue to step 2.

2. **Census in-flight workers.** Count tasks the previous batch is
   still working on, so this iteration only tops up the deficit
   instead of always launching `N`:

       ALIVE=$(skills/pm/scripts/pm status --queue <Q> --json \
               | jq '[.tasks[] | select(.current_status=="working")] | length')
       NEED=$(( N - ALIVE ))

   If `NEED <= 0`, every slot is full. `ScheduleWakeup` for ~5 minutes
   (270s — stays inside the prompt-cache TTL) and re-check.

3. **Spawn `NEED` workers** with the canonical worker prompt above.
   Single message, parallel `Agent` calls.

4. **Reclaim policy on limit-hit (step 1 wait/stop branches).** When
   the controller is about to sleep through a five-hour reset, its
   already-running workers will almost certainly fail mid-task. Don't
   leave their claims dangling for `pm sweep` to find later — actively
   reclaim them:

       skills/pm/scripts/pm owned --queue <Q> --json \
         | jq -r '.[].text_sha256' \
         | while read sha; do
             skills/pm/scripts/pm reclaim --task "$sha" \
               --reason "five-hour cap hit; resuming after $(date -r $RESET)"
           done

   This re-queues them as `new` so a fresh worker on the next iteration
   picks them up from the top. The original worker, if still alive,
   will lose its CAS on the next chain write and exit cleanly.

5. **Wait for this batch's workers to finish, then loop.** When you
   exit the loop body, decide between `ScheduleWakeup(short)` (queue
   probably has more work, just want to give workers time to make
   progress) and `ScheduleWakeup(long)` (waiting on a five-hour
   reset). The `wait_seconds` from `pm limits` is the right value for
   the long case; for the short case, prefer 270s to stay in the
   prompt cache.

### Stop conditions

The controller exits the loop (no further wake-ups) when ANY of:

- `pm limits` returns exit 2 (weekly reserve breached).
- `pm next --queue <Q>` returns `null` AND no working tasks remain in
  the census — the queue is genuinely drained.
- The user interrupts.

On stop, summarize: tasks done this run, tasks rejected, tasks left
(blocked-on-deps or zero), how much of the weekly budget was consumed,
and whether the stop was budget-driven or queue-empty.

### Why ScheduleWakeup, not Bash sleep

Hourly waits can be 30–60 minutes. A `sleep` in Bash keeps the session
pinned and burns the prompt cache on re-entry. `ScheduleWakeup` lets
the conversation go idle; the harness re-enters you when the timer
fires, and the user can interrupt at any point with a normal message.
For waits under ~270s, the prompt cache stays warm; for longer waits,
the one cache miss is amortized against tens of minutes of idle.
