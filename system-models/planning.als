module planning

/*
  Formal model of the hashharness-backed planning skill set.

  Maps to:
    skills/pm/scripts/{plan,executing,report,finished,store}.py
    skills/pm/{plan,next,executing,report,finished,execute}/SKILL.md

  Modeling decisions:
    - The append-only TaskStatus chain is abstracted as "current latest".
      Concurrency correctness depends on which append wins the latest-tip
      race, not on the full chain history. The chain itself is audit-only.
    - Claim is modeled in two phases (start + commit | abort) so the
      read-status / append-status race window in executing.py is explicit.
        startClaim(a, t)   ~ executing.py reads "new" and prepares the append
        commitClaim(x)     ~ x's append wins the chain-head compare-and-swap
        abortClaim(x)      ~ x's append loses; hashharness's chain_predecessor
                             check on `prevStatus` rejects it with 'head moved'
    - Single-step "atomic" transitions assume serialized linearization of
      append events (one append per step). Concurrent appends are modeled
      as overlapping startClaim attempts that resolve via commit/abort.
    - The Progress fact requires every started attempt to eventually resolve.
      This is the liveness assumption that makes safety meaningful.

  Verifies:
    1. Terminal absorption (done/rejected never transitions out).
    2. Proof-of-work requirement before any task reaches done/rejected.
    3. Single owner: at most one agent owns a task's latest status.
    4. Dependency gate: deps are done at the moment a task is claimed.
    5. Race safety: no two attempts on the same task both commit.
    6. Liveness: every claim attempt eventually resolves.

  Boundary (excluded with rationale):
    - Subtask spawnedAt link to a specific TaskStatus event — the model does
      not track per-event identity; only "parent task" is captured.
    - Reports chain (prevReport) — collapsed to "task has at least one report".
    - Slug-uniqueness race in plan() — documented as a known gap; see
      GapSlugRace assertion below.
    - Hash integrity / schema validation — assumed sound (hashharness's job).
*/

abstract sig Phase {}
one sig PNew, PWorking, PDone, PRejected extends Phase {}

sig Agent {}

sig Task {
  deps: set Task,
  parent: lone Task,
  var phase: lone Phase,
  var owner: lone Agent
}

// Tasks that declare a verifier in plan-time attributes. Immutable.
sig RequiresVerifier in Task {}

// Tasks marked sticky at plan time. Bound to whichever Agent first
// claims them; subsequent transitions on this task and any sticky
// descendants must come from the same agent. Reclaim strips the
// binding (the model collapses Agent and Context — same atom).
sig Sticky in Task {}

// NoCycle: derived from `plan[t]`'s `t not in t.deps` precondition plus
// Task immutability (deps locked at creation; no transition mutates them).
// Self-loops are refused at plan time (plan.py exit 11) and longer cycles
// are unreachable because no later transition can add an edge into a Task.
fact NoCycle    { no t: Task | t in t.^deps }
fact NoSelfDep  { all t: Task | t not in t.deps }
fact NoSelfParent { all t: Task | t.parent != t }

// An Attempt represents one execution of `executing.py`.
// Each attempt fires exactly once: startClaim → (commitClaim | abortClaim).
sig Attempt {
  who:  one Agent,
  task: one Task
}

// ===== Mutable state =====
var sig Pending        in Task    {}
var sig HasReport      in Task    {}
var sig HasProof       in Task    {}
var sig OpenAttempts   in Attempt {}
var sig Committed      in Attempt {}
// Set of tasks whose verifier has run and passed for the current report.
// Cleared if/when the task transitions back to PNew (e.g. via reclaim).
var sig VerifierPassed in Task    {}
// Tasks that were cancelled by a supervisor/planner rather than completed
// or rejected by their worker. Audit-only: cancellation maps to PRejected
// at the phase level. The agent who cancelled is recorded in the cancelled
// TaskStatus's attributes via the runtime; the formal sig flags the fact.
var sig Cancelled      in Task    {}

