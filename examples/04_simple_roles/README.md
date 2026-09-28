# Example 04 — Simple roles (role-based API keys)

A small message board secured by **role-carrying API keys**, demonstrating Part 2
of the authentication roadmap (#132): a per-app role vocabulary, roles carried on
the credential, and the role → policy translation centralized in **one place**.

It builds on [example 03](../03_api_key_auth) (API-key authentication) and adds
the authorization dimension.

## The idea

Every resource is **readable by default** — anonymous callers included — because
reads are public reference data on this board. Roles gate the **writes**:

| Role | `threads` | `messages` |
| --- | --- | --- |
| `ADMIN` | full access | full access |
| `MODERATOR` | read | create / update / delete any |
| `USER` | read | create any; **edit own rows only** |

The headline rule is the `USER` row: *read all of `messages`, but only update /
delete its own rows*. It is expressed by the union of two policies — `ReadOnly`
(reads every row) and [`Owner`](../../src/resourcey/v2/auth/auth_policy.py)
(`Owner(owner_field="author_id")`, scopes the by-id writes). The union model
OR-combines them, and `OR(All, own) == All` for reads, so the two compose to
**read-all / write-own**.

The public read default is what makes an anonymous read a `200`: the builder runs
with `Posture.OPTIONAL`, so a missing credential is anonymous (a *presented but
invalid* key is still a `401`), and each public resource falls to a
resource-level `ReadOnly` default.

All of the rules live in `ROLE_POLICIES` in
[`simple_roles/app.py`](simple_roles/app.py) — a
[`RolePolicyResolver`](../../src/resourcey/v2/auth/auth_role.py) with a global
map, a per-resource map, and per-resource defaults. Nothing else in the app
encodes a permission.

Roles are **per-app**: the `Role` `StrEnum` declares the set this app recognizes
(`Role.ADMIN` makes routing free of magic strings), while the credential carries
the plain string.

## Stored principals (the `users` resource)

Part 2 carried roles on the credential, so a role needed no lookup. The
**principal** a key acts as, however, is now a stored row: the `users` table,
served read-only at `/users`.

A key's `PRINCIPAL_ID` names a `users` row, and the authenticator validates it:
a key whose principal is missing or `enabled=False` is rejected (`401`), so a
stored principal's `enabled` flag is authoritative over the credential (disable a
user to revoke every key that acts as it). Two principals are seeded — by the
committed migration, and by [`simple_roles/seed.py`](simple_roles/seed.py) for a
database built directly from the ORM metadata:

| Username | Id (`PRINCIPAL_ID`) |
| --- | --- |
| `admin` | `00000000-0000-0000-0000-000000000001` |
| `user` | `11111111-1111-1111-1111-111111111111` |

The `users` resource is **admin-only and not public**: it is deliberately absent
from the resource-level defaults, so no role maps a `User` grant and only
`ADMIN`'s global `AllowAll` reaches it — a `USER` / `MODERATOR` / anonymous
caller searching `/users` gets an empty page and a by-id read is a `404`
(existence is not leaked). It is also read-only — the view narrows it to the
read subset, so no write route is ever mounted.

Stored **groups / roles / permissions** (so membership is resolved per request)
are a later rung ([#133](https://github.com/tofarr/resourcey/issues/133)); here
`User` is the identity store only.

## Roles on the credential (no DB lookup)

Each accepted key has `roles` in the environment, so a presented key's roles are
resolved without touching a database:

```dotenv
APP_API_KEYS_0_ID=admin
APP_API_KEYS_0_KEY=admin-key
APP_API_KEYS_0_PRINCIPAL_ID=00000000-0000-0000-0000-000000000001  # the stored `admin` user
APP_API_KEYS_0_ROLES_0=ADMIN

APP_API_KEYS_1_ID=user
APP_API_KEYS_1_KEY=user-key
APP_API_KEYS_1_PRINCIPAL_ID=11111111-1111-1111-1111-111111111111  # the stored `user` principal `Owner` scopes on
APP_API_KEYS_1_ROLES_0=USER
```

A key with no `roles` can still read the public board, but has no write grant. A
key whose `PRINCIPAL_ID` names no live `users` row is rejected outright — the
credential's roles are irrelevant if the principal is not a stored, enabled user.

## Caller-private caching

The public reads are the same for every caller, but a `USER`'s *writes* are
own-rows only. The `Owner` policy declares `scopes_to_caller = True`, so the
authorization service marks the response `Cache-Control: private` when a
caller-scoped policy is in the union: a shared cache (proxy/CDN) therefore
neither stores nor revalidates it, and an `ETag` / `Last-Modified` alone cannot
let one principal's validator replay another principal's slice.

## What to try

```bash
# anonymous reads are public
curl localhost:8084/threads
curl localhost:8084/messages

# the admin can create and read anything
curl -H 'X-API-Key: admin-key' -X POST localhost:8084/threads \
     -H 'Content-Type: application/json' -d '{"title":"Hello"}'

# a user creates a message, then reads every message...
curl -H 'X-API-Key: user-key' -X POST localhost:8084/messages \
     -H 'Content-Type: application/json' -d '{"thread_id":1,"text":"hi"}'
curl -H 'X-API-Key: user-key' localhost:8084/messages

# ...but can only edit its own (404 on someone else's)
curl -H 'X-API-Key: user-key' -X PATCH localhost:8084/messages/1 \
     -H 'Content-Type: application/json' -d '{"text":"edited"}'

# only the admin sees the stored principals; everyone else gets an empty page
curl -H 'X-API-Key: admin-key' localhost:8084/users
curl -H 'X-API-Key: user-key'  localhost:8084/users   # {"items": []}
```

## Run it

```bash
uv sync --extra test
uv run --env-file .env alembic upgrade head   # creates the schema and seeds the principals
uv run --env-file .env uvicorn simple_roles.app:app --port 8084
```

`v2` does no `.env` loading of its own, so the `--env-file` flag (uvicorn's, or
`uv run --env-file`) is what populates the process environment.

## Tests

```bash
uv run pytest
```

The suite runs against an isolated SQLite database (schema applied by the
committed Alembic migration, principals seeded by it) through the full request →
auth → role → principal-store → service → SQLAlchemy stack.
