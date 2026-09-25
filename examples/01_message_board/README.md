# Example 1 — The Message Board

The first end-to-end example application built on `resourcey`, ported to the
**v2** API. Two resources — `Thread` and `Message` — with a one-to-many relation,
wired into a runnable FastAPI app with auto-generated REST endpoints, an Alembic
migration, and a SQLite database by default.

This is the canonical "does this framework actually work for a real app?" smoke
test, and the reference a new user reads first.

## v2 in one paragraph

`v2` is **model-first**: you declare the SQLAlchemy ORM model you already work
with, and the framework infers the DTO and the six REST models from it. There is
no DTO-to-model generation and no declarative base to manage — SQLAlchemy is the
schema of record, so migrations and foreign keys stay SQLAlchemy's / Alembic's
concern. The app is assembled from three pieces: a `SqlSessionManager` (the
engines), a `core.manifest.Manifest` (the resource set and lifecycle), and the
`http.app.create_app` free function (the routes + error handlers + CORS).

## Resources

| Resource  | Fields                                                                 |
| --------- | --------------------------------------------------------------------- |
| `Thread`  | `id: int`, `title: str`, `description: str \| None`, `created_at`, `updated_at` |
| `Message` | `id: int`, `thread_id: int` (FK → `threads.id`), `text: str`, `created_at`, `updated_at` |

Both are **SQLAlchemy ORM models** in `message_board/models.py`; `Thread` and
`Message` resources are thin `SqlResource` subclasses over them.
`Message.thread_id` is a real `ForeignKey` column to `threads.id`.

`Message.search` opts into a declared filter surface: a `MessageSearchFilter`
(`BaseObjectFilter`) returned from `get_search_filter_type()`, exposing
`thread_id__eq` (list a thread's messages) and `text__contains` (substring
search). Declaring it also *narrows* the surface — v2 otherwise derives one from
the read model — so `?id__eq=` is rejected.

## Layout

```
01_message_board/
├── README.md            # this file
├── pyproject.toml       # standalone — resourcey from git (parent checkout in-repo)
├── alembic.ini          # Alembic config (URL comes from APP_SQL_CONNECTIONS_0_URL)
├── .env                 # APP_* config (committed); pass with --env-file
├── .gitignore
├── message_board/       # the importable app package
│   ├── app.py           # manager + manifest + app (uvicorn target)
│   ├── models.py        # Thread & Message ORM models + Base (schema of record)
│   └── message.py       # MessageSearchFilter + MessageResource (declared filter)
├── migrations/
│   ├── env.py           # Alembic env, diffs against the ORM metadata
│   └── versions/        # generated + reviewed revisions
└── tests/
    ├── test_smoke.py    # HTTP smoke test (in-memory SQLite, httpx ASGI)
    └── test_e2e.py      # full suite against the committed migration
```

## Run it

This example is a **standalone project**. From within the `01_message_board`
directory:

```bash
# 1. Install dependencies. Inside the resourcey repo this resolves resourcey
#    from the parent checkout (your branch); a copied-out example falls back to
#    main — see "Running inside the resourcey repository" below.
uv sync

# 2. Apply the database migration (creates message_board.db)
uv run --env-file .env alembic upgrade head

# 3. Start the server. v2 does no .env loading, so pass the file explicitly.
uv run uvicorn message_board.app:app --env-file .env --reload --port 8081
# → Uvicorn running on http://127.0.0.1:8081
```

Interactive API docs are at `http://127.0.0.1:8081/docs`.

### Running inside the resourcey repository

The `pyproject.toml` depends on `resourcey` from GitHub `main`, but adds a
`[tool.uv.sources]` override to the parent checkout (`../..`). So when this
example lives inside a resourcey checkout, `uv sync` builds the working tree —
your branch or PR — not published `main`. This is the mode the CI e2e job uses,
so the example is a real integration test for the code under review.

A copy of this directory placed outside the repo can no longer resolve that
relative path. Run it with the override disabled to fall back to the git
dependency:

```bash
uv sync --no-sources        # and `uv run --no-sources ...` thereafter
```

### Testing

Run the tests (in-memory SQLite smoke + migrated-file e2e, no server needed):

```bash
uv sync --extra test
uv run pytest
```

### Configuration

The example uses the v2 config blocks with a committed `.env`. v2 reads the
process-wide `APP` prefix and does **no** `.env` loading, so every command that
needs config passes `--env-file .env` (or exports the vars).

