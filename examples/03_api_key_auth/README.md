# Example 3 — API Key Authentication

A message board (like example 01) secured by **API keys read from the
environment**. There are no users, no sessions, no auth tables, and no
`/auth/*` routes: a request that presents a valid key gets the full REST API,
and anything else gets `401`.

This is the end-to-end demonstration of the `v2` authentication seam —
`resourcey.v2.auth` (issue #118), the successor to `resourcey.auth2` — and of
the `v2/http` `DependencyBuilder` seam (issue #86), which applies the posture to
every resource from one object.

## What this example demonstrates

| Concern | Where | Notes |
| ------- | ----- | ----- |
| API-key auth | `resourcey.v2.auth.auth_api_key.ApiKeyDependencyBuilder` | Reads accepted keys from config; no users, no DB, no sessions. |
| One-object posture | `api_key_auth/app.py` | The builder is passed to `create_app(..., dependency_builder=...)`, so it secures every resource. |
| Key rotation | `APP_API_KEYS_*` | A list: add the new key alongside the old, deploy, then remove the old. |
| Fail-closed | builder default | An empty key list denies every request with `401`. |
| Digest at rest | `v2/auth` | The key is hashed on load; the served entry holds only the SHA-256 digest. |

## Resources

Only two plain `SqlResource`s — the same message board as example 01. Nothing in
either file mentions authentication; the key check is composed in by the
`DependencyBuilder`.

| Resource | Fields | Notes |
| -------- | ------ | ----- |
| `Thread` | `id: int`, `title`, `description`, timestamps | Parent of a message. |
| `Message` | `id: int`, `thread_id` (FK → `threads.id`), `text`, timestamps | `thread_id__eq` is filterable so a thread's messages can be listed. |

Both are **SQLAlchemy ORM models** in `api_key_auth/models.py` (v2 is
model-first); the resources are thin `SqlResource` subclasses over them.

## Layout

```
03_api_key_auth/
├── README.md            # this file
├── pyproject.toml       # standalone — resourcey from git (parent checkout in-repo)
├── alembic.ini          # Alembic config (URL comes from APP_SQL_CONNECTIONS_0_URL)
├── .env                 # APP_* config + the API-key posture (committed)
├── .gitignore
├── api_key_auth/        # the importable app package
│   ├── app.py           # manager + manifest + app, with the API-key builder
│   ├── models.py        # Thread & Message ORM models + Base (schema of record)
│   └── message.py       # MessageResource (declared filter)
├── migrations/
│   ├── env.py              # Alembic env, diffs against the ORM metadata
│   ├── script.py.mako
│   └── versions/
│       └── <rev>_api_key_auth_init.py  # threads + messages (no auth table)
└── tests/
    ├── conftest.py        # throwaway cursor key per test
    ├── test_smoke.py      # correct / incorrect / missing key (in-memory SQLite)
    └── test_e2e.py        # the same posture against the committed migration
```

## Run it

This example is a **standalone project**. From within the `03_api_key_auth`
directory:

```bash
# 1. Install. Inside the resourcey repo this builds the parent checkout (your
#    branch); a copied-out example falls back to main — see below.
uv sync

# 2. Apply the migration (creates api_key_auth.db).
uv run --env-file .env alembic upgrade head

# 3. Start the server. v2 does no .env loading, so pass the file explicitly.
uv run uvicorn api_key_auth.app:app --env-file .env --reload --port 8083
```

Open http://localhost:8083/docs for the OpenAPI UI.

### Running inside the resourcey repository

`pyproject.toml` depends on `resourcey` from GitHub `main`, but adds a
`[tool.uv.sources]` override to the parent checkout (`../..`). Inside a
resourcey checkout `uv sync` therefore builds the working tree — your branch or
PR — instead of published `main`. A copy of this directory placed outside the
repo cannot resolve that path; run it with the override disabled:

```bash
uv sync --no-sources        # and `uv run --no-sources ...` thereafter
```

## Try the flow

The key is in `.env` (`APP_API_KEYS_0_KEY=example-api-key`):

```bash
# With the correct key → 201.
curl -X POST http://localhost:8083/threads \
  -H 'X-API-Key: example-api-key' \
  -H 'Content-Type: application/json' \
  -d '{"title":"Admin thread","description":"hi"}'

# The same key as a Bearer token also works.
curl -H 'Authorization: Bearer example-api-key' http://localhost:8083/threads

# With a wrong key → 401.
curl -i -X POST http://localhost:8083/threads \
  -H 'X-API-Key: nope' \
  -H 'Content-Type: application/json' \
  -d '{"title":"Nope"}'

# With no key → 401.
curl -i http://localhost:8083/threads
```

## How it works

The whole posture is one object and one argument:

```python
key_inner = config_api_key_resource(ApiKeysConfig.get_instance())
builder = ApiKeyDependencyBuilder(key_resource=key_inner)
manifest = Manifest(resources=[..., config_api_key_view(key_inner)], managers=[manager])
app = create_app(manifest, dependency_builder=builder)
```

`.env`:

```bash
APP_API_KEYS_0_ID=example
APP_API_KEYS_0_KEY=example-api-key
```

- **`DependencyBuilder` seam (#86).** `create_app`'s `dependency_builder=`
  argument is resolved once at route-registration time, so the one builder swaps
  the per-request dependency for *every* resource. The builder's
  `get_service_dependency` returns a dependency that first requires a valid key
  and only then yields the resource's service — so an unauthenticated request is
  rejected before any storage is opened.
- **Keys from the environment.** `APP_API_KEYS_0_ID` / `_KEY`, `_1_*`, … (or a
  JSON array in `APP_API_KEYS`) become the accepted keys. Keep the list to more
  than one during a rotation so old and new clients both work while you roll the
  change out.
- **`401`, consistently.** A missing key and a wrong key both return `401` with
  a `WWW-Authenticate: Bearer realm="api-key"` challenge, so the endpoint does
  not reveal whether a credential was expected and the response complies with
  HTTP. The schemes are declared with FastAPI `Security`, so both `X-API-Key`
  and `Bearer` appear in the OpenAPI schema.
- **The key stays out of the database.** There is nothing to migrate for auth —
  the example's schema is just `threads` and `messages`. The key lives only in
  the environment; in production supply it from your secret manager rather than
  committing it as this example does. The served key resource holds only a
  SHA-256 digest and hides it from every response and from the query surface.

### Configuring the key outside `.env`

Any environment source works, since the key list is parsed by the standard
config machinery. For example:

```bash
export APP_API_KEYS='[{"id":"k1","key":"key-one"},{"id":"k2","key":"key-two"}]'
uv run uvicorn api_key_auth.app:app --env-file .env
```

To require the key on a *custom* router rather than via the builder, use the
general-purpose dependency:

```python
from fastapi import APIRouter, Depends
from resourcey.v2.auth.auth_api_key import ApiKeyDependencyBuilder

router = APIRouter(
    dependencies=[Depends(ApiKeyDependencyBuilder(key_resource=key_inner).api_key_dependency)]
)
```

## Migrations

v2 has **no** `resourcey migrate` wrapper (that CLI reads the v1
`ResourceyBase` / `FrameworkConfig.manifest`, which do not exist in v2). Since
SQLAlchemy is the schema of record, the example drives **Alembic directly**
against `api_key_auth.models.Base.metadata`.

The committed revision under `migrations/versions/` was generated with:

```bash
uv run --env-file .env alembic revision --autogenerate -m "api key auth init"
```

Apply / roll back from scratch:

```bash
rm -f api_key_auth.db
uv run --env-file .env alembic upgrade head
uv run --env-file .env alembic downgrade base
```

## Tests

```bash
uv sync --extra test
uv run pytest -q
```

The smoke suite (`tests/test_smoke.py`) builds the app against an in-memory
SQLite database with an injected key list, then pins the client outcomes:
correct key (CRUD succeeds), incorrect key (`401`), missing key (`401`), and an
empty configured key list (fail-closed) — including the `Bearer` fallback and
the `WWW-Authenticate` challenge. `tests/test_e2e.py` runs the same posture
against an isolated database created by applying the committed migration.

## Notes

- This posture grants the holder of a key access to **every** resource; it
  models no principal and no per-action authorization. Per-user permissions
  (users, sessions, and a policy engine) live in `resourcey.auth` and plug into
  the same `DependencyBuilder` seam shown here.
- The DB-backed key source (`stored_api_key_resource` / `stored_api_key_view` in
  `resourcey.v2.auth.auth_api_key_resource`) mints and revokes keys through the
  REST surface (`POST /api-keys`, …); this example uses the config-list source
  because it keeps the keys entirely in the environment.
- The key resource is exposed read-only at `GET /api-keys` (ids and names only —
  the digest is hidden from every response and from the query surface, so
  `?key__eq=` is a `400`). A write is a `405`, because the list backend narrows
  its actions to the read subset.
- `APP_ENCRYPTION_KEY_VALUE` is a throwaway dev secret (it encrypts pagination
  cursors). Replace it before any deployment.
