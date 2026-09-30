// realtime.qnt — laws for the optional realtime channel (issue #17).
//
// The channel is a transport capability, not a storage backend and not a
// standard resource Action. This spec pins the *rules* it implements, unrolled
// over a finite universe (as filtering.qnt / sorting.qnt / background_tasks.qnt
// do):
//
//   * the **event vocabulary**: a successful write emits exactly the matching
//     write event; a read-like action emits nothing; a resource emits nothing
//     for an action it does not declare (so a read-only resource emits nothing);
//   * **emission only after a successful write**: a rolled-back / failed write
//     emits nothing (the wrapper publishes only once the inner write returned);
//   * the **payload is the read-model projection**, so a field hidden by a
//     ResourceView can never appear in an event (no leaked field);
//   * **delivery is policy-filtered per subscriber** against the same Policy
//     reduction the REST layer uses; an out-of-scope row is never delivered and
//     a no-policy subscriber receives nothing (fail-closed);
//   * **fan-out**: every subscriber gets every event it is admitted for, and
//     subscribers are independent.
//
// Invariants:
//   - writeEmitsMatchingEvent / readEmitsNothing / undeclaredActionEmitsNothing.
//   - aRolledBackWriteEmitsNothing.
//   - hiddenFieldNeverAppears (the projection is a subset of the read model).
//   - outOfScopeIsNeverDelivered / noPolicySubscriberReceivesNothing.
//   - fanOutReachesEveryAdmittedSubscriber / subscribersAreIndependent.
module realtime {
  // The write actions a resource can declare, plus the read-like ones that
  // emit nothing. Mirrors core/service.Action (the CRUD subset).
  type Action = Create | Read | Update | Delete | Search | Count | BatchRead | BatchEdit

  // The event vocabulary. Write-only: a read emits nothing.
  type EventKind = Created | Updated | Deleted | NoEvent

  // Reduce a batch action to its singular CRUD action, as normalize_action
  // does; read-like actions stay as they are (they emit nothing anyway).
  pure def normalizeAction(act: Action): Action =
    if (act == Count) Search
    else if (act == BatchRead) Read
    else if (act == BatchEdit) Update
    else act

  // The event a *successful* write of this action emits, or NoEvent when the
  // action emits nothing (every read-like action, and any write action the
  // resource does not declare). ``declared`` is the resource's action set.
  pure def eventFor(declared: Set[Action], act: Action): EventKind =
    if (not(declared.contains(act))) NoEvent
    else match act {
      Create => Created |
      Update => Updated |
      Delete => Deleted |
      _      => NoEvent  // read-like (and batch, which fans out per edit)
    }

  // ---- Emission only after a successful write -----------------------------
  // A write attempt either succeeds or rolls back. The wrapper publishes only on
  // success, so the emitted event is NoEvent for a failed write.
  pure def emittedAfterWrite(declared: Set[Action], act: Action, succeeded: bool): EventKind =
    if (succeeded) eventFor(declared, act) else NoEvent

  // ---- The payload is the read-model projection ---------------------------
  // The read model is the set of fields a read response exposes; a ResourceView
  // narrows it (a hidden field is removed). The event carries exactly the
  // projection, so its fields are a subset of the read model.
  pure def projectionFields(readModel: Set[str], hidden: Set[str]): Set[str] =
    readModel.exclude(hidden)

  // ---- Per-subscriber policy filtering ------------------------------------
  // A policy reduces to a filter over rows: All (match every row) or None
  // (match nothing). Mirrors the SearchFilter vocabulary.
  type Filter = AllF | NoneF

  // A subscriber's read scope: the OR-combination of its policies' READ
  // reductions. An empty policy set (or one that reduces to deny) is None
  // (fail-closed) — a no-policy subscriber receives nothing.
  pure def reduceReadScope(ps: Set[Filter]): Filter =
    if (ps.exists(f => f == AllF)) AllF else NoneF

  // Whether an event for ``row`` is deliverable to a subscriber whose read scope
  // is ``scope``. A deleted event carries no row, so it is deliverable only when
  // the scope is unscoped (All) — fail-closed otherwise.
  pure def deliverable(scope: Filter, rowInScope: bool, hasRow: bool): bool =
    if (hasRow) (scope == AllF) and rowInScope
    else scope == AllF

  // ---- A finite universe --------------------------------------------------
  pure val allActions: Set[Action] =
    Set(Create, Read, Update, Delete, Search, Count, BatchRead, BatchEdit)

  // A writable resource declares every action; a read-only one declares the read
  // subset only (so it emits nothing).
  pure val writable: Set[Action] = allActions
  pure val readOnly: Set[Action] = Set(Read, Search, Count, BatchRead)

