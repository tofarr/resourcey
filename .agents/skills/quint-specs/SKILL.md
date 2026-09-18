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
quint typecheck specs/*.qnt
quint test specs/<spec>.qnt --main=<spec>
```

Run these whenever behavior has changed. They are also part of the pre-PR
verification (see the `pr-quality-checks` skill).
