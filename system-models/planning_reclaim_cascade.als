module planning_reclaim_cascade

/*
  planning_reclaim_cascade.als — formal model of `pm reclaim --cascade`.

  Maps to: skills/pm/scripts/reclaim.py (the cascade() function and the
  reclaim_one() per-task gate it composes).

  ===== Why a separate module =====
  Distinct from planning_cancel_cascade.als because the per-task
  transition is different:
    cancel-cascade : non-terminal task → PRejected (close it forever)
    reclaim-cascade: PWorking task     → PNew     (release the lease)
  The skip-conditions also differ:
    cancel-cascade : skip if absorbing (done/rejected/superseded)
    reclaim-cascade: skip if NOT working (so PNew is also skipped)

  Both share the parent⁻¹ DFS walk and the visited-cycle-breaker.

  Verifies:
    RC1  NoWorkingDescendantLeftWorking
                                       — after cascade(root), every
                                         working descendant via parent⁻¹
                                         is now PNew with no owner.
    RC2  NewDescendantsUntouched       — descendants already in PNew are
                                         not touched (sweep would catch
                                         them if their parent was the one
                                         crashed; explicit reclaim leaves
                                         them alone).
    RC3  TerminalDescendantsUntouched  — descendants in PDone/PRejected
                                         stay where they were.
    RC4  CascadeIsParentTransitive     — A→B→C, reclaiming A reaches C
                                         if all three are working.
    RC5  NonDescendantUntouched        — tasks NOT in root.~^parent stay
                                         where they were.
    RC6  ReclaimRefusesNonWorkingRoot  — reclaim on a root not currently
                                         in PWorking refuses (matches
                                         reclaim_one's exit 6).

  Boundary (intentionally excluded):
    * Heartbeat-vs-reclaim race — verified separately in
      planning_lease.als (LiveHeartbeatBlocksReclaim). This module
      models the cascade STRUCTURE; per-task race-safety is orthogonal.
    * Superseded tasks — reclaim_one refuses any non-working state, so
      superseded children are skipped just like done/rejected. Modeled
      here by treating non-working as untouched; explicit Superseded
      phase not needed.
    * Per-task chain_predecessor races — verified in planning.als.
*/

abstract sig Phase {}
one sig PNew, PWorking, PDone, PRejected extends Phase {}

sig Agent {}

// Queues are how the runtime partitions Tasks for listing / scheduling.
// Tasks on different queues link freely via parentTask — the cascade
// walks the parent⁻¹ closure regardless of queue residency. Explicit
// here so a future "scan parent's queue only" optimization fails the
// model loudly instead of silently matching the queue-agnostic
// assertions below.
sig Queue {}

sig Task {
  parent:    lone Task,                 // parentTask link (immutable)
  taskQueue: one Queue,                 // immutable — Task lives on one queue
  var phase: lone Phase,
  var owner: lone Agent
}

var sig Pending in Task {}

fact NoSelfParent { all t: Task | t.parent != t }
fact NoCycle      { no t: Task | t in t.^parent }

// ===== Init =====
fact Init {
  no Pending
  all t: Task | no t.phase and no t.owner
}

// ===== Static invariants =====
fact PhaseIffPending     { always all t: Task | one t.phase <=> t in Pending }
// Owner can persist across `working → done/rejected` (the runtime
// records agent attribution on the working TaskStatus, which stays
// on the chain). Owner is stripped on reclaim (PNew with no owner).
fact OwnerOnlyAfterClaim { always all t: Task | one t.owner => t.phase != PNew }

// ===== Phase predicates =====
pred isNew      [t: Task] { t.phase = PNew }
pred isWorking  [t: Task] { t.phase = PWorking }
pred isDone     [t: Task] { t.phase = PDone }
pred isRejected [t: Task] { t.phase = PRejected }
pred isTerminal [t: Task] { isDone[t] or isRejected[t] }

fun descendants[root: Task]: set Task { Pending & (root.~^parent) }

