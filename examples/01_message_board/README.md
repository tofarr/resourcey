# Example 1 — The Message Board

The first end-to-end example application built on `resourcey`. Two resources —
`Thread` and `Message` — with a one-to-many relation, wired into a runnable
FastAPI app with auto-generated REST endpoints, an Alembic migration, and a
SQLite database by default.

This is the canonical "does this framework actually work for a real app?"
smoke test, and the reference a new user reads first.

## Resources

| Resource  | Fields                                                                 |
| --------- | --------------------------------------------------------------------- |
| `Thread`  | `id: int`, `title: str`, `description: str`, `created_at`, `updated_at` |
| `Message` | `id: int`, `thread_id: int` (FK → `threads.id`), `text: str`, `created_at`, `updated_at` |

`Message.thread_id` is a real foreign-key column to `threads.id`, expressed
with the explicit `ResourceyField(column=Column(..., ForeignKey("threads.id")))`
escape hatch (issue #24 will deliver a higher-level relation API; this example
is the integration test that API must keep satisfying).

`Message.search` supports `thread_id__eq` filtering via a declared
`MessageSearchFilter`, so a client can list a thread's messages.

## Layout

```
01_message_board/
├── README.md            # this file
├── pyproject.toml       # standalone — resourcey from git (parent checkout in-repo)
├── .env                 # SQLite config + manifest path (committed, ready to run)
├── .gitignore
├── message_board/       # the importable app package
│   ├── app.py           # manifest + app entry point (uvicorn target)
│   ├── thread.py        # Thread resource declaration
│   └── message.py       # Message resource declaration + MessageSearchFilter
├── migrations/
│   ├── env.py           # Alembic environment (generated)
│   └── versions/        # generated + reviewed revisions
└── tests/
    └── test_smoke.py    # HTTP smoke test (in-memory SQLite, httpx ASGI)
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
uv run resourcey migrate upgrade

# 3. Start the server
uv run uvicorn message_board.app:app --reload --port 8081
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

Run the smoke tests (in-memory SQLite, no server needed):

```bash
uv sync --extra test
uv run pytest
```

### Configuration

The example uses the stock `FrameworkConfig` with a committed `.env` that
points it at a local SQLite file (`message_board.db`). The connection is a
single `RESOURCEY_DATABASE_URL`; the URL scheme selects the driver. Edit `.env`
to change the database:

| Env var                  | Default                                | Purpose                                    |
| ------------------------ | -------------------------------------- | ------------------------------------------ |
| `RESOURCEY_DATABASE_URL` | `sqlite+aiosqlite:///message_board.db` | Complete SQLAlchemy URL (driver in scheme). |
| `RESOURCEY_MANIFEST`     | `message_board.app:manifest`           | Dotted import path to the resource manifest. |

To target Postgres instead, set a Postgres URL — the password can be embedded
in the URL or supplied separately via `RESOURCEY_DATABASE_PASSWORD` (kept in its
own variable so it can be encrypted at rest, e.g. with SOPS):

```bash
RESOURCEY_DATABASE_URL=postgresql+asyncpg://resourcey@localhost:5432/resourcey
RESOURCEY_DATABASE_PASSWORD=secret
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

…and the same actions for `/messages`.

## Migrations

The committed revision under `migrations/versions/` was generated with:

```bash
resourcey migrate autogenerate -m "message board init"
```

It is a reviewed draft (per the framework's "generated revisions are drafts"
caveat). Re-apply from scratch:

```bash
rm -f message_board.db
resourcey migrate upgrade
```
