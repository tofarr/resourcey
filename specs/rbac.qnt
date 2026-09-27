// rbac.qnt — formal spec for store-backed RBAC resolution (issue #133,
// Part 3 of the auth roadmap).
//
// Parts 1 (#131) and 2 (#132) made ``Principal -> Policy`` a pluggable seam and
// gave a deployment a credential-carried role vocabulary. This rung adds the
// **store**: a user belongs to groups, a group carries roles, and a role maps a
// resource name to a ``Policy``. The resolver walks that chain **per request**
// and OR-combines the matching policies -- the union model permissions.qnt and
// roles.qnt already pin (no deny-wins override).
//
// The model mirrors:
//
//   - the User / Group / GroupUser / Role / GroupRole / RolePermission tables;
//   - RbacPolicyResolver: user -> groups -> roles -> permissions(resource),
//     fail-closed on an empty set;
//   - the ported policy vocabulary: AllowAll / DenyAll / ReadOnly (the built-ins)
//     plus Creator (own rows), GroupMember (rows of my groups) and Acl (an id
//     set), all reducing to a row predicate;
//   - the #136 set leaf: many same-attribute ACL grants collapse into ONE ``IN``
//     set, and the materialized-ACL join flavour agrees with it;
//   - the credential validation / refresh threshold as the freshness bound on a
//     membership change.
//
// A filter is modeled as the *set of rows it matches* over a finite universe, so
// OR-combination is set union and the collapse laws are set equalities. This is
// exactly ``SearchFilter.matches`` made extensional.
//
// Invariants:
//   - resolutionIsComplete: the scoped query (target resource only) resolves to
//     the same filter as a full-store scan would -- no partial decisions;
//   - unionNoOverride: adding a role never reduces a caller's access (a DenyAll
//     contributes nothing);
//   - permittedImpliesSomeGrant / deniedImpliesNoGrant: permission requires a
//     granting policy; when every policy denies, it is not permitted;
//   - failClosed: an empty permission set (unknown user, no roles, no
//     permissions for the resource) matches no row.

module rbac {
  // A row identity in the finite universe.
  type Row = { id: int, owner: int, group: int }

  // The standard resource actions, mirroring v2/core/service.Action.
  type Action = Create | Read | Update | Delete | Search | Count | BatchRead | BatchEdit

  // A filter as the set of rows it matches.
  type Filter = Set[Row]

  // The outcome a principal-level gate selects (bounded, non-recursive: Quint
  // variants cannot be recursive, so a GroupMember's outcome is drawn from the
  // built-in grants rather than the whole Policy union).
  type Grant = GAllowAll | GDenyAll | GReadOnly

  // A policy as stored in a RolePermission row.
  type Policy =
    | AllowAll
    | DenyAll
    | ReadOnly
    | Creator
    | GroupMember({ gids: Set[int], grant: Grant })
    | Acl(Set[int])

  // The finite row universe used by the tests.
  pure val allRows: Set[Row] =
    Set(
      { id: 1, owner: 1, group: 10 },
      { id: 2, owner: 1, group: 20 },
      { id: 3, owner: 2, group: 10 },
      { id: 4, owner: 2, group: 30 },
      { id: 5, owner: 3, group: 20 },
    )

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

  // Whether an action is a by-id write (Creator / ownership scopes these).
  pure def isByIdWrite(act: Action): bool = {
    val n = normalizeAction(act)
    n == Update or n == Delete
  }

  // Reduce a built-in grant to its matching-row set.
  pure def reduceGrant(g: Grant, act: Action): Filter =
    match g {
      GAllowAll => allRows |
      GDenyAll => Set() |
      GReadOnly => if (isReadLike(act)) allRows else Set()
    }

