# Model Isomorphism Check

This note sketches how to map common agent-orchestration abstractions onto
`hashharness-pm`'s primitives without collapsing distinct semantics too early.

## Core position

It is tempting to say "queue = agent", but that should not be the default
modeling choice.

That mapping works for some questions, but it blurs an important distinction:

- an **agent** is the actor that claims or emits work
- a **queue** is the coordination lane, mailbox, or execution domain where work
  waits

The current model in [planning.als](./planning.als) already gives a strong
semantic core:

- `Task`
- dependency edges
- claim protocol
- ownership
- reports
- terminal proof gates

That core should be reused and refined, not overloaded immediately.

## Recommended primitive mapping

- `Queue` as mailbox / execution lane / coordination domain
- `Task` as work item or message envelope
- `TaskStatus` chain as the execution trace of one work item
- `Agent` as either:
  - a worker instance that claims tasks from a queue
  - a role or kind attached to a queue or task

If `Queue = Agent` everywhere, the model loses the distinction between:

- who owns work
- where work waits
- how work is routed
- how delegation differs from scheduling

That distinction matters for race safety, routing, delegation, and flow
verification.

## Proposed model set

### 1. `agent_core.als`

Purpose: extract the reusable semantic core from `planning.als`.

Contents:

- queues
- workers
- claims
- dependencies
- reports
- terminal states
- optional message payloads

Goal: define the base primitives that later mappings refine.

### 2. `stateful_graph_mapping.als`

External analogue: LangGraph or another stateful agent graph.

Mapping:

- node invocation -> task
- graph edge -> dependency or next-task spawn rule
- graph state / checkpoint -> report or task-local artifact
- subgraph / specialist -> queue or role-constrained worker pool

Main checks:

- no node runs before required predecessors complete
- resumption after interruption preserves completed-node effects
- branching does not produce duplicate claims on the same logical node

### 3. `message_passing_mapping.als`

External analogue: AutoGen-style message-passing agents.

Mapping:

- agent -> queue plus allowed worker kind
- message -> task in receiver queue
- reply -> spawned task linked back to parent
- conversation thread -> chain of parent/spawn relationships

Main checks:

- every delivered message has exactly one current owner
- no reply appears without an earlier inbound message
- bounded liveness: open conversations eventually resolve or remain explicitly
  pending

### 4. `crews_flows_tasks_mapping.als`

External analogue: CrewAI-style crews, flows, and tasks.

Mapping:

- crew -> set of queues or worker kinds
- flow -> dependency DAG or spawn policy
- task -> existing `Task`
- manager delegation -> task creation into specialist queues

Main checks:

- flow order is respected
- hierarchical delegation does not orphan subtasks
- completion of a flow requires closure of all required child tasks

### 5. `mapping_refinement.md` or `mapping_matrix.als`

Purpose: capture which external abstraction refines which local primitive, and
where the fit is lossy rather than exact.

This should make explicit that the mappings are not fully 1:1.

## Key design decisions to settle first

1. Is a queue a mailbox, an agent role, or a whole crew?
   Recommendation: model it as mailbox / coordination lane first.
2. Are agents persistent entities, or only claimants?
   Recommendation: start with claimants.
3. Does stateful graph state live in `TaskReport`, a new artifact type, or a
   derived view over status chains?
   Recommendation: start with `TaskReport`.
4. Does message-passing need conversation identity separate from parent/child
   task links?
   Recommendation: probably yes.

## Suggested build order

1. Refactor the reusable semantic core out of [planning.als](./planning.als).
2. Build `stateful_graph_mapping.als` first.
   Reason: it is closest to the existing dependency and claim model.
3. Build `message_passing_mapping.als` second.
   Reason: it forces the missing thread/message identity decisions.
4. Build `crews_flows_tasks_mapping.als` third.
   Reason: it can reuse the first two layers as constrained orchestration.

## Recommendation

Do not start with `queue = agent` as the global equivalence.

Start with:

- queue = coordination lane
- agent = claimant / worker kind
- task = message or work unit
- report = checkpoint or evidence

That gives cleaner refinement paths to:

- stateful agent graphs
- message-passing agent systems
- crews / flows / tasks systems

## Additional notes

1. Consider the distinction and interaction between agent state, task state,
   and flow state.
   These should not be collapsed into one lifecycle too early. Some frameworks
   treat task progression as primary, while others treat agent memory or flow
   position as the dominant state.

2. Consider ownership of an agent over a task, while also allowing the task or
   flow to move through different agents or workflows via routing.
   The model should distinguish:
   - current owner
   - original assignee or creator
   - routing policy
   - transfer or re-claim conditions

3. Consider message-driven task creation under at least two ownership rules:
   - ownership by message author
   - ownership by message receiver

   This matters because a message may create a task that is:
   - owned by the sender as a delegated request
   - owned by the receiver as accepted work
   - initially unowned until claimed from the receiver's queue
