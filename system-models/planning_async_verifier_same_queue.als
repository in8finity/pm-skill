module planning_async_verifier_same_queue

/*
  planning_async_verifier_same_queue.als — sibling model exploring the
  "same queue + role discriminator" topology proposed at the end of
  the 2026-05-26 design session.

  Compare against planning_async_verifier.als (separate verify-queue
  variant). The proofs converge on F1-F9; this model adds F10 (rollup
  consistency with verifier verdict) and removes the queue-level
  discriminator in favor of a `role` attribute.

  Structure (the user's proposal):

      P                            ← grouping parent (Role: parent, empty body)
      ├── work        role=RWorker
      ├── verify      role=RVerifier   antiAffineWith=work   depends_on=[work]
      └── rollup      role=RRollup    depends_on=[work, verify]

  All four tasks live on the **same queue**. The Rollup child is the
  policy point for iterate-or-close:
    - verify SDone   → rollup may close SDone → parent rolls up cleanly
    - verify SRejected → rollup must NOT close SDone; either close
                          SRejected (propagate failure) or replan the
                          worker (iteration loop, parent stays SWorking)

  Anti-affinity moves from "verifier vs parent" to "verifier vs the
  task it verifies" — a more general primitive. The model uses
  `antiAffineWith: lone Task` instead of the separate-queue model's
  `antiAffineParent: lone Bool`. Verifier-children set antiAffineWith
  pointing at their worker sibling.

  Maps to (after the convention lands):
    skills/pm/scripts/plan.py     — `pm plan --verifier` auto-emits the
                                    three-child structure
    skills/pm/scripts/executing.py — anti-affinity gate (~10 LOC):
                                    refuse claim when candidate matches
                                    task.antiAffineWith.claimedBy
    skills/pm/scripts/finished.py — rollup child enforces F10 as part
                                    of its own verifier check (script-
                                    path verifier reading sibling's
                                    status)
    skills/pm/plan/SKILL.md       — convention documentation

  Verifies (static model):
    F1  VerifiedParentHasThreeChildren   — auto-spawn shape: work + verify + rollup
    F2  RoleOnlyForVerifiedSubtree       — role attribute scope
    F3  NoRecursiveVerifier              — no infinite regress
    F4  VerifierNotSticky                — enables F5 structurally
    F5  VerifierRunsInSeparateContext    — verifier claimant ≠ worker claimant
    F6  ParentNotDoneWhileChildUnsettled — composition with rollup gate
    F7  RejectedVerifierBlocksParentDone — follows from F10 + rollup gate
    F8  AntiAffinityPrimitiveHolds       — reusable antiAffineWith rule
    F9  FreshContextPerVerification      — GAP: orchestrator one-shot policy
    F10 RollupConsistentWithVerifierVerdict — NEW: rollup SDone ⟹ verifier SDone

  Scenarios:
    HappyPath_VerifiedSubtreeAllDone     — SAT: 4-task subtree all SDone
    VerifierFailure_RollupRejected       — SAT: verifier rejected → rollup rejected
    TryRollupDoneWithRejectedVerifier    — UNSAT: F10 enforces consistency
    TryWorkerVerifierSameAgent           — UNSAT: anti-affinity refuses
    WorkerVerifierRollupDistinctAgents   — SAT: three roles, three agents
    PersistentVerifierWorker             — SAT: same agent verifies 2 parents
    OneShotVerifierWorker                — SAT: distinct agents per verify task
    TryFreshContextWithSharedAgent       — UNSAT under oneShotRegime predicate
*/

abstract sig Bool {}
one sig True, False extends Bool {}

abstract sig Status {}
one sig SNew, SWorking, SDone, SRejected, SSuperseded extends Status {}

// Role discriminator replaces the verify-queue / main-queue split.
// A task without a role is either the grouping parent or an ordinary
// task that has no auto-spawn structure.
abstract sig Role {}
one sig RWorker, RVerifier, RRollup extends Role {}

sig Agent {}

sig Task {
  status:           one Status,
  parent:           lone Task,        // ordinary parentTask link
  role:             lone Role,        // RWorker/RVerifier/RRollup on the three structured children
  requiresVerifier: lone Bool,        // True at plan time → auto-spawn three children
  claimedBy:        lone Agent,       // absent in SNew
  sticky:           lone Bool,
  // Generalized anti-affinity: points at the task whose claimant this
  // task must NOT match. Verifier-children set antiAffineWith → worker
  // sibling. The runtime gate (~10 LOC in executing.py) reads the
  // target's claimedBy and refuses on match.
  antiAffineWith:   lone Task
}

