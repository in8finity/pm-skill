module planning_async_verifier

/*
  planning_async_verifier.als — formal model of the async-verification-queue
  approach to the `verifier:` contract.

  Background. The current `pm-plan --verifier <spec>` contract has three forms:
    - `skill:NAME` / `prompt:CRITERION` — worker self-attests in the report
      (trust-the-worker; fragile, see feedback/verifier-attestation-
      reliability-2026-05-25.md)
    - `verify-skill:NAME` / `verify-prompt:CRITERION` — `pm finished` spawns
      a fresh subprocess (claude -p or codex exec) for an independent
      re-judge
    - `<absolute path>` — `pm finished` runs the script directly

  This model explores a fourth shape: **auto-spawned verifier-child**.
  At plan time, a task with `--verifier <spec>` automatically gets a
  sibling-child on a separate `verify-*` queue. The child's body is
  "read parent's report, apply <spec>, attest done/rejected." A separate
  worker pool drains the verify queue; each verifier attempt runs in
  its own agent context.

  Composition. The model layers on top of:
    - planning_parent_gate.als#CrossQueueParentNotFinishedWhilePendingChild
      (parent can't close until child terminal — restated here as a fact)
    - planning_cancel_cascade.als#CC7_CascadeIsCrossQueue
    - planning_reclaim_cascade.als#RC7_CascadeIsCrossQueue
    (cross-queue cancel/reclaim already propagates to verify-queue children)

  Maps to (after the convention lands):
    skills/pm/scripts/plan.py     — auto-spawn verifier-child on --verifier
    skills/pm/scripts/finished.py — parent rollup gate (already cross-queue;
                                    needs verifier-rejection tightening — F7)
    skills/pm/plan/SKILL.md       — convention documentation

  Verifies (static model):
    F1 VerifiedParentHasVerifierChild     — auto-spawn is total
    F2 VerifierChildOnVerifyQueue         — structural placement
    F3 NoRecursiveVerifier                — verifier-children can't have verifiers
    F4 VerifierChildNotSticky             — non-sticky enables F5 structurally
    F5 VerifierRunsInSeparateContext      — anti-affinity from parent
    F6 ParentNotDoneWhileVerifierUnsettled — composition with cross-queue gate
    F7 RejectedVerifierBlocksParentDone   — verdict propagation (proposed tighten)
    F8 AntiAffinityPrimitiveHolds          — the reusable primitive
    F9 FreshContextPerVerification        — GAP ASSERTION: no two verifier-children
                                            share a claimant. Holds iff orchestrator
                                            uses one-shot worker prompts. Fails for
                                            persistent (loop-until-empty) workers.

  Worker-regime sub-model:
    The async-verifier design composes with two orthogonal orchestrator
    dispatch regimes:
      (a) persistent worker — one Agent invocation processes many verifier
          tasks sequentially. Context accumulates; cheaper per call.
      (b) one-shot worker — each verifier task gets a fresh Agent
          invocation; the sub-agent exits after one verdict. Fresh
          context per verification; more cold-start overhead.
    Anti-affinity (F5/F8) holds in either regime. F9 distinguishes them:
    only one-shot satisfies F9.

  Scenarios:
    HappyPath_PassedVerification          — SAT: parent done, verifier-child done
    VerifierFailure_ParentStuck           — SAT: rejected child, parent != done
    TrySameAgentClaimsBoth                — UNSAT: anti-affinity refuses
    VerifierClaimedByDifferentAgent        — SAT: post-fix happy path
    StickyAntiAffineComposition           — SAT: sticky + anti-affine compose
    PersistentWorkerRegime                — SAT: one agent claims 2 verifiers
    OneShotWorkerRegime                   — SAT: 2 verifiers, 2 distinct agents
    TryFreshContextWithSharedAgent        — UNSAT under fact: F9 enforced
*/

abstract sig Bool {}
one sig True, False extends Bool {}

abstract sig Status {}
one sig SNew, SWorking, SDone, SRejected, SSuperseded extends Status {}

sig Queue {}
one sig MainQueue, VerifyQueue extends Queue {}

sig Agent {}

