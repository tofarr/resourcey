---
name: pr-quality-checks
description: The lint, type, coverage, and spec gates to run before opening or updating a pull request. Call this before creating or pushing a PR branch.
version: "2.0.0"
---

# Quality gates

## The gates (mirrored by `.github/workflows/ci.yml`)

```bash
make lint      # ruff check . && ruff format --check .
make type      # mypy src/resourcey
make test      # pytest tests/unit --cov=resourcey --cov-fail-under=90 -n 0
make specs     # quint typecheck + quint test over the spec set
```

`make lint` and `make type` do not take `uv run` — the targets already wrap it.
`mypy` is configured strict for `src/resourcey`; examples are excluded from the
root config and linted as standalone projects.

Run all four **locally before pushing**. Do not open a PR and fix CI reactively.
If an environment genuinely cannot run a step, say so in the PR description
rather than skipping it silently.

## Iteration vs. the gate

* `make test-fast` — parallel (`-n auto`), **no** coverage gate. Use it while
  iterating.
* `make test` — `-n 0` so coverage attribution is deterministic. This is the
  gate; run it once at the end.

## Specs

Run `make specs` when behaviour with a spec changed (see the `quint-specs`
skill). The target pre-seeds the Quint evaluator via
`scripts/install_quint_evaluator.sh`, which fetches from GitHub's direct
release-download URL instead of the rate-limited API.

## Examples

The ten example apps are separate `uv` projects. A change to a shared framework
module can break one without breaking the root suite. When your change touches
anything an example uses (resources, routes, config, auth, filestore, triggers,
realtime, jobs), run that example's suite:

```bash
cd examples/06_filestore && uv run pytest
```

CI runs each example's `uv run pytest` as its own job, so a broken example is a
red PR.

## Definition of done

* All four gates green.
* New public behaviour has tests (happy path + error path).
* A behaviour change with a spec has the spec updated.
* No `openhands` import added to `util`.
* No `__all__` added.
