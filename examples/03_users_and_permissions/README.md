# Example 3 — Users and Permissions

A message board (like example 01) extended with **users**, **per-user
permissions**, and an **authenticated, permission-checked REST API**. Every
resource is served through a `SecuredService` wrapper that resolves the
principal from a session cookie and enforces a per-action permission policy
(`Permitted` / `CreatorPermission`) loaded from the `user_permissions` table.
Access is **fail-closed**: no `UserPermission` row for a (user, resource,
action) triple means no access — never a silent open.

This is the canonical "does the auth + RBAC story actually compose end to end?"
integration test for the framework.

## What this example demonstrates

| Concern | Where | Notes |
| ------- | ----- | ----- |
| Dev IdP login (cookie) | `POST /auth/dev/login` | Local username/password → JWE session cookie. Use for local dev; replace with a real IdP in production. |
| Secured services | `users_and_permissions/_secured.py` | `SecuredSqlResource` wraps `SqlService` in `SecuredService` with a DB-backed `PermissionResolver`. |
| Per-user permissions | `UserPermission` resource | Each row is one policy for one (user, resource-type) pair. |
| Creator-scoped access | `CreatorPermission` | The regular user can only read/update/delete their own threads, messages, and user record. |
| Fail-closed | every resource | No matching permission row → `NoneSearchFilter` → empty search / 403 create / 404 read. |

## Resources

| Resource | Fields | Notes |
| -------- | ------ | ---- |
| `Thread` | `id: UUID`, `title`, `description`, `creator_id`, timestamps | `creator_id` is auto-stamped from the principal on create. |
| `Message` | `id: UUID`, `thread_id` (FK → `threads.id`), `text`, `creator_id`, timestamps | `creator_id` auto-stamped; `thread_id` filterable. |
| `User` | `id: UUID`, `email`, `username`, `enabled`, `password`, `idp_user_id`, `creator_id`, timestamps | Mirrors the auth `User` table + `creator_id`; the resource-generated model is the superset. |
| `UserPermission` | `id: UUID`, `user_id` (FK → `users.id`), `resource_type`, `permission` (JSON), timestamps | The policy store; `permission` is a serialized `Permission` policy. |

All four resources inherit from `SecuredSqlResource` (this example) instead of
`SqlResource`. The `id: UUID` fields use a Python-side `uuid4` default — SQLite
has no `gen_random_uuid()` server default, so the column carries
`default=uuid4` via the `ResourceyField(column=...)` escape hatch.

## Layout

```
03_users_and_permissions/
├── README.md            # this file
├── pyproject.toml       # standalone project — depends on resourcey from git
├── .env                 # SQLite config + cookie flags for local HTTP dev
├── .gitignore
├── users_and_permissions/   # the importable app package
│   ├── app.py              # manifest + app entry point (auth + dev routers mounted)
│   ├── _secured.py         # SecuredSqlResource — wraps SqlService in SecuredService
│   ├── thread.py           # Thread resource
│   ├── message.py          # Message resource + MessageSearchFilter
│   ├── user.py             # User resource (superset of auth User)
│   └── user_permission.py  # UserPermission resource + UserPermissionSearchFilter
├── migrations/
│   ├── env.py              # Alembic env (merges ResourceyBase + AuthBase metadata)
│   ├── script.py.mako
│   └── versions/
│       └── 0001_init_users_and_permissions.py  # schema + seed (admin + regular user)
└── tests/
    ├── conftest.py        # throwaway encryption keys per test
    └── test_smoke.py      # full RBAC flow against in-memory SQLite
```

## Run it

This example is a **standalone project**. From within the
`03_users_and_permissions` directory:

```bash
# 1. Install (pulls resourcey from its main branch on GitHub).
uv sync

# 2. Apply the migration (creates users_and_permissions.db + seeds two users).
uv run resourcey migrate upgrade

# 3. Start the server.
uv run uvicorn users_and_permissions.app:app --reload
```

Open http://localhost:8083/docs for the OpenAPI UI.

### Seeded users

The migration seeds two users so you can exercise both sides of the permission
model immediately:

| Username | Password | Permissions |
| -------- | -------- | ----------- |
| `admin` | `admin` | `Permitted` on `Thread`, `Message`, `User`, `UserPermission` (full access). |
| `regular` | `regular` | `CreatorPermission(on_match=Permitted, on_create=Permitted)` on `Thread`, `Message`, `User` (own items only); nothing on `UserPermission`. |

