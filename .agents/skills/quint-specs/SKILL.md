---
name: quint-specs
description: Rules for Quint formal specs — when to update specs, where they live, and how to verify them. Load when changing behavior that has a Quint spec.
version: "2.0.0"
---

# Formal specs (Quint)

* Every resource action and state machine has a `.qnt` spec in `specs/`.
* Invariants (permissions, ownership, uniqueness) are expressed and checked.
* When changing behavior, update the spec first, then implement, then run it.

## Verification

```bash
make specs
```

That one target is the **single source of truth** for the spec set, the
per-spec `--match` run names, and the evaluator pre-seed. CI's `specs` job runs
`make specs` — it deliberately keeps no copy of the command list, because the
two copies had already drifted (CI still named pre-rewrite filestore runs and
never ran `cache_freshness`). When you add, rename, or retire a `run`, edit the
`Makefile` only.

For one spec while iterating:

```bash
quint typecheck specs/<spec>.qnt
quint test specs/<spec>.qnt --main=<spec>
```

`quint test` / `quint run` need a `quint_evaluator` binary that the CLI
downloads on first use from the **unauthenticated** GitHub releases API. On
shared CI runner IPs that call is routinely rate-limited, so the job fails with
`Failed to fetch from GitHub: rate limit exceeded` before any spec runs.
`scripts/install_quint_evaluator.sh` fetches the same asset from GitHub's direct
release-download URL (no API, no limit) and pre-seeds the CLI's cache, after
which the CLI skips its own download. It is idempotent, and `make specs` runs it
first.

## Which spec covers what

| Spec | Pins |
|---|---|
| `resource_actions.qnt` | the eight actions, search / count predicates, read-only subsets |
| `conditional_write.qnt` | condition semantics, policy scope folded into the write |
| `dto_defaults.qnt` | the `DTO` conventions (uuid4 id, intent wins, timestamps) |
| `exposure.qnt` | `get_exposed_resource`, view narrowing, batch-union narrowing |
| `secret_serialization.qnt` | redact / expose / encrypt precedence |
| `filtering.qnt` | normalisation, NULL-safe negation, pushdown soundness, `in` |
| `sorting.qnt` | total order, keyset agreement, descending mirror, cursor binding |
| `cache_defaults.qnt` / `cache_freshness.qnt` | strategy selection and freshness |
| `auth.qnt` / `api_key.qnt` / `principal_store.qnt` | authentication pipeline, key secrecy, stored-principal authority |
| `permissions.qnt` / `roles.qnt` / `rbac.qnt` | policy reductions, role mapping, store resolution |
| `oauth.qnt` / `me.qnt` | OIDC verification, the self-only `me` read |
| `filestore.qnt` | medium-native create / delete, capability binding |
| `background_tasks.qnt` | cron matching, tick selection |
| `triggers.qnt` | fire-once-per-operation, success-only, isolation, background |
| `realtime.qnt` | subscription gate, handshake posture, per-subscriber filtering |
| `jobs.qnt` | single-winner claim, terminal stickiness, attempt cap, recovery |

Run `make specs` whenever behavior has changed. It is also part of the pre-PR
verification (see the `pr-quality-checks` skill).
