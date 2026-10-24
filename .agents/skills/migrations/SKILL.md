---
name: migrations
description: Alembic autogeneration from the SQLAlchemy models, and how to review and apply a revision. Load when working on migrations or schema changes.
version: "2.0.0"
---

# Migrations

SQLAlchemy is the schema of record. A resource is declared over an ORM model,
and migrations are plain Alembic revisions diffed against that model's
`Base.metadata`. There is no `resourcey migrate` wrapper.

## Autogeneration

* `alembic revision --autogenerate -m "add foo column"` diffs the ORM metadata
  against the database and writes a revision. `env.py` points at the app's
  `Base.metadata` (see the examples' `migrations/` directories).
* `alembic check` is the dry run: it exits non-zero when the models and the
  migrations disagree, which is what CI-adjacent review should assert.
* Generated migrations are plain Alembic revisions — inspect and edit them
  like any hand-written migration. Never blindly apply one: autogeneration
  cannot detect table or column *renames*, and a rename looks like a drop
  followed by a create (which destroys the data). Rename by hand.
* A `ModelType` column is stored as `JSON`, so adopting it on an existing
  `JSON` column produces no DDL change and `alembic check` agrees.

## Apply / roll back

```bash
alembic upgrade head       # apply
alembic downgrade -1       # one step back
```

The examples run Alembic with the environment loaded (the framework does no
`.env` loading): `uv run --env-file .env alembic upgrade head`.

Several subsystems own tables an app does not model as a resource, each on its
own `DeclarativeBase`: `ApiKeyBase`, `RbacBase`, `OAuthBase` /
`ExternalIdentityBase`, `OAuthTokenBase`, `FileBlobBase`, `JobsBase`. Import the
ones you use in `env.py` so their tables land in `target_metadata`, or — in dev
— call the shipped helper, which runs `create_all` for you:
`create_blob_tables(session_factory)` / `create_oauth_token_tables(session_factory)`.
In production, generate a real revision against those bases; do not rely on
`create_all`.
