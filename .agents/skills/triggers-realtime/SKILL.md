---
name: triggers-realtime
description: Edit-event triggers (webhooks, pub/sub) and the WebSocket subscription channel — the Trigger contract, TriggeredResource/TriggeredService, TriggerConfig, TriggeredDependencyBuilder, WebhookTrigger, RedisTrigger, Channel, add_realtime, and add_asyncapi. Load when reacting to writes or pushing events to clients.
version: "2.0.0"
---

# Triggers and realtime

Two rungs over `core`'s `Resource` / `Service`. A trigger reacts to a write; the
realtime channel is a subscriber-facing push socket. Publishing rides the trigger
seam — the realtime layer adds no second mechanism.

## `Trigger` — the edit-event contract

```python
class Trigger(DiscriminatedUnionMixin, ABC, Generic[T, K]):
    @abstractmethod
    async def callback(self, edits: TriggerEdits[T, K], results: TriggerResults[T]) -> None: ...
```

A trigger is a configured, polymorphic callback. An app declares its own kinds
(a webhook notifier, a pub/sub publisher, …) as subclasses carrying whatever
pydantic fields they need — no framework schema change, and each kind is
env-parseable by dotted path.

`callback` runs **once per edit operation, never per item**:

* a single `create()` normalizes to `[Create(item=payload)]` / `[result]`;
* a batch of five edits passes its five `Edit` nodes and their five results
  verbatim, positionally aligned.

A delete or a miss is `None` in `results`, and **a miss still fires** — it is
not an error. The trigger sees the **inner DTO**, not the projected REST model.

`ResourceBoundTrigger` is an optional protocol: a trigger implementing
`bind_resource(resource)` receives the resource it is attached to once, at
registration. A generic trigger needs no such context; `RedisTrigger` does, to
tag and project a row before publishing.

## Attaching triggers

Two ways, both supported:

### `TriggeredResource` — the no-config seam

```python
TriggeredResource(inner, *, on_edit=(trigger,), background=True)
```

Unlike `ResourceView`, a trigger changes nothing observable — no field hidden,
no action narrowed, the cache policy untouched — so every schema / action /
query-sort-surface / cache-policy / registration / lifecycle method is a plain
delegation. Only `get_service` changes, wrapping the inner service in a
`TriggeredService`. `get_exposed_resource()` returns `self`, because the wrapper
must be what gets registered or the mounted service would have no triggers.

`on_edit` accepts only `Trigger` instances (a bare callable would lose the
config fields `Trigger` exists to carry) and rejects a duplicate by value
equality, both at construction.

### `TriggerConfig` + `TriggeredDependencyBuilder` — the config-driven rung

```
APP_TRIGGERS_0_RESOURCE_PATH=threads
APP_TRIGGERS_0_TRIGGER_KIND=myapp.webhooks.NotifyWebhook
APP_TRIGGERS_0_TRIGGER_URL=https://example.com/hook
```

`TriggerConfig.triggers` is a flat list of `TriggerEntry` (`resource_path` +
a polymorphic `trigger`); `resource_triggers()` groups it by path.

```python
create_app(
    manifest,
    dependency_builder=TriggeredDependencyBuilder.from_config(
        TriggerConfig.get_instance(),
        inner=AuthorizedDependencyBuilder(...),  # composes, never replaces
    ),
)
```

`TriggeredDependencyBuilder` resolves configured triggers **per resource path**
and wraps that resource's service, so a deployment can attach a webhook to an
existing app with no resource change. It composes with an `inner` builder
(default `OpenDependencyBuilder`); either order is the app's choice — this
builder only wraps what `inner` yields. A resource with no configured triggers
passes its inner dependency through **verbatim** (identity, not a copy), and a
resource that is already a `TriggeredResource` is never wrapped twice: the
constructor list wins.

## `TriggeredService` — execution policy

Forwards every action to the inner verbatim and, after a **write** (`create` /
`update` / `delete`, or a batch of them) returns successfully, fires every configured
trigger once with that operation's whole `(edits, results)`.

* Reads never fire; an inner exception on a write fires nothing — a trigger only
  ever sees a success.
* **Per-invocation isolation**: each trigger's callback runs in its own
  `asyncio.gather(..., return_exceptions=True)` slot, so one raising trigger is
  caught, `logger.exception`-logged, and never stops the rest.
