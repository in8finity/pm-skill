# hashharness-pm

**A formally-verified planning board for parallel coding agents.** Sixteen Claude Code skills and a `pm` CLI dispatcher coordinate multi-step skill executions over [hashharness](https://github.com/in8finity/hashharness)'s append-only hash-chained storage. Every concurrency guarantee is proved by an Alloy or Dafny model checked into this repo.

Existing agent frameworks give you orchestration *or* durability *or* race safety — `hashharness-pm` is small, storage-first, and gives all three with formal guarantees. Long-running autonomous queues need all three.

## At a glance

```bash
pm plan      --title "Build X" --text "..."        # enqueue
pm next                                             # pull next runnable task
pm executing --task <sha>                           # claim (race-safe via chain_predecessor)
pm report    --task <sha> --title "..." --text-file out.md
pm finished  --task <sha>                           # close (verifier-gated)
```

That five-command worker loop is what every agent runs in parallel. Around it sit supervisor primitives (`pm cancel / replan / sweep / reclaim`), batch primitives (`pm bulk-plan`), and three meta-skills that drive *another* skill end-to-end through this very queue (`pm-{auto,assisted,guided}-skill-execution`). Full bootstrap in [Quick start](#quick-start).

---

## What you actually get

Six capabilities, ordered by what matters first.

### 1. A formally-verified queue protocol

Ten Alloy modules prove **67 invariants** over the protocol; six Dafny ports add **94 unbounded inductive lemmas**; **47 golden integration tests** assert the same properties at runtime. Every property is enforced at four layers — code, skill text, model, test — and the cross-source consistency is tracked in `system-models/reports/planning-{reconciliation,enforcement}.md`.

The properties that protect a parallel-agent queue:

| Property | Where enforced |
|---|---|
| Done/rejected is absorbing — a finished task never transitions out | `finished.py` rejects unless current is `working`/`new`; `cancel.py` exit 6 on terminal |
| A terminal status always has a `proof` link to a TaskReport | `finished.py` refuses without a report → exit 7; `cancel_task` synthesizes proof before the rejected status |
| At most one agent owns the latest TaskStatus of a task | hashharness `chain_predecessor` on `prevStatus` (compare-and-swap on the TaskStatus head) → `HeadMoved` → `ClaimLost` → `executing.py` exit 8 |
| Dependencies are `done` at the moment a task is claimed | `next.py` skips blocked tasks |
| Parent rolls up children — a wrapper task stays unrunnable until every child is in `{done, rejected, superseded}` | `next.py`/`pull.py` `children_settled()` filter |
| Verifier-required tasks cannot reach `done` without a passing verifier | `finished.py` runs the verifier and refuses on non-zero exit → exit 9 |
| Sticky chains stay bound to one agent context | `store.check_sticky_eligibility`; refusal exit 10 across `executing`/`heartbeat`/`report`/`finished` |
| Sticky binding rebinds cleanly after reclaim | `store.reclaim` writes status with no `context_id`; next claim sets a fresh one |
| A live worker is never wrongly reclaimed (heartbeat-vs-reclaim race) | `sweep.py` snapshots the heartbeat tip, then `store.reclaim(preempt_heartbeat=True, …)` writes a preempt heartbeat first; `chain_predecessor` on `prevHeartbeat` rejects if a worker raced → `WorkerStillAlive` → sweep aborts |
| Zombie heartbeats from displaced agents are refused | `heartbeat.py` checks current working status's `agent` matches `--agent` → exit 12 if not |
| A dead worker's task is recoverable | `sweep.py` reclaims tasks past heartbeat TTL; `store.reclaim` appends `new` status with `reclaimed=true` |
| Two parallel `pm plan` calls cannot both create the same slug | `Task.text` is `task:<queue>/<slug>`; hashharness rejects duplicate `text_sha256` → `SlugTaken` → exit 4 |
| `--depends-on` self-loop / non-existent / forever-blocked rejected at plan time; `--parent` self-parent / cycle likewise | `plan.py` and `bulk_plan.py` exit 11 |
| Every claim attempt eventually resolves (commit or abort) | `executing.py` always exits 0/6/8/10 |

Both race conditions are content-addressed gates inside hashharness: slug uniqueness rides the `text_sha256` index, and claim ordering rides the per-`(work_package_id, type)` `chain_predecessor` head pointer. The scripts plumb structural rejections up to operator-visible exit codes.

See [Verifying the models](#verifying-the-models) to re-run everything yourself.

### 2. A worker loop you can give to N agents in parallel

```
1. pm next --queue <Q>             → JSON or "null"
2. pm executing --task TASK        → exit 0 win | 6 pre-claim refusal | 8 race-lost | 10 sticky-context refusal
3. read task.attributes.body, do the work
4. pm report --task TASK --title T --text-file ...
5. pm finished --task TASK         → exit 0 done | 7 missing report | 9 verifier failed | 10 sticky-context refusal
```

`pm plan` itself can also exit 4 (slug already taken). `pm execute` (the `pm-execute` skill) spawns N agents in parallel running this loop. Workers tick a heartbeat (`pm heartbeat`) on a configurable interval so the sweeper can distinguish a long-running task from a dead one.

### 3. Three skills to drive *another* skill end-to-end

Each one wraps the queue to plan one task per SKILL.md step, chained by `dependsOn`, with the audit trail on the status + report chains. Pick by how much agency you want the meta-skill to take.

- **Auto** (`pm-auto-skill-execution`) — hands-off run. Every choice the target skill would normally ask is resolved to its documented default; the choice and reasoning are recorded in the task report. Auto rejects steps whose preconditions can't be satisfied automatically. Best for routine runs, batch processing, well-understood skills.
- **Assisted** (`pm-assisted-skill-execution`) — default-pick at routine gates, pause-and-ask at critical ones. Doesn't auto-reject when a decision is required; escalates to the user instead and resumes once answered. Best for mostly-routine runs that may need 1–3 user inputs (the everyday mode for skills you understand most of, but not all).
- **Guided** (`pm-guided-skill-execution`) — step-by-step with user-in-the-loop gates. Pauses after each step to surface decisions, accept user-supplied subtask requests, and confirm before moving on. Best for novel problems and sign-off gates.

All three accept `--depth N`: at `N=0` each step is one task even if it invokes another skill; at `N≥1` the inner skill's steps become real subtasks under the wrapper, and the parent-rolls-up-children gate keeps the wrapper unrunnable until the expansion completes. `pm bulk-plan --chain-siblings` enqueues the whole tree in one call.

### 4. Sticky-context binding for shared expensive reads

When a queue has items that share expensive reads (chunk files, repo clones, model loads, dataset slices), naive parallelism makes every worker re-fetch; serial wastes wall time. Plan a *cluster parent* with `pm plan --sticky` whose body lists the shared resources, then `pm plan --parent <PARENT>` for each item that needs them — subtasks inherit `sticky` automatically. The first worker to claim the parent (with its own `PM_CONTEXT_ID`) binds the cluster; subsequent claims by other contexts are refused with exit 10, naturally routing other workers to *other* clusters. The bound worker reads the shared resources once into its own context, then claims and drains the subtasks reusing that read.

Singletons stay non-sticky and ride the normal pull path. Sticky binding turns "shared cache" into a first-class queue topology — no external cache, no inter-worker coordination beyond the planning board's own race-safe gates. The `StickyChainCoherence`, `StickyBindingOnlyAtClaim`, and `RebindAfterReclaim` (SR1–SR5) properties are formally verified.

### 5. Workdir-scoped queues for multi-project setups

`pm plan` records `os.path.realpath(cwd)` (or `$PM_WORKDIR`) into `task.attributes.workdir` at plan time, and `pm next` filters out tasks whose workdir doesn't match the caller's. The result: a single hashharness instance can host queues for many independent workspaces without cross-talk — a worker started in `~/projects/A` only ever pulls tasks planned from `~/projects/A`, even if `~/projects/B` is also using the same backend. Subtasks inherit the parent's workdir so a planner in one repo can spawn children that stay scoped there. Useful for developer machines running multiple projects against shared hashharness storage, or for sandboxed worker pools that should only see tasks scoped to their assigned workspace.

### 6. Supervisor primitives for recovery

Long-running queues develop pathologies: a worker dies mid-claim, a task gets stuck because its dependency was resolved wrong, a whole subtree needs to be redone with adjusted parameters, an upstream step has a bug and downstream artifacts are now stale. Four primitives, each with a verified failure-mode contract:

- **`pm cancel`** terminates a task (and optionally cascades to unfinished subtasks) regardless of ownership; synthesizes a `TaskReport` carrying the cancel reason so the closing `rejected` status still satisfies the proof-of-work invariant.
- **`pm replan`** restarts a task with three cascade modes: **`--no-cascade`** (just the target — right for sandbox/transient failures), **default cascade-up** (also reset the target's `dependsOn` ancestors — right when upstream output is suspect), **`--cascade-down`** (also reset every task transitively depending on the target — right when target's output is now stale and downstream artifacts must rebuild), **`--cascade-down-parents`** (additionally reset rollup parents whose children were invalidated — closes the stale-rollup hazard for `--depth ≥1` skill expansions). Supports body / verifier edits via `--text` / `--verifier` (clone-and-supersede mode).
- **`pm sweep` + `store.reclaim`** detect heartbeat-stale claimants (the worker process died holding a `working` status), append a `new` status with `reclaimed=true`, and let the queue route the task to a healthy worker. Race-safe against a still-live worker via a preempt heartbeat (`chain_predecessor` on `prevHeartbeat`).
- **`pm reclaim`** is the manual force-release variant — supervisor / human override that doesn't wait for sweep TTL. With `--cascade` also reclaims every working descendant via `parentTask` reverse-links.

These keep an autonomous queue self-healing without requiring an operator to surgically edit storage.

---

## Quick start

0. **First time only — install hashharness.** The bundled installer creates an isolated venv, generates a launcher, and writes a source-able `env` file:
   ```bash
   skills/pm/scripts/pm install --to-home --yes      # → ~/.hashharness/
   # alternatives: --to-claude (~/.claude/hashharness/), --to-project (./.hashharness/), --where /custom/path
   ```
   Idempotent — re-running on an existing install reports the location and exits 0. `pm install --check` tests without installing.

1. **Start the MCP server.** The installer's launcher already wires the env vars:
   ```bash
   ~/.hashharness/launch.sh &
   source ~/.hashharness/env                 # exports HASHHARNESS_MCP_URL
   ```
   Or run hashharness directly if you installed it some other way:
   ```bash
   HASHHARNESS_MCP_TRANSPORT=http \
   HASHHARNESS_HTTP_PORT=38417 \
   HASHHARNESS_DATA_DIR=$HOME/.hashharness/data \
   python -m hashharness.mcp_server
   ```

2. **Register the planning schema** (once per data dir):
   ```bash
   skills/pm/scripts/pm setup
   ```

3. **Drive the worker loop** — through Claude Code via the `pm-*` skills, or directly:
   ```bash
   pm plan     --title "Build X" --text "Detailed description..."
   pm next                          # pull next runnable
   pm executing --task <sha>        # claim
   pm report   --task <sha> --title "done" --text-file out.md
   pm finished --task <sha>         # close (requires a report)
   ```
   Skills read `HASHHARNESS_MCP_URL` (default `http://127.0.0.1:38417/mcp`).

---

## Task verifiers (post-execution gates)

A Task can declare a `--verifier` at plan time. When a worker calls `pm finished`, the verifier runs against the latest TaskReport before the `done` transition is allowed. Non-zero verifier exit blocks the close and leaves the task in `working` (`pm finished` exits 9). `--rejected` bypasses the verifier — rejecting work doesn't claim success, so the gate isn't load-bearing on that path. `--skip-verifier` is the documented escape hatch (records `verifier_exit = -1` on the closing status, so the bypass is auditable).

Four forms of `--verifier <spec>`:

| Form | Who applies the criterion | When `pm finished` runs |
|---|---|---|
| **`skill:NAME`** *(self-attestation, default for skill-based checks)* | the worker | parses a `## Verifier Attestation` block embedded in the TaskReport (fields: `verifier:` matching the spec verbatim, `verdict: PASS\|FAIL[: reason]`, `evidence:`) and gates on the verdict. **No subprocess spawn** — the worker is contractually responsible for actually running the skill. |
| **`prompt:CRITERION`** *(self-attestation with free-form criterion)* | the worker | same attestation contract as `skill:`, just with arbitrary criterion text. |
| **`verify-skill:NAME`** / **`verify-prompt:CRITERION`** *(opt-in, independent re-judgment)* | a fresh `claude -p` subprocess that `pm finished` spawns | independently re-checks the task body + report against the skill / criterion. Higher cost; useful when self-attestation isn't trusted enough. The LLM must terminate output with `VERDICT: PASS` or `VERDICT: FAIL: <reason>`. Requires the `claude` CLI on PATH (else exit 127). |
| **`<absolute path>`** (or shell-prefixed: `env FOO=bar /path/to/check.sh`) | a subprocess spawned by `pm finished` | receives env `PM_TASK`, `PM_REPORT_SHA`, `PM_QUEUE`, `PM_SLUG`, `PM_VERIFIER` plus positional `<task-sha> <report-sha>`. Exit 0 = pass; non-zero = fail; verifier_summary captures stdout + stderr (truncated). |

The verifier outcome (command, exit code, summary, timeout flag) is recorded as attributes on the closing `TaskStatus(done)` — the audit chain documents who checked the work and what they observed. The same attributes ride along on `--skip-verifier` close-outs (with `verifier_exit = -1`) so a downstream auditor can spot bypasses. `VerifierGateOnDone` and `VerifyRequiresWorkingReport` are formally verified.

---

## Verifying the models

The repo carries **ten Alloy modules** and **six Dafny ports** of the planning protocol, verified with the [`formal-modeling`](https://github.com/in8finity/claude-plugin) skill's bundled `verify.sh` runner — a single dispatcher that routes `.als` files through Alloy 6 and `.dfy` files through Dafny + Z3.

### What you need

- **`.als` files** — Java 17+ JDK (or falls back to Docker `eclipse-temurin:17-jdk` if no local Java). First run downloads Alloy 6 (~20 MB) and caches under `.alloy/` next to the script.
- **`.dfy` files** — `dafny` on PATH (`brew install dafny` on macOS — bundles Z3).
- **`python3`** — for output formatters.

Install the formal-methods plugin via Claude Code:

```bash
claude plugin install morozov-claude-plugin
# verify.sh ends up at:
#   ~/.claude/plugins/cache/morozov-claude-plugin/formal-methods/<version>/skills/formal-modeling/scripts/verify.sh
```

Or run Alloy/Dafny directly if you'd rather not depend on the plugin (`alloy` JAR + `dafny verify`). The repo's models are vanilla Alloy 6 / Dafny — no Claude-specific bindings.

### Re-running everything

```bash
verify=~/.claude/plugins/cache/morozov-claude-plugin/formal-methods/1.3.0/skills/formal-modeling/scripts/verify.sh

# Alloy (bounded counterexamples + scenarios)
bash $verify system-models/planning.als                       # 13 checks
bash $verify system-models/planning_lease.als                 # 6 checks
bash $verify system-models/planning_plan_race.als             # 1 check
bash $verify system-models/planning_replan.als                # 11 checks (R1–R11 incl. cascade-down + duality)
bash $verify system-models/planning_cancel_cascade.als        # 6 checks
bash $verify system-models/planning_reclaim_cascade.als       # 6 checks
bash $verify system-models/planning_isolation.als             # 7 checks (static)
bash $verify system-models/planning_parent_gate.als           # 6 checks (parent-rolls-up-children)
bash $verify system-models/planning_replan_with_parent_gate.als  # 6 checks (cross-feature)
bash $verify system-models/planning_sticky_rebinding.als      # 5 checks (sticky context after reclaim)

# Dafny (unbounded inductive proofs over the same protocol)
bash $verify system-models/planning.dfy                       # 19 lemmas + 28 functions
bash $verify system-models/planning_plan_race.dfy             # 5 lemmas
bash $verify system-models/planning_replan.dfy                # 11 lemmas (R1-R8 + Inv preservation)
bash $verify system-models/planning_lease.dfy                 # 10 lemmas
bash $verify system-models/planning_cancel_cascade.dfy        # CC1-CC6 (24 verified)
bash $verify system-models/planning_reclaim_cascade.dfy       # RC1-RC6 (25 verified)

# Integration suite (live MCP server required)
python3 tests/integration/test_golden.py                      # 47 golden flows
```

**Totals**: Alloy **67/67 checks**; Dafny **94 verified** across 6 files; integration **47/47 flows**. The Alloy↔Dafny coverage diff is in `system-models/reports/alloy-dafny-reconciliation.md`.

To reproduce the historical slug-race counterexample, swap `commitPlan[p]` for `commitPlanBuggy[p]` in `planning_plan_race.als`'s `Transitions` fact and re-run; the counterexample re-appears in 4 steps.

### Why two formalisms

- **Alloy** — bounded model checker. Generates concrete counterexamples within a scope (`for 4 but 8 steps`), excellent for design exploration and showing stakeholders "here's the trace where the system breaks." Fast iteration, visual.
- **Dafny** — inductive theorem prover over Z3. Proves properties for traces of *any length*. Slower to write but stronger guarantee — once a Dafny lemma passes, no scope-exhaustion concern. Doesn't generate counterexamples; you have to know the property you want.

The convention in this repo: **Alloy first** (find the right property, see counterexamples, validate scope), then **port to Dafny** for unbounded confidence. The ten Alloy modules and six Dafny ports both verify the same surface; the cross-formalism table tracks which lives where.

---

## How it compares

The closest direct comparators are agent orchestration frameworks. The workflow tools below are included separately because they are not agent-first products, even though they are now used to run agent workloads.

### Direct agent orchestration frameworks

| System | Primary abstraction | Persistence / state | Multi-agent patterns | Worker claiming | Proof / report chain | Formal protocol model |
|---|---|---|---|---|---|---|
| **hashharness-pm** | Task + TaskStatus + TaskReport | Append-only hash-chained items in hashharness | Parallel worker queue with dependencies | Explicit `next` / `executing` claim protocol | Yes, first-class | Yes, Alloy/Dafny in repo |
| [OpenAI Agents SDK](https://platform.openai.com/docs/guides/agents-sdk/) | Agents, tools, handoffs | Run state and traces in SDK/runtime | Managers, handoffs, agents-as-tools | No queue claim primitive | No | No |
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) | Stateful agent graph | Checkpointed graph state | Single, multi-agent, hierarchical graphs | No queue claim primitive¹ | No | No |
| [CrewAI](https://docs.crewai.com/en/introduction) | Crews, flows, tasks | Framework-managed run state | Sequential, hierarchical, hybrid crews | No queue claim primitive | No | No |
| [AutoGen](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/core-concepts/agent-and-multi-agent-application.html) | Message-passing agents | Agent-local state + runtime messaging | Multi-agent conversations and patterns | No queue claim primitive | No | No |
| [Semantic Kernel Agent Orchestration](https://learn.microsoft.com/en-us/semantic-kernel/frameworks/agent/agent-orchestration/) | Agents + orchestration runtime | Runtime-managed orchestration state | Concurrent, sequential, handoff, group chat | No queue claim primitive | No | No |
| [Mastra](https://mastra.ai/agents) | Agents, workflows, agent networks | Stateful agent runtime | Workflows and agent networks | No queue claim primitive | No | No |

¹ LangGraph the library has no task queue; durable queueing was deliberately moved to the separate hosted [LangGraph Platform](https://blog.langchain.com/building-langgraph/), which manages execution internally rather than exposing a worker pull-and-claim API.

### Adjacent workflow/orchestration systems used for agents

These are credible comparisons on durability, retries, and state management, but they are broader workflow products rather than small agent coordination layers:

| System | Core fit | Why it is still relevant here | Evidence of agent usage |
|---|---|---|---|
| [Temporal](https://temporal.io/) | Durable workflow engine | Strong match on long-running state, retries, task queues, and failure recovery | [AI agents overview](https://ai.temporal.io/), [AI/agent workflow articles](https://temporal.io/blog/categories/Using%20Temporal) |
| [Prefect](https://www.prefect.io/docs) | State-oriented workflow orchestration | Strong match on dynamic state transitions and human-in-the-loop workflows | [AI Teams page](https://www.prefect.io/solutions/agents), [Pydantic AI integration article](https://www.prefect.io/blog/prefect-pydantic-integration) |

For storage-model analogues rather than orchestration analogues, [git-bug](https://github.com/git-bug/git-bug) and [Radicle](https://radicle.xyz/) are closer to the immutable collaborative-object side of the design than to the agent-coordination side.

In short: `hashharness-pm` is a small, storage-first coordination layer for parallel coding agents. It overlaps with agent frameworks on orchestration, and with workflow engines on durability, but is more explicit than either about immutable task records, claim races, and proof-of-work closure.

---

## Threat model

The formal model verifies the protocol assuming every state transition goes through `pm`. A client writing directly to hashharness via MCP can bypass most assertions (state-machine ordering, proof-of-work, dep gate, sticky-context check, verifier gate). What survives a bypass is the storage layer: item immutability, schema link types, link-target existence, `text_sha256` uniqueness on the canonical slug key, and `chain_predecessor` head-move enforcement on `prevStatus` / `prevReport` / `prevHeartbeat` (so even a bypass can't double-claim or fork a chain).

For cooperative-agent usage (the actual use case), convention is sufficient. See `system-models/reports/planning-reconciliation.md#threat-model` for hardening options if adversarial bypass becomes a concern.

---

## Reference

### Storage model (hashharness)

Four item types are registered in the planning schema:

| Type | `text` | Key attributes | Links |
|---|---|---|---|
| **Task** | `task:<queue>/<slug>` (canonical key — slug uniqueness is structural) | `slug`, `queue`, `body`, optional `verifier`, `sticky`, `workdir` | `parentTask`, `spawnedAt → TaskStatus`, `dependsOn[]` |
| **TaskStatus** | `<note>\n#nonce:<random>` | `status ∈ {new, working, done, rejected, superseded}`; sticky claims also carry `context_id`; reclaim/cancel close-out statuses carry `reclaimed` / `cancelled` flags | `task`, `prevStatus` (chain_predecessor), `proof → TaskReport` |
| **TaskReport** | the user's report body | (none — body is the proof) | `task`, `prevReport` (chain_predecessor) |
| **TaskHeartbeat** | `hb:<task[:8]>:<agent>\n#nonce:<random>` | `agent` | `task`, `claimStatus → TaskStatus(working)`, `prevHeartbeat` (chain_predecessor) |

Four chains exist per task: status, report, heartbeat, and (for subtasks) `parentTask` plus `spawnedAt` to the parent's TaskStatus current at spawn time. The three `chain_predecessor` links are the load-bearing race-resolution gate — hashharness compare-and-swaps the per-`(work_package_id, type)` head pointer on every append, rejecting stale writes with 'head moved'.

### Layout

```
hashharness-pm/
├── skills/
│   └── pm/                          # Sixteen Claude Code skills + shared scripts
│       ├── plan/SKILL.md                    # pm-plan        — enqueue a task
│       ├── next/SKILL.md                    # pm-next        — pull next runnable task
│       ├── executing/SKILL.md               # pm-executing   — claim a task
│       ├── report/SKILL.md                  # pm-report      — submit proof of work
│       ├── finished/SKILL.md                # pm-finished    — close as done/rejected
│       ├── execute/SKILL.md                 # pm-execute     — spawn N parallel workers
│       ├── cancel/SKILL.md                  # pm-cancel      — supervisor override: terminate + cascade to subtasks
│       ├── replan/SKILL.md                  # pm-replan      — restart a task (3 cascade modes)
│       ├── heartbeat/SKILL.md               # pm-heartbeat   — keep a working claim alive (exit 12 if lease lost)
│       ├── sweep/SKILL.md                   # pm-sweep       — reclaim stale working tasks; race-safe via preempt heartbeat
│       ├── reclaim/SKILL.md                 # pm-reclaim     — manual force-release of a stuck working claim (with --cascade)
│       ├── install/SKILL.md                 # pm-install     — bootstrap the hashharness MCP backend into a venv
│       ├── extract-steps/SKILL.md           # pm-extract-steps — semantic step extraction with bash validation + nested-skill recursion
│       ├── auto-skill-execution/SKILL.md    # pm-auto-skill-execution    — drive another skill end-to-end, no prompts
│       ├── assisted-skill-execution/SKILL.md # pm-assisted-skill-execution — default-pick + escalate at critical gates
│       ├── guided-skill-execution/SKILL.md  # pm-guided-skill-execution  — drive another skill with user-in-the-loop gates
│       ├── skill-shared/extract_steps.py    # SKILL.md step extractor used by auto/guided
│       ├── scripts/
│       │   ├── pm                       # bash dispatcher
│       │   ├── plan.py / next.py / executing.py / report.py / finished.py   # worker-loop primitives
│       │   ├── replan.py / cancel.py / sweep.py / reclaim.py / heartbeat.py # supervisor primitives
│       │   ├── pull.py                  # atomic next + claim with race retry
│       │   ├── store.py                 # hashharness write helpers + HeadMoved/SlugTaken/ClaimLost
│       │   ├── mcp_client.py            # JSON-RPC over HTTP (tool / tool_safe)
│       │   ├── setup_schema.py          # registers Task/TaskStatus/TaskReport/TaskHeartbeat
│       │   ├── schema_fragment.json     # schema with chain_predecessor links
│       │   ├── context_id.py            # PM_CONTEXT_ID generator (sticky-session id)
│       │   ├── bulk_plan.py / heal_orphans.py / queue_status.py             # operator helpers
│       │   ├── stress_claim_race.py     # race smoke-tester
│       │   └── now_iso.py
│       └── README.md
├── system-models/
│   ├── planning.als                              # core protocol model (13 checks)
│   ├── planning_lease.als                        # ownership liveness + heartbeat-vs-reclaim race (6 checks)
│   ├── planning_plan_race.als                    # slug-race verifier (1 check)
│   ├── planning_replan.als                       # replan: 4 modes + cascade-up + cascade-down + duality (11 checks)
│   ├── planning_cancel_cascade.als               # cancel --cascade correctness (6 checks)
│   ├── planning_reclaim_cascade.als              # reclaim --cascade correctness (6 checks)
│   ├── planning_isolation.als                    # cross-queue + workdir isolation (7 checks)
│   ├── planning_parent_gate.als                  # parent-rolls-up-children gate (6 checks)
│   ├── planning_replan_with_parent_gate.als      # cross-feature: replan modes × parent gate (6 checks)
│   ├── planning_sticky_rebinding.als             # sticky context binding after reclaim (5 checks + 3 SAT)
│   ├── planning.dfy                              # Dafny port of planning.als — unbounded proofs
│   ├── planning_plan_race.dfy
│   ├── planning_replan.dfy
│   ├── planning_lease.dfy
│   ├── planning_cancel_cascade.dfy
│   ├── planning_reclaim_cascade.dfy
│   ├── model-isomorphism-check.md   # mapping note for related agent frameworks
│   └── reports/
│       ├── planning-reconciliation.md       # model ↔ code/skills cross-source audit
│       ├── planning-enforcement.md          # gate audit chain across model/code/skills/tests
│       ├── alloy-dafny-reconciliation.md    # Alloy ↔ Dafny coverage diff
│       ├── alloy-cross-model-soundness.md   # pairwise check across Alloy modules
│       ├── planning-blind-spots.md          # known gaps & open questions
│       └── cache-staleness-investigation.md # historical: pre-migration claim-race investigation
└── tests/
    └── integration/test_golden.py    # 47 golden-flow live integration tests
```

### Reports

- `system-models/reports/planning-reconciliation.md` — per-property cross-source consistency table (model ↔ code ↔ skills ↔ schema), threat model, boundary review.
- `system-models/reports/planning-enforcement.md` — for each verified property, the gate audit chain across model / code / skill texts / integration tests, plus the storage-layer gate-artifact check.
- `system-models/reports/alloy-dafny-reconciliation.md` — Alloy ↔ Dafny coverage diff (which properties live in which formalism, and which Alloy-only layers haven't been ported yet).
- `system-models/reports/alloy-cross-model-soundness.md` — pairwise check across the Alloy modules: do they contradict each other where scopes overlap, or sit as consistent specializations? (Spoiler: sound, with one documented scope gap on `TerminalAbsorbing` × `replan_reset`.)
- `system-models/reports/planning-blind-spots.md` — known modeling gaps and open design questions.
- `system-models/reports/cache-staleness-investigation.md` — historical artifact: the pre-migration claim-race investigation that motivated the move to `chain_predecessor`. Kept for context, not a current-state document.

---

## Acknowledgments

- [hashharness](https://github.com/in8finity/hashharness) — append-only text store with MCP server.
- The Alloy 6 models, Dafny ports, and audit reports were produced and verified using the [`formal-modeling`](https://github.com/in8finity/claude-plugin) skill — its bundled `verify.sh` runner handles both `.als` (Alloy 6) and `.dfy` (Dafny + Z3) with auto-setup of the underlying solvers.