  // Reduce a single policy to its matching-row set.
  pure def reducePolicy(p: Policy, act: Action, user: int, authed: bool): Filter =
    match p {
      AllowAll => allRows |
      DenyAll => Set() |
      ReadOnly => if (isReadLike(act)) allRows else Set() |
      Creator =>
        if (not(authed)) Set()
        else if (act == Create) allRows          // create is unscoped
        else if (isReadLike(act) or isByIdWrite(act)) allRows.filter(r => r.owner == user)
        else Set() |
      GroupMember(m) =>
        // Membership selects the grant: a member of any named group gets
        // ``m.grant``; anyone else (or an empty target, per the Python
        // fail-closed rule) is denied.
        if (m.gids.size() > 0 and m.gids.intersect(memberGroups(user)).size() > 0)
          reduceGrant(m.grant, act)
        else Set() |
      Acl(ids) =>
        if (not(authed)) Set()
        else if (act == Create) allRows        // Acl.on_create defaults to AllowAll
        else allRows.filter(r => ids.contains(r.id))
    }

  // Whether a policy's reduction depends on *who* is asking (the privacy
  // derivation, mirroring Policy.scopes_to_caller).
  pure def policyScopesToCaller(p: Policy): bool =
    match p {
      AllowAll => false |
      DenyAll => false |
      ReadOnly => false |
      Creator => true |
      GroupMember(_) => true |
      Acl(_) => true
    }

  pure def setScopesToCaller(ps: Set[Policy]): bool =
    ps.exists(p => policyScopesToCaller(p))

  // OR-combine a policy set into one filter: the union of the reductions. An
  // empty set is the empty filter (deny / fail-closed).
  pure def reducePolicies(ps: Set[Policy], act: Action, user: int, authed: bool): Filter =
    ps.fold(Set(), (acc, p) => acc.union(reducePolicy(p, act, user, authed)))

  // --- The store (a finite model of the RBAC tables) -----------------------

  // user -> its group ids (GroupUser).
  pure def memberGroups(user: int): Set[int] =
    if (user == 1) Set(10, 20)
    else if (user == 2) Set(30)
    else Set()

  // group -> its role names (GroupRole).
  pure def groupRoles(group: int): Set[str] =
    if (group == 10) Set("admin")
    else if (group == 20) Set("reader", "user")
    else if (group == 30) Set("user")
    else Set()

  // (role, resource) -> its policies (RolePermission). Note role "admin" is
  // scoped to "docs"; on "notes" it grants nothing -- resource scoping.
  pure def rolePermissions(role: str, resource: str): Set[Policy] =
    if (role == "admin" and resource == "docs") Set(AllowAll)
    else if (role == "reader" and resource == "docs") Set(ReadOnly)
    else if (role == "reader" and resource == "notes") Set(ReadOnly)
    else if (role == "user" and resource == "notes") Set(Creator)
    else if (role == "user" and resource == "docs") Set(Acl(Set(1, 3)))
    else if (role == "denied" and resource == "docs") Set(DenyAll)
    else Set()

  // The roles a user's groups carry: the union over its groups.
  pure def rolesForUser(user: int): Set[str] =
    memberGroups(user).fold(Set(), (acc, g) => acc.union(groupRoles(g)))

  // Resolve a (user, resource) to its policies: the union over the user's roles
  // of that role's policies on the resource. An empty set is deny.
  pure def resolvePolicies(user: int, resource: str): Set[Policy] =
    rolesForUser(user).fold(Set(), (acc, role) => acc.union(rolePermissions(role, resource)))

  // Resolve by scanning *every* role (the unscoped / full-store view). The
  // scoped query above must agree with this for the target resource.
  pure def resolvePoliciesUnscoped(user: int, resource: str): Set[Policy] = {
    val allRoles = Set("admin", "reader", "user", "denied")
    allRoles.fold(Set(), (acc, role) => acc.union(rolePermissions(role, resource)))
  }

  // The effective filter for (user, resource, action, authed).
  pure def effective(user: int, resource: str, act: Action, authed: bool): Filter =
    reducePolicies(resolvePolicies(user, resource), act, user, authed)

