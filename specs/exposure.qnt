// exposure.qnt — formal spec for exposure and the service-dependency seam
// (issue #62).
//
// A resource is declared once and a single hook decides what the outside world
// sees: get_exposed_resource() returns one of
//
//   Self     -> the resource itself (the default; no override needed)
//   Wrapper  -> a projection over the same storage that hides some fields
//   Hidden   -> None, meaning "internal-only": no routes are registered
//
// Two hooks are deliberately separate: get_exposed_resource() decides *what*
// the outside world sees (the exposed resource drives the schemas AND the
// service), while the DependencyBuilder decides *how* each request's service
// is built (its return is non-optional, so it can never suppress routes).
//
// Invariants:
//   - hiddenRegistersNoRoutes: Hidden is the sole gate on routes.
//   - builderNeverSuppressesRoutes: a builder's presence/kind cannot change
//     whether routes exist -- exposure alone decides.
//   - hiddenFieldsNeverExposed: a wrapper's hidden fields are absent from the
//     exposed read model (field hiding is not cosmetic).
//   - hiddenFieldsNeverQueryable: a wrapper's hidden fields are absent from the
//     query surface (`sort=` / `field__op=`) too -- otherwise the relative
//     order (?sort=secret) or the value (?secret__eq=v) of a field the outside
//     world never sees would leak. The query surface narrows with the read
//     model.
//   - wrapperDoesNotDelegate: a wrapper reports itself exposed; delegating the
//     decision to an inner resource that returns itself would re-expose the
//     hidden fields (the leak this design avoids).
//   - batchActionsRequireTheirSingular: batch_read is dropped when read is
//     absent, so narrowing a surface by removing read cannot leave a batch side
//     door to it. There is no per-resource batch write action any more (#179):
//     the only batch write surface is the app-level `/batch-edit`.
//   - batchEditKindsAreDeclared: every `kind` the app-level batch-edit body
//     admits for a resource maps to a declared singular action (Update
//     included), so a resource that hides update cannot update through the
//     batch.
//   - batchEditTargetsAreWriters: a resource is in the batch-edit union exactly
//     when it declares at least one write action -- the gate that replaced the
//     removed `batch_edit` action.

module exposure {
  type Action = Create | Read | Update | Delete | Search | Count | BatchRead

  // A resource declaration: the fields it stores (the internal truth) and the
  // actions its service can fulfil.
  type Resource = { fields: Set[str], actions: Set[Action] }

  // The get_exposed_resource() decision. Wrapper carries the fields the
  // projection hides from the exposed read model.
  type Exposure = Self | Wrapper(Set[str]) | Hidden

  // Whether the route builder registers any routes. Only Hidden suppresses.
  pure def routesRegistered(e: Exposure): bool =
    match e { Hidden => false | _ => true }

  // The fields the outside world receives: the exposed resource's read model.
  pure def exposedFields(r: Resource, e: Exposure): Set[str] =
    match e {
      Hidden => Set() |
      Self => r.fields |
      Wrapper(hidden) => r.fields.exclude(hidden)
    }

  // A builder either returns the resource's own dependency (Default) or wraps
  // it in a security posture (Secured). Its return is non-optional.
  type Builder = Default | Secured

  // Routes depend only on exposure -- a builder never adds or removes them.
  pure def routesGivenBuilder(e: Exposure, b: Builder): bool =
    all { b == Default or b == Secured, routesRegistered(e) }

  // The actions the service a builder yields for resource `r` serves. A builder
  // is a *decorator*, not a filter: it may wrap the service but never narrows
  // the action contract, so every declared action still has a route.
  pure def actionsServedByBuilder(r: Resource, _b: Builder): Set[Action] =
    r.actions

  // The default builder is the identity: the handler receives the resource's
  // own service, serving exactly its declared actions.
  pure def defaultBuilderIsIdentity(r: Resource): bool =
    actionsServedByBuilder(r, Default) == r.actions

