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

module exposure {
  type Action = Create | Read | Update | Delete | Search | Count | BatchRead | BatchEdit

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

  run narrowingIsAllowedWideningIsRejected = {
    val r = { fields: Set("id"), actions: Set(Read, Search) }
    all {
      assert(narrowerIsAllowedWiderIsNot(r, Set(Read))),
      assert(not(narrowerIsAllowedWiderIsNot(r, Set(Read, Create)))),
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
    }
  }
}
