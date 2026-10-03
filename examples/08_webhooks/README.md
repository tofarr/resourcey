# Example 08 — Triggers / Webhooks

A demonstration of `resourcey.triggers` (issue [#155](https://github.com/tofarr/resourcey/issues/155) /
[#156](https://github.com/tofarr/resourcey/pull/156)): attaching an **edit-event
trigger** — the framework's generic seam for webhooks — to a resource, built to
close out the original ask in issue
[#18](https://github.com/tofarr/resourcey/issues/18).

It is the same `Thread` / `Message` board as example 01. The point here is not
a new domain — it's the trigger wiring. The configured trigger is
`resourcey.triggers.webhook_trigger.WebhookTrigger` — the framework's own,
generic HTTP-delivering trigger (`url`, `headers` with secret values, and a
configurable `retry` strategy; no HTTP-sending code lives in this example). It
really does `POST` over HTTP: a production deployment would point it at a
separate, independently owned service, but to stay a **working**, runnable
example with nothing external to stand up, this app also mounts its own
receiving endpoint (`webhooks_example/webhook_receiver.py`) on the *same*
FastAPI app and points both configured triggers at it. That is enough to prove
the mechanism end-to-end — a genuine delivery (fires once per write, only on
success, isolated per trigger, in the background by default) over real HTTP —
watch the `uvicorn` console while you make requests below: the receiver logs
exactly what arrived, in a banner that is unmistakable among the usual
`uvicorn` access-log noise.

## Two ways to attach a trigger

This example wires **both** rungs `resourcey.triggers` offers, on two
different resources, so it is also the reference for choosing between them:

| Resource | Wiring style | Where |
| -------- | ------------ | ----- |
| `threads` | **Direct, no config.** The resource declaration itself wraps the plain `SqlResource` in `TriggeredResource(..., on_edit=[...])`. | `webhooks_example/app.py` |
| `messages` | **Opt-in, config-driven.** The resource declaration (`webhooks_example/message.py`) is untouched; the trigger comes from `TriggerConfig` (parsed from `APP_TRIGGERS_*`) via `TriggeredDependencyBuilder.from_config(...)`, passed as `create_app`'s `dependency_builder=`. | `.env` + `webhooks_example/app.py` |

The *same* builder is passed for every resource. For `threads` — already a
`TriggeredResource` — the builder detects that and passes the request straight
through unchanged, so there is no double-wrapping; `messages` gets wrapped
only because `.env` configures an entry for it. Delete the `APP_TRIGGERS_0_*`
lines and `messages` goes back to a plain, trigger-free resource with **no
code change** — that's the whole point of the config-driven rung.

## Layout

```
08_webhooks/
├── README.md                  # this file
├── pyproject.toml             # standalone — resourcey from git (parent checkout in-repo)
├── alembic.ini                # Alembic config (URL comes from APP_SQL_CONNECTIONS_0_URL)
├── .env                       # APP_* config (committed), including APP_TRIGGERS_*
├── .gitignore
├── webhooks_example/           # the importable app package
│   ├── app.py                 # manager + manifest + both trigger-wiring styles (uvicorn target)
│   ├── models.py              # Thread & Message ORM models + Base (schema of record)
│   ├── message.py             # MessageResource (plain -- its trigger comes from config)
│   └── webhook_receiver.py    # the example's own receiving endpoint (POST /_webhooks/{name})
├── migrations/
│   ├── env.py                 # Alembic env, diffs against the ORM metadata
│   └── versions/               # generated + reviewed revisions
└── tests/
    ├── test_smoke.py          # trigger firing over in-memory SQLite (httpx ASGI, caplog)
    └── test_e2e.py            # full REST suite against the committed migration
```

## Run it

A **standalone** `uv` project. From within `08_webhooks`:

```bash
uv sync
uv run --env-file .env alembic upgrade head          # creates webhooks_example.db
uv run uvicorn webhooks_example.app:app --env-file .env --reload --port 8088
# → Uvicorn running on http://127.0.0.1:8088
```

Interactive docs are at `http://127.0.0.1:8088/docs`.

### Running inside the resourcey repository

`pyproject.toml` depends on `resourcey` from GitHub `main` but adds a
`[tool.uv.sources]` override to the parent checkout (`../..`), so `uv sync`
builds the working tree (your branch or PR). A copy placed outside the repo can
no longer resolve that path — use `uv sync --no-sources` to fall back to the
git dependency.

### Testing

```bash
uv sync --extra test
uv run pytest
```

## Watch it fire

Create a thread (the **direct** wiring — `audit-log`):

```bash
curl -s -X POST http://127.0.0.1:8088/threads \
  -H "Content-Type: application/json" \
  -d '{"title":"Hello","description":"first thread"}'
```

The server console logs a genuine, received HTTP request — this is
`webhook_receiver.py`'s route handler, reached by a real `POST` the
`WebhookTrigger` made over loopback:

```
INFO:webhooks_example.webhook_receiver:
======================================================================
WEBHOOK RECEIVED -- subscriber='audit-log'
======================================================================
Headers: {'x-webhook-secret': 'audit-log-dev-secret', 'user-agent': 'python-httpx/0.28.1', ...}
[
  {
    "kind": "create",
    "item": {
      "title": "Hello",
      "description": "first thread"
    },
    "result": {
      "id": 1,
      "title": "Hello",
      "description": "first thread",
      "created_at": "...",
      "updated_at": "..."
    }
  }
]
======================================================================
```

Create a message under it (the **config-driven** wiring — `slack-notify`, from
`.env`):

```bash
curl -s -X POST http://127.0.0.1:8088/messages \
  -H "Content-Type: application/json" \
  -d '{"thread_id":1,"text":"hi there"}'
```

```
INFO:webhooks_example.webhook_receiver:
======================================================================
WEBHOOK RECEIVED -- subscriber='slack-notify'
======================================================================
Headers: {'x-webhook-secret': 'slack-notify-dev-secret', ...}
[
  {
    "kind": "create",
    "item": {"thread_id": 1, "text": "hi there"},
    "result": {"id": 1, "thread_id": 1, "text": "hi there", ...}
  }
]
======================================================================
```

Update and delete both fire too:

```bash
curl -s -X PATCH http://127.0.0.1:8088/messages/1 \
  -H "Content-Type: application/json" -d '{"text":"edited"}'
# → logs: "kind": "update", "item": {"text": "edited"}, "result": {..., "text": "edited", ...}

curl -s -o /dev/null -w "%{http_code}\n" -X DELETE http://127.0.0.1:8088/messages/1
# → 204, logs: [{"kind": "delete", "id": 1}]
```

A **batch edit** delivers **one** webhook request, not one per item — the
whole batch is one edit operation:

```bash
curl -s -X POST http://127.0.0.1:8088/messages/batch-edit \
  -H "Content-Type: application/json" \
  -d '[
        {"kind":"Create","item":{"thread_id":1,"text":"first"}},
        {"kind":"Create","item":{"thread_id":1,"text":"second"}},
        {"kind":"Create","item":{"thread_id":1,"text":"third"}}
      ]'
# → exactly one WEBHOOK RECEIVED banner, naming all three records:
#   [{"kind": "create", ..., "text": "first"}, {"kind": "create", ..., "text": "second"}, {"kind": "create", ..., "text": "third"}]
```

A **failed** write fires nothing — try updating a message that doesn't exist:

```bash
curl -s -X PATCH http://127.0.0.1:8088/messages/9999 \
  -H "Content-Type: application/json" -d '{"text":"nope"}'
# → 404 {"error":{"code":"not_found",...}}, no log line
```

## Configuration

The framework reads the process-wide `APP` prefix and does **no** `.env`
loading, so every command passes `--env-file .env`.

| Env var | Default | Purpose |
| ------- | ------- | ------- |
| `APP_SQL_CONNECTIONS_0_NAME` | `main` | Connection name (first is the default). |
| `APP_SQL_CONNECTIONS_0_URL` | `sqlite+aiosqlite:///webhooks_example.db` | Complete SQLAlchemy URL (driver in scheme). |
| `APP_ENCRYPTION_KEY_ID` / `_VALUE` | `dev` / *(dev default + warning when unset)* | Cursor-encryption key. |
| `APP_TRIGGERS_0_RESOURCE_PATH` | `messages` | Which resource's service gets wrapped. |
| `APP_TRIGGERS_0_TRIGGER_KIND` | `resourcey.triggers.webhook_trigger.WebhookTrigger` | Dotted path of the `Trigger` subclass. |
| `APP_TRIGGERS_0_TRIGGER_URL` | *(this app's own receiver)* | The URL the trigger `POST`s to — **required**, no default. |
| `APP_TRIGGERS_0_TRIGGER_HEADERS_0_NAME` / `_VALUE` | `X-Webhook-Secret` / *(a dev secret)* | One header sent with every delivery; `_VALUE` is a `SecretStr`. |
| `APP_TRIGGERS_0_TRIGGER_RETRY_KIND` | `FixedDelayRetry` | Which `RetryStrategy` to use (`NoRetry` / `FixedDelayRetry` / `ExponentialBackoffRetry`). |
| `APP_TRIGGERS_0_TRIGGER_RETRY_MAX_RETRIES` / `_DELAY_SECONDS` | `2` / `1` | `FixedDelayRetry`'s own fields. |
| `WEBHOOKS_EXAMPLE_BASE_URL` | `http://127.0.0.1:8088` | Base URL the directly-wired `threads` trigger (`app.py`) points at; a plain process env var, not an `APP_*` config field. |

Add a second entry (`APP_TRIGGERS_1_*`) to attach another trigger, or point
`messages` at more than one subscriber by repeating `APP_TRIGGERS_<n>_*` with
the same `RESOURCE_PATH`. Delete the `APP_TRIGGERS_0_*` lines entirely and
`messages` goes back to a plain, trigger-free resource with no code change.

## `WebhookTrigger` — the framework's generic sender

`resourcey.triggers.webhook_trigger.WebhookTrigger` is the framework's own,
concrete `Trigger`: it carries exactly three delivery-shaping fields —

* `url` — **required, no default**. A webhook with no destination would
  silently deliver nowhere.
* `headers` — a list of `{name, value}` pairs sent with every request; each
  `value` is a `pydantic.SecretStr`, so it is redacted from default
  serialization / `repr` / logging (the convention `DbConfig.password` /
  `ApiKeyConfig.key` already use elsewhere in the framework).
* `retry` — a polymorphic `RetryStrategy` (`NoRetry` by default — a single
  attempt). `FixedDelayRetry` and `ExponentialBackoffRetry` ship too; a
  deployment opts into retries explicitly.

It imports `httpx` **lazily**, behind the `resourcey[webhooks]` extra (this
example's `pyproject.toml` already depends on it) — the same lazy-import shape
`S3FileStore` uses for `boto3`, so no HTTP client becomes a mandatory framework
dependency merely because an app attaches one trigger. Error handling beyond
`retry` — signing, idempotency, ordering — is still the deployment's concern:
`resourcey.triggers` fires best-effort, at-most-once (see the `trigger.py` /
`triggered_service.py` module docstrings). No custom `Trigger` subclass is
needed for this example — `webhooks_example/webhook_receiver.py` is purely the
*receiving* side, proving delivery really happened.

## Auto-generated REST surface

Each resource gets the standard actions (`threads` and `messages` alike):

| Method | Path | Action |
| ------ | ---- | ------ |
| `POST` | `/threads` | create |
| `GET` | `/threads/{id}` | read |
| `PATCH` | `/threads/{id}` | update |
| `DELETE` | `/threads/{id}` | delete |
| `GET` | `/threads` | search |
| `GET` | `/threads/count` | count |
| `GET` | `/threads/batch-read` | batch_read |
| `POST` | `/threads/batch-edit` | batch_edit |

Triggers are purely a side effect of a successful write on these same routes —
no *resource* routes are added. One extra, non-resource endpoint exists purely
to receive the deliveries this example's own triggers make:

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/_webhooks/{name}` | The example's own webhook receiver (`webhook_receiver.py`) — logs what arrived. Not one of the eight standard actions; a real deployment's receiver would live on a separate service entirely. |
