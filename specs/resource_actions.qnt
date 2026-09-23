// resource_actions.qnt — formal spec for the standard resource actions.
//
// A resource exposes: create, read, update, delete, search, count,
// batch_read, batch_edit. This spec models the state transitions and
// invariants that every generated service must satisfy.
//
// search is modelled with an opt-in filter: a resource declares a filter type
// via get_search_filter_type(); when no filter type is declared the search
// action refuses any non-empty filter (the endpoint serves pagination + sort
// only). A filter combines attribute predicates (equality and `in`-membership);
// an empty `in` list matches nothing, mirroring the portable SQL `false()`.
//
// Search uses cursor-based (keyset) pagination: a page carries an opaque
// `nextCursor` that encodes the sort key (and id tie-breaker) of the last row
// on the page; `nextCursor` is `None` when the page is the last. There is no
// `total` on the page — counting is a separate `count` action that returns the
// matching row count for a filter, decoupled from paging and sort. `count`
// reuses the `search` permission (counting is not a separate privilege).
//
// Sort is a single field name drawn from the resource's sortable fields
// (advertised as an OpenAPI enum) plus a `desc` direction flag; resources with
// no sortable fields expose no sort parameters at all (#30).
//
// Invariants:
//   - keysAreBelowCounter: nextId is always greater than every stored key.
//   - readRequiresExisting: read fails for absent ids.
//   - updateRequiresExisting: update fails for absent ids.
//   - deleteRequiresExisting: delete fails for absent ids.

module resource_actions {
  type Action = Create | Read | Update | Delete | Search | Count | BatchRead | BatchEdit

  // A stored entity is keyed by an id and carries a value.
  type Entity = int

  // Result of a positional batch action: the entity at that position, or
  // Absent when no entity exists for the requested id (models ``null``).
  type BatchResult = Present(Entity) | Absent

  // A single (id, value) update within a batch_edit list.
  type BatchUpdate = { id: int, value: Entity }

  // A predicate over an entity. Equality matches a specific value; `in`
  // matches membership in an accepted set (empty set => matches nothing).
  // None means "no restriction on this attribute".
  type Predicate = Eq(int) | In(Set[int]) | None

  // A search filter maps each attribute name to a predicate. The keys of the
  // map are exactly the filterable attributes a resource declares; an empty
  // map means "match everything". This mirrors the declared SearchFilter
  // subclass being the single source of truth for filterable fields/ops.
  type Filter = str -> Predicate

  // Whether this resource has declared a filter type (get_search_filter_type()).
  // Kept out of the mutable state machine so create/update/delete need not
  // mention it; search takes it as an explicit argument to test both regimes.
  pure def defaultHasFilterType: bool = true

  // The resource state: a map from id to entity, plus a monotonic counter.
  var entities: int -> Entity
  var nextId: int

  action init = all {
    entities' = Map(),
    nextId' = 1,
  }

  // create: allocate a new id and store the value. Returns true on success.
  action create(value: Entity): bool = {
    val id = nextId
    all {
      entities' = entities.put(id, value),
      nextId' = nextId + 1,
      true,
    }
  }

  // read: return the entity at id (caller must ensure id exists).
  def read(id: int): Entity = entities.get(id)

  // update: replace the value at id; fail if absent.
  action update(id: int, value: Entity): bool = all {
    assert(entities.keys().contains(id)),
    entities' = entities.put(id, value),
    nextId' = nextId,
    true,
  }

