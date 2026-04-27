/*
  planning_plan_race.dfy — Dafny port of planning_plan_race.als

  Verifies that the structural slug-uniqueness fix (Task.text =
  "task:<queue>/<slug>") guarantees no two Pending tasks share a slug,
  for traces of ANY length.

  The Alloy model also has a `commitPlanBuggy` predicate that reproduces
  the historical counterexample. In Dafny we keep the buggy variant as a
  comment — Dafny doesn't generate counterexamples, so reproducing the
  bug means temporarily replacing the active commit predicate.

  Maps to:
    skills/pm/scripts/store.py      task_identity_text, create_task, SlugTaken
    skills/pm/scripts/plan.py       SlugTaken catch → exit 4
*/

// ============================================================
// State + Action
// ============================================================

// taskSlug is immutable — each Task atom carries one Slug for the
// entire trace. Encoded as a parameter (not part of State) so traces
// share a single, fixed mapping.
datatype State = State(
  pending:   set<int>,
  openPlans: map<int, int>     // attemptId -> taskId
)

datatype Action =
  | StartPlan(p: int, t: int)
  | CommitPlan(p: int)
  | AbortPlan(p: int)
  | Stutter

// ============================================================
// Init + Inv
// ============================================================

ghost predicate Init(s: State) {
  s.pending == {} &&
  s.openPlans == map[]
}

// SAFETY: at any moment, no two pending tasks share a slug.
ghost predicate UniqueSlug(s: State, taskSlug: map<int, int>) {
  forall t1, t2 ::
    t1 in s.pending && t2 in s.pending && t1 != t2 &&
    t1 in taskSlug && t2 in taskSlug
    ==> taskSlug[t1] != taskSlug[t2]
}

// Wellformedness: open plans target tasks not already pending.
// (Mirrors plan.py's pre-check refusing to claim a slug already on the board.)
ghost predicate Wellformed(s: State, taskSlug: map<int, int>) {
  // every open-plan target task has a slug
  (forall p :: p in s.openPlans ==> s.openPlans[p] in taskSlug) &&
  // every pending task has a slug
  (forall t :: t in s.pending ==> t in taskSlug)
}

// ============================================================
// Per-action transitions
// ============================================================

ghost predicate StepStartPlan(s: State, s': State, taskSlug: map<int, int>, p: int, t: int)
{
  t in taskSlug &&
  t !in s.pending &&
  p !in s.openPlans &&
  // saw no Pending task with same slug at read time:
  (forall t' :: t' in s.pending && t' in taskSlug ==> taskSlug[t'] != taskSlug[t]) &&
  s' == s.(openPlans := s.openPlans[p := t])
}

// Active (SAFE) commit — mirrors the production fix where Task.text =
// "task:<queue>/<slug>" makes hashharness reject duplicate text_sha256.
ghost predicate StepCommitPlan(s: State, s': State, taskSlug: map<int, int>, p: int)
{
  p in s.openPlans &&
  var t := s.openPlans[p];
  t in taskSlug &&
  // structural recheck — refuses if slug now taken
  (forall t' :: t' in s.pending && t' in taskSlug ==> taskSlug[t'] != taskSlug[t]) &&
  s' == s.(
    pending   := s.pending + {t},
    openPlans := s.openPlans - {p}
  )
}

ghost predicate StepAbortPlan(s: State, s': State, taskSlug: map<int, int>, p: int)
{
  p in s.openPlans &&
  var t := s.openPlans[p];
  t in taskSlug &&
  // race lost: some pending task already claimed the slug
  (exists t' :: t' in s.pending && t' in taskSlug && taskSlug[t'] == taskSlug[t]) &&
  s' == s.(openPlans := s.openPlans - {p})
}

