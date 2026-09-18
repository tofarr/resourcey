---
name: testing
description: Hermetic test conventions — in-memory SQLite, savepoint transaction isolation, coverage requirements, and how to run tests efficiently. Load before writing or modifying tests, or running the suite.
version: "1.0.0"
---

# Testing

* Unit tests use an **in-memory SQLite** database (aiosqlite) by default for
  speed and hermeticity. Integration tests against PostgreSQL may be added
  later under `tests/integration/` — keep them separate from the unit suite.
* Tests must be hermetic and parallelizable. Per-test isolation uses
  **savepoint transactions**: the `engine` fixture begins an outer transaction
  on a fresh connection, and all DB access goes through the same connection.
  `session.commit()` only releases a savepoint — data is visible within the
  test but rolled back after it.
* Test public behavior, not implementation details. Avoid mocks where a real
  dependency (DB, httpx transport) can be used in-process.
* Every public service function needs at least one happy-path and one
  error-path test.
* Coverage >= 90% is enforced via `make test` (the `--cov-fail-under=90`
  gate). Do not lower the gate; add tests instead.

## Running tests — output discipline

A full suite run can be verbose. For agent contexts, redirect output to a file
and read only the result:

```
uv run python -m pytest tests/unit/ > /tmp/test.log 2>&1
echo "exit $?"
tail -3 /tmp/test.log
```

### Iteration: scope to what changed

A full suite run is expensive. For fast feedback during iteration, scope to
the touched files:

* `make test-fast ARGS=tests/unit/test_resource_service.py` — parallelized,
  no coverage gate.
* `uv run pytest tests/unit -k create` — keyword-filter within a path.

Run the full suite (and the 90% coverage gate via `make test`) **once**, at
the end, before opening the PR — not on every edit.
