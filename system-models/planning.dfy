/*
  planning.dfy — Dafny port of system-models/planning.als

  Verifies the planning protocol for traces of ANY length. This file is
  intentionally aligned to the current Alloy model, including verifier-gated
  completion and supervisor cancellation.

  Maps to:
    skills/pm/scripts/{plan,executing,report,finished,cancel,store}.py
    skills/pm/{plan,next,executing,report,finished,execute}/SKILL.md

  Properties proved:
    InvAlwaysHolds         — the core state invariant holds at every step.
    TerminalAbsorbing      — once a task is done/rejected, it stays there.
    NoDoubleCommitPerTask  — at most one committed attempt exists per task.
    DependenciesDoneAtClaim — every StartClaim sees all deps done.
    ProofRequiredForTerminal — every terminal task has proof.
    VerifierGateOnDone     — verifier-required tasks cannot be done unless
                             verification has passed.
    VerifyRequiresWorkingReport — verify can only happen on working tasks that
                             already have a report.
    CancelledHasProof      — cancelled tasks always carry synthesized proof.
    FairOpenAttemptEventuallyLeavesOpenSet — under a fair-trace assumption,
                             every open claim attempt eventually disappears
                             from `openAttempts`.
*/

datatype Phase = PNew | PWorking | PDone | PRejected

ghost predicate IsTerminal(p: Phase) {
  p == PDone || p == PRejected
}

datatype TaskInfo = TaskInfo(
  deps: set<int>,
  requiresVerifier: bool
)

datatype State = State(
  pending:        set<int>,
  phase:          map<int, Phase>,
  owner:          map<int, int>,           // task -> agent
  hasReport:      set<int>,
  hasProof:       set<int>,
  openAttempts:   map<int, (int, int)>,    // attempt -> (agent, task)
  committed:      map<int, (int, int)>,    // attempt -> (agent, task)
  verifierPassed: set<int>,
  cancelled:      set<int>
)

datatype Action =
  | Plan(t: int)
  | StartClaim(a: int, t: int, x: int)
  | CommitClaim(x: int)
  | AbortClaim(x: int)
  | Report(a: int, t: int)
  | Verify(t: int)
  | Finish(a: int, t: int, terminal: Phase)
  | Cancel(t: int, actor: int)
  | Stutter

ghost predicate Init(s: State) {
  s.pending == {} &&
  s.phase == map[] &&
  s.owner == map[] &&
  s.hasReport == {} &&
  s.hasProof == {} &&
  s.openAttempts == map[] &&
  s.committed == map[] &&
  s.verifierPassed == {} &&
  s.cancelled == {}
}

ghost predicate Inv(s: State, info: map<int, TaskInfo>) {
  (forall t :: t in s.pending <==> t in s.phase) &&
  (forall t :: t in s.owner ==> t in s.phase && s.phase[t] != PNew) &&
  (forall t :: t in s.hasProof ==> t in s.phase && IsTerminal(s.phase[t])) &&
  (forall x :: x in s.committed ==> x !in s.openAttempts) &&
  (forall t :: t in s.phase && s.phase[t] == PWorking ==> t in s.owner) &&
  (forall t :: t in s.phase && IsTerminal(s.phase[t]) ==> t in s.hasProof) &&
  (forall t :: t in s.verifierPassed ==> t in s.phase && s.phase[t] != PNew) &&
  (forall t :: t in s.cancelled ==>
     t in s.phase &&
     s.phase[t] == PRejected &&
     t in s.hasReport &&
     t in s.hasProof &&
     t !in s.owner) &&
  (forall x :: x in s.committed ==>
     s.committed[x].1 in s.phase &&
     s.phase[s.committed[x].1] != PNew) &&
  (forall x1, x2 ::
     x1 in s.committed && x2 in s.committed &&
     s.committed[x1].1 == s.committed[x2].1
     ==> x1 == x2) &&
  (forall t :: t in s.phase && s.phase[t] == PDone && t in info && info[t].requiresVerifier
     ==> t in s.verifierPassed)
}

