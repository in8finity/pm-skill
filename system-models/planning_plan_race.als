module planning_plan_race

/*
  Models the race between concurrent `pm plan` invocations against the
  SAME (queue, slug). Two safety properties are tracked separately
  because the implementation enforces them DIFFERENTLY in hashharness:

  (A) UniqueSlugInQueue — at most one Task record per (queue, slug).
      Enforced STRUCTURALLY: Task.text = "task:<queue>/<slug>", so
      hashharness's text_sha256 primary-key CAS rejects duplicate
      Task creates. Expected solver verdict: PASSES.

  (B) NewStatusUnique — for each Task, AT MOST ONE TaskStatus with
      attributes.status = "new" ever exists on its per-task TaskStatus
      chain. Enforced by hashharness's chain_predecessor CAS on
      TaskStatus.prevStatus: a write with no prevStatus link is rejected
      if the chain already has a head. Expected solver verdict: PASSES
      under the safe append (which is what's now in plan.py).

  HISTORICAL PRE-FIX BUG — captured here for regression awareness:

      The original plan.py heal path called append_status(..., "new")
      WITHOUT pinning prev=None, and append_status performed an inner
      latest_status read to decide what prevStatus to link. Under
      concurrency that inner read could land AFTER a racing peer's
      genesis write, so plan.py would chain a SECOND TaskStatus(new)
      onto the peer's genesis. Chain ends up with two consecutive
      status=new records (chain_predecessor satisfied, but the state-
      machine invariant violated). g9_slug_race observed this in ~10%
      of races.

      The fix landed in plan.py + store.py: append_status grew a
      `force_no_prev: bool` param that skips the inner read and submits
      with no prevStatus link. The backend's chain_predecessor CAS then
      uniformly rejects the loser as HeadMoved, which plan.py catches
      and turns into a clean exit-4 slug-taken refusal.

  Switch the Transitions fact below from `appendNewSafe` to
  `appendNewRacy` to reproduce the historical counterexample.
*/

sig Slug   {}
sig Agent  {}

sig Task {
  slug: one Slug
}

// One NewStatus atom represents one TaskStatus record with
// attributes.status = "new". The invariant is that at most one such
// record ever attaches to any given Task. forTask is the Task this
// status was written against.
sig NewStatus {
  forTask: one Task
}

sig PlanAttempt {
  pwho:  one Agent,
  ptask: one Task
}

// ----- Time-varying state -----
var sig Created     in Task       {}   // Task record committed
var sig EmittedNew  in NewStatus  {}   // TaskStatus(new) appended
var sig OpenPlans   in PlanAttempt{}

fact Init {
  no Created
  no EmittedNew
  no OpenPlans
}

// Distinct PlanAttempts target distinct Task atoms — i.e. distinct
// in-flight create attempts. Different Task atoms can share a Slug
// (Slug is exact=1 in the check scopes below), so two attempts can
// race for the same slug.
fact AttemptsAreDistinct {
  all disj p1, p2: PlanAttempt | p1.ptask != p2.ptask
}

// ===== Transitions =====

// Phase 1: agent reads find_task_by_slug -> None and records intent.
pred startPlan[a: Agent, t: Task, p: PlanAttempt] {
  t not in Created
  p.pwho = a and p.ptask = t
  p not in OpenPlans
  no other: Created | other.slug = t.slug
  OpenPlans'   = OpenPlans + p
  Created'     = Created
  EmittedNew'  = EmittedNew
}

// Phase 2: create_item for the Task. text_sha256 PK CAS is atomic
// per slug — only ONE Created task per slug ever. The agent stays in
// OpenPlans because it still owes a genesis append.
pred createTask[p: PlanAttempt] {
  p in OpenPlans
  no other: Created | other.slug = p.ptask.slug
  Created'     = Created + p.ptask
  OpenPlans'   = OpenPlans
  EmittedNew'  = EmittedNew
}

// Phase 3 (SAFE — production behaviour after the force_no_prev fix).
// The append is guarded by "no NewStatus exists yet for this Task" —
// modelling the backend's chain_predecessor CAS on a write with no
// prevStatus link (head must be empty for the write to land).
pred appendNewSafe[p: PlanAttempt, n: NewStatus] {
  p in OpenPlans
  p.ptask in Created
  no other: EmittedNew | other.forTask = p.ptask
  n.forTask = p.ptask
  n not in EmittedNew
  Created'     = Created
  OpenPlans'   = OpenPlans - p
  EmittedNew'  = EmittedNew + n
}

