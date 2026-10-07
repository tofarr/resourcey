// conditional_write.qnt — formal spec for conditional update / delete.
//
// A single `update` / `delete` (and each write node of a `batch_edit`) may
// carry an optional native `SearchFilter` **condition**. The write applies only
// when the target row's *pre-write* value satisfies the condition; a failed
// condition behaves exactly like an absent row — `update` returns `None`,
// `delete` returns `false` — so a caller cannot tell a condition miss from a
// missing row (no existence leak). In SQL this is the write's extra `WHERE`
// clause; in Mongo the write query's `$and`-merged filter. The authorization
// policy's own row-scoping filter is folded into the same condition as an
// additional (AND) constraint, so a row the policy does not grant is a miss.
//
// Invariants:
//   - a failed-condition write leaves the store unchanged (no partial effect);
//   - a condition miss and an absent target are indistinguishable (both Absent
//     for update, both false for delete);
//   - a policy scope can only *narrow* the effective condition (AND), never
//     widen it, so folding a policy never grants a row the caller's own
//     condition would reject.

module conditional_write {
  // A stored entity keyed by an id and carrying an int value.
  type Entity = int

  // The result of a conditional update: the new value when the write applied,
  // or Absent when the id was missing or the condition did not hold (models
  // ``None``).
  type UpdateResult = Present(Entity) | Absent

  // A condition over a row's value. Any is "no condition" (the identity of
  // AND); Eq matches a specific value; Never matches nothing (the annihilator —
  // e.g. two incompatible Eq scopes ANDed together).
  type Condition = Any | Eq(int) | Never

  // Whether a row value satisfies a condition.
  pure def matches(value: Entity, cond: Condition): bool =
    match cond {
      Any => true |
      Eq(v) => value == v |
      Never => false
    }

  // AND-combine two conditions (the caller's condition and a policy's scope):
  // Any is the identity, Never is the annihilator, and two Eq scopes agree only
  // when their values are equal — otherwise they can never both hold.
  pure def andCondition(a: Condition, b: Condition): Condition =
    match a {
      Never => Never |
      Any => b |
      Eq(x) =>
        match b {
          Never => Never |
          Any => a |
          Eq(y) => if (x == y) Eq(x) else Never
        }
    }

  // The condition actually applied to a write: the caller's condition ANDed
  // with the policy's row scope (Any when there is no policy constraint). This
  // is the fold the AuthorizedService performs before delegating.
  pure def effectiveCondition(caller: Condition, policy: Condition): Condition =
    andCondition(caller, policy)

  // Resource state: a map from id to value, plus a monotonic counter.
  var entities: int -> Entity
  var nextId: int

  action init = all {
    entities' = Map(),
    nextId' = 1,
  }

  // create: allocate a new id and store the value.
  action create(value: Entity): bool = {
    val id = nextId
    all {
      entities' = entities.put(id, value),
      nextId' = nextId + 1,
      true,
    }
  }

  def read(id: int): Entity = entities.get(id)

  // Whether a conditional update at ``id`` would apply against the current
  // state (the row exists and its current value matches ``cond``).
  def canUpdate(id: int, cond: Condition): bool =
    entities.keys().contains(id) and matches(entities.get(id), cond)

  // Whether a conditional delete at ``id`` would apply against the current
  // state.
  def canDelete(id: int, cond: Condition): bool =
    entities.keys().contains(id) and matches(entities.get(id), cond)

  // The value a conditional update would return: the new value on success,
  // Absent on a miss (a failed condition or an absent id — indistinguishable).
  def update_result(id: int, value: Entity, cond: Condition): UpdateResult =
    if (canUpdate(id, cond)) Present(value) else Absent

  // update with a condition: applies only when the current value matches; a
  // miss is a no-op (no partial effect).
  action update(id: int, value: Entity, cond: Condition): bool =
    all {
      entities' = if (canUpdate(id, cond)) entities.put(id, value) else entities,
      nextId' = nextId,
      true,
    }

  // delete with a condition: applies only when the current value matches; a
  // miss is a no-op returning false.
  action delete(id: int, cond: Condition): bool =
    all {
      entities' =
        if (canDelete(id, cond))
          entities.keys().filter(k => k != id).fold(
            Map(),
            (acc, k) => acc.put(k, entities.get(k))
          )
        else
          entities,
      nextId' = nextId,
      true,
    }

  // A conditional batch_edit node mirrors the single action's result policy: an
  // update yields Present/Absent, a delete yields true/false.
  type CondEdit = UpdateEdit({ id: int, value: Entity, cond: Condition }) |
                  DeleteEdit({ id: int, cond: Condition })

  // The positional results a conditional batch_edit would return, computed from
  // the state it is called in. The edits are applied left to right, each seeing
  // the previous edit's effect (an update then a delete of the same id).
  pure def apply_edits(
    state: int -> Entity,
    edits: List[CondEdit],
  ): (int -> Entity, List[UpdateResult]) =
    edits.foldl((state, List()), (acc, e) =>
      match e {
        UpdateEdit(u) =>
          if (acc._1.keys().contains(u.id) and matches(acc._1.get(u.id), u.cond))
            (acc._1.put(u.id, u.value), acc._2.append(Present(u.value)))
          else
            (acc._1, acc._2.append(Absent)) |
        DeleteEdit(d) =>
          if (acc._1.keys().contains(d.id) and matches(acc._1.get(d.id), d.cond))
            (
              acc._1.keys().filter(k => k != d.id).fold(Map(), (m, k) => m.put(k, acc._1.get(k))),
              acc._2.append(Absent),
            )
          else
            (acc._1, acc._2.append(Absent))
      }
    )

