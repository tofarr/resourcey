# Example 09 — Realtime Channel (WebSocket notifications)

A demonstration of `resourcey.realtime` (issue
[#17](https://github.com/tofarr/resourcey/issues/17)): a resource that
publishes a typed event on every successful write, delivered live to
subscribed WebSocket clients over a pluggable `Channel` — in-process by
default, over **Redis pub/sub** when you switch one env var.

It is the same `Thread` / `Message` board as examples 01 and 08. The point
here is not a new domain — it's the realtime wiring. Publishing rides on
`resourcey.triggers` (issue #155): each resource attaches a
`resourcey.triggers.redis_trigger.RedisTrigger`, the framework's own
channel-publishing trigger (despite the name, its `channel=` defaults to the
single-process `InMemoryChannel` — "Redis" is the production case the trigger
is named for, not a hard requirement). The same channel instance is also
handed to `resourcey.realtime.realtime_routes.add_realtime`, which mounts a
single WebSocket endpoint (`/ws`) any client can connect to, subscribe from,
and receive events from — live, as writes happen.

## Why both resources are wired the same way

Example 08 deliberately wires its two resources two *different* ways (direct
vs. config-driven) to show both rungs of `resourcey.triggers`. This example
does not repeat that split, because the realtime channel has a real
constraint the webhook trigger does not: the **same `Channel` instance** must
reach every publishing trigger and `add_realtime` itself — for the default
`InMemoryChannel`, that instance *is* the process's fan-out, so two
independently-constructed ones would never see each other's events. Splitting
`threads` and `messages` across the direct/config-driven rungs here would mean
either building that shared instance twice (broken for `InMemoryChannel`) or
threading it back out of the config-driven builder after the fact — both
noisy, and orthogonal to what this example is about. So `realtime_example/app.py`
builds **one** channel and threads it everywhere; see its module docstring for
the full reasoning.

## Layout

```
09_realtime/
├── README.md                  # this file
├── pyproject.toml             # standalone — resourcey from git (parent checkout in-repo)
├── alembic.ini                # Alembic config (URL comes from APP_SQL_CONNECTIONS_0_URL)
├── .env                       # APP_* config (committed), including the CHANNEL_CLASS switch
├── .gitignore
├── realtime_example/           # the importable app package
│   ├── app.py                  # manager + manifest + shared channel + add_realtime/add_asyncapi (uvicorn target)
│   └── models.py                # Thread & Message ORM models + Base (schema of record)
├── migrations/
│   ├── env.py                  # Alembic env, diffs against the ORM metadata
│   └── versions/                 # generated + reviewed revisions
└── tests/
    ├── test_smoke.py           # REST write -> WebSocket event, in-memory SQLite (TestClient)
    └── test_e2e.py             # full REST + realtime suite against the committed migration
```

## Run it

A **standalone** `uv` project. From within `09_realtime`:

```bash
uv sync
uv run --env-file .env alembic upgrade head          # creates realtime_example.db
uv run uvicorn realtime_example.app:app --env-file .env --reload --port 8089
# → Uvicorn running on http://127.0.0.1:8089
```

Interactive REST docs are at `http://127.0.0.1:8089/docs`; the realtime
channel's own AsyncAPI docs are at `http://127.0.0.1:8089/asyncapi` (see
"AsyncAPI docs" below).

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

Open a WebSocket connection and subscribe to `threads` — any terminal
WebSocket client works; this uses `websocat` (`cargo install websocat` /
`brew install websocat`) because it reads line-delimited JSON from stdin,
which makes the steps below copy/pasteable. A short Python client that does
the same with the `websockets` package (already a test dependency here) is
shown further down.

```bash
websocat ws://127.0.0.1:8089/ws
```

Type (and press enter) to subscribe:

```json
{"type": "subscribe", "resource": "threads"}
```

You get an ack:

```json
{"type":"ack","resource":"threads"}
```

Now, from a second terminal, create a thread over REST:

```bash
curl -s -X POST http://127.0.0.1:8089/threads \
  -H "Content-Type: application/json" \
  -d '{"title":"Hello","description":"first thread"}'
```

Back in the WebSocket terminal, the event arrives — live, with no polling:

```json
{"type":"event","event":{"resource":"threads","kind":"created","id":1,"timestamp":"...","item":{"id":1,"title":"Hello","description":"first thread","created_at":"...","updated_at":"..."}}}
```

Update and delete both fire too:

```bash
curl -s -X PATCH http://127.0.0.1:8089/threads/1 \
  -H "Content-Type: application/json" -d '{"title":"Edited"}'
# → {"type":"event","event":{"resource":"threads","kind":"updated","id":1,...,"item":{...,"title":"Edited",...}}}

curl -s -o /dev/null -w "%{http_code}\n" -X DELETE http://127.0.0.1:8089/threads/1
# → 204, and: {"type":"event","event":{"resource":"threads","kind":"deleted","id":1,"timestamp":"...","item":null}}
```

A `messages` subscription works identically — subscribe with
`{"type": "subscribe", "resource": "messages"}`, or with a filter
(the same `<field>__<op>` vocabulary REST search uses):

```json
{"type": "subscribe", "resource": "messages", "filter": {"thread_id__eq": 1}}
```

Only `messages` rows matching `thread_id == 1` are delivered to this
subscription — a failed write (e.g. a `404` update) fires nothing, exactly as
REST shows nothing happened.

### A minimal Python subscriber

```python
import asyncio
import json
import websockets


async def main() -> None:
    async with websockets.connect("ws://127.0.0.1:8089/ws") as ws:
        await ws.send(json.dumps({"type": "subscribe", "resource": "threads"}))
        print(await ws.recv())  # the ack
        while True:
            print(await ws.recv())  # each event, as it happens


asyncio.run(main())
```

## Composing with auth

This example runs with the framework's default, no-authentication
`OpenDependencyBuilder` — zero setup, any client can subscribe to anything any
REST caller could read. That is not a separate, weaker mechanism than REST's
authorization: `add_realtime`'s `dependency_builder=` is the **same** seam
`create_app` takes, so composing this example with `03_api_key_auth`'s
`AuthorizedDependencyBuilder` (or `04_simple_roles` / `05_full_rbac`'s
row-scoped `Owner` / `RbacPolicyResolver` policies) is a one-line change in
`realtime_example/app.py`:

```python
app = create_app(manifest, dependency_builder=builder)
add_realtime(app, manifest, channel=resolved_channel, dependency_builder=builder)
```

Passing the *same* `builder` to both means:

* the WebSocket handshake is authenticated exactly like a REST request (an
  absent / invalid credential is rejected under the builder's `posture`,
  not silently treated as anonymous);
* a `subscribe` message is validated against the resource's exposed actions
  (a client cannot subscribe to a resource it cannot `read` over REST);
* and — the key guarantee — **every candidate event is re-filtered per
  subscriber** through that same builder's `PolicyResolver`. A row-scoped
  policy (e.g. `04_simple_roles`'s `Owner`) means a subscriber only receives
  the rows its own REST `search` would return; a subscriber with no
  resolved policy receives nothing (fail-closed), the same as an
  out-of-scope REST read being a `404` rather than leaking existence.

No realtime-specific authorization code is needed — the authorization
*decision* is identical REST-and-realtime, because it is the identical
`Policy.to_search_filter` reduction underneath both.

## Switching to Redis (bridging real processes)

By default `CHANNEL_CLASS` is unset in `.env`, which selects
`InMemoryChannel` — correct for one process, no extra dependency. To prove
the cross-process story (e.g. two `uvicorn` workers, or two separate
machines behind a load balancer), start a local Redis and uncomment the two
lines in `.env`:

```bash
docker run --rm -p 6379:6379 redis:7
```

```bash
# .env
CHANNEL_CLASS=resourcey.realtime.realtime_redis_channel.RedisChannel
CHANNEL_URL=redis://localhost:6379/0
```

Run **two** instances of the app on different ports against the same Redis
and the same database:

```bash
uv run uvicorn realtime_example.app:app --env-file .env --port 8089
uv run uvicorn realtime_example.app:app --env-file .env --port 8090
```

Subscribe on `:8090`'s socket, then `POST /threads` against `:8089` — the
event arrives on `:8090` exactly as it would in-process, because `RedisTrigger`
publishes to Redis and *every* instance's `RedisChannel` re-delivers to its
own local subscribers. Delivery is **at-most-once, unordered, no replay** — a
subscriber that was briefly disconnected reconciles with an ordinary REST
`search` (e.g. `?updated_at__gt=...`), which the framework already serves; the
push channel is a latency optimisation over polling, not the source of truth.

## AsyncAPI docs

The realtime channel's in-band message protocol (`subscribe` /
`unsubscribe` / `ping` ⇄ `ack` / `error` / `event` / `pong`) and one
`<resource>Event` message schema per subscribable resource are documented as
a spec-compliant **AsyncAPI 2.6.0** document, generated once from the
manifest at registration time:

| Path | Purpose |
| ---- | ------- |
| `/asyncapi.json` | The raw AsyncAPI document. |
| `/asyncapi` | An HTML viewer — the `/docs` equivalent for this channel (a CDN-hosted AsyncAPI React component pointed at `/asyncapi.json`, no bundler step). |

Both are mounted after `create_app` / `add_realtime` by
`resourcey.realtime.realtime_asyncapi.add_asyncapi(app, manifest)`, mirroring
how `register_file_routes` mounts example 06's non-standard transfer
endpoints.

## Configuration

The framework reads the process-wide `APP` prefix and does **no** `.env`
loading, so every command passes `--env-file .env`.

| Env var | Default | Purpose |
| ------- | ------- | ------- |
| `APP_SQL_CONNECTIONS_0_NAME` | `main` | Connection name (first is the default). |
| `APP_SQL_CONNECTIONS_0_URL` | `sqlite+aiosqlite:///realtime_example.db` | Complete SQLAlchemy URL (driver in scheme). |
| `APP_ENCRYPTION_KEY_ID` / `_VALUE` | `dev` / *(dev default + warning when unset)* | Cursor-encryption key. |
| `CHANNEL_CLASS` | unset → `InMemoryChannel` | **Unprefixed** (a `LazyField`, like `FileStoreConfig`'s `MEDIUM_CLASS`). Set to `resourcey.realtime.realtime_redis_channel.RedisChannel` to bridge processes over Redis. |
| `CHANNEL_URL` | `redis://localhost:6379/0` | `RedisChannel`'s own field, parsed under the `CHANNEL_` prefix once selected above. |
| `APP_HEARTBEAT_SECONDS` | `30` | How often the WebSocket sends a keepalive ping so an idle connection is not reaped by an intermediary. |

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

Realtime publishing is purely a side effect of a successful write on these
same routes — no *resource* routes are added. Two non-resource endpoints exist
purely for the realtime channel itself:

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `WS` | `/ws` | The subscription socket (`add_realtime`). |
| `GET` | `/asyncapi.json` / `/asyncapi` | The channel's AsyncAPI document and HTML viewer (see above). |