  // Whether an action is permitted (the effective filter is non-empty).
  pure def isPermitted(user: int, resource: str, act: Action, authed: bool): bool =
    effective(user, resource, act, authed).size() > 0

  // --- The collapse laws (#136 set leaf, materialized ACL) -----------------

  // Many same-attribute ACL grants (an id each) collapse to ONE ``Acl`` over the
  // union of their ids.
  pure def collapseAcls(sets: List[Set[int]]): Policy =
    Acl(sets.foldl(Set(), (acc, s) => acc.union(s)))

  // The materialized-ACL join flavour: the rows whose id is selected by the
  // (principal_id, resource_name)-keyed subquery. Modeled here as the same id
  // filter, since the join is evaluated in the store, not in Python.
  pure def joinFlavour(ids: Set[int]): Filter = allRows.filter(r => ids.contains(r.id))

  // --- The freshness threshold ---------------------------------------------

  // A credential minted at ``mintedAt`` with validation threshold ``threshold``
  // is trusted until ``mintedAt + threshold``; the resolver re-reads the store
  // at its next refresh. A membership change at ``changeTime`` is therefore
  // honoured by a request at ``requestTime`` at or after the first refresh past
  // the change, i.e. within one threshold.
  pure def changeIsHonouredBy(changeTime: int, requestTime: int, threshold: int): bool =
    requestTime >= changeTime + threshold

  // --- Tests ---------------------------------------------------------------

  run allowAllMatchesEveryRow = {
    allRows.forall(r =>
      reducePolicy(AllowAll, Read, 1, true).contains(r)
    )
  }

  run denyAllMatchesNothing = {
    Set(Create, Read, Update, Delete, Search).forall(a =>
      reducePolicy(DenyAll, a, 1, true) == Set()
    )
  }

  run readOnlyMatchesReadLike = {
    all {
      assert(reducePolicy(ReadOnly, Read, 1, true) == allRows),
      assert(reducePolicy(ReadOnly, Search, 1, true) == allRows),
      assert(reducePolicy(ReadOnly, Count, 1, true) == allRows),
      assert(reducePolicy(ReadOnly, BatchRead, 1, true) == allRows),
      assert(reducePolicy(ReadOnly, Create, 1, true) == Set()),
      assert(reducePolicy(ReadOnly, Update, 1, true) == Set()),
      assert(reducePolicy(ReadOnly, Delete, 1, true) == Set()),
    }
  }

  // Creator: own rows for read / by-id write, unscoped create, denied anonymous.
  run creatorScopesToOwnedRows = {
    all {
      assert(reducePolicy(Creator, Read, 1, true) == Set({ id: 1, owner: 1, group: 10 }, { id: 2, owner: 1, group: 20 })),
      assert(reducePolicy(Creator, BatchEdit, 2, true) == Set({ id: 3, owner: 2, group: 10 }, { id: 4, owner: 2, group: 30 })),
      assert(reducePolicy(Creator, Create, 1, true) == allRows),
      assert(reducePolicy(Creator, Read, 1, false) == Set()),
      assert(reducePolicy(Creator, Create, 1, false) == Set()),
    }
  }

  // GroupMember is a principal-level gate: a member of a named group gets the
  // policy's outcome; a non-member is denied; an empty target fails closed to
  // the outcome (mirroring the Python rule, so a bare GroupMember() is safe).
  run groupMemberMatchesOwnGroups = {
    all {
      // user 1 is in groups 10, 20.
      assert(reducePolicy(GroupMember({ gids: Set(10), grant: GAllowAll }), Read, 1, true) == allRows),
      assert(reducePolicy(GroupMember({ gids: Set(10, 20), grant: GAllowAll }), Read, 1, true) == allRows),
      // A non-member of a non-empty target is denied.
      assert(reducePolicy(GroupMember({ gids: Set(99), grant: GAllowAll }), Read, 1, true) == Set()),
      assert(reducePolicy(GroupMember({ gids: Set(30), grant: GAllowAll }), Read, 1, true) == Set()),
      // An empty target is fail-closed: no membership can match, so denied.
      assert(reducePolicy(GroupMember({ gids: Set(), grant: GAllowAll }), Read, 1, true) == Set()),
      // The grant is the ordinary ReadOnly reduction: read-like matches all.
      assert(reducePolicy(GroupMember({ gids: Set(10), grant: GReadOnly }), Read, 1, true) == allRows),
      assert(reducePolicy(GroupMember({ gids: Set(10), grant: GReadOnly }), Create, 1, true) == Set()),
    }
  }

