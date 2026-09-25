---
name: quint-specs
description: Rules for Quint formal specs — when to update specs, where they live, and how to verify them. Load when changing behavior that has a Quint spec.
version: "1.0.0"
---

# Formal specs (Quint)

* Every resource action and state machine has a `.qnt` spec in `specs/`.
* Invariants (permissions, ownership, uniqueness) are expressed and checked.
* When changing behavior, update the spec first, then implement, then run
  `quint test`.

## Verification commands

```
scripts/install_quint_evaluator.sh   # once; see below
quint typecheck specs/*.qnt
quint test specs/<spec>.qnt --main=<spec>
```

`make specs` runs the full set.

`quint test` / `quint run` need a `quint_evaluator` binary that the CLI
downloads on first use from the **unauthenticated** GitHub releases API. On
shared CI runner IPs that call is routinely rate-limited, so the job fails with
`Failed to fetch from GitHub: rate limit exceeded` before any spec runs.
`scripts/install_quint_evaluator.sh` fetches the same asset from GitHub's direct
release-download URL (no API, no limit) and pre-seeds the CLI's cache, after
which the CLI skips its own download. It is idempotent, and CI runs it before
the spec steps.

Run these whenever behavior has changed. They are also part of the pre-PR
verification (see the `pr-quality-checks` skill).