| Env var                        | Default                                   | Purpose                                    |
| ------------------------------ | ----------------------------------------- | ------------------------------------------ |
| `APP_SQL_CONNECTIONS_0_NAME`   | `main`                                    | Connection name (first is the default).    |
| `APP_SQL_CONNECTIONS_0_URL`    | `sqlite+aiosqlite:///message_board.db`    | Complete SQLAlchemy URL (driver in scheme).|
| `APP_SQL_CONNECTIONS_0_PASSWORD` | *(unset)*                               | Optional separate password (SOPS-friendly).|
| `APP_ENCRYPTION_KEY_ID`        | `dev`                                     | Cursor-encryption key id.                  |
| `APP_ENCRYPTION_KEY_VALUE`     | *(dev default + warning when unset)*      | Cursor-encryption key value.               |

To target Postgres instead, set a Postgres URL — the password can be embedded in
the URL or supplied separately via `APP_SQL_CONNECTIONS_0_PASSWORD` (kept in its
own variable so it can be encrypted at rest, e.g. with SOPS):

```bash
APP_SQL_CONNECTIONS_0_NAME=main
APP_SQL_CONNECTIONS_0_URL=postgresql+asyncpg://resourcey@localhost:5432/resourcey
APP_SQL_CONNECTIONS_0_PASSWORD=secret
```

## Example HTTP requests

Create a thread:

```bash
curl -s -X POST http://127.0.0.1:8081/threads \
  -H "Content-Type: application/json" \
  -d '{"title":"Hello","description":"first thread"}'
# → {"id":1,"title":"Hello","description":"first thread","created_at":"…","updated_at":"…"}
```

Create messages in that thread:

```bash
curl -s -X POST http://127.0.0.1:8081/messages \
  -H "Content-Type: application/json" \
  -d '{"thread_id":1,"text":"hi there"}'
# → {"id":1,"thread_id":1,"text":"hi there","created_at":"…","updated_at":"…"}

curl -s -X POST http://127.0.0.1:8081/messages \
  -H "Content-Type: application/json" \
  -d '{"thread_id":1,"text":"second message"}'
```

List a thread's messages (the `thread_id__eq` filter):

```bash
curl -s "http://127.0.0.1:8081/messages?thread_id__eq=1"
# → {"items":[{…},{…}],"limit":20,"next_cursor":null}
```

Substring search on the message body:

```bash
curl -s "http://127.0.0.1:8081/messages?thread_id__eq=1&text__contains=hi"
# → {"items":[{"id":1,…}],"limit":20,"next_cursor":null}
```

Read, update, delete:

```bash
curl -s http://127.0.0.1:8081/threads/1
curl -s -X PATCH http://127.0.0.1:8081/messages/1 \
  -H "Content-Type: application/json" -d '{"text":"edited"}'
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE http://127.0.0.1:8081/messages/1
# → 204
```

Batch read:

```bash
curl -s "http://127.0.0.1:8081/messages/batch-read?id=1&id=2"
# → [{…},{…}]
```

## Auto-generated REST surface

Each resource gets the standard actions:

| Method   | Path                    | Action      |
| -------- | ----------------------- | ----------- |
| `POST`   | `/threads`              | create      |
| `GET`    | `/threads/{id}`         | read        |
| `PATCH`  | `/threads/{id}`         | update      |
| `DELETE` | `/threads/{id}`         | delete      |
| `GET`    | `/threads`              | search      |
| `GET`    | `/threads/count`        | count       |
| `GET`    | `/threads/batch-read`   | batch_read  |
| `POST`   | `/threads/batch-edit`   | batch_edit  |

…and the same actions for `/messages`. Search is `limit` + `cursor` + `sort` /
`desc` plus the `<field>__<op>` filter params; responses carry `ETag` /
`Last-Modified` / `Cache-Control` and a conditional `GET` can return `304`.

## Migrations

v2 has **no** `resourcey migrate` wrapper (that CLI reads the v1
`ResourceyBase` / `FrameworkConfig.manifest`, which do not exist in v2). Since
SQLAlchemy is the schema of record, the example drives **Alembic directly**
against `message_board.models.Base.metadata`.

The committed revision under `migrations/versions/` was generated with:

```bash
uv run --env-file .env alembic revision --autogenerate -m "message board init"
```

It is a reviewed draft (per Alembic's "autogenerated revisions are drafts"
caveat — renames look like drop+create). Apply / roll back from scratch:

```bash
rm -f message_board.db
uv run --env-file .env alembic upgrade head
uv run --env-file .env alembic downgrade base
```