ghost predicate Step(s: State, s': State, taskSlug: map<int, int>, action: Action)
{
  match action {
    case StartPlan(p, t) => StepStartPlan(s, s', taskSlug, p, t)
    case CommitPlan(p)   => StepCommitPlan(s, s', taskSlug, p)
    case AbortPlan(p)    => StepAbortPlan(s, s', taskSlug, p)
    case Stutter         => s' == s
  }
}

// ============================================================
// Trace + inductive proof
// ============================================================

ghost predicate ValidTrace(trace: seq<State>, actions: seq<Action>, taskSlug: map<int, int>)
{
  |trace| >= 1 &&
  |actions| == |trace| - 1 &&
  Init(trace[0]) &&
  forall i :: 0 <= i < |actions| ==> Step(trace[i], trace[i+1], taskSlug, actions[i])
}

lemma InitImpliesUnique(s: State, taskSlug: map<int, int>)
  requires Init(s)
  ensures  UniqueSlug(s, taskSlug)
{ }

lemma InitImpliesWellformed(s: State, taskSlug: map<int, int>)
  requires Init(s)
  ensures  Wellformed(s, taskSlug)
{ }

lemma StepPreservesUnique(s: State, s': State, taskSlug: map<int, int>, action: Action)
  requires UniqueSlug(s, taskSlug)
  requires Wellformed(s, taskSlug)
  requires Step(s, s', taskSlug, action)
  ensures  UniqueSlug(s', taskSlug)
  ensures  Wellformed(s', taskSlug)
{
  // The CommitPlan branch's structural recheck is what makes this hold.
  // Other branches don't add to s.pending. Dafny dispatches via Step's match.
}

// SAFETY THEOREM: along any trace, the slug-uniqueness invariant holds.
// EQUIVALENT to the Alloy `UniqueSlugInQueue` assertion under safe commitPlan.
lemma UniqueSlugInQueue(trace: seq<State>, actions: seq<Action>, taskSlug: map<int, int>, i: int)
  requires ValidTrace(trace, actions, taskSlug)
  requires 0 <= i < |trace|
  ensures  UniqueSlug(trace[i], taskSlug)
  ensures  Wellformed(trace[i], taskSlug)
  decreases i
{
  if i == 0 {
    InitImpliesUnique(trace[0], taskSlug);
    InitImpliesWellformed(trace[0], taskSlug);
  } else {
    UniqueSlugInQueue(trace, actions, taskSlug, i - 1);
    StepPreservesUnique(trace[i - 1], trace[i], taskSlug, actions[i - 1]);
  }
}

// ============================================================
// Witness scenario — analogous to Alloy's TwoConcurrentPlansSameSlug.
// We construct a concrete trace where two StartPlans on the same slug
// both open, then one commits and the other aborts. This is what
// Alloy's `run` block witnesses; in Dafny we prove existence by
// construction.
// ============================================================

lemma WitnessRaceResolution()
  ensures  exists trace: seq<State>, actions: seq<Action>, taskSlug: map<int, int> ::
             ValidTrace(trace, actions, taskSlug) &&
             |trace| == 5 &&
             // two distinct tasks share a slug
             (exists t1, t2 :: t1 in taskSlug && t2 in taskSlug && t1 != t2 &&
                taskSlug[t1] == taskSlug[t2]) &&
             // exactly one of the two committed (the other aborted) — only
             // one ends up Pending
             |trace[4].pending| == 1
{
  // Construct: two tasks 1,2 sharing slug 100; two attempts 10,11.
  var taskSlug: map<int, int> := map[1 := 100, 2 := 100];
  var s0 := State(pending := {}, openPlans := map[]);
  var s1 := s0.(openPlans := map[10 := 1]);                        // StartPlan(10, 1)
  var s2 := s1.(openPlans := map[10 := 1, 11 := 2]);                // StartPlan(11, 2)
  var s3 := State(pending := {1}, openPlans := map[11 := 2]);       // CommitPlan(10)
  var s4 := State(pending := {1}, openPlans := map[]);              // AbortPlan(11) — loser
  var trace := [s0, s1, s2, s3, s4];
  var actions := [StartPlan(10, 1), StartPlan(11, 2), CommitPlan(10), AbortPlan(11)];
  assert Init(trace[0]);
  assert Step(trace[0], trace[1], taskSlug, actions[0]);
  assert Step(trace[1], trace[2], taskSlug, actions[1]);
  assert Step(trace[2], trace[3], taskSlug, actions[2]);
  assert Step(trace[3], trace[4], taskSlug, actions[3]);
  assert ValidTrace(trace, actions, taskSlug);
  // Exhibit the existential witnesses
  assert taskSlug[1] == taskSlug[2];
  assert |trace[4].pending| == 1;
}