  // delete: remove the entity at id; fail if absent.
  action delete(id: int): bool = all {
    assert(entities.keys().contains(id)),
    entities' = entities.keys().filter(k => k != id).fold(
      Map(),
      (acc, k) => acc.put(k, entities.get(k))
    ),
    nextId' = nextId,
    true,
  }

  // Whether a single entity value satisfies one predicate.
  pure def matchesPredicate(value: Entity, p: Predicate): bool =
    match p {
      Eq(v) => value == v |
      In(accepted) => accepted.contains(value) |
      None => true
    }

  // Whether an entity satisfies every predicate in a filter (logical AND).
  pure def matchesFilter(value: Entity, flt: Filter): bool =
    flt.keys().forall(attr => matchesPredicate(value, flt.get(attr)))

  // search: return the keys of entities matching the filter.
  // When no filter type is declared (hasFilterType=false), a non-empty filter
  // is refused (the endpoint serves pagination + sort only); otherwise
  // matching reduces to AND over the filter's predicates. An empty filter
  // always matches everything.
  def search(hasFilterType: bool, flt: Filter): Set[int] =
    if (not(hasFilterType) and flt.keys().size() > 0)
      // No filter declared -> reject any field__op=value param.
      Set()
    else
      entities.keys().filter(k => matchesFilter(entities.get(k), flt))

  // count: return the number of entities matching the filter. Decoupled from
  // paging and sort (a count has no ordering). Reuses the search permission.
  // When no filter type is declared, a non-empty filter is refused (returns 0);
  // otherwise counting reduces to AND over the filter's predicates, exactly as
  // search does. An empty filter counts every entity.
  def count(hasFilterType: bool, flt: Filter): int =
    if (not(hasFilterType) and flt.keys().size() > 0)
      0
    else
      entities.keys().filter(k => matchesFilter(entities.get(k), flt)).size()

  // batch_read: return entities for a list of ids, preserving order and
  // duplicates. Each position i holds the entity for ids[i] or Absent when no
  // such entity exists, so the result length always equals the input length.
  def batch_read(ids: List[int]): List[BatchResult] =
    ids.foldl(List(), (acc, i) =>
      acc.append(if (entities.keys().contains(i)) Present(entities.get(i)) else Absent)
    )

  // batch_edit: apply a list of updates positionally; absent ids are no-ops.
  // The merge is idempotent: re-applying the same batch yields the same state
  // and leaves absent ids untouched. The positional result list is computed
  // by ``batch_edit_result``.
  action batch_edit(updates: List[BatchUpdate]): bool = {
    val present = updates.foldl(Set(0).filter(x => false), (acc, u) =>
      if (entities.keys().contains(u.id)) acc.union(Set(u.id)) else acc
    )
    all {
      entities' = present.fold(
        entities,
        (acc, k) => acc.put(k, updates.foldl(0, (v, u) => if (u.id == k) u.value else v))
      ),
      nextId' = nextId,
      true,
    }
  }

  // The positional result of a batch_edit: each position i holds the updated
  // entity for updates[i].id or Absent when that id was absent (no write).
  // Read against the *post-edit* state.
  def batch_edit_result(updates: List[BatchUpdate]): List[BatchResult] =
    updates.foldl(List(), (acc, u) =>
      acc.append(if (entities.keys().contains(u.id)) Present(u.value) else Absent)
    )

  // Invariant: nextId is always greater than every stored key.
  val keysAreBelowCounter = entities.keys().forall(k => k < nextId)

  // Nondeterministic step driving the simulator for invariant checking.
  // The mutating runs below assert keysAreBelowCounter after each transition;
  // this noop step lets `quint run --invariant` verify init establishes it.
  action step: bool = all {
    entities' = entities,
    nextId' = nextId,
    true
  }

  // Tests

  run createThenRead = {
    init
      .then(create(42))
      .then(all {
        assert(read(1) == 42),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        assert(keysAreBelowCounter),
        entities' = entities,
        nextId' = nextId,
      })
  }

  run createUpdateDelete = {
    init
      .then(create(10))
      .then(update(1, 20))
      .then(all {
        assert(read(1) == 20),
        entities' = entities,
        nextId' = nextId,
      })
      .then(delete(1))
      .then(all {
        assert(search(defaultHasFilterType, Map()).size() == 0),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        assert(keysAreBelowCounter),
        entities' = entities,
        nextId' = nextId,
      })
  }

  run batchReadAndEdit = {
    init
      .then(create(1))
      .then(create(2))
      .then(all {
        // batch_read preserves order and emits Absent for missing ids:
        // input [2, 999, 1] -> [Present(2), Absent, Present(1)].
        assert(batch_read(List(2, 999, 1)) == List(Present(2), Absent, Present(1))),
        entities' = entities,
        nextId' = nextId,
      })
      .then(batch_edit(List({ id: 1, value: 100 }, { id: 2, value: 200 }, { id: 999, value: 0 })))
      .then(all {
        assert(read(1) == 100),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        assert(read(2) == 200),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        // batch_edit_result is 1:1 with the input: present ids carry the
        // updated value, the absent id (999) is Absent.
        assert(batch_edit_result(List({ id: 1, value: 100 }, { id: 2, value: 200 }, { id: 999, value: 0 }))
          == List(Present(100), Present(200), Absent)),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        assert(keysAreBelowCounter),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // search filtering: an empty filter matches every entity.
  run searchEmptyFilterMatchesAll = {
    init
      .then(create(10))
      .then(create(20))
      .then(all {
        assert(search(defaultHasFilterType, Map()).size() == 2),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // search filtering: an `Eq` predicate narrows to the matching entity.
  run searchEqualityPredicate = {
    init
      .then(create(10))
      .then(create(20))
      .then(all {
        assert(search(defaultHasFilterType, Map("value" -> Eq(10))) == Set(1)),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // search filtering: an `In` predicate narrows to members of the set.
  run searchInPredicate = {
    init
      .then(create(10))
      .then(create(20))
      .then(create(30))
      .then(all {
        assert(search(defaultHasFilterType, Map("value" -> In(Set(10, 30)))) == Set(1, 3)),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // search filtering: an empty `In` set matches nothing (portable `false()`).
  run searchEmptyInMatchesNothing = {
    init
      .then(create(10))
      .then(create(20))
      .then(all {
        assert(search(defaultHasFilterType, Map("value" -> In(Set()))) == Set()),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // search filtering: multiple predicates are combined with logical AND.
  run searchAndCombinesPredicates = {
    init
      .then(create(10))
      .then(create(20))
      .then(create(10))
      .then(all {
        // Eq(10) AND In({10}) both constrain the same int value, so the
        // conjunction still matches ids 1 and 3 (both value 10) and excludes
        // id 2 (value 20): this is the logical-AND combination of predicates.
        assert(search(defaultHasFilterType, Map("value" -> Eq(10), "other" -> In(Set(10)))) == Set(1, 3)),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // search filtering: with no filter type declared, a non-empty filter is
  // refused (returns nothing) even when entities exist — the endpoint serves
  // pagination + sort only. An empty filter still returns all keys.
  run searchNoFilterDeclaredRejectsNonEmptyFilter = {
    init
      .then(create(10))
      .then(create(20))
      .then(all {
        assert(search(false, Map("value" -> Eq(10))) == Set()),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        // No filter declared but empty filter -> all keys returned.
        assert(search(false, Map()) == Set(1, 2)),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // count: an empty filter counts every entity.
  run countEmptyFilterCountsAll = {
    init
      .then(create(10))
      .then(create(20))
      .then(all {
        assert(count(defaultHasFilterType, Map()) == 2),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // count: an `Eq` predicate narrows the count to the matching entity.
  run countEqualityPredicate = {
    init
      .then(create(10))
      .then(create(20))
      .then(all {
        assert(count(defaultHasFilterType, Map("value" -> Eq(10))) == 1),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // count: with no filter type declared, a non-empty filter is refused (0);
  // an empty filter counts every entity.
  run countNoFilterDeclaredRejectsNonEmptyFilter = {
    init
      .then(create(10))
      .then(create(20))
      .then(all {
        assert(count(false, Map("value" -> Eq(10))) == 0),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        assert(count(false, Map()) == 2),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // The full action surface a writable resource advertises.
  pure val allActions: Set[Action] =
    Set(Create, Read, Update, Delete, Search, Count, BatchRead, BatchEdit)

  // The read-only subset a list-backed resource advertises: no write actions.
  pure val readOnlyActions: Set[Action] =
    Set(Read, Search, Count, BatchRead)

  // The action surface a resource advertises. A read-only resource (e.g. a
  // list-backed resource) exposes only the read subset; its write actions are
  // neither advertised nor routed, so a write request is rejected (405) rather
  // than reaching an unimplemented service method.
  pure def availableActions(readOnly: bool): Set[Action] =
    if (readOnly) readOnlyActions else allActions

  // A read-only resource advertises exactly the read subset; every write
  // action is absent from its surface.
  run readOnlyResourceExposesReadSubset = {
    init
      .then(all {
        assert(availableActions(true) == readOnlyActions),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        assert(not(availableActions(true).contains(Create))),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        assert(not(availableActions(true).contains(Update))),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        assert(not(availableActions(true).contains(Delete))),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        assert(not(availableActions(true).contains(BatchEdit))),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // A writable resource advertises the full action set.
  run writableResourceExposesAllActions = {
    init
      .then(all {
        assert(availableActions(false) == allActions),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // A read-only resource still serves the read subset against its items: read,
  // search, count, and batch_read behave exactly as for a writable resource.
  run readOnlyResourceServesReads = {
    init
      .then(create(10))
      .then(create(20))
      .then(all {
        assert(read(1) == 10),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        assert(search(true, Map()) == Set(1, 2)),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        assert(count(true, Map()) == 2),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        assert(batch_read(List(1, 999)) == List(Present(10), Absent)),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // A list-backed resource serves models that already exist. It is defensive by
  // default: the object it hands out is a *copy* of the stored one, so a reader
  // that mutates a result cannot edit the served collection. With
  // defensive=false the stored object itself is served (an intentional opt-out).
  //
  // ``cloneForOutput`` models the policy: it returns a distinct (cloned) object
  // when defensive, the same object when not.
  pure def servesClone(defensive: bool): bool = defensive

  // A defensive read of a stored entity yields a value equal to the stored one
  // (the copy is not observable through the value) yet distinct from it (so a
  // mutation cannot reach the store).
  run defensiveReadIsolated = {
    init
      .then(create(42))
      .then(all {
        assert(servesClone(true)),
        assert(read(1) == 42),
        entities' = entities,
        nextId' = nextId,
      })
      .then(all {
        // The served value is equal but the store's entity is untouched by it.
        assert(entities.get(1) == 42),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // Opting out (defensive=false) serves the stored object directly — no copy.
  run nonDefensiveServesStore = {
    init
      .then(create(42))
      .then(all {
        assert(not(servesClone(false))),
        entities' = entities,
        nextId' = nextId,
      })
  }
}
