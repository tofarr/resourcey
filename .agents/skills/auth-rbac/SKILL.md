---
name: auth-rbac
description: Users, groups, roles, and per-action permission computation for resources. Load when working on auth, permissions, or RBAC.
version: "1.0.0"
---

# Auth & RBAC

`resourcey` models users, groups, and roles so that a per-user permission set
can be computed for every resource action. This was abstracted out of
[`ohev2`](https://github.com/tofarr/ohev2), where the same problems were
solved in a domain-specific setting.

## Entities

* **User** — a principal. Belongs to zero or more groups.
* **Group** — a collection of users. Has zero or more roles.
* **Role** — a named bundle of permissions (action grants/denials) over a
  resource.

## Permission computation

* For a given user and resource action, the framework computes whether the
  action is **permitted** by combining the roles reachable from the user's
  groups.
* Permission rules are declarative: a role grants or denies a
  `(resource, action)` pair. Denials can override grants when a "deny wins"
  policy is configured.
* The computed permission set is cached per request.

## Enforcement

* Every standard service action is permission-checked before execution.
* A `403 forbidden` is returned when the action is not permitted.
* Custom routes opt into enforcement via a dependency; they are not bypassed
  by default.

## Escape hatch

The permission engine is usable without the generated services — call it
directly from a custom route or background job.