// ===== Init =====
fact Init {
  no Pending
  no HasReport
  no HasProof
  no OpenAttempts
  no Committed
  no VerifierPassed
  no Cancelled
  all t: Task | no t.phase and no t.owner
}

// ===== Static invariants (enforced at every state) =====
fact PhaseIffPending      { always all t: Task | one t.phase <=> t in Pending }
fact OwnerImpliesPending  { always all t: Task | one t.owner => t in Pending }
fact OwnerOnlyAfterClaim  { always all t: Task | one t.owner => t.phase != PNew }
fact ProofOnlyTerminal    { always all t: Task | t in HasProof => t.phase in PDone + PRejected }
fact CommittedNotOpen     { always (no (OpenAttempts & Committed)) }
// VerifierPassed only meaningful while task is in working/done — a New
// task hasn't been worked yet, so it can't have a verifier verdict.
fact VerifierPassedImpliesNotNew {
  always all t: Task | t in VerifierPassed => t.phase in PWorking + PDone + PRejected
}
// Cancelled is monotonic and terminal: once cancelled, always cancelled,
// and the phase is PRejected.
fact CancelledImpliesRejected {
  always all t: Task | t in Cancelled => isRejected[t]
}
fact CancelledIsAbsorbing {
  always all t: Task | t in Cancelled => after t in Cancelled
}

// ===== Phase predicates =====
pred isNew      [t: Task] { t.phase = PNew }
pred isWorking  [t: Task] { t.phase = PWorking }
pred isDone     [t: Task] { t.phase = PDone }
pred isRejected [t: Task] { t.phase = PRejected }
pred isTerminal [t: Task] { isDone[t] or isRejected[t] }

// ===== Frame: every Task except `t` keeps phase/owner =====
pred frameOtherTasks[t: Task] {
  all u: Task - t | u.phase' = u.phase and u.owner' = u.owner
}
pred frameAllTasks {
  all u: Task | u.phase' = u.phase and u.owner' = u.owner
}

// ===== Transitions =====

pred plan[t: Task] {
  // Preconditions
  t not in Pending
  t not in t.deps                            // self-loop refused (plan.py exit 11)
  all d: t.deps | d in Pending
  // Parent (if any) must be on the board too
  no t.parent or t.parent in Pending
  // Effect
  Pending'         = Pending + t
  HasReport'       = HasReport
  HasProof'        = HasProof
  OpenAttempts'    = OpenAttempts
  Committed'       = Committed
  VerifierPassed'  = VerifierPassed
  Cancelled'       = Cancelled
  t.phase'  = PNew
  no t.owner'
  frameOtherTasks[t]
}

// Phase 1 of claim: agent reads "new" and appends a working-status event.
// Models the read-then-append window. Multiple agents may startClaim on
// the same task in successive steps before any commit fires.
pred startClaim[a: Agent, t: Task, x: Attempt] {
  // Preconditions
  isNew[t]
  all d: t.deps | isDone[d]                 // dep gate (next.py)
  x.who  = a
  x.task = t
  x not in OpenAttempts
  x not in Committed
  // Effect
  OpenAttempts'    = OpenAttempts + x
  Pending'         = Pending
  HasReport'       = HasReport
  HasProof'        = HasProof
  Committed'       = Committed
  VerifierPassed'  = VerifierPassed
  Cancelled'       = Cancelled
  frameAllTasks
}

