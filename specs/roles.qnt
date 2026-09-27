// roles.qnt — formal spec for simple, per-app roles and the centralized
// ``role -> policy`` translation (issue #132).
//
// Part 1 (#131) made ``Principal -> Policy`` a pluggable seam; this rung gives a
// deployment a small role vocabulary carried on the credential and translated to
// policies in one place. The model mirrors:
//
//   - AppRole / role_key: a role is a plain string, app-scoped (there is no
//     process-wide registry unioning apps' roles).
//   - RolePolicyResolver: a static declaration, global and per-resource, keyed
//     by (role, resource) -> policies.
//   - the union model from permissions.qnt: a principal with several roles has
//     their reduced filters OR-combined, so a deny from one role never overrides
//     another role's grant.
//
// Filters are the three-valued chain  NoMatchF  <=  OwnF  <=  AllF:
//   - NoMatchF = matches no row (deny);
//   - OwnF     = matches rows whose owner is the caller (the Owner policy);
//   - AllF     = matches every row.
// OR-combination is the chain's join (maximum), so ``orF(NoMatchF, x) == x``
// (a DenyAll contributes nothing) and ``orF(AllF, x) == AllF`` (a full grant
// dominates). This is permissions.qnt's union law, extended with the OwnF rung.
//
// Invariants:
//   - resolveIsFailClosed: an un-roled / unknown-role caller resolves to the
//     declaration's default, which is empty (deny) unless opted in.
//   - unionNoOverride: adding roles never reduces a caller's access -- in
//     particular adding a DenyAll-only role changes nothing.
//   - permittedImpliesSomeGrant: a caller is permitted for an action only when
//     some resolved policy grants it (reduces to a non-NoMatchF filter).
//   - deniedImpliesNoGrant: when every resolved policy denies an action, it is
//     not permitted.
//   - ownerPolicyScoping: Owner reduces to OwnF for the read-like / by-id write
//     actions, AllF for create (unscoped), and NoMatchF for an anonymous caller.
//   - perResourceScoping: the same role maps to different policies on different
//     resources.

module roles {
  // The standard resource actions, mirroring v2/core/service.Action.
  type Action = Create | Read | Update | Delete | Search | Count | BatchRead | BatchEdit

  // A filter over rows: the chain NoMatchF <= OwnF <= AllF.
  type Filter = NoMatchF | OwnF | AllF

  // A policy as stored, mirroring the built-ins relevant to roles.
  type Policy = AllowAll | DenyAll | ReadOnly | Owner

  // Reduce COUNT / BATCH_* to their singular CRUD action (normalize_action).
  pure def normalizeAction(act: Action): Action =
    if (act == Count) Search
    else if (act == BatchRead) Read
    else if (act == BatchEdit) Update
    else act

  // Whether an action reads (ReadOnly grants these).
  pure def isReadLike(act: Action): bool = {
    val n = normalizeAction(act)
    n == Read or n == Search
  }

  // Reduce a single policy to its filter for (action, authed).
  pure def reducePolicy(p: Policy, act: Action, authed: bool): Filter =
    match p {
      AllowAll => AllF |
      DenyAll => NoMatchF |
      ReadOnly => if (isReadLike(act)) AllF else NoMatchF |
      Owner =>
        if (not(authed)) NoMatchF
        else if (act == Create) AllF   // create is unscoped
        else OwnF
    }

  // The chain rank, so OR is the join (maximum).
  pure def rank(f: Filter): int =
    match f { NoMatchF => 0 | OwnF => 1 | AllF => 2 }

  // OR-combine two filters: the join of the chain.
  pure def orF(a: Filter, b: Filter): Filter =
    if (rank(a) >= rank(b)) a else b

  // Whether a filter grants anything (is not the deny constant).
  pure def permits(f: Filter): bool = f != NoMatchF