ghost predicate StepPlan(s: State, s': State, info: map<int, TaskInfo>, t: int) {
  t in info &&
  t !in s.pending &&
  (forall d :: d in info[t].deps ==> d in s.pending) &&
  s' == s.(
    pending := s.pending + {t},
    phase := s.phase[t := PNew]
  )
}

ghost predicate StepStartClaim(s: State, s': State, info: map<int, TaskInfo>, a: int, t: int, x: int) {
  t in info &&
  t in s.phase && s.phase[t] == PNew &&
  (forall d :: d in info[t].deps ==> d in s.phase && s.phase[d] == PDone) &&
  x !in s.openAttempts &&
  x !in s.committed &&
  s' == s.(openAttempts := s.openAttempts[x := (a, t)])
}

ghost predicate StepCommitClaim(s: State, s': State, x: int) {
  x in s.openAttempts &&
  x !in s.committed &&
  var pair := s.openAttempts[x];
  var t := pair.1;
  t in s.phase && s.phase[t] == PNew &&
  s' == s.(
    phase := s.phase[t := PWorking],
    owner := s.owner[t := pair.0],
    openAttempts := s.openAttempts - {x},
    committed := s.committed[x := pair]
  )
}

ghost predicate StepAbortClaim(s: State, s': State, x: int) {
  x in s.openAttempts &&
  var t := s.openAttempts[x].1;
  t in s.phase && s.phase[t] != PNew &&
  s' == s.(openAttempts := s.openAttempts - {x})
}

ghost predicate StepReport(s: State, s': State, a: int, t: int) {
  t in s.phase && s.phase[t] == PWorking &&
  t in s.owner && s.owner[t] == a &&
  s' == s.(
    hasReport := s.hasReport + {t},
    verifierPassed := s.verifierPassed - {t}
  )
}

ghost predicate StepVerify(s: State, s': State, t: int) {
  t in s.phase && s.phase[t] == PWorking &&
  t in s.hasReport &&
  s' == s.(verifierPassed := s.verifierPassed + {t})
}

ghost predicate StepFinish(s: State, s': State, info: map<int, TaskInfo>, a: int, t: int, terminal: Phase) {
  IsTerminal(terminal) &&
  t in info &&
  t in s.phase && s.phase[t] == PWorking &&
  t in s.owner && s.owner[t] == a &&
  t in s.hasReport &&
  (terminal == PRejected || !info[t].requiresVerifier || t in s.verifierPassed) &&
  s' == s.(
    phase := s.phase[t := terminal],
    hasProof := s.hasProof + {t}
  )
}

ghost predicate StepCancel(s: State, s': State, t: int, actor: int) {
  actor == actor &&
  t in s.pending &&
  t in s.phase &&
  !IsTerminal(s.phase[t]) &&
  s' == s.(
    phase := s.phase[t := PRejected],
    owner := s.owner - {t},
    hasReport := s.hasReport + {t},
    hasProof := s.hasProof + {t},
    verifierPassed := s.verifierPassed - {t},
    cancelled := s.cancelled + {t}
  )
}