* **Background by default**: firing is `asyncio.create_task(...)`, so webhook
  latency never blocks the edit's response. `background=False` awaits inline for
  reliability-critical paths.
* It is **best-effort, at-most-once** — no redelivery / ordering / exactly-once.
  A trigger needing durability implements that itself.
* `serialization_context()` and `response_is_private()` delegate to the inner.
* Its `__aenter__` / `__aexit__` follow the already-entered-inner rule: an inner
  handed to it already entered (composed behind a `DependencyBuilder`'s own
  `Depends(...)`) stays owned by whoever opened it.

### `TriggerRunner` — the right scope for a background run

A `TriggeredService` is built **fresh per request**, and Starlette closes a
`yield` dependency synchronously right after the handler returns — well before
any webhook latency could elapse. Tracking a background run on that per-request
instance and cancelling it from its `__aexit__` would cancel the task before the
event loop gave it a turn.

`TriggerRunner` is the fix: a small shared object owned by something
**app-scoped** (a `TriggeredResource` instance, or a `TriggeredDependencyBuilder`
added to `Manifest(managers=[...])`) and handed to every per-request
`TriggeredService`. A background run outlives the request and is cancelled only
at app shutdown. `fire(...)` runs each trigger in its own gather slot;
`aclose()` settles every in-flight run; `in_flight` is exposed for tests.

## `WebhookTrigger` — the framework's own HTTP trigger

```python
WebhookTrigger(
    url=...,  # required, no default
    headers=[WebhookHeader(name="X-Sig", value=SecretStr(...))],
    retry=ExponentialBackoffRetry(max_retries=3, initial_delay_seconds=1),
    timeout_seconds=...,
)
```

`url` has **no default** — a webhook with no destination would silently deliver
nowhere. `headers` values are `SecretStr`, redacted by default the same way
`DbConfig.password` is. `retry` is a polymorphic `RetryStrategy`: `NoRetry`
(default), `FixedDelayRetry`, `ExponentialBackoffRetry`; `delays()` is the whole
contract — the seconds to sleep before each retry attempt.

`callback` `POST`s one JSON array per operation (`{"kind", "item"/"id",
"result"}` per `(edit, result)` pair). A failure is retried per
`retry.delays()`; exhausting every attempt re-raises the last error, which
`TriggerRunner` isolates and logs like any other raising trigger.

`httpx` is imported **lazily**, behind the `webhooks` extra
(`resourcey[webhooks]`) — the same shape `S3FileStore` uses for `boto3` — so no
HTTP client becomes a mandatory dependency merely because an app attaches one
trigger. An explicit `client=` injects one directly; `bind_client(...)` is the
escape hatch for when the client can only be built *after* the trigger exists
(its destination is the very app it is attached to).

See `specs/triggers.qnt` and `examples/08_webhooks`.

## `RedisTrigger` — the publisher

`resourcey.triggers.redis_trigger.RedisTrigger(channel=..., resource_path=None)`
publishes one `ResourceEvent` per `(edit, result)` pair after a successful
write — a batch of five edits publishes five discrete events. Despite the name,
`channel=` defaults to `InMemoryChannel`; "Redis" names the production case the
trigger exists for, not a hard requirement. Attach it exactly like a
`WebhookTrigger`:

```python
TriggeredResource(inner, on_edit=[RedisTrigger(channel=channel)])
```

## `Channel` — the fan-out seam

`Channel` (a `DiscriminatedUnionMixin`, `kind` = class name) declares
`publish(event)` and `subscribe() -> AsyncIterator[ResourceEvent]`, both async,
and is its own async context manager (entered through `Manifest(managers=...)`)
so a network client's lifetime ties to the app.

* **`InMemoryChannel`** — the single-process default. Each `subscribe()` gets a
  fresh, bounded `asyncio.Queue` (`queue_maxsize`, default 100) that drops the
  **oldest** on overflow, so one slow subscriber cannot grow an unbounded
  server-side buffer; the dropped event is exactly what a client reconciles with
  a REST `search` anyway.
