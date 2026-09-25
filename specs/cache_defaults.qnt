// cache_defaults.qnt — formal spec for the default cache-strategy selection.
//
// When a resource does not override get_cache_strategy(), the framework picks a
// strategy from two facts: whether the resource is read-only (its advertised
// actions contain no write action), and whether its read model carries an
// ``updated_at`` field.
//
// Selection, in order:
//   * read-only                -> Optimistic (a freshness window, no validator)
//   * else with updated_at     -> LastModified
//   * else                     -> Etag
//
// The read-only branch comes first: a surface that cannot change gains nothing
// from a validator, so it gets a freshness window regardless of its fields.
// That window is *private*: read-only does not imply the same bytes for every
// caller (a permission-narrowed search), so a shared cache must not store it.
//
// Invariants:
//   - readOnlyIsOptimistic: a read-only resource always selects Optimistic,
//     whatever its fields.
//   - writeActionForcesValidator: any resource advertising a write action is
//     never read-only, so it selects a validator strategy.
//   - selectionIsTotal: every (readOnly, hasUpdatedAt) pair maps to exactly one
//     of the three strategies.
//   - readOnlyWindowIsPrivate: the read-only default is always caller-scoped,
//     so a shared cache may not replay it to another caller.

module cache_defaults {
  // The standard actions, as far as read-only detection cares.
  type Action = Create | Read | Update | Delete | Search | Count | BatchRead | BatchEdit

  // The strategy the default selection can return.
  type Strategy = Optimistic | LastModified | Etag

  // The write actions: advertising any of these makes a resource writable.
  pure val writeActions: Set[Action] = Set(Create, Update, Delete, BatchEdit)

  // A resource is read-only when it advertises no write action.
  pure def isReadOnly(supported: Set[Action]): bool =
    supported.intersect(writeActions).size() == 0

  // The freshness window a read-only resource gets (seconds). The optimistic
  // strategy requires a positive window, so this must be > 0.
  pure val readOnlyExpireIn: int = 600

  // Whether the read-only freshness window is caller-scoped (a shared cache
  // must not store it). Read-only does not imply the same bytes per caller.
  pure val readOnlyIsPrivate: bool = true

  // The default selection: read-only first, then updated_at, then ETag.
  pure def defaultStrategy(readOnly: bool, hasUpdatedAt: bool): Strategy =
    if (readOnly) Optimistic
    else if (hasUpdatedAt) LastModified
    else Etag

  // Whether the default selection marks the freshness window private. Only the
  // read-only branch has a freshness window, and it is always private.
  pure def defaultIsPrivate(readOnly: bool): bool =
    readOnly and readOnlyIsPrivate

  // Tests

  val readOnlyActions: Set[Action] = Set(Read, Search, Count, BatchRead)
  val allActions: Set[Action] = Set(
    Create, Read, Update, Delete, Search, Count, BatchRead, BatchEdit
  )

  run readOnlyIsOptimistic = {
    all {
      assert(isReadOnly(readOnlyActions)),
      // Regardless of the read model's fields.
      assert(defaultStrategy(isReadOnly(readOnlyActions), false) == Optimistic),
      assert(defaultStrategy(isReadOnly(readOnlyActions), true) == Optimistic),
    }
  }

  run writeActionForcesValidator = {
    all {
      // The full surface is writable...
      assert(not(isReadOnly(allActions))),
      // ...and a single write action is enough to make it writable.
      assert(not(isReadOnly(Set(Read, Create)))),
      assert(defaultStrategy(isReadOnly(allActions), true) == LastModified),
      assert(defaultStrategy(isReadOnly(allActions), false) == Etag),
    }
  }

  run writableWithUpdatedAtGetsLastModified = {
    assert(defaultStrategy(false, true) == LastModified)
  }

  run writableWithoutUpdatedAtGetsEtag = {
    assert(defaultStrategy(false, false) == Etag)
  }

  run readOnlyDefaultHasPositiveWindow = {
    assert(readOnlyExpireIn > 0)
  }

  run readOnlyWindowIsPrivate = {
    all {
      // The read-only default is caller-scoped whatever its fields...
      assert(defaultIsPrivate(isReadOnly(readOnlyActions))),
      assert(defaultIsPrivate(isReadOnly(Set()))),
      // ...while a writable resource selects a validator and no window.
      assert(not(defaultIsPrivate(isReadOnly(allActions)))),
      assert(not(defaultIsPrivate(false)))
    }
  }

  run selectionIsTotal = {
    // Every input pair maps to one of the three strategies (never anything else).
    all {
      Set(true, false).forall(readOnly =>
        Set(true, false).forall(hasUpdatedAt =>
          Set(Optimistic, LastModified, Etag).contains(
            defaultStrategy(readOnly, hasUpdatedAt)
          )
        )
      ),
      // And the read-only axis is exactly what separates Optimistic from the rest.
      defaultStrategy(true, true) == Optimistic,
      defaultStrategy(false, true) == LastModified,
      defaultStrategy(false, false) == Etag,
    }
  }

  run invariantsHold = {
    all {
      // readOnlyIsOptimistic, over every field state.
      Set(true, false).forall(hasUpdatedAt =>
        defaultStrategy(isReadOnly(readOnlyActions), hasUpdatedAt) == Optimistic
      ),
      // writeActionForcesValidator: every writable action set is never read-only.
      not(isReadOnly(allActions)),
      not(isReadOnly(Set(Read, Create))),
      not(isReadOnly(Set(Read, Update))),
      not(isReadOnly(Set(Read, Delete))),
      not(isReadOnly(Set(Read, BatchEdit))),
      // selectionIsTotal.
      readOnlyExpireIn > 0,
      // readOnlyWindowIsPrivate.
      defaultIsPrivate(isReadOnly(readOnlyActions)),
      defaultIsPrivate(isReadOnly(Set())),
      not(defaultIsPrivate(isReadOnly(allActions))),
      not(defaultIsPrivate(false)),
    }
  }
}
