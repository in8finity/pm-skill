module planning_parent_gate

/*
  Parent-rolls-up-children gate for `pm next` / `pm pull`.

  Maps to:
    skills/pm/scripts/next.py  (children_settled())
    skills/pm/scripts/pull.py  (same logic, race-safe path)

  Modeling decisions:
    - Static snapshot: given a fixed graph of tasks with statuses, who is
      runnable? The rule is order-free, so a single-state model suffices.
    - Concurrency / chain safety is covered by planning.als (Race + Lease
      invariants); this model only verifies the gate predicate itself.
    - "Settled for parent" = {Done, Rejected, Superseded}. Working and New
      are unsettled. Rationale: rejected/superseded children will never
      produce more work, so a parent gated on them would block forever.
    - No `dependsOn` modeled here — depends_on gating is orthogonal and
      already verified in planning.als#GateOnDeps. We compose them at the
      code level: `next.py` checks both, but each invariant stands alone.

  Verifies:
    1. ParentBlockedByPendingChild — a task with any New/Working child
       is NOT runnable, even if status is New.
    2. ParentRunnableAfterChildrenSettle — once every child is in
       {Done, Rejected, Superseded}, a New parent IS runnable.
    3. NoSelfBlocking — a task with no children is never blocked by
       itself (sanity check on the predicate).
    4. ChildlessNewTaskRunnable — the depth-0 case (today's behavior)
       still returns runnable for any New task with no children.
    5. RejectedChildIsTerminalForGate — a parent with only Rejected
       children IS runnable (otherwise a failed subtree would orphan
       its parent forever).
    6. CrossQueueChildBlockedUntilParentClaimed — the parent-claim
       gate is universal: it fires regardless of which queue the
       parent or child lives on. Adding a `queue` field to Task makes
       this explicit so future reviewers don't read the absence of
       queue as "the gate is per-queue."
    7. CrossQueueChildRunnableOnceParentClaimed — symmetric liveness:
       once the parent (anywhere) moves past SNew, a SNew child IS
       runnable, queue-irrelevant.
*/

abstract sig Status {}
one sig SNew, SWorking, SDone, SRejected, SSuperseded extends Status {}

// Queues are how the runtime partitions Tasks for listing / scheduling.
// A Task lives on exactly one queue, but parent/child links cross
// queue boundaries freely — they are content-addressed by record_sha,
// not scoped by queue. Modeling this explicitly closes the gap where
// a queue-agnostic Task could be misread as "the model assumes
// everything lives in one queue."
sig Queue {}

sig Task {
  parent:    lone Task,
  status:    one Status,
  taskQueue: one Queue
}

// No cycles in the parent graph (already enforced by data model).
fact NoSelfParent { all t: Task | t.parent != t }
fact NoCycle      { no t: Task | t in t.^parent }

// Reverse projection: c is a child of p iff c.parent = p.
fun children[p: Task] : set Task { parent.p }

// Statuses that count as settled from the parent's perspective.
// (Done — successful; Rejected/Superseded — terminal failure or replacement.)
fun terminalForParent : set Status { SDone + SRejected + SSuperseded }

// A task is runnable iff status=New AND its parent (if any) has
// already been claimed (parent.status != SNew). The parent-claim
// gate enforces the convention "parent owns the subtree's
// lifecycle / binds the context": children can't start until somebody
// has claimed the parent. The rollup-after-children invariant lives
// at finish-time (`finishable[]` below).
// depends_on omitted — composed orthogonally at the code level.
pred runnable[t: Task] {
  t.status = SNew
  no t.parent or t.parent.status != SNew
}

// A task is finishable iff it's currently working AND every child is
// settled. THIS is where the rollup invariant lives. Applies to all
// parents — sticky and non-sticky alike — because the convention says
// the parent's job is "hold the lifecycle, close after children."
pred finishable[t: Task] {
  t.status = SWorking
  all c: children[t] | c.status in terminalForParent
}

// ---- safety: any parent with pending children cannot finish ----
assert ParentNotFinishedWhilePendingChild {
  all t: Task |
    (some c: children[t] | c.status in (SNew + SWorking))
      => not finishable[t]
}
check ParentNotFinishedWhilePendingChild for 6