ghost predicate Step(s: State, s': State, info: map<int, TaskInfo>, action: Action) {
  match action {
    case Plan(t) => StepPlan(s, s', info, t)
    case StartClaim(a, t, x) => StepStartClaim(s, s', info, a, t, x)
    case CommitClaim(x) => StepCommitClaim(s, s', x)
    case AbortClaim(x) => StepAbortClaim(s, s', x)
    case Report(a, t) => StepReport(s, s', a, t)
    case Verify(t) => StepVerify(s, s', t)
    case Finish(a, t, terminal) => StepFinish(s, s', info, a, t, terminal)
    case Cancel(t, actor) => StepCancel(s, s', t, actor)
    case Stutter => s' == s
  }
}

ghost predicate ValidTrace(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>) {
  |trace| >= 1 &&
  |actions| == |trace| - 1 &&
  Init(trace[0]) &&
  (forall i :: 0 <= i < |actions| ==> Step(trace[i], trace[i + 1], info, actions[i]))
}

ghost predicate FairTrace(trace: seq<State>) {
  forall i, x ::
    0 <= i < |trace| &&
    x in trace[i].openAttempts
    ==>
    exists j :: i < j < |trace| &&
      x !in trace[j].openAttempts &&
      (forall k :: i < k < j ==> x in trace[k].openAttempts)
}

lemma StepPreservesInv(s: State, s': State, info: map<int, TaskInfo>, action: Action)
  requires Inv(s, info)
  requires Step(s, s', info, action)
  ensures Inv(s', info)
{
}

lemma InitImpliesInv(s: State, info: map<int, TaskInfo>)
  requires Init(s)
  ensures Inv(s, info)
{
}

lemma InvAlwaysHolds(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>, i: int)
  requires ValidTrace(trace, actions, info)
  requires 0 <= i < |trace|
  ensures Inv(trace[i], info)
  decreases i
{
  if i == 0 {
    InitImpliesInv(trace[0], info);
  } else {
    InvAlwaysHolds(trace, actions, info, i - 1);
    StepPreservesInv(trace[i - 1], trace[i], info, actions[i - 1]);
  }
}

lemma TerminalAbsorbing(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>, i: int, t: int)
  requires ValidTrace(trace, actions, info)
  requires 0 <= i < |trace|
  requires t in trace[i].phase && IsTerminal(trace[i].phase[t])
  ensures forall j :: i <= j < |trace| ==> t in trace[j].phase && trace[j].phase[t] == trace[i].phase[t]
  decreases |trace| - i
{
  if i == |trace| - 1 {
  } else {
    InvAlwaysHolds(trace, actions, info, i);
    var act := actions[i];
    match act {
      case Finish(_, t', _) =>
        assert trace[i].phase[t] != PWorking;
      case Cancel(t', _) =>
        assert trace[i].phase[t] != PWorking;
      case _ =>
    }
    TerminalAbsorbing(trace, actions, info, i + 1, t);
  }
}

lemma ProofRequiredForTerminal(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>, i: int, t: int)
  requires ValidTrace(trace, actions, info)
  requires 0 <= i < |trace|
  requires t in trace[i].phase && IsTerminal(trace[i].phase[t])
  ensures t in trace[i].hasProof
{
  InvAlwaysHolds(trace, actions, info, i);
}

lemma NoDoubleCommitPerTask(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>, i: int, x1: int, x2: int)
  requires ValidTrace(trace, actions, info)
  requires 0 <= i < |trace|
  requires x1 in trace[i].committed
  requires x2 in trace[i].committed
  requires trace[i].committed[x1].1 == trace[i].committed[x2].1
  ensures x1 == x2
{
  InvAlwaysHolds(trace, actions, info, i);
}

lemma DependenciesDoneAtClaim(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>, i: int)
  requires ValidTrace(trace, actions, info)
  requires 0 <= i < |actions|
  requires actions[i].StartClaim?
  ensures actions[i].t in info
  ensures forall d :: d in info[actions[i].t].deps ==> d in trace[i].phase && trace[i].phase[d] == PDone
{
  assert Step(trace[i], trace[i + 1], info, actions[i]);
  match actions[i] {
    case StartClaim(a, t, x) =>
      assert StepStartClaim(trace[i], trace[i + 1], info, a, t, x);
  }
}

lemma VerifierGateOnDone(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>, i: int, t: int)
  requires ValidTrace(trace, actions, info)
  requires 0 <= i < |trace|
  requires t in trace[i].phase && trace[i].phase[t] == PDone
  requires t in info && info[t].requiresVerifier
  ensures t in trace[i].verifierPassed
{
  InvAlwaysHolds(trace, actions, info, i);
}

lemma VerifyRequiresWorkingReport(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>, i: int)
  requires ValidTrace(trace, actions, info)
  requires 0 <= i < |actions|
  requires actions[i].Verify?
  ensures actions[i].t in trace[i].phase
  ensures trace[i].phase[actions[i].t] == PWorking
  ensures actions[i].t in trace[i].hasReport
{
  assert Step(trace[i], trace[i + 1], info, actions[i]);
  match actions[i] {
    case Verify(t) =>
      assert StepVerify(trace[i], trace[i + 1], t);
  }
}

lemma CancelledHasProof(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>, i: int, t: int)
  requires ValidTrace(trace, actions, info)
  requires 0 <= i < |trace|
  requires t in trace[i].cancelled
  ensures t in trace[i].hasProof
{
  InvAlwaysHolds(trace, actions, info, i);
}

lemma CancelOnlyOnNonTerminal(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>, i: int)
  requires ValidTrace(trace, actions, info)
  requires 0 <= i < |actions|
  requires actions[i].Cancel?
  ensures actions[i].t in trace[i].phase
  ensures !IsTerminal(trace[i].phase[actions[i].t])
{
  assert Step(trace[i], trace[i + 1], info, actions[i]);
  match actions[i] {
    case Cancel(t, actor) =>
      assert StepCancel(trace[i], trace[i + 1], t, actor);
  }
}

lemma OpenAttemptCanDisappearOnlyByResolution(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>, i: int, x: int)
  requires ValidTrace(trace, actions, info)
  requires 0 <= i < |actions|
  requires x in trace[i].openAttempts
  requires x !in trace[i + 1].openAttempts
  ensures actions[i] == CommitClaim(x) || actions[i] == AbortClaim(x)
{
  assert Step(trace[i], trace[i + 1], info, actions[i]);
  match actions[i] {
    case CommitClaim(y) =>
      assert StepCommitClaim(trace[i], trace[i + 1], y);
      if y != x {
        assert x in trace[i].openAttempts - {y};
        assert x in trace[i + 1].openAttempts;
        assert false;
      }
    case AbortClaim(y) =>
      assert StepAbortClaim(trace[i], trace[i + 1], y);
      if y != x {
        assert x in trace[i].openAttempts - {y};
        assert x in trace[i + 1].openAttempts;
        assert false;
      }
    case StartClaim(a, t, y) =>
      assert StepStartClaim(trace[i], trace[i + 1], info, a, t, y);
      assert x in trace[i + 1].openAttempts;
      assert false;
    case Plan(t) =>
      assert StepPlan(trace[i], trace[i + 1], info, t);
      assert x in trace[i + 1].openAttempts;
      assert false;
    case Report(a, t) =>
      assert StepReport(trace[i], trace[i + 1], a, t);
      assert x in trace[i + 1].openAttempts;
      assert false;
    case Verify(t) =>
      assert StepVerify(trace[i], trace[i + 1], t);
      assert x in trace[i + 1].openAttempts;
      assert false;
    case Finish(a, t, terminal) =>
      assert StepFinish(trace[i], trace[i + 1], info, a, t, terminal);
      assert x in trace[i + 1].openAttempts;
      assert false;
    case Cancel(t, actor) =>
      assert StepCancel(trace[i], trace[i + 1], t, actor);
      assert x in trace[i + 1].openAttempts;
      assert false;
    case Stutter =>
      assert trace[i + 1] == trace[i];
      assert x in trace[i + 1].openAttempts;
      assert false;
  }
}

lemma FairOpenAttemptEventuallyLeavesOpenSet(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>, i: int, x: int)
  requires ValidTrace(trace, actions, info)
  requires FairTrace(trace)
  requires 0 <= i < |trace|
  requires x in trace[i].openAttempts
  ensures exists j :: i < j < |trace| && x !in trace[j].openAttempts
{
  var j :| i < j < |trace| &&
           x !in trace[j].openAttempts &&
           (forall k :: i < k < j ==> x in trace[k].openAttempts);
}

lemma FirstDisappearanceIsResolution(trace: seq<State>, actions: seq<Action>, info: map<int, TaskInfo>, i: int, x: int, j: int)
  requires ValidTrace(trace, actions, info)
  requires 0 <= i < j < |trace|
  requires x in trace[i].openAttempts
  requires x !in trace[j].openAttempts
  requires forall k :: i < k < j ==> x in trace[k].openAttempts
  ensures actions[j - 1] == CommitClaim(x) || actions[j - 1] == AbortClaim(x)
{
  assert x in trace[j - 1].openAttempts;
  OpenAttemptCanDisappearOnlyByResolution(trace, actions, info, j - 1, x);
}
