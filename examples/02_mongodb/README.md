# Example 02 — MongoDB message board

A message-board app built on **resourcey** with a **MongoDB** backend,
re-implementing the same Thread + Message resources as
[`01_message_board`](../01_message_board/) but using MongoDB instead of SQL.

## Key differences from the SQL example

| Aspect | SQL example (01) | MongoDB example (02) |
|--------|------------------|----------------------|
| Backend | SQLAlchemy + asyncpg/aiosqlite | motor (async MongoDB) |
| Id type | Auto-increment `int` | Client-generated `UUID` |
| Migrations | Alembic autogeneration | Manual `migrate_document` hook (on read) |
| Relations | Real FK columns | Application-level UUID references |
| Port | 8081 | 8082 |

## Embedded mode (no MongoDB server required)

By default, the app uses an **embedded MongoDB** via
[`mongomock`](https://github.com/mongomock/mongomock) — an in-process
implementation of the MongoDB API. This means you can run the example with
zero external dependencies:

```bash
uv sync
uv run resourcey migrate upgrade
uv run uvicorn message_board.app:app --reload --port 8082
```

The app listens on **port 8082**.

To use a real MongoDB server instead, set `RESOURCEY_MONGO_URL`:

```bash
RESOURCEY_MONGO_URL=mongodb://localhost:27017 uv run uvicorn message_board.app:app --reload --port 8082
```

## Resources

### Thread

```
GET    /threads          — list threads (cursor pagination, optional sort)
POST   /threads          — create a thread
GET    /threads/{id}     — fetch a thread
PATCH  /threads/{id}     — update a thread
DELETE /threads/{id}     — delete a thread
```

### Message

```
GET    /messages?thread_id__eq=<uuid>  — list a thread's messages
POST   /messages                       — create a message in a thread
GET    /messages/{id}                  — fetch a message
PATCH  /messages/{id}                  — update a message
DELETE /messages/{id}                  — delete a message
```

## Manual migration on read

MongoDB resources do **not** use Alembic migrations. Instead, a resource can
override `migrate_document()` — a hook invoked on every read — to lazily
upgrade a document to the current shape. The versioning scheme is
application-defined (e.g. a `schema_version` field on each document). See
[`message_board/thread.py`](message_board/thread.py) for the resource
declaration; the default `migrate_document` is a no-op.

## Tests

```bash
uv sync --extra test
uv run pytest
```
