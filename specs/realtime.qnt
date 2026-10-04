// realtime.qnt — formal spec for the realtime WebSocket channel (issue #17).
//
// Publishing itself is governed by the already-specified TriggeredService
// execution policy (specs/triggers.qnt): RedisTrigger is a plain Trigger, so
// fire-once-per-write / success-only / per-trigger isolation carry over
// unchanged and are not restated here. What IS new in realtime_routes.py is
// the per-connection subscription/delivery state machine this spec pins:
//
//   - the subscription gate: a resource is subscribable only when it exposes
//     Read (RealtimeHub.subscribe's `Action.READ not in supported` check);
//   - the handshake outcome: the same three-way absent/invalid/authenticated
//     classification auth.qnt already pins, gated by posture exactly once
//     more here because RealtimeHub.authenticate is new code composing it
//     (an invalid credential is rejected under every posture; an absent one
//     is rejected only under Required);
//   - the per-subscriber policy filter is resolved ONCE, at subscribe time,
//     by the unchanged PolicyResolver -> Policy.to_search_filter -> or_(...)
//     union already pinned in roles.qnt / rbac.qnt (restated minimally here
//     as the NoMatchF <= OwnF <= AllF chain, since it feeds the genuinely new
//     part below);
//   - the delivery decision (`_deliverable`): a created/updated event carries
//     a row, so delivery is matched against BOTH the resolved policy filter
//     and the subscription's own narrowing filter, AND-combined; a deleted
//     event carries no row, so it can only be matched structurally and is
//     delivered only when BOTH filters are unscoped -- a row-scoped policy or
//     subscription filter cannot be verified against a row that no longer
//     exists, so it fails closed rather than guessing.
//
// Invariants:
//   - subscribableRequiresRead: a resource with no Read support is never
//     subscribable, whatever its other actions.
//   - handshakeInvalidAlwaysRejected / handshakeAbsentGatedByPosture /
//     handshakeAuthenticatedAlwaysGranted: the posture-gated outcome of
//     RealtimeHub.authenticate.
//   - crossResourceNeverDelivers: a subscription to one resource never admits
//     an event for another, regardless of filters.
//   - subFilterNeverWidensBeyondPolicy: a subscription's own filter can only
//     narrow what the resolved policy already allows (AND, never OR) -- a
//     crafted subscription filter can never grant visibility beyond the
//     caller's own scope.
//   - deleteFailsClosedWhenScoped: a deleted event is delivered only to a
//     subscriber whose combined scope (policy AND subscription filter) is
//     fully unscoped; any row-scoping on either side suppresses it.
//   - noMatchDeliversNothing: a subscriber whose resolved policy is NoMatchF
//     (fail-closed / no policy) receives nothing, for any event kind.
//   - unscopedAlwaysDelivers: an unscoped policy with no subscription filter
//     admits every event for its resource, every kind included.

module realtime {
  // ---- The subscription gate (RealtimeHub.subscribe) -----------------------
  // Mirrors core/service.Action; only membership in Read matters here (the
  // normalize_actions reduction itself is exposure.qnt's concern).
  type Action = Create | Read | Update | Delete | Search | Count | BatchRead | BatchEdit

  pure def subscribable(exposedActions: Set[Action]): bool =
    exposedActions.contains(Read)

  // ---- The handshake outcome (RealtimeHub.authenticate) ---------------------
  // The same three-way classification auth.qnt pins (Absent / Invalid /
  // Authenticated), gated by the builder's posture: Invalid is rejected
  // whatever the posture; Absent is rejected only under Required; Authenticated
  // always grants a principal.
  type AuthClass = Absent | Invalid | Authenticated
  type Posture = Required | Optional
  type HandshakeOutcome = Anonymous | AuthenticatedPrincipal | Rejected

  pure def authenticate(posture: Posture, cls: AuthClass): HandshakeOutcome =
    match cls {
      Authenticated => AuthenticatedPrincipal |
      Invalid => Rejected |
      Absent => if (posture == Required) Rejected else Anonymous
    }

  // ---- The per-subscriber policy filter chain -------------------------------
  // Restated minimally from roles.qnt / rbac.qnt: NoMatchF <= OwnF <= AllF,
  // resolved once at subscribe time via the unchanged union-of-policies
  // reduction. This spec does not re-prove that union law; it only needs the
  // chain and `isUnscoped` to state the delivery decision below.
  type Filter = NoMatchF | OwnF | AllF

  pure def isUnscoped(f: Filter): bool = f == AllF

  // A row, reduced to the one attribute the Owner policy's reduction (and a
  // subscription's own narrowing filter) can test.
  type Row = { owner: str }

  pure def matchesPolicy(f: Filter, callerId: str, row: Row): bool =
    match f {
      NoMatchF => false |
      OwnF => row.owner == callerId |
      AllF => true
    }

