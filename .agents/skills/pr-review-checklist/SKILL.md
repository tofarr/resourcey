---
name: pr-review-checklist
description: Checklist for reviewing pull requests in this repo — layering, tests, specs, permissions, escape hatches, and doc accuracy. Load before reviewing a PR.
version: "2.0.0"
---

# PR review checklist

## Correctness

* **Layering** — `util < core < the rest`. No module imports a strictly-higher
  project layer at runtime (`tests/unit/test_isolation.py` enforces it).
  Routers do not query the DB; services hold the logic.
* **Ownership** — whoever opens storage commits and closes it; a service that
  finds storage in `ctx` neither commits nor closes it. A wrapper around an
  already-entered inner must not double-enter or double-close.
* **Existence leakage** — an out-of-scope read / update / delete is a `404`, and
  a condition miss is indistinguishable from an absent row.
* **Atomicity** — an authorization scope or write condition is folded into the
  same write, never a read-then-write.
* **Fail-closed** — an unknown role, an empty policy set, an unmapped external
  identity, an inactive principal, an unconvertible filter, and an
  unknown/unparseable config all deny or raise rather than defaulting open.
* **Secrets** — redacted by default, revealed only through
  `serialization_context()`, encrypted at rest where the layer supports it. A
  projected-away field is also absent from the filter and sort surface.
* **Background work** — a fire-and-forget task is owned by something app-scoped
  and settled on shutdown; it is not cancelled by a per-request exit.

## Tests

* Happy path and error path per public behaviour; real in-process dependencies
  rather than mocks.
* Cache behaviour asserted through the emitted headers (and `private` where the
  response is caller-scoped), not just the body.
* The coverage gate passes and is not lowered.

## Specs and docs

* A behaviour change with a spec (`specs/*.qnt`) updates the spec, and
  `make specs` passes.
* A new public API or a changed config/env surface updates the relevant skill
  and, if it is user-facing, the README or the example README.
* The skill that owns the area was actually consulted — a stale claim in
  `AGENTS.md` or a skill is a review finding, not background noise.

## Style

* `make lint` and `make type` clean; methods short and single-purpose; no
  `__all__`; comments only for the non-obvious (an invariant, a workaround, a
  deliberate trade-off) — never a restatement of the code or a narration of the
  diff.
* No `openhands` import in `resourcey.util.*`.
* A schema change ships a reviewed Alembic migration (autogeneration cannot see
  a rename — it looks like a drop plus a create).

## Escape hatches

A new abstraction does not lock out direct access to FastAPI / SQLAlchemy /
Alembic, and a developer's own route at the same path still wins.
