# hashharness-pm

A planning board for parallel agents — ten Claude Code skills + a `pm` CLI dispatcher backed by [hashharness](https://github.com/in8finity/hashharness)'s append-only hash-chained storage.

The system was designed against a formal model. The model is in this repo. Both fixes that landed (claim race-safety, slug uniqueness) were driven by counterexamples the model produced before the code changed.

## Goal

Give LLM agents a durable substrate for controlling the execution of complex skills and multi-step tasks: a shared planning board where one agent can decompose work into dependent tasks, hand them off to parallel workers, supervise progress through immutable status and report chains, and replan or cancel mid-flight — without losing track of who claimed what, what was proven done, and what is still blocked. The append-only storage and explicit claim protocol exist so that an agent driving a long-running skill can reason about the queue's state at any point and recover deterministically across restarts.

## Use cases

### 1. Planning tooling

Treat the queue as a first-class planning surface for an agent (or a human supervising one). `pm-plan` enqueues tasks with body, verifier, and `dependsOn[]` links; `next` returns the next runnable task once its dependency chain is `done`; `pm-replan` restarts a task and its ancestors when the chain breaks; `pm-cancel` terminates a task and cascades to unfinished subtasks. Subtasks link back to the parent's TaskStatus current at spawn time, so the decomposition is reconstructible from storage alone — useful for breaking a large objective into a dependency graph, handing pieces to parallel workers, and supervising progress without external state.

### 2. Executing a skill in a controlled manner

Two skills wrap the queue to drive *another* skill's documented flow as a sequence of tasks (one task per SKILL.md step, chained by `dependsOn`):

- **Auto** (`pm-auto-skill-execution`) — hands-off run. Every choice the target skill would normally ask the user about is resolved to its documented default; the choice and reasoning are recorded in the task report. Best for routine runs, batch processing, and well-understood skills.
- **Guided** (`pm-guided-skill-execution`) — step-by-step with user-in-the-loop gates. Pauses after each step to surface decisions, accept user-supplied subtask requests, and confirm before moving on. Best for novel problems and sign-off gates.

Both modes give the same audit trail — immutable status chain plus proof-of-work reports per step — so a long-running skill execution can be paused, resumed, or replanned mid-flight without losing what was already proven done. `pm-execute` then drains the resulting queue with N parallel workers when steps are independent.

### 3. Sticky sessions — pin a chain to one agent context

A task planned with `--sticky` binds its TaskStatus chain to whichever agent context first claims it (the binding is recorded as `context_id` on the working-status record). Subsequent claims, reports, heartbeats, and finishes against that task — and any sticky descendants in its parent / dependency chain — must come from the same `$PM_CONTEXT_ID`; mismatches refuse with exit 10. Use it when the work needs continuity that survives across calls but mustn't drift across agents: an in-progress refactor with uncommitted edits in a worktree, a debugging session holding open browser/REPL state, a scratchpad an agent has been building up. The `StickyChainCoherence` and `StickyBindingOnlyAtClaim` properties are formally verified in `system-models/planning.als`.

### 4. Workdir-scoped queues — one hashharness backend, many workspaces

`pm plan` records `os.path.realpath(cwd)` (or `$PM_WORKDIR`) into `task.attributes.workdir` at plan time, and `pm next` filters out tasks whose workdir doesn't match the caller's. The result: a single hashharness instance can host queues for many independent workspaces without cross-talk — a worker started in `~/projects/A` only ever pulls tasks planned from `~/projects/A`, even if `~/projects/B` is also using the same backend. Subtasks inherit the parent's workdir so a planner in one repo can spawn children that stay scoped there. Useful for developer machines running multiple projects against shared hashharness storage, or for sandboxed worker pools that should only see tasks scoped to their assigned workspace.

### 5. Supervisor recovery — cancel, replan, reclaim

Long-running queues develop pathologies: a worker dies mid-claim, a task gets stuck because its dependency was resolved wrong, a whole subtree needs to be redone with adjusted parameters. Three supervisor primitives handle each case:

- **`pm-cancel`** terminates a task (and optionally cascades to unfinished subtasks) regardless of ownership; synthesizes a `TaskReport` carrying the cancel reason so the closing `rejected` status still satisfies the proof-of-work invariant.
- **`pm-replan`** restarts a task — and by default its dependency-chain ancestors — by appending a fresh `new` status, so a different worker can pick the chain up from the start. Supports body / verifier edits via `--text` / `--verifier` (clone-and-supersede mode).
- **`pm sweep` + `store.reclaim`** detect heartbeat-stale claimants (the worker process died holding a `working` status), append a `new` status with `reclaimed=true`, and let the queue route the task to a healthy worker. Verified in `system-models/planning_lease.als` against crash interleavings.

These keep an autonomous queue self-healing without requiring an operator to surgically edit storage when something goes wrong.

## Similar systems

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

## Layout

```
hashharness-pm/
├── skills/
│   └── pm/                          # Ten Claude Code skills + shared scripts
│       ├── plan/SKILL.md                    # enqueue a task
│       ├── next/SKILL.md                    # pull next runnable task
│       ├── executing/SKILL.md               # claim a task
│       ├── report/SKILL.md                  # submit proof of work
│       ├── finished/SKILL.md                # close as done/rejected
│       ├── execute/SKILL.md                 # spawn N parallel workers
│       ├── cancel/SKILL.md                  # supervisor override: terminate + cascade to subtasks
│       ├── replan/SKILL.md                  # restart a task (and dep-chain ancestors) from scratch
│       ├── auto-skill-execution/SKILL.md    # drive another skill end-to-end through the queue, no prompts
│       ├── guided-skill-execution/SKILL.md  # drive another skill step-by-step with user-in-the-loop gates
│       ├── scripts/
│       │   ├── pm                   # bash dispatcher
│       │   ├── plan.py / next.py / executing.py / report.py / finished.py
│       │   ├── store.py             # hashharness write helpers + SlugTaken
│       │   ├── mcp_client.py        # JSON-RPC over HTTP (tool / tool_safe)
│       │   ├── pull.py              # atomic next + claim with race retry
│       │   ├── bulk_plan.py / heal_orphans.py / queue_status.py
│       │   ├── now_iso.py
│       │   ├── setup_schema.py      # registers Task/TaskStatus/TaskReport types
│       │   └── schema_fragment.json
│       └── README.md
└── system-models/
    ├── planning.als                 # core protocol model
    ├── planning_plan_race.als       # slug-race verifier
    └── reports/
        ├── planning-reconciliation.md
        └── planning-enforcement.md
```

## Storage model (hashharness)

Three item types are registered in the planning schema:

| Type | `text` | Key attributes | Links |
|---|---|---|---|
| **Task** | `task:<queue>/<slug>` (canonical key — slug uniqueness is structural) | `slug`, `queue`, `body` (the user's prose) | `parentTask`, `spawnedAt → TaskStatus`, `dependsOn[]` |
| **TaskStatus** | `<note>\n#nonce:<random>` | `status ∈ {new, working, done, rejected}` | `task`, `prevStatus`, `proof → TaskReport` |
| **TaskReport** | the user's report body | (none) | `task`, `prevReport` |

Three chains exist per task: status, report, and (for subtasks) `parentTask` plus `spawnedAt` to the parent's TaskStatus current at spawn time.

## Quick start

1. **Run hashharness in HTTP mode** (separate terminal):
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
3. **Use the skills** — invoke through Claude Code via `pm-plan`, `pm-next`, `pm-executing`, `pm-report`, `pm-finished` (or `pm-replan`, `pm-cancel`, `pm-execute`, `pm-auto-skill-execution`, `pm-guided-skill-execution`), or call `pm` directly:
   ```bash
   pm plan --title "Build X" --text "Detailed description..."
   pm next                       # pulls the next runnable task
   pm executing --task <sha>     # claim it
   pm report --task <sha> --title "done" --text-file out.md
   pm finished --task <sha>      # close (requires a report)
   ```

The skills read `HASHHARNESS_MCP_URL` (default `http://127.0.0.1:38417/mcp`).

## Concurrency guarantees (formally verified)

The Alloy model proves these hold under any interleaving of parallel agents using `pm`:

| Property | Where enforced |
|---|---|
| Done/rejected is absorbing — a finished task never transitions out | `finished.py` rejects unless current is `working`/`new` |
| A terminal status always has a `proof` link to a TaskReport | `finished.py` refuses without a report |
| At most one agent owns the latest TaskStatus of a task | hashharness `chain_predecessor` on `prevStatus` (compare-and-swap on the TaskStatus head) → `HeadMoved` → `ClaimLost` → `executing.py` exit 8 |
| Dependencies are `done` at the moment a task is claimed | `next.py` skips blocked tasks |
| Two parallel `pm plan` calls cannot both create the same slug | `Task.text` is `task:<queue>/<slug>`; hashharness rejects duplicate `text_sha256` → `SlugTaken` → exit 4 |
| Every claim attempt eventually resolves (commit or abort) | `executing.py` always exits 0/6/8 |

Both race conditions are content-addressed gates inside hashharness: slug uniqueness rides the `text_sha256` index, and claim ordering rides the per-(work_package_id, type) `chain_predecessor` head pointer. The scripts plumb those structural rejections up to the operator-visible exit codes.

## Worker loop (`pm` agents)

```
1. pm next --queue <Q>             → JSON or "null"
2. pm executing --task TASK        → exit 0 win | 6 pre-claim | 8 race-lost
3. read task.attributes.body, do the work
4. pm report --task TASK --title T --text-file ...
5. pm finished --task TASK         → requires report, exits 7 if missing
```

`pm execute` (the `pm-execute` skill) spawns N agents in parallel running this loop.

## Threat model

The formal model verifies the protocol assuming every state transition goes through `pm`. A client writing directly to hashharness via MCP can bypass most assertions (state-machine ordering, proof-of-work, dep gate, claim recheck). What survives a bypass is the storage layer: item immutability, schema link types, link-target existence, and `text_sha256` uniqueness on the canonical slug key.

For cooperative-agent usage (the actual use case), convention is sufficient. See `system-models/reports/planning-reconciliation.md#threat-model` for hardening options if adversarial bypass becomes a concern.

## Re-running the model

```bash
# Bring the formal-methods skill's runner along:
verify=~/.claude/plugins/cache/morozov-claude-plugin/formal-methods/1.3.0/skills/formal-modeling/scripts/verify.sh

bash $verify system-models/planning.als            # 6 checks, 5 scenarios — all pass
bash $verify system-models/planning_plan_race.als  # 1 check, 1 scenario
```

To reproduce the historical slug-race counterexample, swap `commitPlan[p]` for `commitPlanBuggy[p]` in `planning_plan_race.als`'s `Transitions` fact and re-run; the counterexample re-appears in 4 steps.

## Reports

- `system-models/reports/planning-reconciliation.md` — per-property cross-source consistency table, threat model, boundary review.
- `system-models/reports/planning-enforcement.md` — for each verified property, the gate audit chain: where it's enforced in code, in the worker loop, and in the spec; what artifact carries the evidence.

## Acknowledgments

- [hashharness](https://github.com/in8finity/hashharness) — append-only text store with MCP server.
- The Alloy 6 model and reports were produced by the [`formal-modeling`](https://github.com/in8finity/claude-plugin) skill.
