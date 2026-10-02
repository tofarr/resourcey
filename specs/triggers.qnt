// triggers.qnt — laws for the edit-event trigger framework (issue #155).
//
// ``TriggeredResource`` / ``TriggeredService`` wrap another resource and, after
// every *successful* create / update / delete / batch_edit, invoke each
// configured ``Trigger`` exactly once with that operation's whole
// ``(edits, results)`` lists. This spec pins the small, pure rules the
// execution policy rests on:
//
//   - editsOnlyFire: only a write action (Create / Update / Delete /
//     BatchEdit) can fire a trigger; a read-like action (Read / Search /
//     Count / BatchRead) never does, regardless of outcome.
//   - successOnly: a write fires only when the inner service succeeded; an
//     inner exception fires nothing.
//   - perOperationNotPerItem: a batch of N edits invokes each trigger exactly
//     once (not N times) -- the invocation count is independent of the
//     batch size.
//   - alignment: the edits and results an invocation carries are the same
//     length, position for position (a delete / a miss is a `None` result,
//     never a missing slot).
//   - perTriggerIsolation: one trigger raising does not stop or change
//     whether any other configured trigger runs -- every trigger's outcome is
//     decided independently.
//   - backgroundNeverBlocks: with background=true the response is ordered
//     before the trigger awaits complete; with background=false the trigger
//     awaits complete before the response (the inline / reliability-critical
//     mode).
//   - exitSettlesEveryRun: on exit, every in-flight run is either left
//     Completed or moved to Cancelled -- never left Pending -- and exit is
//     idempotent (settling an already-settled set changes nothing).
//
// The model is deliberately small and unrolled over finite universes, matching
// how background_tasks.qnt / exposure.qnt unroll theirs.

module triggers {
  type Action = Create | Read | Update | Delete | Search | Count | BatchRead | BatchEdit

  // Only a write action can ever fire a trigger. Reads -- including the
  // batched / counted derivatives -- never do, no matter the outcome.
  pure def isWriteAction(a: Action): bool =
    match a {
      Create => true | Update => true | Delete => true | BatchEdit => true |
      Read => false | Search => false | Count => false | BatchRead => false
    }

  // The firing rule: a write action that succeeded. A failed write (and any
  // read, regardless of outcome) fires nothing.
  pure def fires(a: Action, succeeded: bool): bool =
    isWriteAction(a) and succeeded

  // ---- Alignment: edits and results share one length -----------------------
  // An operation is modelled by how many edit nodes and how many result slots
  // it carries; a real batch_edit keeps them equal position-for-position
  // (Update / Create items, Delete / None results), and a single create /
  // update / delete normalizes to exactly one of each.
  type Op = { editCount: int, resultCount: int }

  pure def aligned(op: Op): bool = op.editCount == op.resultCount

  // ---- Per-operation, not per-item -----------------------------------------
  // The number of times one trigger's callback runs for an operation is
  // always 1 when it fires, regardless of how many edits the operation
  // carries (a batch of 5 is one invocation per trigger, not five).
  pure def invocationsPerTrigger(a: Action, succeeded: bool, _op: Op): int =
    if (fires(a, succeeded)) 1 else 0

  // ---- Per-trigger isolation -------------------------------------------
  // Each configured trigger's outcome (Ok or Raised) is decided
  // independently of the others': the set of triggers that ran is the whole
  // configured set regardless of which subset raises.
  type Outcome = Ok | Raised

  pure def outcomeOf(name: str, raising: Set[str]): Outcome =
    if (raising.contains(name)) Raised else Ok

  // The set of triggers that ran (i.e. were invoked at all) -- isolation
  // means every configured trigger runs, whatever the raising subset.
  pure def ranTriggers(configured: Set[str], _raising: Set[str]): Set[str] =
    configured

  pure def isolationHolds(configured: Set[str], raising: Set[str]): bool =
    // The raising subset (any subset, including the whole / empty set)
    // changes nobody's membership in "ran" -- only their Outcome.
    ranTriggers(configured, raising) == configured

  // ---- Background never blocks ---------------------------------------
  // The order of two events: the edit's HTTP response, and the trigger
  // awaits completing. background=true orders the response first (it is
  // never delayed by trigger latency); background=false awaits inline, so
  // the trigger awaits order before the response.
  type Order = ResponseFirst | TriggerAwaitsFirst

  pure def orderFor(background: bool): Order =
    if (background) ResponseFirst else TriggerAwaitsFirst

  // ---- Exit settles every in-flight run --------------------------------
  // A background run tracked at exit is either already Completed, or still
  // Pending -- in which case exit cancels it. No run is ever left Pending.
  type RunState = Pending | Completed | Cancelled

