// permissions.qnt — formal spec for v2 authorization policies (issue #127).
//
// A Policy reduces to a Filter: which rows a principal may touch for an action.
// This first rung carries one principal-independent policy per resource, so the
// reduction takes the action alone; the OR-combination model is retained so a
// per-principal *set* of policies can be added later without changing the
// effective-filter law.
//
// Union model: a principal's effective permission for a resource is the
// OR-combination of every matching policy's reduced filter. "DenyAll" reduces
// to the None filter (matches nothing), which contributes nothing to the OR —
// it does NOT override other grants. There is no deny-wins override.
//
// Enforcement (the AuthorizedService matrix):
//   - a denied Create is Forbidden (403);
//   - a denied Read / Update / Delete is Hidden (404), so an out-of-scope id is
//     indistinguishable from an absent one and existence does not leak;
//   - a denied Search / Count / Batch action is Empty (an empty page / count 0 /
//     None positions), never an error.
//
// Invariants:
//   - unionNoOverride: a deny policy never suppresses a grant from another
//     policy for the same action (union, not intersection).
//   - permittedImpliesSomeGrant: permission requires at least one policy to
//     grant (reduce to a non-None filter) for that action.
//   - deniedImpliesNoGrant: when every policy denies an action, it is not
//     permitted.
//   - principalIndependentSetsStayShared: a policy set drawn from the built-ins
//     (all principal-independent) is never caller-scoped, so a response under it
//     keeps its shared-cache optimisation.

module permissions {
  // The standard resource actions, mirroring v2/core/service.Action.
  type Action = Create | Read | Update | Delete | Search | Count | BatchRead | BatchEdit

  // A filter over rows: All (match everything) or None (match nothing). Mirrors
  // the SearchFilter vocabulary (AllFilter / NoMatchFilter). Suffixed with F to
  // avoid clashing with Quint's builtin None.
  type Filter = AllF | NoneF

  // A policy as stored. All three built-ins are principal-independent.
  type Policy = AllowAll | DenyAll | ReadOnly

  // Whether a policy's reduction depends on *who* is asking (mirrors
  // Policy.scopes_to_caller). All three built-ins are principal-independent, so
  // a response under them may be shared; only a caller-scoped policy (e.g. the
  // Owner policy in roles.qnt) forces a private response.
  pure def policyScopesToCaller(p: Policy): bool =
    match p { AllowAll => false | DenyAll => false | ReadOnly => false }

  // Whether a response under a policy set may differ per caller — true iff any
  // resolved policy scopes to the caller (the privacy derivation).
  pure def setScopesToCaller(ps: Set[Policy]): bool =
    ps.exists(p => policyScopesToCaller(p))

  // An enforcement decision, mirroring the AuthorizedService matrix.
  type Decision = Allowed | Forbidden | Hidden | Empty

  // Reduce COUNT / BATCH_* to their singular CRUD action (normalize_action):
  // counting reuses the search permission and batching reuses the action it
  // batches.
  pure def normalizeAction(act: Action): Action =
    if (act == Count) Search
    else if (act == BatchRead) Read
    else if (act == BatchEdit) Update
    else act

  // Reduce a single policy to its Filter for an action.
  pure def reducePolicy(p: Policy, act: Action): Filter =
    match p {
      AllowAll => AllF |
      DenyAll => NoneF |
      ReadOnly =>
        val n = normalizeAction(act)
        if (n == Read or n == Search) AllF else NoneF
    }

  // Whether a filter is the All (match-everything) filter.
  pure def isAll(f: Filter): bool =
    match f { AllF => true | _ => false }

  // Whether a filter is the None (match-nothing) filter.
  pure def isNone(f: Filter): bool =
    match f { NoneF => true | _ => false }

  // OR-combine two filters: the union of what each admits.
  // All is the annihilator (All OR x = All); None is the identity (None OR x = x).
  pure def orFilter(a: Filter, b: Filter): Filter =
    if (isAll(a) or isAll(b)) AllF
    else if (isNone(a)) b
    else a

  // The principal's policies for a resource (parameterised for testability).
  pure def policiesFor(user: str, ps: str -> Set[Policy]): Set[Policy] =
    if (ps.keys().contains(user)) ps.get(user) else Set()

  // The effective filter for (user, action): the OR of every policy's
  // reduction. Empty policy set -> None (deny / fail-closed).
  pure def effectiveFilter(user: str, act: Action, ps: str -> Set[Policy]): Filter =
    policiesFor(user, ps).fold(NoneF, (acc, p) => orFilter(acc, reducePolicy(p, act)))

  // Whether an action is permitted: the effective filter is not None.
  pure def isPermitted(user: str, act: Action, ps: str -> Set[Policy]): bool =
    not(isNone(effectiveFilter(user, act, ps)))

  // The enforcement decision for an action and its effective filter — the
  // AuthorizedService matrix.
  pure def enforce(act: Action, f: Filter): Decision =
    if (isAll(f)) Allowed
    else match act {
      Create => Forbidden |
      Read => Hidden |
      Update => Hidden |
      Delete => Hidden |
      _ => Empty  // Search / Count / BatchRead / BatchEdit -> empty / None positions
    }

  // Invariant: a deny policy never suppresses a grant from another policy.
  pure def unionNoOverride(user: str, act: Action,
                           base: Set[Policy], denied: Set[Policy],
                           ps: str -> Set[Policy]): bool = {
    val psBase = Map(user -> base)
    val psBoth = Map(user -> base.union(denied))
    isPermitted(user, act, psBase) implies isPermitted(user, act, psBoth)
  }