// ===== Structural facts =====

fact NoSelfParent { all t: Task | t.parent != t }
fact NoCycle      { no t: Task | t in t.^parent }
fact ClaimImpliesPastNew {
  all t: Task | some t.claimedBy iff t.status != SNew
}

// ===== Auto-spawn convention: three children per verified parent =====

// Every verified parent has exactly one Worker child, one Verifier
// child, and one Rollup child. This is the planner-side auto-spawn
// shape that `pm plan --verifier` would emit.
fact VerifiedParentHasThreeChildren {
  all p: Task | p.requiresVerifier = True => (
    (one w: Task | w.parent = p and w.role = RWorker) and
    (one v: Task | v.parent = p and v.role = RVerifier) and
    (one r: Task | r.parent = p and r.role = RRollup)
  )
}

// Role attribute is meaningful only for children of a verified parent.
// Ordinary tasks (including the grouping parent itself) have no role.
fact RoleOnlyForVerifiedSubtree {
  all t: Task |
    some t.role => (some t.parent and t.parent.requiresVerifier = True)
}

// No recursive verification — none of the three structured children
// can themselves declare a verifier.
fact NoRecursiveVerifierFact {
  all c: Task | some c.role => c.requiresVerifier != True
}

// Verifier-children must NOT be sticky. Sticky binding would conflict
// with the anti-affinity goal (sticky says "same context as first
// claimant"; anti-affinity says "different context from worker
// sibling"). The two can compose later (see StickyAntiAffineComposition
// in the sibling model) but for the default verifier-child role, off.
fact VerifierNotStickyFact {
  all c: Task | c.role = RVerifier => c.sticky != True
}

// Verifier-child's antiAffineWith points at the worker sibling. This
// is what makes F5 hold by construction — the runtime gate then has a
// concrete target to compare candidate claimants against.
fact VerifierAntiAffineWithWorkerSibling {
  all v: Task | v.role = RVerifier =>
    (one w: Task |
      w.parent = v.parent and w.role = RWorker and v.antiAffineWith = w)
}

// The anti-affinity primitive itself, independent of verifier-child
// semantics. Any task that sets antiAffineWith refuses a claim that
// matches the target's claimant. Reusable for future dual-control /
// four-eyes flows.
fact AntiAffineWithBindingRule {
  all t: Task |
    (some t.antiAffineWith
     and some t.claimedBy
     and some t.antiAffineWith.claimedBy)
      => t.claimedBy != t.antiAffineWith.claimedBy
}

// ===== Rollup gate composition =====

// Standard cross-queue parent-rollup gate (proven temporally in
// planning_parent_gate.als). Restated as a static fact: a parent in
// SDone implies every child is in {SDone, SRejected, SSuperseded}.
// Applies to the grouping parent here — it can only roll up to SDone
// once all three of Worker, Verifier, Rollup have settled.
fact ParentDoneOnlyWhenChildrenTerminal {
  all p: Task | p.status = SDone =>
    (all c: Task | c.parent = p =>
      c.status in (SDone + SRejected + SSuperseded))
}

// ===== F10: rollup-consistency policy =====

// The new invariant unique to this topology. The Rollup child closes
// SDone only when the Verifier-child closed SDone. If the verifier
// rejected, the rollup must either reject too (give up) or stay
// SWorking (iterating — typically by replanning the worker). This is
// the runtime policy enforced by the Rollup child's own body, which
// reads the verifier sibling's status and decides accordingly.
//
// Replaces the separate-queue model's `VerifierRejectionBlocksParentDone`
// fact: the policy moves from a special-case rollup-gate tightening
// to a first-class child task with its own logic.
fact RollupSDoneRequiresVerifierSDone {
  all r: Task |
    (r.role = RRollup and r.status = SDone) =>
      (all v: Task |
        (v.parent = r.parent and v.role = RVerifier)
          => v.status = SDone)
}

// Parent-level enforcement: a verified parent cannot reach SDone while
// its verifier-child is SRejected. The standard rollup gate
// (ParentDoneOnlyWhenChildrenTerminal) treats SRejected as terminal-
// for-parent, which works for ordinary cancelled siblings but is too
// lenient here — F7's counterexample (worker SRejected, verifier
// SRejected, rollup SSuperseded, parent SDone) demonstrates the
// concrete configuration that slips past. This fact closes the
// loophole specifically for verifier-children. Implementation: a
// targeted check in finished.py before allowing SDone — refuse if any
// child has role=RVerifier and status=SRejected.
fact ParentSDoneRequiresVerifierSDone {
  all p, v: Task |
    (p.status = SDone
     and v.parent = p
     and v.role = RVerifier)
      => v.status = SDone
}