// Phase 2a: x's append becomes the latest tip. Task transitions New → Working.
// The precondition `isNew` enforces that any earlier commit on the same task
// has already moved the phase, so a second commit cannot fire.
pred commitClaim[x: Attempt] {
  x in OpenAttempts
  isNew[x.task]                              // still new — we won the race
  // Sticky chain coherence — symmetric: any sticky task in this task's
  // connected sticky chain (ancestors via parent/deps, descendants via
  // their parent/deps reaching us) that already has an owner must share
  // our owner. The descendants check matters because a sticky child
  // could in principle have been claimed before its sticky parent;
  // commitClaim of the parent must reject if the child is bound to a
  // different agent.
  x.task in Sticky implies (
    (all anc: (x.task.^parent + x.task.deps) & Sticky |
        (some anc.owner) implies anc.owner = x.who)
    and
    (all desc: Sticky |
        x.task in (desc.^parent + desc.deps)
        and (some desc.owner)
        implies desc.owner = x.who)
  )
  OpenAttempts'    = OpenAttempts - x
  Committed'       = Committed + x
  Pending'         = Pending
  HasReport'       = HasReport
  HasProof'        = HasProof
  VerifierPassed'  = VerifierPassed
  Cancelled'       = Cancelled
  x.task.phase' = PWorking
  x.task.owner' = x.who
  frameOtherTasks[x.task]
}

// Phase 2b: x's append did NOT become the latest tip. Hashharness's
// `chain_predecessor` check on `prevStatus` rejects the append at write
// time ('head moved'); the agent backs off without proceeding.
pred abortClaim[x: Attempt] {
  x in OpenAttempts
  not isNew[x.task]                          // someone else already committed
  OpenAttempts'    = OpenAttempts - x
  Committed'       = Committed
  Pending'         = Pending
  HasReport'       = HasReport
  HasProof'        = HasProof
  VerifierPassed'  = VerifierPassed
  Cancelled'       = Cancelled
  frameAllTasks
}

pred report[a: Agent, t: Task] {
  isWorking[t]
  t.owner = a                                // only the owner files reports
  HasReport'       = HasReport + t
  Pending'         = Pending
  HasProof'        = HasProof
  OpenAttempts'    = OpenAttempts
  Committed'       = Committed
  Cancelled'       = Cancelled
  // A new report invalidates any earlier verifier verdict for this
  // task — the verifier must re-run against the fresh report.
  VerifierPassed'  = VerifierPassed - t
  frameAllTasks
}

// Verifier ran against the latest TaskReport and passed. Mirrors the
// `pm finished` invocation of the verifier script, isolated as its own
// transition so the model can reason about "was the gate run?".
pred verify[t: Task] {
  isWorking[t]
  t in HasReport
  // Effect
  VerifierPassed'  = VerifierPassed + t
  Pending'         = Pending
  HasReport'       = HasReport
  HasProof'        = HasProof
  OpenAttempts'    = OpenAttempts
  Committed'       = Committed
  Cancelled'       = Cancelled
  frameAllTasks
}

// Cancellation: any agent (supervisor, planner) can cancel a non-terminal
// task. NO owner match required — that's the whole point. The cancel
// synthesizes a TaskReport (carrying the cancel reason) so the
// ProofRequiredForTerminal invariant continues to hold by construction.
// In the runtime, `pm cancel` writes a TaskReport with the reason then
// the rejected TaskStatus linking proof to it.
pred cancel[t: Task, by: Agent] {
  // Preconditions
  t in Pending
  not isTerminal[t]
  // Effect
  Cancelled'       = Cancelled + t
  HasReport'       = HasReport + t
  HasProof'        = HasProof + t
  Pending'         = Pending
  OpenAttempts'    = OpenAttempts
  Committed'       = Committed
  VerifierPassed'  = VerifierPassed - t
  t.phase'         = PRejected
  no t.owner'
  frameOtherTasks[t]
}

pred finish[a: Agent, t: Task, terminal: Phase] {
  terminal in PDone + PRejected
  isWorking[t]
  t.owner = a
  t in HasReport                             // PROOF MANDATORY (finished.py:50)
  // Verifier gate: a task with a verifier requirement cannot be finished
  // as `done` until the verifier has run and passed for the current report.
  // `rejected` doesn't need verification — rejecting work doesn't claim
  // success, so the verifier isn't load-bearing.
  (terminal = PRejected) or (t not in RequiresVerifier) or (t in VerifierPassed)
  HasProof'        = HasProof + t
  Pending'         = Pending
  HasReport'       = HasReport
  OpenAttempts'    = OpenAttempts
  Committed'       = Committed
  VerifierPassed'  = VerifierPassed
  Cancelled'       = Cancelled
  t.phase' = terminal
  t.owner' = t.owner
  frameOtherTasks[t]
}