  // Invariant: permission requires at least one policy to grant (reduce to a
  // non-None filter) for that action.
  pure def permittedImpliesSomeGrant(user: str, act: Action,
                                     ps: str -> Set[Policy]): bool = {
    val pols = policiesFor(user, ps)
    isPermitted(user, act, ps) implies
      pols.exists(p => not(isNone(reducePolicy(p, act))))
  }

  // Invariant: when every policy denies an action, it is not permitted.
  pure def deniedImpliesNoGrant(user: str, act: Action,
                                ps: str -> Set[Policy]): bool = {
    val pols = policiesFor(user, ps)
    (pols.forall(p => isNone(reducePolicy(p, act)))) implies
      not(isPermitted(user, act, ps))
  }

  // Every policy set over the three built-ins (2^3 = 8).
  pure val policySets: Set[Set[Policy]] =
    Set(Set(), Set(AllowAll), Set(DenyAll), Set(ReadOnly),
        Set(AllowAll, DenyAll), Set(AllowAll, ReadOnly), Set(DenyAll, ReadOnly),
        Set(AllowAll, DenyAll, ReadOnly))

  // Every action.
  pure val allActions: Set[Action] =
    Set(Create, Read, Update, Delete, Search, Count, BatchRead, BatchEdit)

  // Tests

  run allowAllIsAllForEveryAction = {
    allActions.forall(a => reducePolicy(AllowAll, a) == AllF)
  }

  run denyAllIsNoneForEveryAction = {
    allActions.forall(a => reducePolicy(DenyAll, a) == NoneF)
  }

  run readOnlyGrantsReadLikeAndDeniesTheRest = {
    all {
      assert(reducePolicy(ReadOnly, Read) == AllF),
      assert(reducePolicy(ReadOnly, Search) == AllF),
      assert(reducePolicy(ReadOnly, Count) == AllF),
      assert(reducePolicy(ReadOnly, BatchRead) == AllF),
      assert(reducePolicy(ReadOnly, Create) == NoneF),
      assert(reducePolicy(ReadOnly, Update) == NoneF),
      assert(reducePolicy(ReadOnly, Delete) == NoneF),
      assert(reducePolicy(ReadOnly, BatchEdit) == NoneF),
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

  // Enforcement: a denied create is forbidden, a denied read is hidden, and a
  // denied collection read is empty (not an error).
  run enforcementMatrix = {
    all {
      assert(enforce(Create, NoneF) == Forbidden),
      assert(enforce(Read, NoneF) == Hidden),
      assert(enforce(Update, NoneF) == Hidden),
      assert(enforce(Delete, NoneF) == Hidden),
      assert(enforce(Search, NoneF) == Empty),
      assert(enforce(Count, NoneF) == Empty),
      assert(enforce(BatchRead, NoneF) == Empty),
      assert(enforce(BatchEdit, NoneF) == Empty),
    }
  }

  // Enforcement: an All filter permits every action.
  run enforcementAllowsEverything = {
    allActions.forall(a => enforce(a, AllF) == Allowed)
  }

  run allowAllGrantsAccess = {
    val ps = Map("alice" -> Set(AllowAll))
    all {
      assert(isPermitted("alice", Read, ps)),
      assert(isPermitted("alice", Create, ps)),
      assert(isPermitted("alice", BatchEdit, ps)),
    }
  }

  run denyAllBlocksAccess = {
    val ps = Map("alice" -> Set(DenyAll))
    all {
      assert(not(isPermitted("alice", Read, ps))),
      assert(not(isPermitted("alice", Create, ps))),
    }
  }

  run emptyPoliciesDenies = {
    val ps = Map("alice" -> Set())
    assert(not(isPermitted("alice", Read, ps)))
  }

  run readOnlyGrantsReadNotCreate = {
    val ps = Map("alice" -> Set(ReadOnly))
    all {
      assert(isPermitted("alice", Read, ps)),
      assert(isPermitted("alice", Search, ps)),
      assert(isPermitted("alice", Count, ps)),
      assert(not(isPermitted("alice", Create, ps))),
      assert(not(isPermitted("alice", Update, ps))),
    }
  }

  run denyDoesNotOverrideGrant = {
    // An AllowAll policy + a DenyAll policy -> still permitted (union model).
    val ps = Map("alice" -> Set(AllowAll, DenyAll))
    all {
      assert(isPermitted("alice", Read, ps)),
      assert(isPermitted("alice", Create, ps)),
    }
  }

  run unionCombinesGrants = {
    // ReadOnly grants reads; AllowAll adds the writes -> both permitted.
    val ps = Map("alice" -> Set(ReadOnly, AllowAll))
    all {
      assert(isPermitted("alice", Create, ps)),
      assert(isPermitted("alice", Read, ps)),
    }
  }

  // The three built-ins are principal-independent, so a response under any set
  // of them keeps its shared-cache optimisation (never forced private).
  run builtinsStayShared = {
    all {
      assert(not(policyScopesToCaller(AllowAll))),
      assert(not(policyScopesToCaller(DenyAll))),
      assert(not(policyScopesToCaller(ReadOnly))),
      policySets.forall(ps => not(setScopesToCaller(ps))),
    }
  }

  // The union invariants hold for every policy set and action.
  run invariantsHold = {
    policySets.forall(useSet =>
      allActions.forall(a =>
        unionNoOverride("alice", a, useSet, Set(DenyAll), Map("alice" -> useSet)) and
        permittedImpliesSomeGrant("alice", a, Map("alice" -> useSet)) and
        deniedImpliesNoGrant("alice", a, Map("alice" -> useSet))
      )
    )
  }
}
