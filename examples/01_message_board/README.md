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
├── pyproject.toml       # standalone project — depends on resourcey from git
├── .env                 # SQLite config + resources (committed, ready to run)
├── .gitignore
├── message_board/       # the importable app package
│   ├── app.py           # create_app() entry point (uvicorn factory)
│   ├── resources.py     # registers Thread + Message with the framework
│   ├── thread.py        # Thread resource declaration
│   └── message.py       # Message resource declaration + MessageSearchFilter
└── migrations/
    ├── env.py           # Alembic environment (generated)
    └── versions/        # generated + reviewed revisions
```

## Run it

This example is a **standalone project**. From within the `01_message_board`
directory:

```bash
# 1. Install dependencies (pulls resourcey from its main branch on GitHub)
uv sync

# 2. Apply the database migration (creates message_board.db)
resourcey migrate upgrade

# 3. Start the server
resourcey run
# → Uvicorn running on http://0.0.0.0:8081
```

Or with uvicorn directly (reload mode):

```bash
uvicorn message_board.app:create_app --factory --reload
```

Interactive API docs are at `http://127.0.0.1:8081/docs`.

### Configuration

The example uses the stock `FrameworkConfig` with a committed `.env` that
points it at a local SQLite file (`message_board.db`). SQLite URLs don't fit
the structured `protocol://user:pass@host:port/db` pattern, so the
`RESOURCEY_DATABASE_FULL_DB_URL` escape hatch is used to pass the complete
URL verbatim. Edit `.env` to change the database or server bind:

| Env var                            | Default                           | Purpose                          |
| ---------------------------------- | --------------------------------- | -------------------------------- |
| `RESOURCEY_DATABASE_FULL_DB_URL`   | `sqlite+aiosqlite:///message_board.db` | Complete SQLAlchemy URL (SQLite escape hatch). |
| `RESOURCEY_HOST` / `RESOURCEY_PORT` | `0.0.0.0` / `8081`               | App server bind.                |
| `RESOURCEY_RESOURCES`              | _(set in .env)_                   | Dotted import paths to resources. |

To target Postgres instead, unset `RESOURCEY_DATABASE_FULL_DB_URL` and set the
structured `RESOURCEY_DATABASE_*` vars (`HOST`, `PORT`, `DB_NAME`, etc.).

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
# → {"items":[{…},{…}],"total":2,"limit":20,"offset":0}
```

Substring search on the message body:

```bash
curl -s "http://127.0.0.1:8081/messages?thread_id__eq=1&text__contains=hi"
# → {"items":[{"id":1,…}],"total":1,"limit":20,"offset":0}
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

Each resource gets the seven standard actions:

| Method   | Path                    | Action      |
| -------- | ----------------------- | ----------- |
| `POST`   | `/threads`              | create      |
| `GET`    | `/threads/{id}`         | read        |
| `PATCH`  | `/threads/{id}`         | update      |
| `DELETE` | `/threads/{id}`         | delete      |
| `GET`    | `/threads`              | search      |
| `GET`    | `/threads/batch-read`   | batch_read  |
| `POST`   | `/threads/batch-edit`   | batch_edit  |

…and the same seven for `/messages`.

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