  // The app's static declaration: (role, resource) -> policies. This models
  // RolePolicyResolver's global + per-resource maps collapsed into one place;
  // an undeclared (role, resource) pair yields no policies (fail-closed).
  pure def declaredPolicies(role: str, resource: str): Set[Policy] =
    if (role == "ADMIN") Set(AllowAll)
    else if (role == "READER") Set(ReadOnly)
    else if (role == "USER" and resource == "threads") Set(ReadOnly)
    else if (role == "USER" and resource == "messages") Set(Owner)
    else if (role == "DENIED") Set(DenyAll)
    else Set()

  // The policies a principal's roles resolve to for a resource: the union over
  // its roles of each role's declared policies.
  pure def resolvePolicies(roles: Set[str], resource: str): Set[Policy] =
    roles.fold(Set(), (acc, r) => acc.union(declaredPolicies(r, resource)))

  // The default a resolver falls back to when no role matched. Empty here, so
  // an un-roled / anonymous caller is denied (fail-closed).
  pure def defaultPolicies: Set[Policy] = Set()

  // The policies for a caller: the resolved set, or the default when empty.
  pure def policiesForCaller(roles: Set[str], resource: str): Set[Policy] = {
    val resolved = resolvePolicies(roles, resource)
    if (resolved == Set()) defaultPolicies else resolved
  }

  // Reduce a policy set to its filter for an action: OR-combine the reductions.
  // Empty set -> NoMatchF (deny / fail-closed).
  pure def reducePolicies(ps: Set[Policy], act: Action, authed: bool): Filter =
    ps.fold(NoMatchF, (acc, p) => orF(acc, reducePolicy(p, act, authed)))

  // The effective filter for (roles, resource, action, authed).
  pure def effective(roles: Set[str], resource: str, act: Action, authed: bool): Filter =
    reducePolicies(policiesForCaller(roles, resource), act, authed)

  // Whether an action is permitted: the effective filter is not the deny constant.
  pure def isPermitted(roles: Set[str], resource: str, act: Action, authed: bool): bool =
    permits(effective(roles, resource, act, authed))

  // Finitely many role names / resources / actions / auth states for the
  // invariant sweep.
  pure val roleNames: Set[str] =
    Set("ADMIN", "READER", "USER", "DENIED", "GHOST")

  pure val resources: Set[str] = Set("threads", "messages")

  pure val allActions: Set[Action] =
    Set(Create, Read, Update, Delete, Search, Count, BatchRead, BatchEdit)

  // Every role set over the five names (2^5 = 32), enumerated compositionally.
  pure val roleSets: Set[Set[str]] =
    roleNames.fold(Set(Set()), (acc, r) => acc.union(acc.map(rs => rs.union(Set(r)))))

  // Tests

  run allowAllIsAllForEveryAction = {
    allActions.forall(a =>
      reducePolicy(AllowAll, a, true) == AllF and reducePolicy(AllowAll, a, false) == AllF
    )
  }

  run denyAllIsNoneForEveryAction = {
    allActions.forall(a => reducePolicy(DenyAll, a, true) == NoMatchF)
  }

  run readOnlyGrantsReadLikeAndDeniesTheRest = {
    all {
      assert(reducePolicy(ReadOnly, Read, true) == AllF),
      assert(reducePolicy(ReadOnly, Search, true) == AllF),
      assert(reducePolicy(ReadOnly, Count, true) == AllF),
      assert(reducePolicy(ReadOnly, BatchRead, true) == AllF),
      assert(reducePolicy(ReadOnly, Create, true) == NoMatchF),
      assert(reducePolicy(ReadOnly, Update, true) == NoMatchF),
      assert(reducePolicy(ReadOnly, Delete, true) == NoMatchF),
      assert(reducePolicy(ReadOnly, BatchEdit, true) == NoMatchF),
    }
  }

  run normalizeActionReducesDerivedMembers = {
    all {
      assert(normalizeAction(Count) == Search),
      assert(normalizeAction(BatchRead) == Read),
      assert(normalizeAction(BatchEdit) == Update),
      assert(normalizeAction(Create) == Create),
    }
  }

