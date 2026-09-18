// permissions.qnt — formal spec for RBAC permission computation.
//
// A user reaches roles via groups. A role grants or denies a
// (resource, action) pair. Under the default "deny wins" policy, a denial
// overrides any grant for the same (resource, action).
//
// Invariants:
//   - denialWins: a denied action is never permitted, even if also granted.
//   - permittedImpliesGranted: permission requires an explicit grant.

module permissions {
  type Action = Create | Read | Update | Delete | Search | BatchRead | BatchEdit

  // A (resource, action) pair subject to grant or denial.
  type Perm = (str, Action)

  // Static inputs (treated as immutable parameters).
  pure def userGroups(user: str): Set[str] = Set()
  pure def groupRoles(group: str): Set[str] = Set()
  pure def roleGrants(role: str): Set[Perm] = Set()
  pure def roleDenials(role: str): Set[Perm] = Set()

  // The set of groups a user belongs to (parameterised for testability).
  pure def groupsForUser(user: str, ug: str -> Set[str]): Set[str] = ug.get(user)

  // The set of roles a user reaches through their groups.
  pure def rolesForUser(user: str, ug: str -> Set[str], gr: str -> Set[str]): Set[str] =
    groupsForUser(user, ug).fold(Set(), (acc, g) => acc.union(gr.get(g)))

  // The set of (resource, action) pairs granted to a user.
  pure def grantsForUser(user: str, ug: str -> Set[str], gr: str -> Set[str],
                         rg: str -> Set[Perm]): Set[Perm] =
    rolesForUser(user, ug, gr).fold(Set(), (acc, r) => acc.union(rg.get(r)))

  // The set of (resource, action) pairs denied to a user.
  pure def denialsForUser(user: str, ug: str -> Set[str], gr: str -> Set[str],
                          rd: str -> Set[Perm]): Set[Perm] =
    rolesForUser(user, ug, gr).fold(Set(), (acc, r) => acc.union(rd.get(r)))

  // Under "deny wins", an action is permitted iff granted and not denied.
  pure def isPermitted(user: str, resource: str, act: Action,
                       ug: str -> Set[str], gr: str -> Set[str],
                       rg: str -> Set[Perm], rd: str -> Set[Perm]): bool = {
    val grants = grantsForUser(user, ug, gr, rg)
    val denials = denialsForUser(user, ug, gr, rd)
    grants.contains((resource, act)) and not(denials.contains((resource, act)))
  }

  // Invariant: a denied action is never permitted, even if also granted.
  pure def denialWins(user: str, resource: str, act: Action,
                      ug: str -> Set[str], gr: str -> Set[str],
                      rg: str -> Set[Perm], rd: str -> Set[Perm]): bool = {
    val denials = denialsForUser(user, ug, gr, rd)
    denials.contains((resource, act)) implies
      not(isPermitted(user, resource, act, ug, gr, rg, rd))
  }

  // Invariant: permission requires an explicit grant.
  pure def permittedImpliesGranted(user: str, resource: str, act: Action,
                                   ug: str -> Set[str], gr: str -> Set[str],
                                   rg: str -> Set[Perm], rd: str -> Set[Perm]): bool = {
    val grants = grantsForUser(user, ug, gr, rg)
    isPermitted(user, resource, act, ug, gr, rg, rd) implies
      grants.contains((resource, act))
  }
}
