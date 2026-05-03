module planning_replan_with_parent_gate

/*
  Cross-feature model: how does `pm replan` behave when applied to a
  task tree shaped like the new --depth ≥1 + --chain-siblings layout?

  Composition under test:
    - Parent task has children (parentTask link).
    - Children are chained by depends_on in array order.
    - The parent rolls up its children — pm next/pull skips a parent
      while any child is non-terminal (planning_parent_gate.als).
    - replan supports three cascade modes:
        cascade-up    — reset terminal ancestors via depends_on
        cascade-down  — reset terminal consumers via depends_on (reverse)
        no-cascade    — only the target

  Maps to:
    skills/pm/scripts/{next,pull,replan,store}.py
    system-models/{planning_parent_gate,planning_replan}.als

  The big question: does combining the new parent gate with cascade-
  down produce a "stale rollup" hazard? Concretely — if a parent task
  is `Done` (its rollup status), and we replan a child with cascade-
  down, the child + downstream siblings reset to `New` but the parent
  stays `Done` because parentTask is NOT a depends_on edge. The
  rollup was computed from the children's prior outcomes; with
  children being redone the rollup is now arguably stale.

  Modeling decisions:
    - Static snapshot of "before vs after one replan action". Multi-
      step traces aren't needed for the soundness questions here.
    - Statuses: New, Working, Done, Rejected, Superseded.
      "Settled" = {Done, Rejected, Superseded} (matches code).
    - Two relations: depends_on (data-flow gating) and parentTask
      (rollup grouping). Acyclic each, and they don't mix — a task's
      depends_on chain doesn't traverse parentTask edges.
    - No re-derivation logic — we only check that the post-replan
      state is consistent with the gating predicate, NOT that the
      cascade picked the "right" set of tasks (that's R7-R11).

  Properties checked:
    P1 ParentGateHoldsAfterReplan
        After any replan action, the parent-gate predicate is still
        well-defined (not contradictory): no parent simultaneously
        runnable AND has an unsettled child.
    P2 CascadeDownDoesNotCrossParentBoundary
        cascade-down on a child only resets tasks reachable via
        depends_on, never via parentTask. Documents that parents are
        NOT auto-invalidated by child replan.
    P3 StaleRollupWarning
        Witness scenario: parent is Done, a child gets replanned with
        cascade-down (resetting siblings), parent stays Done but the
        rollup is now derived from outdated child results. This is
        the suspected hazard — model produces a concrete instance the
        user can decide whether to care about.
    P4 ReplanTargetEventuallyRunnable
        After resetting a target via in-place reset, with all upstream
        deps settled and all children settled, the target is runnable.
    P5 SiblingChainOrderingPreservedAfterMidChainReplan
        Replan + cascade-down on a middle sibling resets later
        siblings but earlier siblings stay settled — so the dep
        ordering of "first sibling first" is preserved.
*/

abstract sig Status {}
one sig SNew, SWorking, SDone, SRejected, SSuperseded extends Status {}

sig Task {
  // both pre- and post-replan status, so we can check the transition.
  status:     one Status,
  status_after: one Status,
  parent:     lone Task,
  deps:       set Task          // tasks this one depends_on
}

// Acyclicity for both relations.
fact NoSelfParent  { all t: Task | t.parent != t }
fact NoSelfDep     { all t: Task | t not in t.deps }
fact ParentAcyclic { no t: Task | t in t.^parent }
fact DepsAcyclic   { no t: Task | t in t.^deps }

// "Children" via reverse parent.
fun children[p: Task] : set Task { parent.p }

// "Consumers" via reverse deps — descendants in the data-flow graph.
fun consumers[t: Task] : set Task { deps.t }

fun terminalForParent : set Status { SDone + SRejected + SSuperseded }

// The parent-gate runnable predicate (matches next.py).
pred runnable[t: Task, s: Task -> one Status] {
  s[t] = SNew
  all c: children[t] | s[c] in terminalForParent
  all d: t.deps     | s[d] = SDone
}

// ---- Replan actions, modeled as before/after status transitions ----

// Reset target only (no cascade): target was Terminal, becomes New.
pred replan_no_cascade[target: Task] {
  status[target] in (SDone + SRejected)
  status_after[target] = SNew
  all t: Task - target | status_after[t] = status[t]
}