// ===== Safety assertions =====

assert F1_VerifiedParentHasThreeChildren {
  all p: Task | p.requiresVerifier = True => {
    some w: Task | w.parent = p and w.role = RWorker
    some v: Task | v.parent = p and v.role = RVerifier
    some r: Task | r.parent = p and r.role = RRollup
  }
}
check F1_VerifiedParentHasThreeChildren for 6

assert F2_RoleOnlyForVerifiedSubtree {
  all t: Task | some t.role =>
    (some t.parent and t.parent.requiresVerifier = True)
}
check F2_RoleOnlyForVerifiedSubtree for 6

assert F3_NoRecursiveVerifier {
  all c: Task | some c.role => c.requiresVerifier != True
}
check F3_NoRecursiveVerifier for 6

assert F4_VerifierNotSticky {
  all c: Task | c.role = RVerifier => c.sticky != True
}
check F4_VerifierNotSticky for 6

assert F5_VerifierRunsInSeparateContext {
  all v: Task |
    (v.role = RVerifier
     and some v.antiAffineWith
     and some v.claimedBy
     and some v.antiAffineWith.claimedBy)
      => v.claimedBy != v.antiAffineWith.claimedBy
}
check F5_VerifierRunsInSeparateContext for 6

assert F6_ParentNotDoneWhileChildUnsettled {
  all p, c: Task |
    (c.parent = p and c.status in (SNew + SWorking))
      => p.status != SDone
}
check F6_ParentNotDoneWhileChildUnsettled for 6

// F7 follows from F10 + the rollup gate, but worth stating directly.
assert F7_RejectedVerifierBlocksParentDone {
  all p, v: Task |
    (v.parent = p and v.role = RVerifier and v.status = SRejected)
      => p.status != SDone
}
check F7_RejectedVerifierBlocksParentDone for 6

assert F8_AntiAffinityPrimitiveHolds {
  all t: Task |
    (some t.antiAffineWith
     and some t.claimedBy
     and some t.antiAffineWith.claimedBy)
      => t.claimedBy != t.antiAffineWith.claimedBy
}
check F8_AntiAffinityPrimitiveHolds for 6

// Same gap as in the separate-queue model: F9 fails until orchestrator
// one-shot dispatch is enforced. Cannot be enforced at the pm level.
pred oneShotRegime {
  no disj v1, v2: Task |
    v1.role = RVerifier and v2.role = RVerifier
    and some v1.claimedBy and v1.claimedBy = v2.claimedBy
}

assert F9_FreshContextPerVerification {
  all disj v1, v2: Task |
    (v1.role = RVerifier and v2.role = RVerifier
     and some v1.claimedBy and some v2.claimedBy)
      => v1.claimedBy != v2.claimedBy
}
// Scope must admit two verified parents (= 8 tasks) to expose the
// persistent-worker counterexample. Cannot constrain Role scope — the
// three `one sig` declarations require Role universe ≥ 3.
check F9_FreshContextPerVerification for 10

// NEW for this topology: rollup must be consistent with verifier's
// verdict. If rollup closed SDone, verifier must have closed SDone.
// If verifier was SRejected, rollup is in some non-SDone status.
assert F10_RollupConsistentWithVerifierVerdict {
  all r, v: Task |
    (r.role = RRollup and r.status = SDone
     and v.role = RVerifier and v.parent = r.parent)
      => v.status = SDone
}
check F10_RollupConsistentWithVerifierVerdict for 6

// ===== Scenarios =====

// SAT: the full 4-task subtree all closes cleanly. Parent SDone,
// worker SDone, verifier SDone (PASS), rollup SDone.
run HappyPath_VerifiedSubtreeAllDone {
  some p, w, v, r: Task, disj aw, av, ar: Agent |
    p.requiresVerifier = True
    and w.parent = p and w.role = RWorker
    and v.parent = p and v.role = RVerifier
    and r.parent = p and r.role = RRollup
    and w.status = SDone and v.status = SDone and r.status = SDone
    and p.status = SDone
    and w.claimedBy = aw and v.claimedBy = av and r.claimedBy = ar
} for exactly 4 Task, exactly 3 Agent