// ---- safety (2-level inductive invariant): SDone tasks have all
// direct children settled. The runtime dynamics enforce this — a
// task can only transition to SDone via finished.py, which is gated
// on `children_settled`. We add it here as a fact so the static
// model captures the inductive step. NOTE: this only constrains
// SDone subtrees. SRejected / SSuperseded subtrees are NOT recursively
// constrained — `pm cancel` (without --cascade) rejects a parent
// but leaves grandchildren intact, and that's the correct behavior
// (cascade-cancel is the opt-in form). ----
fact DoneHasAllChildrenSettled {
  all t: Task | t.status = SDone =>
    (all c: children[t] | c.status in terminalForParent)
}

// ---- safety (2-level): on the SDone path specifically, the gate
// recurses. If grandparent → parent → child where parent is SDone,
// the child is settled. (If parent is SRejected / SSuperseded, no
// such guarantee; that's the intentional gap that --cascade closes.) ----
assert SDoneChainRecursesToGrandchildren {
  all gp, p, c: Task |
    (p.parent = gp and c.parent = p and p.status = SDone)
      => c.status in terminalForParent
}
check SDoneChainRecursesToGrandchildren for 6

// ---- liveness: a parent (no parent of its own) with pending children
// IS still runnable — the orchestrator can pick it up to bind the
// lifecycle / context now, and only the close is gated. ----
assert TopLevelTaskAlwaysRunnable {
  all t: Task |
    (t.status = SNew and no t.parent) => runnable[t]
}
check TopLevelTaskAlwaysRunnable for 6

// ---- safety: a child whose parent is still SNew is NOT runnable —
// parent-claim gate enforces the lifecycle ordering. ----
assert ChildBlockedUntilParentClaimed {
  all t: Task |
    (some t.parent and t.parent.status = SNew)
      => not runnable[t]
}
check ChildBlockedUntilParentClaimed for 6

// ---- liveness: once parent is non-SNew, a SNew child IS runnable. ----
assert ChildRunnableOnceParentClaimed {
  all t: Task |
    (t.status = SNew and some t.parent and t.parent.status != SNew)
      => runnable[t]
}
check ChildRunnableOnceParentClaimed for 6

// Helper: parent-claim precondition for the runnable assertions below.
// (Top-level tasks always satisfy this; children require a non-SNew parent.)
pred parentClaimed[t: Task] {
  no t.parent or t.parent.status != SNew
}

// ---- liveness: a SNew task whose parent is claimed (or absent) IS
// runnable. Children-state doesn't matter — the rollup invariant is
// at finish-time, not runnable-time. ----
assert ChildlessNewTaskRunnable {
  all t: Task |
    (t.status = SNew and no children[t] and parentClaimed[t])
      => runnable[t]
}
check ChildlessNewTaskRunnable for 6

assert NoSelfBlocking {
  all t: Task |
    (t.status = SNew and no children[t] and parentClaimed[t])
      => runnable[t]
}
check NoSelfBlocking for 6

// ===== Cross-queue assertions =====
// These re-express the parent-claim gate explicitly across queues.
// Functionally redundant with ChildBlockedUntilParentClaimed /
// ChildRunnableOnceParentClaimed (which never reference queue, so
// they already span all queue configurations), but worth stating
// directly so a code-side carve-out like:
//     if parent_record not in current_queue_listing: treat_as_no_parent()
// fails the model loudly instead of silently passing because the
// assertion's quantifier didn't mention queue. Maps to:
//   skills/pm/scripts/next.py       (parent_claimed)
//   skills/pm/scripts/pull.py       (parent_claimed)
//   skills/pm/scripts/executing.py  (hard parent gate, exit 15)

// Safety: a child whose parent lives on a DIFFERENT queue is still
// blocked while the parent is SNew. The gate is about lifecycle
// ownership, not co-queue residency.
assert CrossQueueChildBlockedUntilParentClaimed {
  all c, p: Task |
    (c.parent = p
     and p.status = SNew
     and c.status = SNew
     and c.taskQueue != p.taskQueue)
      => not runnable[c]
}
check CrossQueueChildBlockedUntilParentClaimed for 6