  // The read model of a resource, and a view that hides ``secret``.
  pure val readModel: Set[str] = Set("id", "title", "author_id", "secret")
  pure val hidden: Set[str] = Set("secret")

  // Two subscribers: one with no policies, one granted (AllowAll).
  pure val noPolicies: Set[Filter] = Set()
  pure val granted: Set[Filter] = Set(AllF)

  // ---- Tests --------------------------------------------------------------

  run writeEmitsMatchingEvent = {
    all {
      assert(eventFor(writable, Create) == Created),
      assert(eventFor(writable, Update) == Updated),
      assert(eventFor(writable, Delete) == Deleted),
    }
  }

  run readEmitsNothing = {
    // No read-like action emits anything.
    Set(Read, Search, Count, BatchRead).forall(a => eventFor(writable, a) == NoEvent)
  }

  run undeclaredActionEmitsNothing = {
    all {
      // A read-only resource emits nothing for any write action...
      assert(eventFor(readOnly, Create) == NoEvent),
      assert(eventFor(readOnly, Update) == NoEvent),
      assert(eventFor(readOnly, Delete) == NoEvent),
      // ...and still emits nothing for the reads it declares.
      assert(eventFor(readOnly, Read) == NoEvent),
    }
  }

  run aRolledBackWriteEmitsNothing = {
    all {
      // Success emits the event; a rollback emits nothing.
      assert(emittedAfterWrite(writable, Create, true) == Created),
      assert(emittedAfterWrite(writable, Create, false) == NoEvent),
      assert(emittedAfterWrite(writable, Update, false) == NoEvent),
      assert(emittedAfterWrite(writable, Delete, false) == NoEvent),
      // A failed write on a read-only resource emits nothing either way.
      assert(emittedAfterWrite(readOnly, Create, true) == NoEvent),
    }
  }

  run hiddenFieldNeverAppears = {
    all {
      // The projection never contains a hidden field...
      not(projectionFields(readModel, hidden).contains("secret")),
      // ...and is a subset of the read model.
      projectionFields(readModel, hidden).subseteq(readModel),
      // With nothing hidden, the projection is the whole read model.
      projectionFields(readModel, Set()) == readModel,
    }
  }

  run outOfScopeIsNeverDelivered = {
    all {
      // An in-scope row reaches a granted subscriber...
      assert(deliverable(reduceReadScope(granted), true, true)),
      // ...but an out-of-scope row never does, even to a granted subscriber.
      assert(not(deliverable(reduceReadScope(granted), false, true))),
      // A deleted event (no row) reaches a granted (unscoped) subscriber...
      assert(deliverable(reduceReadScope(granted), false, false)),
      // ...but a row-scoped subscriber cannot verify a row that is gone, so a
      // deleted event is fail-closed for it.
      assert(not(deliverable(reduceReadScope(Set(NoneF)), false, false))),
    }
  }

  run noPolicySubscriberReceivesNothing = {
    all {
      // An empty policy set reduces to None (fail-closed)...
      assert(reduceReadScope(noPolicies) == NoneF),
      // ...so nothing is deliverable to it — not even an in-scope row.
      assert(not(deliverable(reduceReadScope(noPolicies), true, true))),
      assert(not(deliverable(reduceReadScope(noPolicies), true, false))),
      // A deny-only policy set is likewise fail-closed.
      assert(not(deliverable(reduceReadScope(Set(NoneF)), true, true))),
    }
  }

  run fanOutReachesEveryAdmittedSubscriber = {
    all {
      // Every granted subscriber is admitted for an in-scope row...
      Set(AllF, AllF).forall(f => deliverable(reduceReadScope(Set(f)), true, true)),
      // ...and admission is independent per subscriber: a granted subscriber
      // admits the row even alongside a no-policy one.
      assert(deliverable(reduceReadScope(granted), true, true)),
      assert(not(deliverable(reduceReadScope(noPolicies), true, true))),
    }
  }

  run subscribersAreIndependent = {
    // Delivery to one subscriber does not depend on another's scope: the same
    // row is delivered to the granted subscriber and withheld from the
    // no-policy one in the same tick.
    all {
      assert(deliverable(reduceReadScope(granted), true, true)),
      assert(not(deliverable(reduceReadScope(noPolicies), true, true))),
    }
  }

  run invariantsHold = {
    all {
      writeEmitsMatchingEvent,
      readEmitsNothing,
      undeclaredActionEmitsNothing,
      aRolledBackWriteEmitsNothing,
      hiddenFieldNeverAppears,
      outOfScopeIsNeverDelivered,
      noPolicySubscriberReceivesNothing,
      fanOutReachesEveryAdmittedSubscriber,
      subscribersAreIndependent,
    }
  }
}