pred stutter {
  Pending' = Pending and HasReport' = HasReport and HasProof' = HasProof
  and OpenAttempts' = OpenAttempts and Committed' = Committed
  and VerifierPassed' = VerifierPassed and Cancelled' = Cancelled
  and frameAllTasks
}

fact Transitions {
  always (
    stutter
    or (some t: Task | plan[t])
    or (some a: Agent, t: Task, x: Attempt | startClaim[a, t, x])
    or (some x: Attempt | commitClaim[x])
    or (some x: Attempt | abortClaim[x])
    or (some a: Agent, t: Task | report[a, t])
    or (some t: Task | verify[t])
    or (some a: Agent, t: Task, p: Phase | finish[a, t, p])
    or (some t: Task, by: Agent | cancel[t, by])
  )
}

// Liveness assumption: open attempts must eventually resolve (commit | abort).
// This makes the recheck protocol load-bearing — without it, races could
// leave attempts orphaned and the system would appear to deadlock.
fact AttemptProgress {
  all x: Attempt | always (x in OpenAttempts => eventually x not in OpenAttempts)
}

// ===== Safety assertions =====

// Holds in this file's transition set (worker loop + cancel). Replan
// is a separate supervisor operation modeled in planning_replan.als,
// where `replan_reset[t]` intentionally transitions
// `Done`/`Rejected → New`. A combined-model trace including replan
// would NOT satisfy this assertion — that's by design (the whole
// point of replan). See system-models/reports/alloy-cross-model-
// soundness.md for the cross-module audit.
assert TerminalAbsorbing {
  always all t: Task |
    (isDone[t]     => always isDone[t])
    and (isRejected[t] => always isRejected[t])
}
check TerminalAbsorbing for 4 but 8 steps

assert ProofRequiredForTerminal {
  always all t: Task | isTerminal[t] => t in HasProof
}
check ProofRequiredForTerminal for 4 but 8 steps

assert SingleOwner {
  always all t: Task | lone t.owner
}
check SingleOwner for 4 but 8 steps

// At most one attempt per task ever ends up Committed — this is the core
// race-safety property: even if multiple agents start claiming the same
// task, only one's append becomes the latest tip.
assert NoDoubleCommit {
  always all t: Task | lone (Committed & task.t)
}
check NoDoubleCommit for 4 but 8 steps

// At the step a task transitions New → Working, every dep is already Done.
assert DependenciesDoneAtClaim {
  always all t: Task |
    (isNew[t] and after isWorking[t]) =>
      all d: t.deps | isDone[d]
}
check DependenciesDoneAtClaim for 4 but 10 steps

// Owner can only become set during a commit, and only persists until terminal.
assert OwnerStableThroughWorkingPhase {
  always all t: Task |
    isWorking[t] => one t.owner
}
check OwnerStableThroughWorkingPhase for 4 but 8 steps

// THE VERIFIER GATE: any task that requires a verifier and is currently
// Done must have its verifier registered as Passed. Rejection is exempt
// (rejecting unverifiable work is allowed).
assert VerifierGateOnDone {
  always all t: Task |
    (isDone[t] and t in RequiresVerifier) => t in VerifierPassed
}
check VerifierGateOnDone for 4 but 10 steps

// The verifier can only run on a task that is currently Working AND has
// a TaskReport — there's nothing to verify before a report exists.
assert VerifyRequiresWorkingReport {
  always all t: Task |
    (t not in VerifierPassed and after (t in VerifierPassed)) =>
      (isWorking[t] and t in HasReport)
}
check VerifyRequiresWorkingReport for 4 but 10 steps

// ===== Sticky-context safety =====