  // A subscription's own, optional `{field__op: value}` filter -- abstracted
  // to the two states that matter for delivery: none supplied (NoSubFilter,
  // the common case), or a concrete narrowing clause (OwnerEquals stands in
  // for "any clause that is not the all-matching filter" -- _build_subscription_filter
  // never returns an explicit AllFilter for a non-empty clause dict, so this
  // two-state model is exhaustive for the delivery decision). The full filter
  // algebra (AND/OR/NOT, every operator) is already specified in filtering.qnt
  // and is not re-derived here.
  type SubFilter = NoSubFilter | OwnerEquals(str)

  pure def matchesSub(sf: SubFilter, row: Row): bool =
    match sf { NoSubFilter => true | OwnerEquals(v) => row.owner == v }

  pure def subFilterIsUnscoped(sf: SubFilter): bool =
    match sf { NoSubFilter => true | OwnerEquals(_) => false }

  // ---- The delivery decision (`_deliverable`) -------------------------------
  type EventKind = Created | Updated | Deleted

  // One candidate event, already known to be for the subscription's resource
  // (cross-resource matching is gated separately below).
  type Candidate = { kind: EventKind, row: Row }

  pure def deliverable(policy: Filter, sf: SubFilter, callerId: str, c: Candidate): bool =
    match c.kind {
      Deleted => isUnscoped(policy) and subFilterIsUnscoped(sf) |
      _ => matchesPolicy(policy, callerId, c.row) and matchesSub(sf, c.row)
    }

  // ---- One connection's subscription, matched against one event ------------
  type Subscription = { resource: str, policy: Filter, subFilter: SubFilter, callerId: str }
  type Event = { resource: str, kind: EventKind, row: Row }

  pure def admits(sub: Subscription, ev: Event): bool =
    sub.resource == ev.resource and
      deliverable(sub.policy, sub.subFilter, sub.callerId, { kind: ev.kind, row: ev.row })

  // ---- Finite universes for the invariant sweeps ----------------------------
  pure val allFilters: Set[Filter] = Set(NoMatchF, OwnF, AllF)
  pure val allSubFilters: Set[SubFilter] = Set(NoSubFilter, OwnerEquals("alice"), OwnerEquals("bob"))
  pure val allKinds: Set[EventKind] = Set(Created, Updated, Deleted)
  pure val allRows: Set[Row] = Set({ owner: "alice" }, { owner: "bob" })
  pure val allCallers: Set[str] = Set("alice", "bob")

  // Invariants

  pure def subscribableRequiresRead(actions: Set[Action]): bool =
    subscribable(actions) implies actions.contains(Read)

  pure def crossResourceNeverDelivers(sub: Subscription, otherResource: str,
                                      kind: EventKind, row: Row): bool =
    (sub.resource != otherResource) implies
      not(admits(sub, { resource: otherResource, kind: kind, row: row }))

  pure def subFilterNeverWidensBeyondPolicy(policy: Filter, sf: SubFilter,
                                            callerId: str, row: Row): bool =
    deliverable(policy, sf, callerId, { kind: Created, row: row }) implies
      matchesPolicy(policy, callerId, row)

  pure def deleteFailsClosedWhenScoped(policy: Filter, sf: SubFilter,
                                       callerId: str, row: Row): bool =
    (not(isUnscoped(policy)) or not(subFilterIsUnscoped(sf))) implies
      not(deliverable(policy, sf, callerId, { kind: Deleted, row: row }))

  pure def noMatchDeliversNothing(sf: SubFilter, callerId: str, c: Candidate): bool =
    not(deliverable(NoMatchF, sf, callerId, c))

  pure def unscopedAlwaysDelivers(callerId: str, c: Candidate): bool =
    deliverable(AllF, NoSubFilter, callerId, c)

  // Tests

  run subscribableGateTest = {
    all {
      assert(subscribable(Set(Read))),
      assert(subscribable(Set(Read, Search, Count, BatchRead))),
      assert(not(subscribable(Set()))),
      assert(not(subscribable(Set(Create, Update, Delete)))),
      assert(not(subscribable(Set(Search, Count)))),
    }
  }

  run handshakeAuthenticatedAlwaysGrantsTest = {
    all {
      assert(authenticate(Required, Authenticated) == AuthenticatedPrincipal),
      assert(authenticate(Optional, Authenticated) == AuthenticatedPrincipal),
    }
  }

  run handshakeInvalidAlwaysRejectedTest = {
    all {
      assert(authenticate(Required, Invalid) == Rejected),
      assert(authenticate(Optional, Invalid) == Rejected),
    }
  }

  run handshakeAbsentGatedByPostureTest = {
    all {
      assert(authenticate(Required, Absent) == Rejected),
      assert(authenticate(Optional, Absent) == Anonymous),
      // The posture is the only thing that distinguishes the two outcomes.
      assert(authenticate(Required, Absent) != authenticate(Optional, Absent)),
    }
  }