  // The Owner policy scopes reads / by-id writes to own rows, grants create
  // unscoped, and denies an anonymous caller entirely.
  run ownerPolicyScoping = {
    all {
      assert(reducePolicy(Owner, Read, true) == OwnF),
      assert(reducePolicy(Owner, Search, true) == OwnF),
      assert(reducePolicy(Owner, Update, true) == OwnF),
      assert(reducePolicy(Owner, Delete, true) == OwnF),
      assert(reducePolicy(Owner, Create, true) == AllF),
      assert(reducePolicy(Owner, Read, false) == NoMatchF),
      assert(reducePolicy(Owner, Create, false) == NoMatchF),
    }
  }

  // An unknown role grants nothing, and an un-roled caller falls to the
  // (empty) default -- fail-closed.
  run unknownRoleGrantsNothing = {
    all {
      assert(resolvePolicies(Set("GHOST"), "threads") == Set()),
      assert(not(isPermitted(Set("GHOST"), "threads", Read, true))),
    }
  }

  run unroledCallerUsesTheClosedDefault = {
    all {
      assert(policiesForCaller(Set(), "threads") == Set()),
      assert(effective(Set(), "threads", Read, true) == NoMatchF),
      assert(not(isPermitted(Set(), "threads", Read, true))),
    }
  }

  // The same role maps to different policies on different resources: USER is
  // read-only on threads but own-rows-only on messages.
  run perResourceScoping = {
    all {
      assert(effective(Set("USER"), "threads", Read, true) == AllF),
      assert(effective(Set("USER"), "messages", Read, true) == OwnF),
      assert(effective(Set("USER"), "threads", Create, true) == NoMatchF),
      assert(effective(Set("USER"), "messages", Create, true) == AllF),
      assert(effective(Set("USER"), "messages", Update, true) == OwnF),
    }
  }

  // A role that reads all of X but only its own rows of Y: exactly the
  // per-resource USER rule, on the read surface.
  run readsAllOfXOwnRowsOfY = {
    all {
      assert(isPermitted(Set("USER"), "threads", Read, true)),
      assert(isPermitted(Set("USER"), "messages", Read, true)),
      assert(effective(Set("USER"), "threads", Read, true) == AllF),
      assert(effective(Set("USER"), "messages", Read, true) == OwnF),
    }
  }

  // Multiple roles union their grants: READER contributes reads, USER adds a
  // create on messages (and own-row reads).
  run multipleRolesUnionTheirGrants = {
    all {
      assert(effective(Set("READER", "USER"), "threads", Read, true) == AllF),
      assert(effective(Set("READER", "USER"), "threads", Create, true) == NoMatchF),
      assert(effective(Set("READER", "USER"), "messages", Create, true) == AllF),
      assert(effective(Set("READER", "USER"), "messages", Update, true) == OwnF),
    }
  }

  // Adding a DenyAll-only role never suppresses another role's grant.
  run denyRoleDoesNotOverrideAGrant = {
    all {
      assert(effective(Set("ADMIN"), "threads", Read, true) == AllF),
      assert(effective(Set("ADMIN", "DENIED"), "threads", Read, true) == AllF),
      assert(effective(Set("ADMIN", "DENIED"), "threads", Create, true) == AllF),
    }
  }

  // The union invariants hold for every role set, resource, action and auth
  // state.
  run invariantsHold = {
    roleSets.forall(roles =>
      resources.forall(resource =>
        allActions.forall(act =>
          all {
            // Adding roles never reduces access (no deny-wins override).
            roleNames.forall(extra =>
              isPermitted(roles, resource, act, true) implies
                isPermitted(roles.union(Set(extra)), resource, act, true)
            ),
            // Permission requires some resolved policy to grant it.
            isPermitted(roles, resource, act, true) implies
              policiesForCaller(roles, resource).exists(p =>
                reducePolicy(p, act, true) != NoMatchF
              ),
            // Every policy denying implies not permitted.
            (policiesForCaller(roles, resource).forall(p =>
              reducePolicy(p, act, true) == NoMatchF
            )) implies not(isPermitted(roles, resource, act, true)),
          }
        )
      )
    )
  }
}