  // The positional result list for a conditional batch_edit (deletes are Absent
  // in the result list, exactly as for the unconditional batch).
  def batch_edit_result(edits: List[CondEdit]): List[UpdateResult] =
    apply_edits(entities, edits)._2

  action batch_edit(edits: List[CondEdit]): bool = {
    val outcome = apply_edits(entities, edits)
    all {
      entities' = outcome._1,
      nextId' = nextId,
      true,
    }
  }

  // Nondeterministic step for invariant checking.
  action step: bool = all {
    entities' = entities,
    nextId' = nextId,
    true
  }

  // Invariant: nextId stays above every stored key (mirrors resource_actions).
  val keysAreBelowCounter = entities.keys().forall(k => k < nextId)

  // Tests

  // A holding condition applies the write.
  run updateWithHoldingConditionApplies = {
    init
      .then(create(10))
      .then(all {
        assert(update_result(1, 20, Eq(10)) == Present(20)),
        entities' = entities,
        nextId' = nextId,
      })
      .then(update(1, 20, Eq(10)))
      .then(all {
        assert(read(1) == 20),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // A failing condition is a no-op returning Absent (None) and leaves the row.
  run updateWithFailingConditionIsAbsentAndUnchanged = {
    init
      .then(create(10))
      .then(all {
        assert(update_result(1, 20, Eq(99)) == Absent),
        entities' = entities,
        nextId' = nextId,
      })
      .then(update(1, 20, Eq(99)))
      .then(all {
        assert(read(1) == 10),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // A condition miss and an absent id are indistinguishable (both Absent).
  run conditionMissMatchesAbsentTarget = {
    init
      .then(create(10))
      .then(all {
        assert(update_result(1, 20, Eq(99)) == Absent),   // present id, failing condition
        assert(update_result(999, 20, Eq(10)) == Absent), // absent id
        entities' = entities,
        nextId' = nextId,
      })
  }

  // A holding delete removes the row and reports true.
  run deleteWithHoldingConditionRemoves = {
    init
      .then(create(10))
      .then(all {
        assert(canDelete(1, Eq(10))),
        entities' = entities,
        nextId' = nextId,
      })
      .then(delete(1, Eq(10)))
      .then(all {
        assert(not(entities.keys().contains(1))),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // A failing delete reports false and leaves the row.
  run deleteWithFailingConditionIsFalseAndUnchanged = {
    init
      .then(create(10))
      .then(all {
        assert(not(canDelete(1, Eq(99)))),
        entities' = entities,
        nextId' = nextId,
      })
      .then(delete(1, Eq(99)))
      .then(all {
        assert(read(1) == 10),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // The policy scope is folded in: with a policy condition Eq(10), a row whose
  // value is 10 is writable, a row whose value is 99 is a miss — even with no
  // caller condition.
  run policyScopeGatesTheWrite = {
    init
      .then(create(10))
      .then(create(99))
      .then(all {
        assert(effectiveCondition(Any, Eq(10)) == Eq(10)),
        assert(update_result(1, 20, effectiveCondition(Any, Eq(10))) == Present(20)),
        assert(update_result(2, 20, effectiveCondition(Any, Eq(10))) == Absent),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // A policy scope can only narrow: a caller condition ANDed with an
  // incompatible policy scope never holds (Never), so the write is a miss even
  // on a row the caller's condition alone would admit.
  run policyScopeNarrowsNeverWidens = {
    init
      .then(create(10))
      .then(all {
        assert(effectiveCondition(Eq(10), Eq(20)) == Never),
        assert(update_result(1, 30, effectiveCondition(Eq(10), Eq(20))) == Absent),
        entities' = entities,
        nextId' = nextId,
      })
      .then(update(1, 30, effectiveCondition(Eq(10), Eq(20))))
      .then(all {
        assert(read(1) == 10),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // A condition that can never hold (Never) is always a miss.
  run neverConditionIsAlwaysAbsent = {
    init
      .then(create(10))
      .then(all {
        assert(update_result(1, 20, Never) == Absent),
        assert(not(canDelete(1, Never))),
        entities' = entities,
        nextId' = nextId,
      })
      .then(update(1, 20, Never))
      .then(delete(1, Never))
      .then(all {
        assert(read(1) == 10),
        entities' = entities,
        nextId' = nextId,
      })
  }

  // A conditional batch_edit aligns results positionally, per node.
  run batchEditAlignsConditionalResults = {
    init
      .then(create(10))
      .then(create(20))
      .then(all {
        assert(batch_edit_result(List(
          UpdateEdit({ id: 1, value: 100, cond: Eq(10) }),   // holds -> Present(100)
          UpdateEdit({ id: 2, value: 200, cond: Eq(99) }),   // fails -> Absent
          DeleteEdit({ id: 1, cond: Eq(100) }),              // holds -> removed (Absent)
          DeleteEdit({ id: 2, cond: Eq(0) }),                // fails -> Absent
        )) == List(Present(100), Absent, Absent, Absent)),
        entities' = entities,
        nextId' = nextId,
      })
      .then(batch_edit(List(
        UpdateEdit({ id: 1, value: 100, cond: Eq(10) }),
        UpdateEdit({ id: 2, value: 200, cond: Eq(99) }),
        DeleteEdit({ id: 1, cond: Eq(100) }),
        DeleteEdit({ id: 2, cond: Eq(0) }),
      )))
      .then(all {
        assert(not(entities.keys().contains(1))),  // deleted
        assert(read(2) == 20),                     // update failed -> unchanged
        entities' = entities,
        nextId' = nextId,
      })
  }
}