  // A wrapping builder preserves the action contract: wrapping changes *which*
  // service the handler sees, never the set of actions served.
  pure def wrappedBuilderPreservesTheActionContract(r: Resource, b: Builder): bool =
    actionsServedByBuilder(r, b) == r.actions

  // A wrapper reports itself exposed rather than delegating. Delegating to an
  // inner resource that does not override the hook reaches that resource's
  // *default*, which is Self -- so the route builder sees the inner and the
  // hidden fields return. (Delegation is therefore modelled as Self.)
  pure def delegatedExposure(_inner: Exposure): Exposure = Self

  // Invariants

  pure def hiddenRegistersNoRoutes: bool =
    not(routesRegistered(Hidden))

  pure def builderNeverSuppressesRoutes: bool =
    // For every exposure, routes are the same regardless of builder.
    and {
      routesGivenBuilder(Hidden, Default) == routesGivenBuilder(Hidden, Secured),
      routesGivenBuilder(Self, Default) == routesGivenBuilder(Self, Secured),
      routesGivenBuilder(Wrapper(Set("secret")), Default) ==
        routesGivenBuilder(Wrapper(Set("secret")), Secured),
    }

  pure def hiddenFieldsNeverExposed(r: Resource, hidden: Set[str]): bool =
    // The hidden set is disjoint from the exposed fields.
    exposedFields(r, Wrapper(hidden)).intersect(hidden) == Set()

  // The fields the outside world may filter / sort on. Derived from the same
  // exposure decision as the read model, so the two surfaces cannot diverge.
  pure def queryableFields(r: Resource, e: Exposure): Set[str] =
    exposedFields(r, e)

  pure def hiddenFieldsNeverQueryable(r: Resource, hidden: Set[str]): bool =
    queryableFields(r, Wrapper(hidden)).intersect(hidden) == Set()

  pure def wrapperDoesNotDelegate(r: Resource, hidden: Set[str]): bool = {
    val e = Wrapper(hidden)
    // Non-delegation: the wrapper's own projection is what is served.
    val served = exposedFields(r, e)
    // Delegating would reach the inner's default (Self), serving every field.
    val delegated = exposedFields(r, delegatedExposure(e))
    all {
      routesRegistered(e),
      delegated == r.fields,
      served.subseteq(delegated),
    }
  }

  pure def narrowerIsAllowedWiderIsNot(r: Resource, supported: Set[Action]): bool =
    // A resource may narrow its actions, never widen beyond the declaration.
    supported.subseteq(r.actions)

  // The `kind`s the app-level batch-edit body admits for a resource, given the
  // actions it declares. Each kind is admitted only when the matching singular
  // action is declared -- `Update` included, because a resource can be a batch
  // target while hiding `update`, and the batch must not then be a side door to
  // it.
  pure def batchEditKinds(supported: Set[Action]): Set[str] =
    Set()
      .union(if (supported.contains(Create)) Set("Create") else Set())
      .union(if (supported.contains(Update)) Set("Update") else Set())
      .union(if (supported.contains(Delete)) Set("Delete") else Set())

  // Every body `kind` the batch admits maps to a declared action, and every kind
  // whose action is absent is rejected: "Create" <-> Create, "Update" <->
  // Update, "Delete" <-> Delete.
  pure def batchEditKindsAreDeclared(supported: Set[Action]): bool =
    all {
      if (supported.contains(Create)) Set("Create").subseteq(batchEditKinds(supported)) else true,
      if (supported.contains(Update)) Set("Update").subseteq(batchEditKinds(supported)) else true,
      if (supported.contains(Delete)) Set("Delete").subseteq(batchEditKinds(supported)) else true,
      not(supported.contains(Create)) implies
        not(batchEditKinds(supported).contains("Create")),
      not(supported.contains(Update)) implies
        not(batchEditKinds(supported).contains("Update")),
      not(supported.contains(Delete)) implies
        not(batchEditKinds(supported).contains("Delete")),
    }

