---
name: pr-review-checklist
description: Checklist for agents reviewing pull requests. Load before reviewing a PR to ensure coverage, layering, specs, and escape hatches are respected.
version: "1.0.0"
---

# PR review checklist

Before approving a PR, verify:

* **Layering** — routers → services → repositories → models. No skipped
  layers; no DB access in a route handler.
* **Tests** — at least one happy-path and one error-path test per public
  service function. Coverage gate (90%) passes.
* **Specs** — behavior changes are reflected in `specs/*.qnt` and pass
  `quint typecheck` / `quint test`.
* **Permissions** — new or changed resource actions are permission-checked.
* **Escape hatches** — new abstractions do not lock out direct access to
  FastAPI / SQLAlchemy / Alembic.
* **Vendored utilities** — no `openhands` import introduced in
  `resourcey.util.*`.
* **Style** — `ruff` and `mypy --strict` clean; methods short and
  single-purpose; no `__all__`; comments only for the non-obvious.
* **Migrations** — schema changes produce a reviewed Alembic migration (or
  are gated behind transient dev mode with a flag).