  run createdUpdatedMatchBothFiltersTest = {
    val alice = { owner: "alice" }
    val bob = { owner: "bob" }
    all {
      // An Owner-scoped subscriber sees their own row...
      assert(deliverable(OwnF, NoSubFilter, "alice", { kind: Created, row: alice })),
      // ...but not another principal's.
      assert(not(deliverable(OwnF, NoSubFilter, "alice", { kind: Updated, row: bob }))),
      // A subscription filter further narrows an unscoped policy.
      assert(deliverable(AllF, OwnerEquals("alice"), "alice", { kind: Created, row: alice })),
      assert(not(deliverable(AllF, OwnerEquals("alice"), "alice", { kind: Created, row: bob }))),
      // Both must agree: an Owner policy for alice combined with a
      // subscription filter for bob admits neither row.
      assert(not(deliverable(OwnF, OwnerEquals("bob"), "alice", { kind: Updated, row: alice }))),
      assert(not(deliverable(OwnF, OwnerEquals("bob"), "alice", { kind: Updated, row: bob }))),
    }
  }

  run crossResourceNeverDeliversTest = {
    val sub = { resource: "threads", policy: AllF, subFilter: NoSubFilter, callerId: "alice" }
    val row = { owner: "alice" }
    all {
      assert(admits(sub, { resource: "threads", kind: Created, row: row })),
      assert(not(admits(sub, { resource: "messages", kind: Created, row: row }))),
      assert(crossResourceNeverDelivers(sub, "messages", Created, row)),
    }
  }

  run subFilterNeverWidensBeyondPolicyTest = {
    val row = { owner: "alice" }
    all {
      // An Owner policy for alice plus a subscription filter for alice's own
      // id: the row still has to be alice's to be delivered either way.
      assert(subFilterNeverWidensBeyondPolicy(OwnF, NoSubFilter, "alice", row)),
      assert(subFilterNeverWidensBeyondPolicy(OwnF, OwnerEquals("alice"), "alice", row)),
      // A NoMatch policy is never widened by any subscription filter.
      assert(subFilterNeverWidensBeyondPolicy(NoMatchF, OwnerEquals("alice"), "alice", row)),
      assert(not(deliverable(NoMatchF, OwnerEquals("alice"), "alice", { kind: Created, row: row }))),
    }
  }

  run deleteFailsClosedWhenScopedTest = {
    val row = { owner: "alice" }
    all {
      // Fully unscoped: the delete is delivered.
      assert(deliverable(AllF, NoSubFilter, "alice", { kind: Deleted, row: row })),
      // Row-scoped policy: never delivered, even for the row's own owner.
      assert(not(deliverable(OwnF, NoSubFilter, "alice", { kind: Deleted, row: row }))),
      assert(deleteFailsClosedWhenScoped(OwnF, NoSubFilter, "alice", row)),
      // Unscoped policy but a narrowing subscription filter: still suppressed.
      assert(not(deliverable(AllF, OwnerEquals("alice"), "alice", { kind: Deleted, row: row }))),
      assert(deleteFailsClosedWhenScoped(AllF, OwnerEquals("alice"), "alice", row)),
      // A deny policy is (vacuously) covered by the same rule.
      assert(not(deliverable(NoMatchF, NoSubFilter, "alice", { kind: Deleted, row: row }))),
      assert(deleteFailsClosedWhenScoped(NoMatchF, NoSubFilter, "alice", row)),
    }
  }

  run noMatchDeliversNothingTest = {
    val row = { owner: "alice" }
    all {
      assert(noMatchDeliversNothing(NoSubFilter, "alice", { kind: Created, row: row })),
      assert(noMatchDeliversNothing(NoSubFilter, "alice", { kind: Updated, row: row })),
      assert(noMatchDeliversNothing(NoSubFilter, "alice", { kind: Deleted, row: row })),
      assert(noMatchDeliversNothing(OwnerEquals("alice"), "alice", { kind: Created, row: row })),
    }
  }

  run unscopedAlwaysDeliversTest = {
    val alice = { owner: "alice" }
    val bob = { owner: "bob" }
    all {
      assert(unscopedAlwaysDelivers("alice", { kind: Created, row: alice })),
      assert(unscopedAlwaysDelivers("alice", { kind: Updated, row: bob })),
      assert(unscopedAlwaysDelivers("alice", { kind: Deleted, row: alice })),
    }
  }

  // The invariants hold across every (filter, subFilter, caller, row, kind)
  // combination in the finite universes above, not just the hand-picked
  // examples the tests above exercise.
  run invariantsHold = {
    all {
      allFilters.forall(policy =>
        allSubFilters.forall(sf =>
          allCallers.forall(callerId =>
            allRows.forall(row =>
              all {
                subFilterNeverWidensBeyondPolicy(policy, sf, callerId, row),
                deleteFailsClosedWhenScoped(policy, sf, callerId, row),
              }
            )
          )
        )
      ),
      allSubFilters.forall(sf =>
        allCallers.forall(callerId =>
          allRows.forall(row =>
            allKinds.forall(kind =>
              noMatchDeliversNothing(sf, callerId, { kind: kind, row: row })
            )
          )
        )
      ),
      allCallers.forall(callerId =>
        allRows.forall(row =>
          allKinds.forall(kind =>
            unscopedAlwaysDelivers(callerId, { kind: kind, row: row })
          )
        )
      ),
    }
  }
}