  pure def settleOne(s: RunState): RunState =
    match s { Pending => Cancelled | Completed => Completed | Cancelled => Cancelled }

  pure def settle(states: List[RunState]): List[RunState] =
    states.foldl(List(), (acc, s) => acc.append(settleOne(s)))

  pure def noneLeftPending(states: List[RunState]): bool =
    not(states.foldl(false, (acc, s) => acc or (s == Pending)))

  // ---- Tests ----------------------------------------------------------

  run editsOnlyFire = {
    all {
      // Every write action fires when it succeeds...
      assert(fires(Create, true)),
      assert(fires(Update, true)),
      assert(fires(Delete, true)),
      assert(fires(BatchEdit, true)),
      // ...and no read-like action ever fires, success or not.
      assert(not(fires(Read, true))),
      assert(not(fires(Search, true))),
      assert(not(fires(Count, true))),
      assert(not(fires(BatchRead, true))),
      assert(not(fires(Read, false))),
      assert(not(fires(Search, false))),
      assert(not(fires(Count, false))),
      assert(not(fires(BatchRead, false))),
    }
  }

  run successOnly = {
    all {
      assert(not(fires(Create, false))),
      assert(not(fires(Update, false))),
      assert(not(fires(Delete, false))),
      assert(not(fires(BatchEdit, false))),
    }
  }

  run alignmentTest = {
    all {
      assert(aligned({ editCount: 1, resultCount: 1 })),
      assert(aligned({ editCount: 5, resultCount: 5 })),
      assert(aligned({ editCount: 0, resultCount: 0 })),
      assert(not(aligned({ editCount: 5, resultCount: 4 }))),
    }
  }

  run perOperationNotPerItemTest = {
    val single = { editCount: 1, resultCount: 1 }
    val batchOfFive = { editCount: 5, resultCount: 5 }
    all {
      // A single create/update/delete fires once...
      assert(invocationsPerTrigger(Create, true, single) == 1),
      // ...and a batch of five still fires each trigger exactly once, not
      // five times: the invocation count is independent of batch size.
      assert(invocationsPerTrigger(BatchEdit, true, batchOfFive) == 1),
      assert(
        invocationsPerTrigger(Create, true, single) ==
          invocationsPerTrigger(BatchEdit, true, batchOfFive)
      ),
      // A failed batch fires zero invocations, whatever its size.
      assert(invocationsPerTrigger(BatchEdit, false, batchOfFive) == 0),
    }
  }

  run perTriggerIsolationTest = {
    val configured = Set("webhookA", "webhookB", "webhookC")
    all {
      // No trigger raises: all three ran.
      assert(isolationHolds(configured, Set())),
      assert(ranTriggers(configured, Set()) == configured),
      // The first trigger raises: the other two still ran (isolation), and
      // the raising one still ran too -- it is caught, not skipped.
      assert(isolationHolds(configured, Set("webhookA"))),
      assert(ranTriggers(configured, Set("webhookA")) == configured),
      assert(outcomeOf("webhookA", Set("webhookA")) == Raised),
      assert(outcomeOf("webhookB", Set("webhookA")) == Ok),
      assert(outcomeOf("webhookC", Set("webhookA")) == Ok),
      // Every trigger raising still leaves every trigger having run.
      assert(isolationHolds(configured, configured)),
      assert(ranTriggers(configured, configured) == configured),
    }
  }

  run backgroundNeverBlocksTest = {
    all {
      assert(orderFor(true) == ResponseFirst),
      assert(orderFor(false) == TriggerAwaitsFirst),
      assert(orderFor(true) != orderFor(false)),
    }
  }

  run exitSettlesEveryRunTest = {
    val allPending = List(Pending, Pending, Pending)
    val mixed = List(Completed, Pending, Cancelled)
    val allDone = List(Completed, Cancelled, Completed)
    all {
      assert(noneLeftPending(settle(allPending))),
      assert(settle(allPending) == List(Cancelled, Cancelled, Cancelled)),
      assert(noneLeftPending(settle(mixed))),
      assert(settle(mixed) == List(Completed, Cancelled, Cancelled)),
      // Exit is idempotent: settling an already-settled set changes nothing.
      assert(settle(allDone) == allDone),
      assert(settle(settle(mixed)) == settle(mixed)),
    }
  }

  run invariantsHold = {
    all {
      editsOnlyFire,
      successOnly,
      alignmentTest,
      perOperationNotPerItemTest,
      perTriggerIsolationTest,
      backgroundNeverBlocksTest,
      exitSettlesEveryRunTest,
    }
  }
}
