# Example 05 — Full RBAC (store-backed users, groups, roles & permissions)

A message board secured by **stored, server-authoritative RBAC**, demonstrating
Part 3 of the authentication roadmap (#133): real `users` / `groups` /
`group_users` / `roles` / `group_roles` / `role_permissions` tables, resolved
**per request** through group → role → permission.

It builds on [example 04](../04_simple_roles): where 04 carried roles on the
credential, 05 keeps only the principal's `user_id` on the credential and reads
everything else from the store.

## The idea

The credential (an API key) names a **user**. That user's **groups** carry
**roles**, and each role has **permissions** — a `resource` name plus a
[`Policy`](../../src/resourcey/auth/auth_policy.py). The resolver walks that
chain for the target resource and OR-combines the matching policies.

Seeded roles:

| Role | `threads` | `messages` |
| --- | --- | --- |
| `admin` | full access | full access |
| `viewer` | read-only | read-only |
| `author` | — | **own rows only** (`Owner(owner_field="author_id")`) |
| `denied` | — | `DenyAll` |

A principal holding **two roles** unions their filters: `viewer` ∪ `author`
reads *all* of `threads` and *all* of `messages`, but may update / delete only
its own messages. `denied`'s `DenyAll` never suppresses another role's grant
(the union model — there is no deny-wins override).

## Resolution, scaling, and freshness

`RbacPolicyResolver` (in
[`auth_rbac_resolver.py`](../../src/resourcey/auth/auth_rbac_resolver.py))
resolves, per request:

1. the principal's groups (`group_users`),
2. the roles those groups carry (`group_roles`),
3. the permissions those roles hold **for the target resource**
   (`role_permissions`, filtered by `resource`),

then binds group membership onto any `GroupMember` policy and returns the
policies; `AuthorizedService` OR-combines their reductions.

The result is **complete** for `(principal, resource)` — a disjunction cannot be
evaluated from a partial view. To scale, the resolution **collapses at the
store**: the query is scoped to the target resource and the principal's groups, a
`SELECT DISTINCT` collapses roles that share a policy, and many grants still
collapse under the union (k read grants become `AllFilter`, same-attribute ACLs
become one `IN` set via the set-valued leaf).

For a genuinely unbounded per-object grant set, the documented escape hatch is
the **materialized ACL**: enumerate object ids in `resource_acls` keyed by
`(principal_id, resource_name)` and evaluate the permission as a join /
subquery (`WHERE id IN (SELECT resource_id FROM resource_acls WHERE ...)`), so
the grants never enter Python. That table is derived data — a second cache to
invalidate on write — so prefer a broad rule like `Owner` ("everything I own").
The enumeration path is capped at `ACL_MAX_IDS` (100).

**Freshness.** The credential's validation / refresh threshold bounds how long a
resolved decision may be reused. The resolver makes that explicit via its
`cache_ttl`: `None` (the default, and what an API key uses) resolves the store on
every request, so a membership change is immediate; a positive value bounds
staleness to that window.

## Ownership scoping and the 404-vs-403 rule

A single-item read / update / delete of a row the principal cannot touch is a
**404**, not a 403 — existence does not leak. A denied **create** is a **403**,
and a denied collection read is **emptied** (never an error). This is the same
matrix as #127.

## Run it

```bash
uv sync --extra test
uv run --env-file .env alembic upgrade head
uv run --env-file .env python -m full_rbac.seed          # seed users/groups/roles
uv run --env-file .env uvicorn full_rbac.app:app --port 8085
```

the framework does no `.env` loading of its own, so the `--env-file` flag (uvicorn's, or
`uv run --env-file`) populates the process environment.

## Try it

```bash
# the admin can create and read anything
curl -H 'X-API-Key: admin-key' -X POST localhost:8085/threads \
     -H 'Content-Type: application/json' -d '{"title":"Hello"}'

# a viewer reads every message, but cannot write
curl -H 'X-API-Key: viewer-key' localhost:8085/messages

# an author reads every message but updates only its own
curl -H 'X-API-Key: author-key' localhost:8085/messages
```

## Tests

```bash
uv run pytest
```

The suite runs against an isolated SQLite database (schema applied by the
committed Alembic migration) through the full request → auth → resolve → service
→ SQLAlchemy stack.