// Sticky chain coherence: at any moment, every sticky task with an
// owner shares that owner with every sticky ancestor (parent / dep) that
// also has an owner. No two distinct contexts can both be live within
// one sticky chain.
assert StickyChainCoherence {
  always all t: Task |
    (t in Sticky and one t.owner) =>
      all anc: (t.^parent + t.deps) & Sticky |
        (some anc.owner) =>
          anc.owner = t.owner
}
check StickyChainCoherence for 4 but 10 steps

// A new sticky binding can only happen on a task that just transitioned
// to PWorking via commitClaim — i.e., the binding is established at
// claim time, not retroactively, and only on the task being claimed.
assert StickyBindingOnlyAtClaim {
  always all t: Task |
    (t in Sticky and no t.owner and after (some t.owner)) =>
      after isWorking[t]
}
check StickyBindingOnlyAtClaim for 4 but 10 steps

// ===== Cancellation safety =====

// Cancelled tasks are always in PRejected (and remain so — terminal-absorbing).
assert CancelledIsRejectedTerminal {
  always all t: Task | t in Cancelled => (isRejected[t] and always isRejected[t])
}
check CancelledIsRejectedTerminal for 4 but 10 steps

// Cancellation can only fire on non-terminal tasks (you can't cancel a
// finished task — keep the terminal-absorbing rule honest).
assert CancelOnlyOnNonTerminal {
  always all t: Task |
    (t not in Cancelled and after (t in Cancelled)) =>
      not isTerminal[t]
}
check CancelOnlyOnNonTerminal for 4 but 10 steps

// Even cancelled tasks satisfy the proof-of-work gate by construction —
// cancel synthesizes a TaskReport (carrying the cancel reason) and adds
// to HasProof at the same step. So ProofRequiredForTerminal continues to
// hold for the union of done/rejected, including cancelled rejections.
assert CancelledHasProof {
  always all t: Task | t in Cancelled => t in HasProof
}
check CancelledHasProof for 4 but 10 steps

// ===== Liveness scenarios =====

run HappyPath {
  some t: Task, a: Agent | eventually (
    isNew[t] and eventually (
      isWorking[t] and t.owner = a and eventually (
        t in HasReport and eventually isDone[t]
      )
    )
  )
} for exactly 1 Task, exactly 1 Agent, exactly 1 Attempt, 8 steps

// Verifier-gated happy path: task requires a verifier, runs through
// claim → report → verify → finish, ends in Done with VerifierPassed.
run VerifiedHappyPath {
  some t: Task, a: Agent |
    t in RequiresVerifier and eventually (
      isWorking[t] and t.owner = a and eventually (
        t in HasReport and eventually (
          t in VerifierPassed and eventually isDone[t]
        )
      )
    )
} for exactly 1 Task, exactly 1 Agent, exactly 1 Attempt, 10 steps

// Negative scenario — verifier-required task reaches Done WITHOUT
// VerifierPassed. Should be UNSAT under the model: the finish gate
// blocks it.
run TryFinishWithoutVerifier {
  some t: Task | t in RequiresVerifier and eventually (
    isDone[t] and t not in VerifierPassed
  )
} for exactly 1 Task, exactly 1 Agent, exactly 1 Attempt, 10 steps
expect 0

// A non-owner agent can cancel a working task. Demonstrates that cancel
// is reachable without satisfying the owner-match constraint that finish
// requires. Witnesses gap #6's resolution.
run CancelByNonOwner {
  some t: Task, disj a1, a2: Agent |
    eventually (
      isWorking[t] and t.owner = a1 and eventually (
        isRejected[t] and t in Cancelled and no t.owner
        // a2 invoked cancel; a1 was the owner; cancel succeeded anyway
      )
    )
} for exactly 1 Task, exactly 2 Agent, exactly 1 Attempt, 10 steps

// Cancelling a fresh (never-claimed) task. Useful to confirm cancel
// works without needing a worker.
run CancelFreshTask {
  some t: Task, a: Agent |
    eventually (
      isNew[t] and eventually (isRejected[t] and t in Cancelled)
    )
} for exactly 1 Task, exactly 1 Agent, exactly 1 Attempt, 8 steps

