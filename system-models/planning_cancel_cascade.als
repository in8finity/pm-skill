module planning_cancel_cascade

/*
  planning_cancel_cascade.als — formal model of `pm cancel --cascade`.

  Maps to: skills/pm/scripts/cancel.py (the cascade() function and the
  cancel_one() per-task gate it composes).

  ===== Why a separate module =====
  planning.als models cancel as a single-task transition (`pred cancel`).
  The `--cascade` mode walks parentTask reverse-links DFS and applies
  cancel to every undone descendant. Proving cascade correctness needs a
  transition that talks about the *closure* of parent-reverse, not just
  one task.

  Three options for modeling cascade:
    (a) Sequence of single cancels (temporal — needs liveness).
    (b) Atomic batch (one step covers root + all undone descendants).
    (c) Hybrid (atomic per-task with a fairness assumption).

  This file picks (b) — atomic batch. The runtime DFS is iterative,
  but every iteration writes to a different task's TaskStatus chain,
  so the writes are independent (chain_predecessor races resolve
  per-chain). The atomic abstraction is faithful for the cascade-
  correctness properties: it proves what's true after the operation
  completes.

  Verifies:
    CC1  NoDescendantLeftUndone        — after cascade(root), every
                                          undone descendant via parent⁻¹
                                          is absorbing.
    CC2  PreviousTerminalUntouched     — descendants already in PDone
                                          or PRejected are unchanged.
    CC3  CascadeOnlyTransitionsNonTerminal
                                       — only PNew/PWorking → PRejected;
                                          never re-closes a done task.
    CC4  CascadeIsParentTransitive     — if A→B→C via parent, cancelling
                                          A cascades to C.
    CC5  NonDescendantUntouched        — tasks NOT in root.~^parent stay
                                          where they were.
    CC6  CascadeRefusesAbsorbingRoot   — cascade on a root that's
                                          already absorbing is a no-op
                                          (no descendants change).

  Boundary (intentionally excluded):
    * Replan / supersede — verified in planning_replan.als. Cascade's
      interaction with superseded children: cancel_one refuses them
      (post-R4 fix), so the cascade DFS visits but doesn't transition
      them. Modeled here by having `cascadeCancel` skip absorbing
      descendants.
    * Heartbeat / lease — orthogonal.
    * Per-task chain_predecessor races — verified in planning.als.
*/

abstract sig Phase {}
one sig PNew, PWorking, PDone, PRejected extends Phase {}

// Queues are how the runtime partitions Tasks for listing / scheduling.
// Tasks on different queues link freely via parentTask — the cascade
// walks the parent⁻¹ closure regardless of queue residency. Modeling
// the field explicitly closes the gap where a queue-agnostic Task
// could be misread as "the cascade is per-queue."
sig Queue {}

sig Task {
  parent:    lone Task,                 // parentTask link (immutable)
  taskQueue: one Queue,                 // immutable — Task lives on one queue
  var phase: lone Phase
}

var sig Pending in Task {}

fact NoSelfParent { all t: Task | t.parent != t }
fact NoCycle      { no t: Task | t in t.^parent }

// ===== Init =====
fact Init {
  no Pending
  all t: Task | no t.phase
}

// ===== Static invariant =====
fact PhaseIffPending { always all t: Task | one t.phase <=> t in Pending }

// ===== Phase predicates =====
pred isNew      [t: Task] { t.phase = PNew }
pred isWorking  [t: Task] { t.phase = PWorking }
pred isDone     [t: Task] { t.phase = PDone }
pred isRejected [t: Task] { t.phase = PRejected }
pred isTerminal [t: Task] { isDone[t] or isRejected[t] }   // = absorbing here

// Descendant set: tasks transitively reachable via parent⁻¹ from root.
// In Alloy, `t in root.~^parent` means t has root as a parent-ancestor.
fun descendants[root: Task]: set Task { Pending & (root.~^parent) }

// ===== Lifecycle (just enough to reach Done / Rejected) =====

pred plan[t: Task] {
  t not in Pending
  no t.parent or t.parent in Pending
  Pending' = Pending + t
  t.phase' = PNew
  all u: Task - t | u.phase' = u.phase
}

pred claim[t: Task] {
  isNew[t]
  Pending' = Pending
  t.phase' = PWorking
  all u: Task - t | u.phase' = u.phase
}

pred finish[t: Task, terminal: Phase] {
  terminal in PDone + PRejected
  isWorking[t]
  Pending' = Pending
  t.phase' = terminal
  all u: Task - t | u.phase' = u.phase
}

// ===== Cascade-cancel transition =====