sig Task {
  taskQueue:        one Queue,
  status:           one Status,
  parent:           lone Task,        // ordinary parentTask link
  requiresVerifier: lone Bool,        // True at plan time if --verifier was set
  claimedBy:        lone Agent,       // absent in SNew, present otherwise
  sticky:           lone Bool,        // True if sticky-context-bound to first claimant
  // Anti-affinity to parent: when True, the claim primitive must refuse
  // a candidate claimant that matches the parent's current claimant.
  // Mirrors sticky structurally (a per-task binding rule enforced at
  // claim time) but inverted: sticky = "must match first claimant's
  // context"; anti-affine = "must differ from parent's claimant." The
  // two are orthogonal — a task may be neither, one, or both. Verifier-
  // children get antiAffineParent = True by construction (the auto-spawn
  // convention sets it). A general primitive: any task that needs
  // independent re-judgement (verification, dual-control, four-eyes
  // review) can opt in by setting it.
  antiAffineParent: lone Bool
}

// ===== Structural facts =====

fact NoSelfParent { all t: Task | t.parent != t }
fact NoCycle      { no t: Task | t in t.^parent }
fact ClaimImpliesPastNew {
  all t: Task | some t.claimedBy iff t.status != SNew
}

// ===== Verifier-child shape =====

// A "verifier-child" is a task on the verify queue with a parent link.
pred isVerifierChild[c: Task] {
  c.taskQueue = VerifyQueue and some c.parent
}

// Only verifier-children live on the verify queue (no stray verify-queue tasks).
fact OnlyVerifierChildrenOnVerifyQueue {
  all c: Task | c.taskQueue = VerifyQueue => isVerifierChild[c]
}

// Every parent that declared --verifier has exactly one verifier-child.
// This is the auto-spawn contract enforced at plan time.
fact VerifiedParentHasExactlyOneVerifierChild {
  all p: Task |
    p.requiresVerifier = True =>
      one c: Task | isVerifierChild[c] and c.parent = p
}

// Verifier-children don't themselves declare a verifier — no infinite regress.
fact NoRecursiveVerifierFact {
  all c: Task | isVerifierChild[c] => c.requiresVerifier != True
}

// Verifier-children are NEVER sticky-context-bound. If they were, sticky
// binding would force claim by the parent's agent and defeat the
// separate-context goal of this design.
fact VerifierChildNotStickyFact {
  all c: Task | isVerifierChild[c] => c.sticky != True
}

// Verifier-children carry antiAffineParent = True by construction. This
// is the auto-spawn-time setting that gives F5 its teeth. The planner
// stamps it when emitting the verifier-child task.
fact VerifierChildIsAntiAffine {
  all c: Task | isVerifierChild[c] => c.antiAffineParent = True
}

// The anti-affinity primitive itself: a task marked antiAffineParent
// cannot be claimed by the same agent that holds its parent's claim.
// Enforced at claim time in `pm executing` (planned-but-not-yet-built
// code change): refuse the claim when candidate matches parent.claimedBy.
fact AntiAffineParentBindingRule {
  all c: Task |
    (c.antiAffineParent = True
     and some c.parent
     and some c.claimedBy
     and some c.parent.claimedBy)
      => c.claimedBy != c.parent.claimedBy
}

// ===== Rollup gate composition =====

// Cross-queue parent rollup gate (proven temporally in
// planning_parent_gate.als#CrossQueueParentNotFinishedWhilePendingChild)
// restated here as a static fact. Parent SDone implies every child is in
// the "terminal-for-parent" set: {SDone, SRejected, SSuperseded}.
fact ParentDoneOnlyWhenChildrenTerminal {
  all p: Task | p.status = SDone =>
    (all c: Task | c.parent = p =>
      c.status in (SDone + SRejected + SSuperseded))
}

// ===== Proposed tightening: verifier verdict propagates =====

// The standard rollup gate treats SRejected as terminal-for-parent. That
// works for ordinary cancelled children (parent isn't blocked) but is
// WRONG for verifier-children: a rejected verifier means verification
// FAILED, which must block the parent from reaching SDone.
//
// This fact represents the additional invariant the async-verifier design
// needs at the rollup-gate. Without it, F7 finds a counterexample where
// a parent reaches SDone past a rejected verifier-child.
fact VerifierRejectionBlocksParentDone {
  all p, c: Task |
    (c.parent = p and isVerifierChild[c] and c.status = SRejected)
      => p.status != SDone
}

// ===== Safety assertions =====

assert F1_VerifiedParentHasVerifierChild {
  all p: Task | p.requiresVerifier = True =>
    some c: Task | isVerifierChild[c] and c.parent = p
}
check F1_VerifiedParentHasVerifierChild for 6

