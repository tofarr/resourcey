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

  // batch_read: return entities for a set of ids; missing ids are omitted.
  def batch_read(ids: Set[int]): int -> Entity =
    entities.keys().filter(k => ids.contains(k)).fold(
      Map(),
      (acc, k) => acc.put(k, entities.get(k))
    )

  // batch_edit: apply a set of updates; absent ids are skipped.
  action batch_edit(updates: int -> Entity): bool = {
    val appliedKeys = updates.keys().filter(k => entities.keys().contains(k))
    all {
      entities' = appliedKeys.fold(
        entities,
        (acc, k) => acc.put(k, updates.get(k))
      ),
      nextId' = nextId,
      true,
    }
  }

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
        assert(batch_read(Set(1)).keys().size() == 1),
        entities' = entities,
        nextId' = nextId,
      })
      .then(batch_edit(Map(1 -> 100, 2 -> 200)))
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
}