// Cascade-down: target + every terminal descendant via deps reset.
pred replan_cascade_down[target: Task] {
  status[target] in (SDone + SRejected)
  status_after[target] = SNew
  let descs = ^(deps).target |
    (all d: descs | status[d] in (SDone + SRejected) =>
                    status_after[d] = SNew)
    and (all d: descs | status[d] not in (SDone + SRejected) =>
                        status_after[d] = status[d])
    and (all t: Task - target - descs | status_after[t] = status[t])
}

// Cascade-up: target + every terminal ancestor via deps reset.
pred replan_cascade_up[target: Task] {
  status[target] in (SDone + SRejected)
  status_after[target] = SNew
  let ancs = target.^deps |
    (all a: ancs | status[a] in (SDone + SRejected) =>
                   status_after[a] = SNew)
    and (all a: ancs | status[a] not in (SDone + SRejected) =>
                       status_after[a] = status[a])
    and (all t: Task - target - ancs | status_after[t] = status[t])
}

// ===== Properties =====

// P1: After any replan action, parent-gate is still consistent — no task
// is both runnable AND has an unsettled child in the post-state.
assert P1_ParentGateHoldsAfterReplan {
  all target: Task |
    (replan_no_cascade[target]
     or replan_cascade_down[target]
     or replan_cascade_up[target])
    => (all t: Task |
         runnable[t, status_after] =>
           (all c: children[t] | status_after[c] in terminalForParent))
}
check P1_ParentGateHoldsAfterReplan for 5

// P2: cascade-down only resets via deps, never via parentTask. A task
// reachable only by parentTask (a child) is NOT reset by cascade-down
// on its parent.
assert P2_CascadeDownDoesNotCrossParentBoundary {
  all target: Task |
    replan_cascade_down[target]
    => (all c: children[target] |
         c not in target.^(~deps)            // c is reachable from target only via parent, not deps
         => status_after[c] = status[c])
}
check P2_CascadeDownDoesNotCrossParentBoundary for 5

// P3: Stale-rollup witness — parent Done, replan a child with cascade-
// down, parent stays Done but its child is now New. This is a concrete
// instance of the hazard the model surfaces; users decide whether to
// extend cascade-down to traverse parentTask too.
run StaleRollupWitness {
  some par, kid: Task |
    kid in children[par]
    and status[par] = SDone
    and status[kid] = SDone
    and replan_cascade_down[kid]
    and status_after[par] = SDone
    and status_after[kid] = SNew
} for 4

// P4: a target with no upstream-or-child obstacles is runnable after
// in-place reset.
assert P4_ReplanTargetRunnableWhenUnblocked {
  all target: Task |
    (replan_no_cascade[target]
     and (all d: target.deps     | status[d] = SDone)
     and (all c: children[target] | status[c] in terminalForParent))
    => runnable[target, status_after]
}
check P4_ReplanTargetRunnableWhenUnblocked for 5

// P5: cascade-down on a middle sibling resets later siblings via deps,
// but earlier siblings (which the target depends on, not the other way)
// remain settled — sibling chain ordering preserved.
assert P5_SiblingChainOrderingPreservedAfterMidChainReplan {
  all par, mid: Task |
    (mid in children[par]
     and replan_cascade_down[mid])
    => (all earlier: Task |
         (earlier in children[par] and earlier in mid.^deps)
         => status_after[earlier] = status[earlier])
}
check P5_SiblingChainOrderingPreservedAfterMidChainReplan for 5

// Witness: a 3-sibling chain par→[k1→k2→k3], k2 is replanned with
// cascade-down. Expected: k1 stays Done, k2 → New, k3 → New (k3 depends
// on k2). Parent stays Done (P3 hazard applies).
run MidChainReplanScenario {
  some par, k1, k2, k3: Task |
    k1 != k2 and k2 != k3 and k1 != k3
    and parent[k1] = par and parent[k2] = par and parent[k3] = par
    and k1 in k2.deps and k2 in k3.deps     // k2 depends on k1; k3 depends on k2
    and status[par] = SDone
    and status[k1] = SDone
    and status[k2] = SDone
    and status[k3] = SDone
    and replan_cascade_down[k2]
    and status_after[k1] = SDone
    and status_after[k2] = SNew
    and status_after[k3] = SNew
} for 5