// ===== Frame helpers =====
pred frameOtherTasks[ts: set Task] {
  all u: Task - ts | u.phase' = u.phase and u.owner' = u.owner
}

// ===== Lifecycle (just enough to reach Working / Done / Rejected) =====

pred plan[t: Task] {
  t not in Pending
  no t.parent or t.parent in Pending
  Pending' = Pending + t
  t.phase' = PNew
  no t.owner'
  frameOtherTasks[t]
}

pred claim[a: Agent, t: Task] {
  isNew[t]
  Pending' = Pending
  t.phase' = PWorking
  t.owner' = a
  frameOtherTasks[t]
}

pred finish[t: Task, terminal: Phase] {
  terminal in PDone + PRejected
  isWorking[t]
  Pending' = Pending
  t.phase' = terminal
  t.owner' = t.owner             // keep owner attribution on the terminal status
  frameOtherTasks[t]
}

// ===== Reclaim-cascade transition =====

// Atomic cascade: root + every working descendant transition PWorking
// → PNew with no owner. Non-working descendants (PNew/PDone/PRejected)
// are left untouched. Mirrors reclaim.py's `cascade()` function which
// only recurses + reclaims on `working` children.
pred cascadeReclaim[root: Task] {
  // Precondition matches reclaim_one's exit 6:
  root in Pending
  isWorking[root]
  // The set we'll transition: root + working descendants.
  let toReclaim = (root + descendants[root]) & { t: Task | isWorking[t] } |
    Pending' = Pending and
    (all t: toReclaim | t.phase' = PNew and no t.owner') and
    (all t: Task - toReclaim | t.phase' = t.phase and t.owner' = t.owner)
}

pred stutter {
  Pending' = Pending and
  (all t: Task | t.phase' = t.phase and t.owner' = t.owner)
}

fact Transitions {
  always (
    stutter
    or (some t: Task | plan[t])
    or (some a: Agent, t: Task | claim[a, t])
    or (some t: Task, p: Phase | finish[t, p])
    or (some t: Task | cascadeReclaim[t])
  )
}

// ===== Safety assertions =====

assert RC1_NoWorkingDescendantLeftWorking {
  always all root: Task |
    cascadeReclaim[root] =>
      after (all d: descendants[root] |
        // d was working at the start of the cascade ⇒ d is now PNew with no owner.
        // This is the contrapositive form: any d still working AFTER cascade
        // must NOT have been working before. (Formally simpler.)
        (isWorking[d] => no d.owner))     // trivially: working descendants
                                          //  before the step are reset
  // Alt direct form:
  always all root: Task, d: Task |
    (cascadeReclaim[root] and d in descendants[root] and isWorking[d])
    => after (isNew[d] and no d.owner)
}
check RC1_NoWorkingDescendantLeftWorking for 4 but 8 steps

assert RC2_NewDescendantsUntouched {
  always all root: Task, d: Task |
    (cascadeReclaim[root] and d in descendants[root] and isNew[d])
    => d.phase' = PNew
}
check RC2_NewDescendantsUntouched for 4 but 8 steps

assert RC3_TerminalDescendantsUntouched {
  always all root: Task, d: Task |
    (cascadeReclaim[root] and d in descendants[root] and isTerminal[d])
    => d.phase' = d.phase
}
check RC3_TerminalDescendantsUntouched for 4 but 8 steps

assert RC4_CascadeIsParentTransitive {
  always all a, b, c: Task |
    (b.parent = a and c.parent = b
     and cascadeReclaim[a]
     and isWorking[c])
    => after isNew[c]
}
check RC4_CascadeIsParentTransitive for 4 but 8 steps

assert RC5_NonDescendantUntouched {
  always all root: Task, t: Task |
    (cascadeReclaim[root]
     and t != root
     and t not in descendants[root])
    => (t.phase' = t.phase and t.owner' = t.owner)
}
check RC5_NonDescendantUntouched for 4 but 8 steps

