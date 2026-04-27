module planning_plan_race

/*
  Models the slug-uniqueness race window in plan.py.

  Maps to: skills/pm/scripts/plan.py (lines 36–48)

  THE BUG
  -------
  plan.py performs:
      slug = ...
      existing = find_task_by_slug(queue, slug)
      if existing: refuse
      task = create_task(...)
  with NO atomic guard between the existence check and the create. Two
  parallel plan() invocations with the same slug both pass the check
  (both see no existing task) and both proceed to create_task. Result:
  two Pending Tasks share a slug.

  This file EXISTS TO PRODUCE A COUNTEREXAMPLE TRACE that demonstrates
  the race. The reconciliation report (Discrepancy 2) classifies it as
  a Conflict; this model converts the prose claim into a formal proof.

  Two-phase plan:
      startPlan(a, t, p)   ~ plan.py reads find_task_by_slug → None
      commitPlan(p)        ~ plan.py calls create_task (NO RECHECK)
      abortPlan(p)         ~ optional honest-loser path (not in current code)

  A `commitPlanSafe` predicate is included for contrast — it adds the
  recheck that would close the bug. The Transitions fact uses the BUGGY
  commitPlan so the safety check produces a counterexample.
*/

sig Slug  {}
sig Agent {}

sig Task {
  slug: one Slug
}

sig PlanAttempt {
  pwho:  one Agent,
  ptask: one Task
}

var sig Pending   in Task         {}
var sig OpenPlans in PlanAttempt  {}

fact Init {
  no Pending
  no OpenPlans
}

// Each PlanAttempt is for a specific (agent, task) pair.
// Distinct attempts target distinct tasks (one create per attempt).
fact AttemptsAreDistinct {
  all disj p1, p2: PlanAttempt | p1.ptask != p2.ptask
}

// ===== Transitions =====

// Phase 1: agent reads "no Pending task has this slug" and records intent.
pred startPlan[a: Agent, t: Task, p: PlanAttempt] {
  t not in Pending
  p.pwho = a and p.ptask = t
  p not in OpenPlans
  // Mirror of `find_task_by_slug → None`:
  no other: Pending | other.slug = t.slug
  // Effect
  OpenPlans' = OpenPlans + p
  Pending'   = Pending
}

// Phase 2 (BUGGY): commit unconditionally. Mirrors the OLD plan.py before
// the fix — kept here for contrast and to reproduce the historical
// counterexample.
pred commitPlanBuggy[p: PlanAttempt] {
  p in OpenPlans
  Pending'   = Pending + p.ptask
  OpenPlans' = OpenPlans - p
}

// Phase 2 (SAFE — landed in production): the commit only succeeds if the
// slug is still unclaimed. In code, this is enforced structurally because
// `Task.text` is now `task:<queue>/<slug>` and hashharness rejects
// duplicate `text_sha256` (so two parallel commits with the same slug
// can never both succeed — the second gets a SlugTaken exception). The
// `abortPlan` predicate models the loser's path.
pred commitPlan[p: PlanAttempt] {
  p in OpenPlans
  no other: Pending | other.slug = p.ptask.slug      // structural slug-key collision
  Pending'   = Pending + p.ptask
  OpenPlans' = OpenPlans - p
}

pred abortPlan[p: PlanAttempt] {
  p in OpenPlans
  some other: Pending | other.slug = p.ptask.slug    // race lost — slug is taken
  Pending'   = Pending
  OpenPlans' = OpenPlans - p
}

pred stutter {
  Pending' = Pending and OpenPlans' = OpenPlans
}

fact Transitions {
  always (
    stutter
    or (some a: Agent, t: Task, p: PlanAttempt | startPlan[a, t, p])
    or (some p: PlanAttempt | commitPlan[p])
    or (some p: PlanAttempt | abortPlan[p])
  )
}

// To reproduce the historical bug (counterexample to UniqueSlugInQueue),
// swap `commitPlan[p]` for `commitPlanBuggy[p]` in the Transitions fact
// above and remove the `abortPlan[p]` line.

// Liveness: every started plan must eventually commit (otherwise the
// model could trivially avoid the bug by stuttering forever).
fact PlanProgress {
  all p: PlanAttempt | always (p in OpenPlans => eventually p not in OpenPlans)
}

// ===== Safety goal =====
// At every moment, no two pending tasks share a slug.
// EXPECTED RESULT under the buggy commitPlan: COUNTEREXAMPLE.
assert UniqueSlugInQueue {
  always all disj t1, t2: Task |
    (t1 in Pending and t2 in Pending) => t1.slug != t2.slug
}
check UniqueSlugInQueue
  for exactly 2 Task, exactly 1 Slug, exactly 2 Agent, exactly 2 PlanAttempt, 6 steps

// ===== Scenario witnessing the bug =====
run TwoConcurrentPlansSameSlug {
  some disj t1, t2: Task, disj p1, p2: PlanAttempt |
    t1.slug = t2.slug
    and p1.ptask = t1 and p2.ptask = t2
    and eventually (t1 in Pending and t2 in Pending)
} for exactly 2 Task, exactly 1 Slug, exactly 2 Agent, exactly 2 PlanAttempt, 6 steps