// ===== Sticky-context scenarios =====

// Sticky-chain happy path: parent and child are both sticky; one agent
// claims both in succession.
run StickyChainSameAgent {
  some disj p, c: Task, a: Agent |
    p in Sticky and c in Sticky and c.parent = p and
    eventually (isWorking[p] and p.owner = a and eventually (
      isWorking[c] and c.owner = a
    ))
} for exactly 2 Task, exactly 1 Agent, exactly 2 Attempt, 10 steps

// Sticky-chain reject: agent B cannot claim a sticky child after agent A
// claimed the sticky parent. Should be UNSAT — the model rejects the
// claim because of the StickyChainCoherence-required precondition.
run TryStickyChainCrossAgent {
  some disj p, c: Task, disj a, b: Agent |
    p in Sticky and c in Sticky and c.parent = p and
    eventually (isWorking[p] and p.owner = a and eventually (
      isWorking[c] and c.owner = b
    ))
} for exactly 2 Task, exactly 2 Agent, exactly 2 Attempt, 10 steps
expect 0

// Negative scenario — cancelling a task already in PDone. Should be
// UNSAT (terminal-absorbing).
run TryCancelDoneTask {
  some t: Task | eventually (
    isDone[t] and after (t in Cancelled and isRejected[t])
  )
} for exactly 1 Task, exactly 1 Agent, exactly 1 Attempt, 10 steps
expect 0

// Two agents both attempt to claim the same task. Exactly one becomes Owner;
// the other's attempt aborts. Demonstrates the race window resolves safely.
run ClaimRace {
  some t: Task, disj a1, a2: Agent, disj x1, x2: Attempt |
    x1.who = a1 and x1.task = t
    and x2.who = a2 and x2.task = t
    and eventually (
      x1 in Committed and x2 not in Committed
      and t.owner = a1
      and x1 not in OpenAttempts
      and x2 not in OpenAttempts
    )
} for exactly 1 Task, exactly 2 Agent, exactly 2 Attempt, 8 steps

// Two agents working on independent tasks at the same time.
run ParallelWork {
  some disj t1, t2: Task, disj a1, a2: Agent |
    no t1.deps and no t2.deps
    and eventually (
      isWorking[t1] and t1.owner = a1
      and isWorking[t2] and t2.owner = a2
    )
} for exactly 2 Task, exactly 2 Agent, exactly 2 Attempt, 10 steps

// Dep gate works: t2 depends on t1 and only becomes claimable after t1 done.
run DepGating {
  some disj t1, t2: Task, a: Agent |
    t2.deps = t1 and no t1.deps
    and eventually (isDone[t1] and eventually (isWorking[t2] and t2.owner = a))
} for exactly 2 Task, exactly 1 Agent, exactly 2 Attempt, 12 steps

// Rejected branch: a task is finished as rejected (still requires a report).
run RejectionPath {
  some t: Task, a: Agent | eventually (
    isWorking[t] and t.owner = a and eventually (
      t in HasReport and eventually (isRejected[t] and t in HasProof)
    )
  )
} for exactly 1 Task, exactly 1 Agent, exactly 1 Attempt, 8 steps


// ===== Gap assertions (documented system-level holes) =====

// Historical note: slug uniqueness is intentionally NOT modeled in this
// core protocol file because it lives in the separate planning_plan_race.als
// model. That companion model captures the plan-time race explicitly and
// verifies the current structural fix:
//
//   Task.text = "task:<queue>/<slug>"
//
// so duplicate slug creates collide on text_sha256 inside hashharness.
//
// If slug semantics ever need to move back into the core model, extend Task
// with `slug: one SlugId` and add a property "no two pending tasks share a
// slug".
//
// Sketch:
// sig SlugId {}
// sig Task { ..., slug: one SlugId }
// assert UniqueSlugInQueue {
//   always all disj t1, t2: Pending | t1.slug != t2.slug
// }
// check UniqueSlugInQueue for 4 but 6 steps    -- expected: COUNTEREXAMPLE