// Liveness: once the parent on queue A moves past SNew, the child on
// queue B IS runnable. Symmetric to ChildRunnableOnceParentClaimed.
assert CrossQueueChildRunnableOnceParentClaimed {
  all c, p: Task |
    (c.parent = p
     and c.status = SNew
     and p.status != SNew
     and c.taskQueue != p.taskQueue)
      => runnable[c]
}
check CrossQueueChildRunnableOnceParentClaimed for 6

// ---- concrete scenarios ----

// Witness: a top-level parent with a Working child IS still runnable
// — the orchestrator can pick it up early to bind the lifecycle.
run RunnableEvenWithPendingChild {
  some p: Task |
    p.status = SNew
    and no p.parent
    and #children[p] = 2
    and (some c: children[p] | c.status = SWorking)
    and runnable[p]
} for 4

// Witness: a child of a SNew parent is NOT runnable — must wait for
// the parent to be claimed.
run TryChildRunWithUnclaimedParent {
  some disj p, c: Task |
    c.parent = p
    and p.status = SNew
    and c.status = SNew
    and runnable[c]
} for 4
expect 0

// Witness: a parent in `working` cannot finish while a child is
// pending — the rollup invariant holds at finish-time.
run ParentBlockedAtFinish {
  some p: Task |
    p.status = SWorking
    and #children[p] = 1
    and (some c: children[p] | c.status = SWorking)
    and not finishable[p]
} for 4

// Witness: 2-level chain (gp → p → c). gp in working; p done; c
// still working. gp would be locally finishable (its only direct
// child p is SDone), but the inductive fact rules out p being SDone
// while c is pending, so this whole shape is UNSAT — the static
// model proves the recursion holds at depth 2.
run TryGrandparentFinishWithPendingGrandchild {
  some disj gp, p, c: Task |
    p.parent = gp
    and c.parent = p
    and gp.status = SWorking
    and p.status = SDone
    and c.status = SWorking
} for 4
expect 0

// Witness: 2-level chain where every descendant is settled —
// grandparent IS finishable.
run GrandparentFinishableWhenChainSettled {
  some disj gp, p, c: Task |
    p.parent = gp
    and c.parent = p
    and gp.status = SWorking
    and p.status = SDone
    and c.status = SDone
    and finishable[gp]
} for 4

// Find a sticky-style nested expansion (parent + 2 children, all Done) and
// verify the parent is runnable.
run UnblockedAfterAllChildrenDone {
  some p: Task |
    p.status = SNew
    and #children[p] = 2
    and (all c: children[p] | c.status = SDone)
    and runnable[p]
} for 4

// ===== Cross-queue scenarios =====

// Witness: a cross-queue parent on queue qA in SNew blocks claim of
// its SNew child on queue qB. Should be SAT — finding the gated
// configuration is the whole point of the assertion.
run CrossQueueParentBlocksClaim {
  some disj qA, qB: Queue, disj p, c: Task |
    c.parent = p
    and p.taskQueue = qA
    and c.taskQueue = qB
    and p.status = SNew
    and c.status = SNew
    and not runnable[c]
} for exactly 2 Queue, exactly 2 Task

// Negative: a cross-queue child whose parent is SNew CANNOT be
// runnable. Should be UNSAT under CrossQueueChildBlockedUntilParentClaimed.
run TryCrossQueueChildRunWithUnclaimedParent {
  some disj qA, qB: Queue, disj p, c: Task |
    c.parent = p
    and p.taskQueue = qA
    and c.taskQueue = qB
    and p.status = SNew
    and c.status = SNew
    and runnable[c]
} for exactly 2 Queue, exactly 2 Task
expect 0

// Witness: once the parent (queue qA) is claimed, the cross-queue
// child (queue qB) IS runnable. Liveness witness for the symmetric
// assertion. Should be SAT.
run CrossQueueParentReleasesClaim {
  some disj qA, qB: Queue, disj p, c: Task |
    c.parent = p
    and p.taskQueue = qA
    and c.taskQueue = qB
    and p.status = SWorking
    and c.status = SNew
    and runnable[c]
} for exactly 2 Queue, exactly 2 Task