### Try the flow

```bash
# Login as admin (sets the resourcey_session cookie).
curl -c cookies.txt -X POST http://localhost:8083/auth/dev/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}'

# Create a thread (admin is Permitted → 201).
curl -b cookies.txt -X POST http://localhost:8083/threads \
  -H 'Content-Type: application/json' \
  -d '{"title":"Admin thread","description":"hi"}'

# List user permissions (admin sees all 7 seeded rows).
curl -b cookies.txt http://localhost:8083/user-permissions

# Now login as the regular user (fresh cookie jar).
curl -c regular.txt -X POST http://localhost:8083/auth/dev/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"regular","password":"regular"}'

# The regular user can create their own thread…
curl -b regular.txt -X POST http://localhost:8083/threads \
  -H 'Content-Type: application/json' -d '{"title":"My thread"}'

# …but cannot read the admin's thread (CreatorPermission → 404, no leak).
curl -b regular.txt http://localhost:8083/threads/<admin_thread_id>

# …and cannot create a UserPermission row (no grant → 403, fail-closed).
curl -b regular.txt -X POST http://localhost:8083/user-permissions \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"<regular_id>","resource_type":"Thread","permission":{"kind":"Permitted"}}'
```

The session cookie also works as a `Bearer` token for API clients that can't
hold cookies:

```bash
TOKEN=$(curl -s -X POST http://localhost:8083/auth/dev/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin"}' | jq -r ...)  # or grab from cookie
curl -H "Authorization: Bearer $TOKEN" http://localhost:8083/threads
```

## How it works

### `SecuredSqlResource` (the one new class)

`SecuredSqlResource(SqlResource)` overrides `open_service` to wrap the
`SqlService` in a `SecuredService` before yielding it to the route handler:

```python
class SecuredSqlResource(SqlResource):
    def open_service(self, request: Request) -> Iterator[SecuredService]:
        session = self._ctx.get(SESSION_FACTORY_KEY)()
        principal = _resolve_principal(request, session)
        inner = SqlService(self, session)
        yield SecuredService(inner, principal, _permission_resolver(session))
```

- **Principal** — `_resolve_principal` reads the session cookie (or `Bearer`
  token) from the request, decrypts the JWE via `TokenService`, and returns
  `(user_id, groups)`. No/invalid token → principal is `(None, [])`.
- **PermissionResolver** — a DB-backed resolver that loads the principal's
  `UserPermission` rows for the resource type and applies the policy. `None`
  (no rows) → `NoneSearchFilter` → deny (fail-closed).
- **`creator_id` stamping** — `SecuredService._authorize_create` stamps
  `creator_id` from the principal on create so `CreatorPermission` can scope
  "your own" without the client supplying it.

### Coexistence with the auth tables

The auth feature ships its own ORM models (`User`, `UserPermission`,
`IdpAccessToken`, …) on `AuthBase`. This example's `User` and `UserPermission`
resources generate models on `ResourceyBase` for the **same physical tables**.
The resource models are supersets — `User` adds `creator_id` — so the merged
Alembic metadata (ResourceyBase first, then AuthBase tables not already
present) produces one `users` table with every column both layers need.

## Tests

```bash
uv run pytest -q
```

The smoke suite (`tests/test_smoke.py`) builds the app against an in-memory
SQLite database, seeds the two users + their permissions, and exercises the
full RBAC flow over `httpx`'s ASGI transport: anonymous denial, admin full
access, regular-user creator-scoped access, cross-user 404s (no information
leak), and `Bearer`-token auth.

## Notes

- The `.env` sets `RESOURCEY_AUTH_COOKIE_SECURE=false` and
  `RESOURCEY_AUTH_COOKIE_SAMESITE=lax` so the OpenAPI docs page can log in over
  plain HTTP. In production set `SECURE=true` and `SAME_SITE=strict` behind HTTPS.
- `RESOURCEY_ENCRYPTION_KEY_VALUE` is a throwaway dev secret. Replace it before
  any deployment.
- The dev IdP (`/auth/dev`) is for local development only. A production
  deployment wires a real OAuth IdP through the `/auth` routes instead.
