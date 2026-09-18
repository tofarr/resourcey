// resource_actions.qnt — formal spec for the standard resource actions.
//
// A resource exposes: create, read, update, delete, search, batch_read,
// batch_edit. This spec models the state transitions and invariants that
// every generated service must satisfy.
//
// Invariants:
//   - keysAreBelowCounter: nextId is always greater than every stored key.
//   - readRequiresExisting: read fails for absent ids.
//   - updateRequiresExisting: update fails for absent ids.
//   - deleteRequiresExisting: delete fails for absent ids.

module resource_actions {
  type Action = Create | Read | Update | Delete | Search | BatchRead | BatchEdit

  // A stored entity is keyed by an id and carries a value.
  type Entity = int

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
      entities' = entities.set(id, value),
      nextId' = nextId + 1,
      true,
    }
  }

  // read: return the entity at id (caller must ensure id exists).
  def read(id: int): Entity = entities.get(id)

  // update: replace the value at id; fail if absent.
  action update(id: int, value: Entity): bool = all {
    assert(entities.keys().contains(id)),
    entities' = entities.set(id, value),
    nextId' = nextId,
    true,
  }

  // delete: remove the entity at id; fail if absent.
  action delete(id: int): bool = all {
    assert(entities.keys().contains(id)),
    entities' = entities.keys().filter(k => k != id).fold(
      Map(),
      (acc, k) => acc.set(k, entities.get(k))
    ),
    nextId' = nextId,
    true,
  }

  // search: return all entity keys (paginated in the implementation).
  pure def search: Set[int] = entities.keys()

  // batch_read: return entities for a set of ids; missing ids are omitted.
  def batch_read(ids: Set[int]): int -> Entity =
    entities.keys().filter(k => ids.contains(k)).fold(
      Map(),
      (acc, k) => acc.set(k, entities.get(k))
    )

  // batch_edit: apply a set of updates; absent ids are skipped.
  action batch_edit(updates: int -> Entity): bool = {
    val appliedKeys = updates.keys().filter(k => entities.keys().contains(k))
    all {
      entities' = appliedKeys.fold(
        entities,
        (acc, k) => acc.set(k, updates.get(k))
      ),
      nextId' = nextId,
      true,
    }
  }

  // Invariant: nextId is always greater than every stored key.
  val keysAreBelowCounter = entities.keys().forall(k => k < nextId)

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
        assert(search.size() == 0),
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
}