* **`RedisChannel`** — the clustered rung over `redis.asyncio` pub/sub. Every
  app instance publishes to one Redis channel name and every instance
  re-delivers to *its own* local subscribers — the correct fan-out behind a load
  balancer. **Policy filtering is local and never serialized**: a published event
  carries no principal and no filter; each instance re-applies its own
  subscribers' policies before send. `redis` is imported lazily
  (`_require_redis()`, behind `resourcey[redis]`); an injected `client=` is the
  escape hatch and is never closed on exit — only a self-built client is.

`RealtimeConfig` (a `BaseConfig`) selects the channel with no code change:
`APP_CHANNEL_CLASS` names a `Channel` subclass and its own fields parse under
`APP_CHANNEL_` (e.g. `APP_CHANNEL_URL` for `RedisChannel`); unset selects
`InMemoryChannel`. `APP_HEARTBEAT_SECONDS` defaults to 30.

**The same `Channel` instance must reach every publishing trigger and
`add_realtime`**, or (for `InMemoryChannel`) they fan out into separate,
mutually invisible processes-of-one.

## `ResourceEvent`

`ResourceEvent(resource, kind, id, timestamp, item)` with
`EventKind = CREATED | UPDATED | DELETED`. It is write-only — a `read` emits
nothing, because the publishing trigger is itself only invoked after a
successful write. `item` is the resource's **read-model projection**, never the
raw DTO, so a field a `ResourceView` hides from REST cannot leak onto the event
stream either. Frozen, because it is serialized to JSON for both the Redis bridge
and the WebSocket wire.

## `add_realtime` — the subscription socket

```python
add_realtime(app, manifest, *, channel, dependency_builder=None,
             config=None, path="/ws")
```

Mounted **after** `create_app` (a subscription socket is not one of the eight
standard actions). The socket is an **authorization boundary, not a dumb pipe**,
and reuses REST's own seams:

* The handshake is authenticated with the **same** `Authenticator` the passed
  `dependency_builder` carries (default `OpenDependencyBuilder`, which
  authenticates nobody).
* A `subscribe` message names a resource and is validated against its **exposed
  actions** — a client cannot subscribe to a resource it cannot `read` over
  REST — with an optional filter parsed through the identical `<field>__<op>`
  vocabulary REST search uses.
* The subscriber's `PolicyResolver` policies are resolved **once at subscribe
  time** (the OR-combination of every policy's `READ` reduction, a
  `NoMatchFilter` when empty — fail-closed) and **every candidate event is
  re-filtered per subscriber** before send, so an out-of-scope row is never
  delivered.
* A `deleted` event carries no row, so it is delivered only when the
  subscriber's policy **and** its own filter are both unscoped (`AllFilter`) —
  a row-scoped subscriber cannot be verified against a row that no longer
  exists, so it fails closed there and reconciles with a REST `search` instead.

In-band protocol — client → server: `subscribe` / `unsubscribe` / `ping`;
server → client: `ack` / `error` / `event` / `ping`. Keeping the filter tree off
the URL lets the protocol grow. `RealtimeHub` is the per-app state (resource set,
channel, auth seam); `RealtimeRejectionError` is a refused handshake or
subscription.

Each connection runs two tasks — `_deliver` (forwarding matching events) and
`_heartbeat` (a keepalive `ping` every `heartbeat_seconds`, so an idle connection
is not reaped by an intermediary) — both cancelled on disconnect.

See `specs/realtime.qnt` and `examples/09_realtime`.

## `add_asyncapi` — the documentation

`add_asyncapi(app, manifest, ...)` builds a spec-compliant **AsyncAPI 2.6.0**
document once, at registration time, from the manifest: the six static protocol
messages plus one `<resource>Event` message per **subscribable** resource (the
same `Action.READ`-in-exposed-actions gate the hub enforces, so the document
never promises a subscription the hub would reject), whose `item` schema is the
resource's own read model. It mounts `GET /asyncapi.json` plus `GET /asyncapi`
(a small static page loading the AsyncAPI React Component from a CDN — the same
"CDN script + link to the JSON this app serves" shape Swagger UI uses). Both
routes are `include_in_schema=False`.

## Delivery guarantees

At-most-once, unordered, no replay. Pub/sub drops an event for a subscriber that
is down or briefly disconnected, and the reconnect reconcile is the ordinary REST
`search` (a cursor, or `updated_at__gt=`), which the framework already serves.
The push channel is a latency optimisation, never the source of truth.