assert RC6_ReclaimRefusesNonWorkingRoot {
  always all root: Task |
    cascadeReclaim[root] => isWorking[root]
}
check RC6_ReclaimRefusesNonWorkingRoot for 4 but 8 steps

// RC7: cross-queue closure — when a working descendant lives on a
// different queue from the root, the reclaim cascade still reaches
// it. Maps to the runtime cross-queue find_items scan in
// `store._find_children_global` (post-fix code in commit bb79ed8).
// Functionally redundant with RC1/RC4 (queue-agnostic by construction)
// — kept distinct so a code-side carve-out like "scan parent's
// queue only" fails the model loudly instead of silently passing.
assert RC7_CascadeIsCrossQueue {
  always all root, child: Task |
    (child.parent = root
     and root.taskQueue != child.taskQueue
     and cascadeReclaim[root]
     and child in Pending
     and isWorking[child])
      => after (isNew[child] and no child.owner)
}
check RC7_CascadeIsCrossQueue for 4 but 8 steps

// Bonus: post-reclaim there is no owner anywhere in the reclaimed set.
assert RC_OwnerStrippedFromReclaimedSet {
  always all root: Task |
    cascadeReclaim[root] =>
      (after no (root + descendants[root]).owner
        or (root + descendants[root]) in {t: Task | not isWorking[t]})
}
// (Stated weakly because non-working descendants weren't reclaimed.
// The strict form is the per-d "after no d.owner" pinned by RC1.)

// ===== Liveness scenarios =====

// S1: parent + working child both reclaimed.
run S1_TwoLevelCascade {
  some disj root, child: Task, a: Agent |
    child.parent = root and
    eventually (
      isWorking[root] and isWorking[child] and root.owner = a and
      after (isNew[root] and isNew[child] and no root.owner and no child.owner)
    )
} for exactly 2 Task, exactly 1 Agent, 8 steps

// S2: mixed-state — root working, child new, second child done. Cascade
// reclaims root only; leaves new + done children alone.
run S2_MixedStateCascade {
  some disj root, c1, c2: Task, a: Agent |
    c1.parent = root and c2.parent = root and
    eventually (
      isWorking[root] and isNew[c1] and isDone[c2] and
      after (isNew[root] and isNew[c1] and isDone[c2])
    )
} for exactly 3 Task, exactly 2 Agent, 12 steps

// S3: three-deep all-working chain.
run S3_ThreeDeepCascade {
  some disj a, b, c: Task, ag: Agent |
    b.parent = a and c.parent = b and
    eventually (
      isWorking[a] and isWorking[b] and isWorking[c] and
      after (isNew[a] and isNew[b] and isNew[c]
             and no a.owner and no b.owner and no c.owner)
    )
} for exactly 3 Task, exactly 3 Agent, 12 steps

// Negative: try to cascade-reclaim a root in PNew. Should be UNSAT.
run TryReclaimNewRoot {
  some root: Task | eventually (
    isNew[root] and cascadeReclaim[root]
  )
} for exactly 1 Task, exactly 1 Agent, 6 steps
expect 0

// Negative: try to cascade-reclaim a terminal root. Should be UNSAT.
run TryReclaimDoneRoot {
  some root: Task | eventually (
    isDone[root] and cascadeReclaim[root]
  )
} for exactly 1 Task, exactly 1 Agent, 8 steps
expect 0

// S4: cross-queue cascade — root on qA + working child on qB.
// Reclaim root → child reclaimed too despite the queue boundary.
// Witness for RC7.
run S4_CrossQueueCascade {
  some disj qA, qB: Queue, disj root, child: Task, a: Agent |
    child.parent = root and
    root.taskQueue = qA and child.taskQueue = qB and
    eventually (
      isWorking[root] and isWorking[child] and root.owner = a and
      after (isNew[root] and isNew[child]
             and no root.owner and no child.owner)
    )
} for exactly 2 Task, exactly 2 Queue, exactly 1 Agent, 8 steps
