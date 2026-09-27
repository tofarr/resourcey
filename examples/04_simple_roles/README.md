# Example 04 — Simple roles (role-based API keys)

A small message board secured by **role-carrying API keys**, demonstrating Part 2
of the authentication roadmap (#132): a per-app role vocabulary, roles carried on
the credential, and the role → policy translation centralized in **one place**.

It builds on [example 03](../03_api_key_auth) (API-key authentication) and adds
the authorization dimension.

## The idea

Three roles, and one resolver declaring how each maps to policies:

| Role | `threads` | `messages` |
| --- | --- | --- |
| `ADMIN` | full access | full access |
| `MODERATOR` | read-only | full access |
| `USER` | read-only | **own rows only** |

The headline rule is the `USER` row: *read all of `threads`, but only read /
update / delete its own rows of `messages`*. That is expressed by the
[`Owner`](../../src/resourcey/v2/auth/auth_policy.py) policy
(`Owner(owner_field="author_id")`).

All of the rules live in `ROLE_POLICIES` in
[`simple_roles/app.py`](simple_roles/app.py) — a
[`RolePolicyResolver`](../../src/resourcey/v2/auth/auth_role.py) with a global
map and a per-resource map. Nothing else in the app encodes a permission.

Roles are **per-app**: the `Role` `StrEnum` declares the set this app recognizes
(`Role.ADMIN` makes routing free of magic strings), while the credential carries
the plain string.

## Roles on the credential (no DB lookup)

Each accepted key has `roles` in the environment, so a presented key's roles are
resolved without touching a database:

```dotenv
APP_API_KEYS_0_ID=admin
APP_API_KEYS_0_KEY=admin-key
APP_API_KEYS_0_ROLES_0=ADMIN

APP_API_KEYS_1_ID=user
APP_API_KEYS_1_KEY=user-key
APP_API_KEYS_1_PRINCIPAL_ID=<a-uuid>   # the principal id `Owner` scopes on
APP_API_KEYS_1_ROLES_0=USER
```

A key with no `roles` authenticates but is denied everything (fail-closed).

## What to try

```bash
# the admin can create and read anything
curl -H 'X-API-Key: admin-key' -X POST localhost:8084/threads \
     -H 'Content-Type: application/json' -d '{"title":"Hello"}'

# a user reads every thread...
curl -H 'X-API-Key: user-key' localhost:8084/threads

# ...but only its own messages
curl -H 'X-API-Key: user-key' localhost:8084/messages
```

## Run it

```bash
uv sync --extra test
uv run --env-file .env alembic upgrade head
uv run --env-file .env uvicorn simple_roles.app:app --port 8084
```

`v2` does no `.env` loading of its own, so the `--env-file` flag (uvicorn's, or
`uv run --env-file`) is what populates the process environment.

## Tests

```bash
uv run pytest
```

The suite runs against an isolated SQLite database (schema applied by the
committed Alembic migration) through the full request → auth → role → service →
SQLAlchemy stack.