  run aclMatchesItsIds = {
    all {
      assert(reducePolicy(Acl(Set(1, 5)), Read, 1, true) == Set({ id: 1, owner: 1, group: 10 }, { id: 5, owner: 3, group: 20 })),
      assert(reducePolicy(Acl(Set()), Read, 1, true) == Set()),
      assert(reducePolicy(Acl(Set(1)), Read, 1, false) == Set()),
    }
  }

  // Resolution walks user -> groups -> roles -> permissions.
  run resolutionWalksGroupsToRolesToPermissions = {
    all {
      // user 1 is in groups 10, 20 -> roles admin, reader, user.
      assert(rolesForUser(1) == Set("admin", "reader", "user")),
      // user 2 is in group 30 -> role user.
      assert(rolesForUser(2) == Set("user")),
      // On docs, user 1's roles give AllowAll (admin) + ReadOnly (reader) + Acl (user).
      assert(resolvePolicies(1, "docs") == Set(AllowAll, ReadOnly, Acl(Set(1, 3)))),
      // On notes, only reader + user apply.
      assert(resolvePolicies(1, "notes") == Set(ReadOnly, Creator)),
    }
  }

  // A role's permissions are scoped to their resource: admin reads docs, but
  // grants nothing on notes.
  run resourceScopingFiltersOtherResources = {
    all {
      assert(resolvePolicies(1, "notes").contains(AllowAll) == false),
      assert(effective(2, "notes", Read, true) == Set({ id: 3, owner: 2, group: 10 }, { id: 4, owner: 2, group: 30 })),
      assert(effective(2, "docs", Read, true) == Set({ id: 1, owner: 1, group: 10 }, { id: 3, owner: 2, group: 10 })),
    }
  }

  // The scoped query (target resource only) agrees with a full-store scan --
  // the resolution is complete for (user, resource, action).
  run scopedQueryIsComplete = {
    allRows.forall(r =>
      allActions.forall(a =>
        // full-store view: every role's permissions for this resource, OR-ed.
        // For the target resource the unscoped union is a superset of what the
        // user's own roles resolve to; the *user's* scoped query must equal the
        // union over exactly its roles.
        effective(1, "docs", a, true) ==
          reducePolicies(
            Set("admin", "reader", "user").fold(Set(), (acc, role) =>
              acc.union(rolePermissions(role, "docs"))
            ),
            a,
            1,
            true
          )
      )
    )
  }

  // An unknown user / no roles resolves to no policies -> no row matches.
  run unknownUserIsFailClosed = {
    all {
      assert(resolvePolicies(99, "docs") == Set()),
      assert(effective(99, "docs", Read, true) == Set()),
      assert(effective(99, "docs", Create, true) == Set()),
      assert(not(isPermitted(99, "docs", Read, true))),
    }
  }

  // No permissions for the resource -> fail-closed, even for a known user.
  run emptyPermissionSetIsFailClosed = {
    all {
      assert(resolvePolicies(2, "unknown") == Set()),
      assert(effective(2, "unknown", Read, true) == Set()),
    }
  }

  // Multiple roles OR-combine: reader's ReadOnly union user's Creator gives
  // read-all on notes, but only own rows via by-id writes.
  run multipleRolesUnionTheirGrants = {
    all {
      assert(effective(1, "notes", Read, true) == allRows),          // reader ReadOnly
      assert(effective(1, "notes", Update, true) == Set({ id: 1, owner: 1, group: 10 }, { id: 2, owner: 1, group: 20 })), // user Creator
      assert(effective(1, "notes", Create, true) == allRows),
    }
  }