// SAT: verifier failed → rollup itself rejected (give-up branch of
// iterate-or-close). Parent is NOT SDone; it propagates the rejection
// via the rollup gate or stays in working pending operator action.
run VerifierFailure_RollupRejected {
  some p, w, v, r: Task |
    p.requiresVerifier = True
    and w.parent = p and w.role = RWorker
    and v.parent = p and v.role = RVerifier
    and r.parent = p and r.role = RRollup
    and w.status = SDone
    and v.status = SRejected
    and r.status = SRejected
    and p.status != SDone
} for exactly 4 Task, 4 Agent

// UNSAT: F10 refuses a configuration where rollup closes SDone past
// a rejected verifier. This is the load-bearing assertion of the
// iterate-or-close design.
run TryRollupDoneWithRejectedVerifier {
  some p, w, v, r: Task |
    p.requiresVerifier = True
    and w.parent = p and w.role = RWorker
    and v.parent = p and v.role = RVerifier
    and r.parent = p and r.role = RRollup
    and v.status = SRejected
    and r.status = SDone
} for exactly 4 Task, 4 Agent
expect 0

// UNSAT: anti-affinity refuses a configuration where the worker and
// verifier share a claimant. The verifier's antiAffineWith → worker.
run TryWorkerVerifierSameAgent {
  some p, w, v: Task, a: Agent |
    p.requiresVerifier = True
    and w.parent = p and w.role = RWorker
    and v.parent = p and v.role = RVerifier
    and w.claimedBy = a and v.claimedBy = a
} for exactly 3 Task, exactly 1 Agent
expect 0

// SAT: worker, verifier, rollup all claimed by distinct agents.
// The natural happy-path: orchestrator spawns three role-specialized
// Agent invocations, each with its own PM_CONTEXT_ID.
run WorkerVerifierRollupDistinctAgents {
  some p, w, v, r: Task, disj aw, av, ar: Agent |
    p.requiresVerifier = True
    and w.parent = p and w.role = RWorker
    and v.parent = p and v.role = RVerifier
    and r.parent = p and r.role = RRollup
    and w.claimedBy = aw and v.claimedBy = av and r.claimedBy = ar
} for exactly 4 Task, exactly 3 Agent

// SAT: persistent verifier worker — one agent claims 2 verifier-
// children across two distinct verified parents. F9 fails on this
// configuration. Same regime as the separate-queue model's
// PersistentWorkerRegime.
run PersistentVerifierWorker {
  some disj p1, p2: Task,
       disj w1, w2, v1, v2, r1, r2: Task,
       a_v: Agent |
    p1.requiresVerifier = True and p2.requiresVerifier = True
    and w1.parent = p1 and w1.role = RWorker
    and v1.parent = p1 and v1.role = RVerifier
    and r1.parent = p1 and r1.role = RRollup
    and w2.parent = p2 and w2.role = RWorker
    and v2.parent = p2 and v2.role = RVerifier
    and r2.parent = p2 and r2.role = RRollup
    and v1.claimedBy = a_v and v2.claimedBy = a_v  // SAME verifier agent
} for exactly 8 Task, 8 Agent

// SAT: one-shot verifier dispatch — two verifier-children, two
// distinct claimants. F9 holds on this configuration.
run OneShotVerifierWorker {
  some disj p1, p2: Task,
       disj w1, w2, v1, v2, r1, r2: Task,
       disj a_v1, a_v2: Agent |
    p1.requiresVerifier = True and p2.requiresVerifier = True
    and w1.parent = p1 and w1.role = RWorker
    and v1.parent = p1 and v1.role = RVerifier
    and r1.parent = p1 and r1.role = RRollup
    and w2.parent = p2 and w2.role = RWorker
    and v2.parent = p2 and v2.role = RVerifier
    and r2.parent = p2 and r2.role = RRollup
    and v1.claimedBy = a_v1 and v2.claimedBy = a_v2  // DISTINCT agents
} for exactly 8 Task, 8 Agent

// UNSAT under oneShotRegime predicate: cannot have two verifier-
// children sharing a claimant. The predicate IS F9 as a constraint.
run TryFreshContextWithSharedAgent {
  oneShotRegime
  and some disj v1, v2: Task, a: Agent |
    v1.role = RVerifier and v2.role = RVerifier
    and v1.claimedBy = a and v2.claimedBy = a
} for 6
expect 0