assert F2_VerifierChildOnVerifyQueue {
  all c: Task | isVerifierChild[c] => c.taskQueue = VerifyQueue
}
check F2_VerifierChildOnVerifyQueue for 6

assert F3_NoRecursiveVerifier {
  all c: Task | isVerifierChild[c] => c.requiresVerifier != True
}
check F3_NoRecursiveVerifier for 6

assert F4_VerifierChildNotSticky {
  all c: Task | isVerifierChild[c] => c.sticky != True
}
check F4_VerifierChildNotSticky for 6

// F5 — verifier-children run in a different agent context than their
// parent. With `antiAffineParent = True` set by the auto-spawn convention
// (VerifierChildIsAntiAffine) and the anti-affinity binding rule
// (AntiAffineParentBindingRule), this assertion holds by construction.
// The composition (VerifierChildIsAntiAffine ∘ AntiAffineParentBindingRule)
// is the formal counterpart to the runtime change: `pm executing` looks
// up parent.claimedBy and refuses when it matches the candidate. ~10
// lines of code in skills/pm/scripts/executing.py.
assert F5_VerifierRunsInSeparateContext {
  all p, c: Task |
    (c.parent = p
     and isVerifierChild[c]
     and some p.claimedBy
     and some c.claimedBy)
      => c.claimedBy != p.claimedBy
}
check F5_VerifierRunsInSeparateContext for 6

// F8 — the underlying anti-affinity primitive (independent of verifier-
// child semantics). Any task with antiAffineParent = True respects the
// rule. Lets the same primitive serve future dual-control / four-eyes
// flows without re-deriving it from the verifier-child fact stack.
assert F8_AntiAffinityPrimitiveHolds {
  all c: Task |
    (c.antiAffineParent = True
     and some c.parent
     and some c.claimedBy
     and some c.parent.claimedBy)
      => c.claimedBy != c.parent.claimedBy
}
check F8_AntiAffinityPrimitiveHolds for 6

assert F6_ParentNotDoneWhileVerifierUnsettled {
  all p, c: Task |
    (c.parent = p and isVerifierChild[c]
     and c.status in (SNew + SWorking))
      => p.status != SDone
}
check F6_ParentNotDoneWhileVerifierUnsettled for 6

assert F7_RejectedVerifierBlocksParentDone {
  all p, c: Task |
    (c.parent = p and isVerifierChild[c] and c.status = SRejected)
      => p.status != SDone
}
check F7_RejectedVerifierBlocksParentDone for 6

// ===== Worker-regime predicate =====

// Holds when the orchestrator uses one-shot worker dispatch: every
// verifier-child has a distinct claimant. The orchestrator enforces
// this by writing a prompt like "claim ONE task, do the verification,
// exit" and re-issuing the `Agent` tool call per task. There's no
// `pm`-level primitive for this — `pm executing` cannot tell whether
// two claims come from "the same agent's next iteration" vs "two
// different agents". It's a property of the dispatch wrapper.
pred oneShotRegime {
  no disj c1, c2: Task |
    isVerifierChild[c1] and isVerifierChild[c2]
    and some c1.claimedBy and c1.claimedBy = c2.claimedBy
}

// F9 — fresh context per verification. Equivalent to oneShotRegime,
// stated as an assertion so the solver looks for a counterexample.
// **Gap assertion**: fails without a fact enforcing one-shot dispatch.
// The persistent-worker regime is the counterexample.
assert F9_FreshContextPerVerification {
  all disj c1, c2: Task |
    (isVerifierChild[c1] and isVerifierChild[c2]
     and some c1.claimedBy and some c2.claimedBy)
      => c1.claimedBy != c2.claimedBy
}
check F9_FreshContextPerVerification for 4 but exactly 2 Queue

// ===== Scenarios =====

// Witness: happy path — parent SDone, verifier-child SDone (verification PASSED).
run HappyPath_PassedVerification {
  some p, c: Task |
    p.taskQueue = MainQueue
    and p.requiresVerifier = True
    and c.parent = p and isVerifierChild[c]
    and p.status = SDone
    and c.status = SDone
} for exactly 2 Task, exactly 2 Queue, exactly 2 Agent

// Witness: verifier failed → parent CANNOT reach SDone (F7 holds).
run VerifierFailure_ParentStuck {
  some p, c: Task |
    p.taskQueue = MainQueue
    and p.requiresVerifier = True
    and c.parent = p and isVerifierChild[c]
    and c.status = SRejected
    and p.status != SDone
} for exactly 2 Task, exactly 2 Queue, exactly 2 Agent

