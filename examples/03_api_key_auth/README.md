# Example 3 — API Key Authentication

A message board (like example 01) secured by a single **API key read from the
environment**. There are no users, no sessions, no auth tables, and no
`/auth/*` routes: a request that presents a valid key gets the full REST API, and
anything else gets `401`.

This is the simplest end-to-end demonstration of issue #63's authentication
seam — the `resourcey.auth2` package (the successor to `resourcey.auth`) — and
of issue #62's `DependencyBuilder`, which applies the posture to every resource
from one config value.

## What this example demonstrates

| Concern | Where | Notes |
| ------- | ----- | ----- |
| API-key auth | `resourcey.auth2.auth2_api_key.ApiKeyDependencyBuilder` | Reads accepted keys from config; no users, no DB, no sessions. |
| One-value posture | `.env` | `DEPENDENCY_BUILDER_CLASS` selects the builder; `DEPENDENCY_BUILDER_API_KEYS_0` supplies the key. |
| Key rotation | `DEPENDENCY_BUILDER_API_KEYS_*` | A list: add the new key alongside the old, deploy, then remove the old. |
| Fail-closed | builder default | An empty key list denies every request with `401`. |

## Resources

Only two plain `SqlResource`s — the same message board as example 01. Nothing in
either file mentions authentication; the key check is composed in by the
`DependencyBuilder`.

| Resource | Fields | Notes |
| -------- | ------ | ----- |
| `Thread` | `id: int`, `title`, `description`, timestamps | Parent of a message. |
| `Message` | `id: int`, `thread_id` (FK → `threads.id`), `text`, timestamps | `thread_id__eq` is filterable so a thread's messages can be listed. |

## Layout

```
03_api_key_auth/
├── README.md            # this file
├── pyproject.toml       # standalone — resourcey from git (parent checkout in-repo)
├── .env                 # SQLite config + the API-key posture
├── .gitignore
├── api_key_auth/        # the importable app package
│   ├── app.py           # manifest (Thread + Message) + app entry point
│   ├── thread.py        # Thread resource
│   └── message.py       # Message resource + MessageSearchFilter
├── migrations/
│   ├── env.py              # Alembic env (ResourceyBase.metadata)
│   ├── script.py.mako
│   └── versions/
│       └── 9c1f0a2b7d34_api_key_auth_init.py  # threads + messages
└── tests/
    ├── conftest.py        # throwaway encryption key per test
    └── test_smoke.py      # correct / incorrect / missing key
```

## Run it

This example is a **standalone project**. From within the `03_api_key_auth`
directory:

```bash
# 1. Install. Inside the resourcey repo this builds the parent checkout (your
#    branch); a copied-out example falls back to main — see below.
uv sync

# 2. Apply the migration (creates api_key_auth.db).
uv run resourcey migrate upgrade

# 3. Start the server.
uv run uvicorn api_key_auth.app:app --reload --port 8083
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

The key is in `.env` (`DEPENDENCY_BUILDER_API_KEYS_0=example-api-key`):

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

Three lines of configuration carry the whole posture:

```python
class ApiKeyDependencyBuilder(DependencyBuilder):
    api_keys: list[SecretStr]

    def get_service_dependency(self, resource: BaseResource) -> Callable[..., Any]:
        ...
```

`.env`:

```bash
DEPENDENCY_BUILDER_CLASS=resourcey.auth2.auth2_api_key.ApiKeyDependencyBuilder
DEPENDENCY_BUILDER_API_KEYS_0=example-api-key
```

- **`DependencyBuilder` seam (#62).** `FrameworkConfig.dependency_builder` is
  resolved once at route-registration time, so the one `DEPENDENCY_BUILDER_CLASS`
  value swaps the per-request dependency for *every* resource. The builder's
  `get_service_dependency` returns a dependency that first requires a valid key
  and only then yields the resource's service — so an unauthenticated request is
  rejected before any storage is opened.
- **Keys from the environment.** `DEPENDENCY_BUILDER_API_KEYS_0`, `_1`, …
  (or a JSON array in `DEPENDENCY_BUILDER_API_KEYS`) become the accepted keys.
  Keep the list to more than one during a rotation so old and new clients both
  work while you roll the change out.
- **`401`, consistently.** A missing key and a wrong key both return `401`
  with a `WWW-Authenticate: Bearer` challenge, so the endpoint does not reveal
  whether a credential was expected and the response complies with HTTP. The
  schemes are declared with FastAPI `Security`, so both `X-API-Key` and
  `Bearer` appear in the OpenAPI schema.
- **The key stays out of the database.** There is nothing to migrate for auth —
  the example's schema is just `threads` and `messages`. The key lives only in
  the environment; in production supply it from your secret manager rather than
  committing it as this example does.

### Configuring the key outside `.env`

Any environment source works, since the builder is built by the standard config
machinery. For example:

```bash
export DEPENDENCY_BUILDER_CLASS=resourcey.auth2.auth2_api_key.ApiKeyDependencyBuilder
export DEPENDENCY_BUILDER_API_KEYS='["key-one","key-two"]'   # JSON array form
uv run uvicorn api_key_auth.app:app
```

To require the key on a *custom* router rather than via the builder, use the
general-purpose dependency:

```python
from fastapi import APIRouter, Depends
from resourcey.auth2.auth2_api_key import get_api_key_dependency

router = APIRouter(dependencies=[Depends(get_api_key_dependency())])
```

## Tests

```bash
uv run pytest -q
```

The smoke suite (`tests/test_smoke.py`) builds the app against an in-memory
SQLite database with the builder selected exactly as `.env` selects it, then
pins the client outcomes: correct key (CRUD succeeds), incorrect key (`401`),
missing key (`401`), and an empty configured key list (fail-closed) — including
the `Bearer` fallback and the `WWW-Authenticate` challenge.

## Notes

- This posture grants the holder of the key access to **every** resource; it
  models no principal and no per-action authorization. Per-user permissions
  live in `resourcey.auth` (users, sessions, and a policy engine, wired through
  the same `DependencyBuilder` seam shown here).
- `RESOURCEY_ENCRYPTION_KEY_VALUE` is a throwaway dev secret (it encrypts
  pagination cursors). Replace it before any deployment.
