// permissions.qnt — formal spec for RBAC permission computation (issue #4).
//
// Policy-union model: a principal's effective permission for a resource is
// the OR-combination of every matching policy's reduced filter. A policy
// reduces to a Filter for a given (user, action, groups) triple; "deny"
// is the None filter (matches nothing), which contributes nothing to the
// OR — it does NOT override other grants. There is no deny-wins override.
//
// This replaces the legacy grant/denial model. A policy is action-aware:
// the same stored policy can grant READ yet deny CREATE, without separate
// grant/denial records.
//
// Invariants:
//   - unionNoOverride: a deny policy never suppresses a grant from another
//     policy for the same action (union, not intersection).
//   - permittedImpliesSomeGrant: permission requires at least one policy to
//     grant (reduce to a non-None filter) for that action.
//   - deniedImpliesNoGrant: when every policy denies an action, it is not
//     permitted.

module permissions {
  type Action = Create | Read | Update | Delete | Search | Count | BatchRead | BatchEdit

  // A filter over items: All (match everything), None (match nothing), or
  // Own (match items the principal created). Mirrors SearchFilter built-ins.
  // Suffixed with F to avoid clashing with Quint's builtin None.
  type Filter = AllF | NoneF | OwnF(str)

  // A policy reduces to a Filter for (user, action, groups). A stored policy
  // is action-aware — e.g. Permitted -> All for every action; Denied -> None
  // for every action; ReadOnly -> All for read/search/count, None otherwise;
  // Creator -> Own for non-create actions the policy grants, None for create
  // unless on_create grants.
  // Creator carries a record of grant flags: grantOwn (own items),
  // grantOthers (others' items), grantCreate (create action).
  type Policy =
    Permitted | Denied | ReadOnly | Creator({grantOwn: bool, grantOthers: bool, grantCreate: bool})

  // Reduce ReadOnly: All for read-like actions, None otherwise.
  pure def readOnlyFilter(act: Action): Filter =
    if (act == Read or act == Search or act == Count) AllF else NoneF

  // Reduce Creator for a non-create action.
  pure def creatorNonCreate(grantOwn: bool, grantOthers: bool, user: str): Filter =
    if (grantOwn and grantOthers) AllF
    else if (grantOwn) OwnF(user)
    else if (grantOthers) AllF
    else NoneF

  // Reduce Creator for a given action.
  pure def creatorFilter(r: {grantOwn: bool, grantOthers: bool, grantCreate: bool},
                         user: str, act: Action): Filter =
    if (act == Create) (if (r.grantCreate) AllF else NoneF)
    else creatorNonCreate(r.grantOwn, r.grantOthers, user)

  // Reduce a single policy to its Filter for (user, action, groups).
  pure def reducePolicy(p: Policy, user: str, act: Action, groups: Set[str]): Filter =
    match p {
      Permitted => AllF |
      Denied => NoneF |
      ReadOnly => readOnlyFilter(act) |
      Creator(r) => creatorFilter(r, user, act)
    }

  // The principal's policies for a resource (parameterised for testability).
  pure def policiesFor(user: str, ps: str -> Set[Policy]): Set[Policy] =
    if (ps.keys().contains(user)) ps.get(user) else Set()

  // Whether a filter is the All (match-everything) filter.
  pure def isAll(f: Filter): bool =
    match f { AllF => true | _ => false }

  // Whether a filter is the None (match-nothing) filter.
  pure def isNone(f: Filter): bool =
    match f { NoneF => true | _ => false }

  // Extract the owner string from an OwnF filter (assumes input is OwnF).
  pure def ownOwner(f: Filter): str =
    match f { OwnF(u) => u | _ => "" }

  // OR-combine two filters: the union of what each admits.
  // All is the annihilator (All OR x = All); None is the identity (None OR x = x);
  // Own OR Own = Own; Own OR None = Own; Own OR All = All.
  pure def orFilter(a: Filter, b: Filter): Filter =
    if (isAll(a) or isAll(b)) AllF
    else if (isNone(a)) b
    else if (isNone(b)) a
    else {
      // Both are OwnF — union is OwnF if same owner, else AllF.
      val u = ownOwner(a)
      val v = ownOwner(b)
      if (u == v) OwnF(u) else AllF
    }

