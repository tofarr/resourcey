---
name: testing
description: Hermetic test conventions — in-memory SQLite, savepoint isolation, fixtures, coverage gate, and how to run the suite efficiently. Load before writing or modifying tests, or running the suite.
version: "2.0.0"
---

# Testing

## Conventions

* Unit tests use **in-memory SQLite** (aiosqlite) — fast and hermetic.
  Integration tests against PostgreSQL, if added, go under `tests/integration/`
  and stay separate from the unit suite.
* Tests must be hermetic and parallelizable. Per-test isolation uses
  **savepoint transactions**: the `engine` fixture begins an outer transaction
  on a fresh connection and all DB access goes through that connection, so
  `session.commit()` only releases a savepoint — data is visible within the test
  and rolled back after it.
* Test public behavior, not implementation details. Do not use mocks unless
  strictly necessary: a real in-process dependency (SQLite, `mongomock`, an
  `httpx` `ASGITransport`) is almost always available and gives a real code
  path.
* Every public service function needs at least one happy-path and one
  error-path test.
* Coverage ≥ 90% is enforced (`--cov-fail-under=90`). Do not lower the gate;
  add tests instead.

## Layout

`tests/unit/` mirrors the framework: one file per module or feature area
(`test_isolation.py` pins the layer ranks and the `core` file set; the rest
cover services, backends, auth, cache, filestore, jobs, tasks, triggers,
realtime, config). Example apps have their own `tests/` and run under their own
`.venv`.

## Running

```bash
make test-fast                      # parallel, no coverage — for iteration
make test                           # full suite + coverage gate (-n 0)
uv run pytest tests/unit -k create  # keyword filter
uv run pytest tests/unit/test_sql_service.py -x -q
```

`make test` disables xdist (`-n 0`) so coverage attribution is deterministic —
that is why it is the gate and `test-fast` is not.

In an agent context a full run is verbose; redirect and read the tail:

```bash
uv run pytest tests/unit > /tmp/test.log 2>&1; echo "exit $?"; tail -5 /tmp/test.log
```

Run the full gate **once**, at the end, before opening the PR — not on every
edit.

## Testing a resource over HTTP

The pattern most tests use: build a `Manifest`, `create_app` it, and drive it
with an `httpx.AsyncClient` over `ASGITransport`:

```python
async with (
    manifest,
    httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
):
    ...
```

Entering the manifest is what starts managers (engines, stores, channels), so
tests exercise the real lifecycle rather than a stubbed one.

## Testing cache behaviour

Assert on the emitted headers (`ETag`, `Cache-Control`, `Expires`) and on a
conditional request returning `304`. For a caller-scoped resource, assert
`Cache-Control` starts with `private` — that is the authorization guarantee, not
a nicety.
