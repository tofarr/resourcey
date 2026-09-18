# resourcey

A resource-oriented Python web framework built on FastAPI, SQLAlchemy, and
Alembic — **progressive enhancement with escape hatches to the underlying
stack.**

## Why?

Most web frameworks either hide the database entirely or make you wire every
layer by hand. `resourcey` takes a middle path: you declare a **resource**
with enough metadata to describe how it should be stored, validated, and
exposed, and the framework generates a service for you. When the abstraction
gets in the way, you drop back a rung — to FastAPI, SQLAlchemy, or Alembic —
and nothing breaks.

The guiding principles:

* **Resource-oriented.** A resource is the central unit. Define it once with
  rich metadata; derive Pydantic schemas, SQLAlchemy models, a REST service,
  migrations, and permission checks from that single declaration.
* **Progressive enhancement.** Each layer is opt-in. Use the generated
  service, or write your own route that calls the repository directly, or
  query SQLAlchemy yourself. The layers compose; they don't trap you.
* **Escape hatches.** FastAPI, SQLAlchemy, and Alembic are first-class
  citizens, not implementation details. You can always reach them.

## What a resource gives you

Declaring a resource produces:

* **Pydantic models** for request/response validation, derived from the
  resource's field metadata.
* **SQLAlchemy models** for persistence, derived from the same metadata.
* **A service** exposing the standard actions over REST:
  `create`, `read`, `update`, `delete`, `search`, `batch_read`, `batch_edit`.
* **Migrations** — Alembic autogeneration from the current models, plus a
  *transient dev mode* that builds tables on the fly so you can iterate
  without writing migrations.
* **Permissions** — the resource declares which actions a role may perform,
  and the framework computes the effective permission set per user.

## Stack

| Concern | Tool |
|---|---|
| HTTP | FastAPI |
| Validation | Pydantic v2 |
| ORM | SQLAlchemy 2 (async) |
| Migrations | Alembic |
| Package management | uv |
| Formal specs | Quint |
| Tests | pytest (≥90% coverage enforced) |

## Permissions (users, groups, roles)

`resourcey` models users, groups, and roles so that a per-user permission set
can be computed for every resource action. The permission engine is reusable
across applications and was abstracted out of
[`ohev2`](https://github.com/tofarr/ohev2), where it solved the same problems
in a domain-specific setting.

## Configuration

A typed environment-variable parser is bundled in `resourcey.util.env_parser`
(vendored from the
[OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk/blob/28e8ed273617992e9556410804f54937cc059878/openhands-agent-server/openhands/agent_server/env_parser.py),
written by the same author). It supports complex nested types and polymorphism
that `pydantic-settings` cannot express. There is **no runtime dependency** on
the SDK.

A `DiscriminatedUnionMixin` is also bundled in `resourcey.util.models` for
polymorphic models keyed by a `kind` discriminator — likewise vendored from
the SDK with no dependency.

## Status

Early-stage. The roadmap is tracked in
[GitHub issues](https://github.com/tofarr/resourcey/issues). See `AGENTS.md`
for contributor rules and `specs/` for the formal Quint specifications.

## License

MIT
