# Example 08 — Triggers / Webhooks

A demonstration of `resourcey.triggers` (issue [#155](https://github.com/tofarr/resourcey/issues/155) /
[#156](https://github.com/tofarr/resourcey/pull/156)): attaching an **edit-event
trigger** — the framework's generic seam for webhooks — to a resource, built to
close out the original ask in issue
[#18](https://github.com/tofarr/resourcey/issues/18).

It is the same `Thread` / `Message` board as example 01. The point here is not
a new domain — it's the trigger wiring. The configured trigger,
`LoggingWebhookTrigger`, does not actually call out over the network: it
**logs** exactly what a real webhook sender would have POSTed, and to which
URL. That is enough to prove the mechanism end-to-end (fires once per write,
only on success, isolated per trigger, in the background by default) without a
second server, a mock, or any network access — watch the `uvicorn` console
while you make requests below. Swapping the log line for a real
`httpx.AsyncClient().post(...)` call is the only change a production webhook
sender needs; see `webhooks_example/triggers.py`.

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
│   └── triggers.py            # LoggingWebhookTrigger -- the example's concrete Trigger
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

The server console logs:

```
INFO:webhooks_example.webhook:[webhook:audit-log] would POST to https://example.com/webhooks/inbox: create -> {'id': 1, 'title': 'Hello', ...}
```

Create a message under it (the **config-driven** wiring — `slack-notify`, from
`.env`):

```bash
curl -s -X POST http://127.0.0.1:8088/messages \
  -H "Content-Type: application/json" \
  -d '{"thread_id":1,"text":"hi there"}'
```

```
INFO:webhooks_example.webhook:[webhook:slack-notify] would POST to https://hooks.example.com/services/T000/B000/XXXX: create -> {'id': 1, 'thread_id': 1, 'text': 'hi there', ...}
```

Update and delete both fire too:

```bash
curl -s -X PATCH http://127.0.0.1:8088/messages/1 \
  -H "Content-Type: application/json" -d '{"text":"edited"}'
# → logs: ... update -> {'id': 1, ..., 'text': 'edited', ...}

curl -s -o /dev/null -w "%{http_code}\n" -X DELETE http://127.0.0.1:8088/messages/1
# → 204, logs: ... delete id=1
```

A **batch edit** fires its trigger **once**, not once per item — the whole
batch is one edit operation:

```bash
curl -s -X POST http://127.0.0.1:8088/messages/batch-edit \
  -H "Content-Type: application/json" \
  -d '[
        {"kind":"Create","item":{"thread_id":1,"text":"first"}},
        {"kind":"Create","item":{"thread_id":1,"text":"second"}},
        {"kind":"Create","item":{"thread_id":1,"text":"third"}}
      ]'
# → exactly one log line, naming all three results:
#   ... create -> {'id': 1, ..., 'text': 'first', ...}; create -> {'id': 2, ..., 'text': 'second', ...}; create -> {'id': 3, ..., 'text': 'third', ...}
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
| `APP_TRIGGERS_0_TRIGGER_KIND` | `webhooks_example.triggers.LoggingWebhookTrigger` | Dotted path of the `Trigger` subclass. |
| `APP_TRIGGERS_0_TRIGGER_NAME` | `slack-notify` | The simulated subscriber's label (a `LoggingWebhookTrigger` field). |
| `APP_TRIGGERS_0_TRIGGER_URL` | *(an example URL)* | The URL a real sender would `POST` to. |

Add a second entry (`APP_TRIGGERS_1_*`) to attach another trigger, or point
`messages` at more than one subscriber by repeating `APP_TRIGGERS_<n>_*` with
the same `RESOURCE_PATH`.

## Writing a real webhook `Trigger`

`resourcey.triggers` ships only the abstract `Trigger` contract — deliberately
no HTTP-sending implementation, so no HTTP client becomes a mandatory
framework dependency. Replace `LoggingWebhookTrigger`'s `callback` body with a
real delivery, keeping the same shape:

```python
import httpx
from resourcey.triggers.trigger import Trigger, TriggerEdits, TriggerResults


class NotifyWebhook(Trigger):
    url: str

    async def callback(self, edits: TriggerEdits, results: TriggerResults) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(self.url, json=[r.model_dump(mode="json") for r in results if r])
```

Point `APP_TRIGGERS_0_TRIGGER_KIND` at its dotted path and it drops in with no
other change — error handling, retries, and signing are the trigger
implementation's job (`resourcey.triggers` fires best-effort, at-most-once; see
the `trigger.py` / `triggered_service.py` module docstrings).

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
no new endpoints are added by this example.