  // Normalise a declared action set: a batch action is dropped when the
  // singular action it batches is absent. batch_read reads, so it needs read.
  pure def normalizedActions(actions: Set[Action]): Set[Action] =
    actions.exclude(if (actions.contains(Read)) Set() else Set(BatchRead))

  // Normalisation removes only batch actions, only when their prerequisite is
  // gone, and is idempotent. A batch action never survives without its singular
  // action, and a singular action is never removed.
  pure def batchActionsRequireTheirSingular(actions: Set[Action]): bool = {
    val n = normalizedActions(actions)
    all {
      n.contains(BatchRead) implies n.contains(Read),
      actions.intersect(Set(Read, Create, Update, Delete)).subseteq(n),
      normalizedActions(n) == n,
    }
  }

  // Whether a resource appears in the app-level `/batch-edit` union. This is
  // the gate that replaced `Action.BATCH_EDIT`: a resource with no write action
  // has no node kind to accept, so it is skipped.
  pure def isBatchEditTarget(actions: Set[Action]): bool =
    actions.intersect(Set(Create, Update, Delete)).size() > 0

  // A target admits at least one kind, and a non-target admits none: the gate
  // and the body cannot disagree.
  pure def batchEditTargetsAreWriters(actions: Set[Action]): bool =
    all {
      isBatchEditTarget(actions) implies batchEditKinds(actions).size() > 0,
      not(isBatchEditTarget(actions)) implies batchEditKinds(actions) == Set(),
    }

  // Tests

  run selfIsTheDefault = {
    val r = { fields: Set("id", "name", "secret"), actions: Set(Read) }
    val e = Self
    all {
      assert(routesRegistered(e)),
      assert(exposedFields(r, e) == Set("id", "name", "secret")),
    }
  }

  run hiddenRegistersNoRoutesTest = {
    val r = { fields: Set("id", "secret"), actions: Set(Read) }
    all {
      assert(not(routesRegistered(Hidden))),
      assert(exposedFields(r, Hidden) == Set()),
    }
  }

  run wrapperHidesAnExposedResource = {
    val r = { fields: Set("id", "name", "secret"), actions: Set(Read) }
    val e = Wrapper(Set("secret"))
    all {
      assert(routesRegistered(e)),
      assert(exposedFields(r, e) == Set("id", "name")),
      assert(exposedFields(r, e).intersect(Set("secret")) == Set()),
    }
  }

  run wrapperNarrowsTheQuerySurface = {
    // The query surface (sort / filter) narrows with the read model: a hidden
    // field is neither returned nor queryable, so its order and value cannot
    // leak through ?sort= / ?field__op=.
    val r = { fields: Set("id", "name", "secret"), actions: Set(Read, Search) }
    val e = Wrapper(Set("secret"))
    all {
      assert(queryableFields(r, e) == Set("id", "name")),
      assert(queryableFields(r, e).intersect(Set("secret")) == Set()),
      assert(hiddenFieldsNeverQueryable(r, Set("secret"))),
    }
  }

  run delegatedWrapperWouldLeak = {
    // The failure mode: delegating to the inner's default (Self) serves all
    // fields, so the hidden "secret" returns.
    val r = { fields: Set("id", "name", "secret"), actions: Set(Read) }
    assert(exposedFields(r, delegatedExposure(Wrapper(Set("secret")))) == r.fields)
  }

  run builderDoesNotChangeExposure = {
    all {
      assert(routesGivenBuilder(Hidden, Default) == false),
      assert(routesGivenBuilder(Hidden, Secured) == false),
      assert(routesGivenBuilder(Wrapper(Set("secret")), Secured) == true),
    }
  }

  run defaultBuilderIsIdentityTest = {
    // The default builder yields the resource's own service over the
    // request-scoped ctx -- it serves exactly the declared actions.
    val r = { fields: Set("id", "name"), actions: Set(Read, Search, Count) }
    all {
      assert(defaultBuilderIsIdentity(r)),
      assert(actionsServedByBuilder(r, Default) == r.actions),
    }
  }