  // The effective filter for (user, resource, action): the OR of every
  // policy's reduction. Empty policy set -> None (deny / fail-closed).
  pure def effectiveFilter(user: str, act: Action, groups: Set[str],
                           ps: str -> Set[Policy]): Filter =
    policiesFor(user, ps).fold(NoneF, (acc, p) => orFilter(acc, reducePolicy(p, user, act, groups)))

  // Whether an action is permitted: the effective filter is not None.
  pure def isPermitted(user: str, act: Action, groups: Set[str],
                       ps: str -> Set[Policy]): bool =
    not(isNone(effectiveFilter(user, act, groups, ps)))

  // Invariant: a deny policy never suppresses a grant from another policy.
  // Adding a Denied policy to a set that already permits must not remove
  // permission (union, not deny-wins).
  pure def unionNoOverride(user: str, act: Action, groups: Set[str],
                           base: Set[Policy], denied: Set[Policy],
                           ps: str -> Set[Policy]): bool = {
    val psBase = Map(user -> base)
    val psBoth = Map(user -> base.union(denied))
    isPermitted(user, act, groups, psBase) implies
      isPermitted(user, act, groups, psBoth)
  }

  // Invariant: permission requires at least one policy to grant (reduce to a
  // non-None filter) for that action.
  pure def permittedImpliesSomeGrant(user: str, act: Action, groups: Set[str],
                                     ps: str -> Set[Policy]): bool = {
    val pols = policiesFor(user, ps)
    isPermitted(user, act, groups, ps) implies
      pols.exists(p => not(isNone(reducePolicy(p, user, act, groups))))
  }

  // Invariant: when every policy denies an action, it is not permitted.
  pure def deniedImpliesNoGrant(user: str, act: Action, groups: Set[str],
                                ps: str -> Set[Policy]): bool = {
    val pols = policiesFor(user, ps)
    (pols.forall(p => isNone(reducePolicy(p, user, act, groups)))) implies
      not(isPermitted(user, act, groups, ps))
  }

  // Tests

  run permittedGrantsAccess = {
    val ps = Map("alice" -> Set(Permitted))
    all {
      assert(isPermitted("alice", Read, Set(), ps)),
      assert(isPermitted("alice", Create, Set(), ps)),
    }
  }

  run deniedBlocksAccess = {
    val ps = Map("alice" -> Set(Denied))
    all {
      assert(not(isPermitted("alice", Read, Set(), ps))),
      assert(not(isPermitted("alice", Create, Set(), ps))),
    }
  }

  run emptyPoliciesDenies = {
    val ps = Map("alice" -> Set())
    assert(not(isPermitted("alice", Read, Set(), ps)))
  }

  run readOnlyGrantsReadNotCreate = {
    val ps = Map("alice" -> Set(ReadOnly))
    all {
      assert(isPermitted("alice", Read, Set(), ps)),
      assert(isPermitted("alice", Search, Set(), ps)),
      assert(isPermitted("alice", Count, Set(), ps)),
      assert(not(isPermitted("alice", Create, Set(), ps))),
      assert(not(isPermitted("alice", Update, Set(), ps))),
    }
  }

  run denyDoesNotOverrideGrant = {
    // A Permitted policy + a Denied policy -> still permitted (union model).
    val ps = Map("alice" -> Set(Permitted, Denied))
    assert(isPermitted("alice", Read, Set(), ps))
  }

  run creatorOwnOnly = {
    // Creator grants own items read, denies others and create.
    val ps = Map("alice" -> Set(Creator({grantOwn: true, grantOthers: false, grantCreate: false})))
    all {
      assert(isPermitted("alice", Read, Set(), ps)),
      assert(not(isPermitted("alice", Create, Set(), ps))),
    }
  }

  run unionCombinesGrants = {
    // One policy grants create, another grants read -> both permitted.
    val ps = Map("alice" -> Set(Creator({grantOwn: false, grantOthers: false, grantCreate: true}), ReadOnly))
    all {
      assert(isPermitted("alice", Create, Set(), ps)),
      assert(isPermitted("alice", Read, Set(), ps)),
      assert(not(isPermitted("alice", Update, Set(), ps))),
    }
  }
}
