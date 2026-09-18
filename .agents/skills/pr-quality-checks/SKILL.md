---
name: pr-quality-checks
description: Required lint, type, coverage, and spec checks to run before opening or updating a pull request. Call this skill before creating or pushing a PR branch.
version: "1.0.0"
---

# Code quality gates (enforced in CI)

* `ruff check .` and `ruff format --check .` clean.
* `mypy --strict` clean (no `Any` without explicit `# type: ignore` + reason).
* Unit coverage >= 90%. New code without tests blocks merge.
* Quint specs compile and pass.

If a change can't meet a gate, flag it explicitly rather than silently bypassing it.

## Pre-PR verification — run locally before opening a PR

Do not push a branch and rely on CI to catch failures. Run these locally and
ensure they are green *before* opening (or updating) a pull request:

1. **lint-type-coverage** (mirrors CI):
   ```
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy
   make test
   ```
   `make test` runs the full suite with coverage and the 90% gate (xdist
   disabled for deterministic coverage attribution). For fast iteration
   *before* this gate, use `make test-fast` (coverage-free, parallelized).

2. **specs** (only when behavior changed, per the `quint-specs` skill):
   ```
   quint typecheck specs/*.qnt
   quint test specs/<spec>.qnt --main=<spec>
   ```

If any step fails, fix it before opening the PR — do not open the PR and
address CI failures reactively. If the environment cannot run a step, say so
explicitly in the PR description rather than skipping it silently.