// Negative: try to find a config where the same agent claims both. Under
// the anti-affinity rule (AntiAffineParentBindingRule + VerifierChildIs-
// AntiAffine), this should be UNSAT — the model refuses the configuration
// rather than accepting it. Pre-fix this was SAT; the flip is the
// model-level evidence that the proposed enforcement closes the F5 gap.
run TrySameAgentClaimsBoth {
  some p, c: Task, a: Agent |
    p.taskQueue = MainQueue and p.requiresVerifier = True
    and c.parent = p and isVerifierChild[c]
    and p.claimedBy = a and c.claimedBy = a
} for exactly 2 Task, exactly 2 Queue, exactly 1 Agent
expect 0

// Witness: a verifier-child is claimed by a different agent than its
// parent — the post-fix happy path. SAT confirms the rule doesn't
// over-constrain (still admits valid claims).
run VerifierClaimedByDifferentAgent {
  some p, c: Task, disj a1, a2: Agent |
    p.taskQueue = MainQueue and p.requiresVerifier = True
    and c.parent = p and isVerifierChild[c]
    and p.claimedBy = a1 and c.claimedBy = a2
} for exactly 2 Task, exactly 2 Queue, exactly 2 Agent

// Interaction witness: a task can be BOTH sticky AND antiAffineParent.
// Sticky binds the chain to whoever claims first; anti-affinity refuses
// the parent's claimant. Together: the verifier-child binds to the first
// non-parent claimant and then locks subsequent claims to that context.
// Useful when verification is multi-step and re-claims should stay in
// the same NON-parent context. (Not the verifier-child default, but the
// model proves the composition is satisfiable.)
run StickyAntiAffineComposition {
  some p, c: Task, disj a1, a2: Agent |
    c.parent = p
    and c.sticky = True
    and c.antiAffineParent = True
    and p.claimedBy = a1 and c.claimedBy = a2
} for exactly 2 Task, exactly 1 Queue, exactly 2 Agent

// ===== Worker-regime scenarios =====

// SAT: persistent-worker regime. One agent (a2) claims both verifier-
// children c1, c2 — sequentially in a real run, but the snapshot just
// shows them sharing the claimant. F9 fails on this configuration.
// Anti-affinity (F5) still holds: both verifier claimants differ from
// their parents' claimants.
run PersistentWorkerRegime {
  some disj p1, p2: Task, disj c1, c2: Task, disj a1, a3: Agent, a2: Agent |
    p1.requiresVerifier = True and p2.requiresVerifier = True
    and p1.taskQueue = MainQueue and p2.taskQueue = MainQueue
    and c1.parent = p1 and c2.parent = p2
    and isVerifierChild[c1] and isVerifierChild[c2]
    and p1.claimedBy = a1 and p2.claimedBy = a3 and a1 != a3
    and c1.claimedBy = a2 and c2.claimedBy = a2  // SAME verifier agent
    and a2 != a1 and a2 != a3
} for exactly 4 Task, exactly 2 Queue, exactly 3 Agent

// SAT: one-shot regime. Two verifier-children, each with its own
// distinct claimant. F9 holds on this configuration.
run OneShotWorkerRegime {
  some disj p1, p2: Task, disj c1, c2: Task,
       disj a1, a2, a3, a4: Agent |
    p1.requiresVerifier = True and p2.requiresVerifier = True
    and p1.taskQueue = MainQueue and p2.taskQueue = MainQueue
    and c1.parent = p1 and c2.parent = p2
    and isVerifierChild[c1] and isVerifierChild[c2]
    and p1.claimedBy = a1 and p2.claimedBy = a2
    and c1.claimedBy = a3 and c2.claimedBy = a4  // DISTINCT verifier agents
} for exactly 4 Task, exactly 2 Queue, exactly 4 Agent

// Negative: under the one-shot regime predicate, can we still find a
// configuration where two verifier-children share a claimant? Should
// be UNSAT — the predicate refuses it.
run TryFreshContextWithSharedAgent {
  oneShotRegime
  and some disj c1, c2: Task, a: Agent |
    isVerifierChild[c1] and isVerifierChild[c2]
    and c1.claimedBy = a and c2.claimedBy = a
} for 4 but exactly 2 Queue
expect 0