// Phase 3 (RACY — historical pre-fix behaviour). Append without the
// CAS guard. Modelled here so swapping `appendNewSafe` for
// `appendNewRacy` in the Transitions fact reproduces the counterexample
// to NewStatusUnique. Kept for regression awareness.
pred appendNewRacy[p: PlanAttempt, n: NewStatus] {
  p in OpenPlans
  p.ptask in Created
  n.forTask = p.ptask
  n not in EmittedNew
  // INTENTIONALLY MISSING:
  //   no other: EmittedNew | other.forTask = p.ptask
  Created'     = Created
  OpenPlans'   = OpenPlans - p
  EmittedNew'  = EmittedNew + n
}

// Self-heal branch in plan.py (lines 67-89). The agent's intended Task
// atom didn't get created — a peer with the same slug won createTask.
// The peer's Created Task has no NewStatus yet, so plan.py "heals" by
// appending a NewStatus to the PEER's Task. With force_no_prev=True
// the append carries the chain-predecessor CAS (modelled by the
// `no EmittedNew for peer` guard); without it, the heal is racy.
pred healPlanSafe[p: PlanAttempt, n: NewStatus] {
  p in OpenPlans
  some peer: Created {
    peer.slug = p.ptask.slug
    peer != p.ptask
    no other: EmittedNew | other.forTask = peer
    n.forTask = peer
  }
  n not in EmittedNew
  Created'     = Created
  OpenPlans'   = OpenPlans - p
  EmittedNew'  = EmittedNew + n
}

// abortPlan: peer already has a Created task AND a NewStatus — slug-
// taken refusal (plan.py exit 4). Models the honest losing path.
pred abortPlan[p: PlanAttempt] {
  p in OpenPlans
  some peer: Created {
    peer.slug = p.ptask.slug
    peer != p.ptask
    some other: EmittedNew | other.forTask = peer
  }
  Created'     = Created
  OpenPlans'   = OpenPlans - p
  EmittedNew'  = EmittedNew
}

pred stutter {
  Created'     = Created
  EmittedNew'  = EmittedNew
  OpenPlans'   = OpenPlans
}

// USES `appendNewSafe` + `healPlanSafe` — matches the live code after
// the force_no_prev fix. Both safety assertions PASS.
//
// To reproduce the historical pre-fix counterexample, swap
//   appendNewSafe -> appendNewRacy
//   healPlanSafe  -> a `healPlanRacy` variant with the guard removed
// in this fact. The NewStatusUnique check will then produce a witness.
fact Transitions {
  always (
    stutter
    or (some a: Agent, t: Task, p: PlanAttempt | startPlan[a, t, p])
    or (some p: PlanAttempt | createTask[p])
    or (some p: PlanAttempt, n: NewStatus | appendNewSafe[p, n])
    or (some p: PlanAttempt, n: NewStatus | healPlanSafe[p, n])
    or (some p: PlanAttempt | abortPlan[p])
  )
}

// Liveness: every in-flight plan eventually leaves OpenPlans.
fact PlanProgress {
  all p: PlanAttempt | always (p in OpenPlans => eventually p not in OpenPlans)
}

// ===== Safety goals =====

// (A) Slug uniqueness on Task records — structural via text_sha256 PK.
// EXPECTED: PASSES.
assert UniqueSlugInQueue {
  always all disj t1, t2: Created | t1.slug != t2.slug
}
check UniqueSlugInQueue
  for exactly 2 Task, exactly 1 Slug, exactly 2 Agent,
      exactly 2 PlanAttempt, exactly 2 NewStatus, 10 steps

// (B) At most one TaskStatus(status=new) per Task. With appendNewSafe /
// healPlanSafe in Transitions, EXPECTED: PASSES. Empirically verified
// by tests/integration/test_golden.py::g9b_genesis_chain_singleton —
// 40/40 post-fix races produce chain length 1.
assert NewStatusUnique {
  always all disj n1, n2: EmittedNew | n1.forTask != n2.forTask
}
check NewStatusUnique
  for exactly 2 Task, exactly 1 Slug, exactly 2 Agent,
      exactly 2 PlanAttempt, exactly 2 NewStatus, 10 steps

// ===== Scenarios =====
// Slug-taken refusal: two attempts, one wins create + appends genesis,
// the other aborts because a peer already has a Created+EmittedNew.
run WinnerWinsLoserAborts {
  some disj p1, p2: PlanAttempt, n1: NewStatus |
    eventually (n1 in EmittedNew and p1.ptask in Created)
    and eventually (p2 not in OpenPlans)
} for exactly 2 Task, exactly 1 Slug, exactly 2 Agent,
      exactly 2 PlanAttempt, exactly 2 NewStatus, 10 steps