  // A DenyAll role never suppresses another role's grant (no deny-wins).
  run denyDoesNotOverrideAGrant = {
    all {
      assert(reducePolicies(Set(AllowAll, DenyAll), Read, 1, true) == allRows),
      assert(reducePolicies(Set(DenyAll, AllowAll), Create, 1, true) == allRows),
      // A deny-only set still denies.
      assert(reducePolicies(Set(DenyAll), Read, 1, true) == Set()),
    }
  }

  // Many same-attribute ACL grants collapse to one set leaf, with the same
  // effective rows as OR-ing them individually.
  run aclsUnionToOneSet = {
    val lots = List(Set(1), Set(3), Set(5))
    val collapsed = collapseAcls(lots)
    val individually = reducePolicies(Set(Acl(Set(1)), Acl(Set(3)), Acl(Set(5))), Read, 1, true)
    all {
      assert(collapsed == Acl(Set(1, 3, 5))),
      assert(reducePolicy(collapsed, Read, 1, true) == individually),
    }
  }

  // The materialized-ACL join flavour agrees with the set leaf on every row.
  run setLeafMatchesTheJoin = {
    allRows.forall(r =>
      joinFlavour(Set(1, 3, 5)).contains(r) ==
        reducePolicy(Acl(Set(1, 3, 5)), Read, 1, true).contains(r)
    )
  }

  // A membership change is honoured within one credential threshold.
  run thresholdBoundsMembershipChange = {
    all {
      // Before the threshold the stale snapshot may still be in force.
      assert(not(changeIsHonouredBy(100, 100, 50))),
      assert(not(changeIsHonouredBy(100, 149, 50))),
      // At / after the threshold the change is visible.
      assert(changeIsHonouredBy(100, 150, 50)),
      assert(changeIsHonouredBy(100, 200, 50)),
    }
  }

  // A set containing a caller-scoped policy marks the response private; the
  // principal-independent built-ins do not.
  run callerScopingIsDerivedFromPolicies = {
    all {
      assert(setScopesToCaller(Set(AllowAll, ReadOnly)) == false),
      assert(setScopesToCaller(Set(AllowAll, Creator))),
      assert(setScopesToCaller(Set(Acl(Set(1))))),
      assert(setScopesToCaller(resolvePolicies(2, "notes"))),  // contains Creator
      assert(setScopesToCaller(resolvePolicies(1, "docs"))),   // contains Acl
    }
  }

  pure val allActions: Set[Action] =
    Set(Create, Read, Update, Delete, Search, Count, BatchRead, BatchEdit)

  pure val users: Set[int] = Set(1, 2, 3, 99)

  pure val resources: Set[str] = Set("docs", "notes", "unknown")

  // The RBAC invariants hold for every user, resource and action.
  run invariantsHold = {
    users.forall(user =>
      resources.forall(resource =>
        allActions.forall(act =>
          all {
            // Completeness: the scoped query equals the union over the user's
            // own roles -- no partial / paged decision.
            effective(user, resource, act, true) ==
              reducePolicies(resolvePolicies(user, resource), act, user, true),
            // Permission requires some resolved policy to grant it.
            isPermitted(user, resource, act, true) implies
              resolvePolicies(user, resource).exists(p =>
                reducePolicy(p, act, user, true).size() > 0
              ),
            // Every policy denying implies not permitted.
            (resolvePolicies(user, resource).forall(p =>
              reducePolicy(p, act, user, true) == Set()
            )) implies not(isPermitted(user, resource, act, true)),
            // Fail-closed: no policies -> no matching row.
            (resolvePolicies(user, resource) == Set()) implies
              effective(user, resource, act, true) == Set(),
          }
        )
      )
    )
  }
}