// Atomic cascade: root and every undone descendant become PRejected.
// Already-absorbing descendants are left alone (mirrors cancel_one's
// post-R4 refusal of done/rejected/superseded — superseded omitted
// here, see boundary note).
pred cascadeCancel[root: Task] {
  // Precondition: root must be on the board AND non-absorbing — mirrors
  // cancel.py main() short-circuiting (return 6) when cancel_one
  // refuses the root with `primary is None`. The cascade DFS only
  // runs after the primary succeeds.
  root in Pending
  not isTerminal[root]
  // The set we'll transition: root + undone descendants.
  let toReject = (root + descendants[root]) & { t: Task | not isTerminal[t] } |
    Pending' = Pending and
    (all t: toReject | t.phase' = PRejected) and
    (all t: Task - toReject | t.phase' = t.phase)
}

pred stutter {
  Pending' = Pending and (all t: Task | t.phase' = t.phase)
}

fact Transitions {
  always (
    stutter
    or (some t: Task | plan[t])
    or (some t: Task | claim[t])
    or (some t: Task, p: Phase | finish[t, p])
    or (some t: Task | cascadeCancel[t])
  )
}

// ===== Safety assertions =====

assert CC1_NoDescendantLeftUndone {
  always all root: Task |
    cascadeCancel[root] =>
      after (all d: descendants[root] | isTerminal[d])
}
check CC1_NoDescendantLeftUndone for 4 but 8 steps

assert CC2_PreviousTerminalUntouched {
  always all root: Task |
    cascadeCancel[root] =>
      (all t: Task |
        isTerminal[t] => t.phase' = t.phase)
}
check CC2_PreviousTerminalUntouched for 4 but 8 steps

assert CC3_CascadeOnlyTransitionsNonTerminal {
  always all root: Task, t: Task |
    (cascadeCancel[root] and t.phase' != t.phase)
    => not isTerminal[t]
}
check CC3_CascadeOnlyTransitionsNonTerminal for 4 but 8 steps

// CC4: parent-transitive — if a→b→c via parent, cancelling a leaves c
// absorbing too. (Falls out of CC1 since c ∈ descendants[a], but
// stating it directly proves the closure walk is captured.)
assert CC4_CascadeIsParentTransitive {
  always all a, b, c: Task |
    (b.parent = a and c.parent = b and cascadeCancel[a] and c in Pending) =>
      after isTerminal[c]
}
check CC4_CascadeIsParentTransitive for 4 but 8 steps

assert CC5_NonDescendantUntouched {
  always all root: Task, t: Task |
    (cascadeCancel[root]
     and t != root
     and t not in descendants[root])
    => t.phase' = t.phase
}
check CC5_NonDescendantUntouched for 4 but 8 steps

// CC6: cascade can never fire on an absorbing root. (Runtime
// counterpart: cancel.py main() returns 6 without entering the
// cascade DFS when cancel_one refuses the root.)
assert CC6_CascadeRefusesAbsorbingRoot {
  always all root: Task |
    cascadeCancel[root] => not isTerminal[root]
}
check CC6_CascadeRefusesAbsorbingRoot for 4 but 8 steps

// CC7: cross-queue closure — when a descendant lives on a different
// queue from the root, the cascade still reaches it. Maps to the
// runtime cross-queue find_items scan in
// `store._find_children_global` (post-fix code in commit bb79ed8).
// Functionally redundant with CC1/CC4 (which never reference queue,
// so they already span all queue configurations) — kept as a
// distinct assertion so a code-side carve-out like
// "only cascade within parent's own queue" fails the model loudly
// instead of silently passing because the existing assertions
// didn't mention queue.
assert CC7_CascadeIsCrossQueue {
  always all root, child: Task |
    (child.parent = root
     and root.taskQueue != child.taskQueue
     and cascadeCancel[root]
     and child in Pending
     and not isTerminal[child])
      => after isTerminal[child]
}
check CC7_CascadeIsCrossQueue for 4 but 8 steps

// ===== Liveness scenarios =====

// Single-level cascade: root + one undone child both end up rejected.
run S1_TwoLevelCascade {
  some disj root, child: Task |
    child.parent = root and
    eventually (
      isWorking[root] and isWorking[child] and
      after (isRejected[root] and isRejected[child])
    )
} for exactly 2 Task, 8 steps

// Mixed-state cascade: root undone, one child done (untouched), one
// child working (cancelled). Demonstrates CC2 + CC3 together.
run S2_MixedStateCascade {
  some disj root, c1, c2: Task |
    c1.parent = root and c2.parent = root and
    eventually (
      isWorking[root] and isDone[c1] and isWorking[c2] and
      after (isRejected[root] and isDone[c1] and isRejected[c2])
    )
} for exactly 3 Task, 12 steps

// Three-deep cascade: a→b→c via parent. Cancelling a leaves b and c
// rejected (CC4 witness).
run S3_ThreeDeepCascade {
  some disj a, b, c: Task |
    b.parent = a and c.parent = b and
    eventually (
      isWorking[a] and isWorking[b] and isWorking[c] and
      after (isRejected[a] and isRejected[b] and isRejected[c])
    )
} for exactly 3 Task, 12 steps

// Negative — try to cancel a non-descendant via cascade. Should be
// UNSAT under CC5: the non-descendant's phase must be unchanged across
// the cascade transition.
run TryCascadeAffectsNonDescendant {
  some disj root, other: Task |
    no other.parent and
    other not in root.~^parent and       // other is NOT a descendant
    eventually (
      isWorking[root] and isWorking[other] and
      cascadeCancel[root] and             // transition fires at THIS state
      after isRejected[other]             // and other is rejected at the next
    )
} for exactly 2 Task, 10 steps
expect 0

// Cross-queue scenario: root on queue qA, child on queue qB. Cancel
// the root → the child is rejected too, demonstrating the cascade
// closure walks across queue boundaries.
run S4_CrossQueueCascade {
  some disj qA, qB: Queue, disj root, child: Task |
    child.parent = root and
    root.taskQueue = qA and child.taskQueue = qB and
    eventually (
      isWorking[root] and isWorking[child] and
      after (isRejected[root] and isRejected[child])
    )
} for exactly 2 Task, exactly 2 Queue, 8 steps