  run wrappedBuilderPreservesTheActionContractTest = {
    // Wrapping changes which service the handler sees, never the action set:
    // a hidden resource still mounts no routes, and a visible one serves every
    // declared action through the wrapped service.
    val r = { fields: Set("id", "name"), actions: Set(Read, Search, Count) }
    all {
      assert(wrappedBuilderPreservesTheActionContract(r, Default)),
      assert(wrappedBuilderPreservesTheActionContract(r, Secured)),
      assert(actionsServedByBuilder(r, Secured) == r.actions),
    }
  }

  run narrowingIsAllowedWideningIsRejected = {
    val r = { fields: Set("id"), actions: Set(Read, Search) }
    all {
      assert(narrowerIsAllowedWiderIsNot(r, Set(Read))),
      assert(not(narrowerIsAllowedWiderIsNot(r, Set(Read, Create)))),
    }
  }

  run batchEditAdmitsOnlyDeclaredActions = {
    // A resource with no write action: its body admits no kind at all.
    val readOnly = Set(Read, Search, Count)
    // A resource exposing update but not create / delete.
    val readUpdate = Set(Read, Update, Search, Count)
    // A resource that also declares create / delete admits all three kinds.
    val allActions = Set(Read, Update, Create, Delete, Search, Count)
    all {
      assert(batchEditKinds(readOnly) == Set()),
      assert(batchEditKinds(readUpdate) == Set("Update")),
      assert(batchEditKinds(allActions) == Set("Update", "Create", "Delete")),
      assert(batchEditKindsAreDeclared(readOnly)),
      assert(batchEditKindsAreDeclared(readUpdate)),
      assert(batchEditKindsAreDeclared(allActions)),
    }
  }

  run batchEditTargetsAreWritersTest = {
    all {
      assert(not(isBatchEditTarget(Set(Read, Search, Count)))),
      assert(isBatchEditTarget(Set(Read, Create))),
      assert(isBatchEditTarget(Set(Read, Update))),
      assert(isBatchEditTarget(Set(Read, Delete))),
      assert(batchEditTargetsAreWriters(Set(Read, Search, Count))),
      assert(batchEditTargetsAreWriters(Set(Read, Update))),
      assert(batchEditTargetsAreWriters(Set(Read, Create, Update, Delete))),
    }
  }

  run batchActionsRequireTheirSingularTest = {
    // Removing read drops batch_read; nothing else is derived.
    val withRead = Set(Read, Search, Count, BatchRead)
    val noRead = Set(Search, Count, BatchRead)
    all {
      assert(batchActionsRequireTheirSingular(withRead)),
      assert(batchActionsRequireTheirSingular(noRead)),
      // read present -> batch_read survives.
      assert(normalizedActions(withRead).contains(BatchRead)),
      // read absent -> batch_read is dropped.
      assert(not(normalizedActions(noRead).contains(BatchRead))),
      assert(batchActionsRequireTheirSingular(Set(Search, Count))),
    }
  }

  run invariantsHold = {
    val r = { fields: Set("id", "name", "secret"), actions: Set(Read, Search) }
    all {
      assert(hiddenRegistersNoRoutes),
      assert(builderNeverSuppressesRoutes),
      assert(hiddenFieldsNeverExposed(r, Set("secret"))),
      assert(hiddenFieldsNeverQueryable(r, Set("secret"))),
      assert(wrapperDoesNotDelegate(r, Set("secret"))),
      assert(defaultBuilderIsIdentity(r)),
      assert(wrappedBuilderPreservesTheActionContract(r, Secured)),
      assert(batchEditKindsAreDeclared(r.actions)),
      assert(batchEditTargetsAreWriters(r.actions)),
      assert(batchActionsRequireTheirSingular(r.actions)),
    }
  }
}
