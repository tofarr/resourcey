"""The realtime WebSocket transport (issue #17).

:func:`add_realtime` mounts a single WebSocket route (default ``/ws``) that an
app calls **after** :func:`~resourcey.http.app.create_app`, exactly as
:func:`~resourcey.filestore.file_routes.register_file_routes` mounts the file
handshake. It adds no :class:`~resourcey.core.service.Action` member and
touches no core code -- a subscription socket is genuinely not one of the
eight standard actions, and publishing is the independent, already-wired
:class:`~resourcey.triggers.redis_trigger.RedisTrigger` seam (see that
module); this one only *delivers*.

The socket is an **authorization boundary**, not a dumb pipe:

* the handshake is authenticated with the *same* ``Authenticator`` seam as REST
  (an API key / cookie works identically), so an absent credential is anonymous
  only under an optional posture and a presented-but-invalid one is rejected;
* a ``subscribe`` message names a resource and an optional filter; the
  subscription is validated against the resource's **exposed actions** (a
  client cannot subscribe to a resource it cannot read over REST) and the
  filter is parsed with the framework's own ``<field>__<op>`` vocabulary;
* the subscriber's ``PolicyResolver`` policies are resolved **once** at
  subscribe time and every candidate event is filtered against them **per
  subscriber** before send -- an out-of-scope row is never delivered and a
  no-policy subscriber receives nothing (fail-closed). A row-scoped policy
  (``Owner`` / ``Acl`` / group) means the server decides per event whether
  *this* subscriber may see *this* row.

Delivery is **at-most-once, unordered, no replay** -- pub/sub drops an event
for a subscriber that is down or briefly disconnected. The reconnect reconcile
is the ordinary REST ``search`` (with a cursor / ``updated_at__gt=``), which
the framework already serves; the push channel is a latency optimisation over
polling, not the source of truth. Each connection has a bounded outbound
buffer (the channel's queue) so one slow client cannot exhaust a process.

Protocol (in-band messages, so the filter tree stays off the URL and the
protocol can grow -- resubscribe, filter changes):

* client -> server: ``{"type": "subscribe", "resource": ..., "filter": {...}}``,
  ``{"type": "unsubscribe", "resource": ...}``, ``{"type": "ping"}``
* server -> client: ``{"type": "ack", "resource": ...}`` /
  ``{"type": "error", "message": ...}`` / ``{"type": "event", "event": {...}}`` /
  ``{"type": "ping"}``

This protocol -- and the message / event schemas below -- is exactly what
:func:`~resourcey.realtime.realtime_asyncapi.add_asyncapi` documents, so the
two modules must be read together when either changes.

This module is part of ``resourcey.realtime``; it imports only lower framework
layers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, TypeAdapter, ValidationError
from starlette.requests import HTTPConnection

from resourcey.auth.auth_policy import AllowAllResolver, Policy, PolicyResolver
from resourcey.core.errors import InvalidInputError
from resourcey.core.manifest import Manifest
from resourcey.core.resource import Resource
from resourcey.core.service import Action, normalize_actions
from resourcey.http.dependency_builder import DependencyBuilder, OpenDependencyBuilder
from resourcey.http.routes import _filter_surface
from resourcey.realtime.realtime_channel import Channel
from resourcey.realtime.realtime_config import RealtimeConfig
from resourcey.util.search_filter import SEPARATOR, AllFilter, SearchFilter, build_filter

# The default mount path of the subscription socket.
DEFAULT_REALTIME_PATH = "/ws"

# The WebSocket close code for a policy violation (RFC 6455 1008), used when
# the handshake is rejected (unauthenticated / invalid credential).
_POLICY_VIOLATION = 1008


@dataclass
class _Subscription:
    """One client subscription: a resource plus the filters that gate delivery."""

    resource: str
    exposed: Resource[Any, Any]
    read_model: type[BaseModel]
    policy_filter: SearchFilter[Any]
    sub_filter: SearchFilter[Any] | None = None


@dataclass
class _Connection:
    """One open socket: its subscriptions and a lock serialising outbound sends."""

    websocket: WebSocket
    subscriptions: list[_Subscription] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, payload: dict[str, Any]) -> bool:
        """Send ``payload`` under the send lock; ``False`` if the socket is gone."""
        try:
            async with self.lock:
                await self.websocket.send_json(payload)
            return True
        except (WebSocketDisconnect, RuntimeError):
            return False


class RealtimeHub:
    """The subscription socket's state: the resource set, the channel, the auth.

    Built once by :func:`add_realtime`; the route handler drives one
    :class:`_Connection` per client through it.
    """

    def __init__(
        self,
        manifest: Manifest,
        channel: Channel,
        *,
        dependency_builder: DependencyBuilder,
        heartbeat_seconds: int,
    ) -> None:
        self.channel = channel
        self.heartbeat_seconds = heartbeat_seconds
        self._builder = dependency_builder
        self._resources: dict[str, Resource[Any, Any]] = {}
        for resource in manifest.resources:
            exposed = resource.get_exposed_resource()
            if exposed is not None:
                self._resources[exposed.get_resource_path()] = exposed

    # -- authentication -------------------------------------------------

    async def authenticate(self, conn: HTTPConnection) -> Any | None:
        """Resolve the handshake's principal, or ``None`` for an anonymous caller.

        Uses the builder's ``Authenticator`` / posture when it has one (an
        ``AuthorizedDependencyBuilder``); the default open builder
        authenticates nobody. A presented-but-invalid credential, or a missing
        credential under a required posture, raises
        :class:`RealtimeRejectionError`.
        """
        authenticator = getattr(self._builder, "authenticator", None)
        if authenticator is None:
            return None
        result = await authenticator.authenticate(conn)
        if result.principal is not None:
            return result.principal
        if result.credential_present and not result.credential_valid:
            raise RealtimeRejectionError("Invalid credential.")
        posture = getattr(self._builder, "posture", None)
        if posture is not None and str(posture) == "required":
            raise RealtimeRejectionError("Authentication required.")
        return None

    def _policy_resolver(self) -> PolicyResolver:
        resolver = getattr(self._builder, "policy_resolver", None)
        return resolver if resolver is not None else AllowAllResolver()

    # -- subscription ---------------------------------------------------

    async def subscribe(
        self, principal: Any | None, resource_name: Any, raw_filter: Any
    ) -> _Subscription:
        """Validate and build one subscription, or raise :class:`RealtimeRejectionError`.

        The resource must exist and be readable over REST; the filter is
        parsed with the resource's own query surface; and the subscriber's
        policies for the resource are resolved (fail-closed when they reduce
        to deny).
        """
        if not isinstance(resource_name, str) or resource_name not in self._resources:
            raise RealtimeRejectionError(f"Unknown resource {resource_name!r}.")
        exposed = self._resources[resource_name]
        supported = normalize_actions(exposed.get_supported_actions())
        if Action.READ not in supported:
            raise RealtimeRejectionError(f"Resource {resource_name!r} is not subscribable.")
        try:
            sub_filter = _build_subscription_filter(exposed, raw_filter)
        except InvalidInputError as exc:
            raise RealtimeRejectionError(str(exc)) from exc
        policies = await self._policy_resolver().resolve(exposed, principal)
        policy_filter = await _read_filter(policies, principal)
        return _Subscription(
            resource=resource_name,
            exposed=exposed,
            read_model=exposed.get_rest_models().read_response,
            policy_filter=policy_filter,
            sub_filter=sub_filter,
        )


class RealtimeRejectionError(Exception):
    """A handshake or subscription the server refuses (closes / errors the socket)."""


def add_realtime(
    app_or_router: FastAPI | APIRouter,
    manifest: Manifest,
    *,
    channel: Channel,
    dependency_builder: DependencyBuilder | None = None,
    config: RealtimeConfig | None = None,
    path: str = DEFAULT_REALTIME_PATH,
) -> APIRouter:
    """Mount the realtime subscription socket.

    Args:
        app_or_router: A ``FastAPI`` app / ``APIRouter`` (duck-typed).
        manifest: The resource set served; the socket resolves subscription
            targets from it.
        channel: The channel this socket's subscribers are delivered from.
            Must be the **same instance** every publishing
            :class:`~resourcey.triggers.redis_trigger.RedisTrigger` forwards
            to, so a write in this process reaches this process's
            subscribers, and must already be entered (e.g. via
            ``Manifest(managers=[channel])``).
        dependency_builder: The builder whose ``Authenticator`` /
            ``PolicyResolver`` the socket reuses (default
            :class:`~resourcey.http.dependency_builder.OpenDependencyBuilder`,
            which authenticates nobody and grants everything).
        config: The realtime config (heartbeat); default
            ``RealtimeConfig.get_instance()``.
        path: The mount path (default ``/ws``).
    """
    builder = dependency_builder if dependency_builder is not None else OpenDependencyBuilder()
    resolved_config = config if config is not None else RealtimeConfig.get_instance()
    hub = RealtimeHub(
        manifest,
        channel,
        dependency_builder=builder,
        heartbeat_seconds=resolved_config.heartbeat_seconds,
    )

    router = APIRouter()

    async def handler(websocket: WebSocket) -> None:
        try:
            principal = await hub.authenticate(websocket)
        except RealtimeRejectionError:
            await websocket.close(code=_POLICY_VIOLATION)
            return
        await websocket.accept()
        await _serve_connection(websocket, hub, principal)

    router.add_api_websocket_route(path, handler, name="realtime")
    app_or_router.include_router(router)
    return router


async def _serve_connection(websocket: WebSocket, hub: RealtimeHub, principal: Any | None) -> None:
    """Receive subscription messages and deliver events until the socket closes."""
    connection = _Connection(websocket=websocket)
    deliver = asyncio.create_task(_deliver(connection, hub))
    heartbeat = asyncio.create_task(_heartbeat(connection, hub.heartbeat_seconds))
    try:
        while True:
            message = await websocket.receive_json()
            await _handle_message(connection, hub, principal, message)
    except WebSocketDisconnect:
        pass
    finally:
        deliver.cancel()
        heartbeat.cancel()
        await asyncio.gather(deliver, heartbeat, return_exceptions=True)


async def _handle_message(
    connection: _Connection, hub: RealtimeHub, principal: Any | None, message: Any
) -> None:
    """Dispatch one client message (``subscribe`` / ``unsubscribe`` / ``ping``)."""
    if not isinstance(message, dict):
        await connection.send({"type": "error", "message": "Malformed message."})
        return
    kind = message.get("type")
    if kind == "ping":
        await connection.send({"type": "pong"})
        return
    if kind == "unsubscribe":
        name = message.get("resource")
        connection.subscriptions = [s for s in connection.subscriptions if s.resource != name]
        await connection.send({"type": "ack", "resource": name})
        return
    if kind != "subscribe":
        await connection.send({"type": "error", "message": f"Unknown message type {kind!r}."})
        return
    try:
        subscription = await hub.subscribe(
            principal, message.get("resource"), message.get("filter")
        )
    except RealtimeRejectionError as exc:
        await connection.send({"type": "error", "message": str(exc)})
        return
    connection.subscriptions = [
        s for s in connection.subscriptions if s.resource != subscription.resource
    ]
    connection.subscriptions.append(subscription)
    await connection.send({"type": "ack", "resource": subscription.resource})


async def _deliver(connection: _Connection, hub: RealtimeHub) -> None:
    """Forward each channel event to this connection's matching subscriptions."""
    async for event in hub.channel.subscribe():
        if not connection.subscriptions:
            continue
        if _deliverable(connection.subscriptions, event):
            delivered = await connection.send(
                {"type": "event", "event": event.model_dump(mode="json")}
            )
            if not delivered:
                return


def _deliverable(subscriptions: list[_Subscription], event: Any) -> bool:
    """Whether any of ``subscriptions`` admits ``event`` for this subscriber.

    A ``created`` / ``updated`` event carries the (already projected -- see
    :mod:`~resourcey.triggers.redis_trigger`) row, so it is matched against
    the subscription's policy filter and its own filter. A ``deleted`` event
    carries no row, so it is delivered only when **both** filters are unscoped
    (``AllFilter``) -- a row-scoped subscriber cannot be verified for a row
    that no longer exists, so it is fail-closed and reconciles with a REST
    ``search`` instead.
    """
    item = event.item
    for subscription in subscriptions:
        if subscription.resource != event.resource:
            continue
        if item is None:
            if _is_unscoped(subscription.policy_filter) and (
                subscription.sub_filter is None or _is_unscoped(subscription.sub_filter)
            ):
                return True
            continue
        try:
            row = subscription.read_model.model_validate(item)
        except Exception:
            continue
        if not subscription.policy_filter.matches(row):
            continue
        if subscription.sub_filter is not None and not subscription.sub_filter.matches(row):
            continue
        return True
    return False


async def _heartbeat(connection: _Connection, interval: int) -> None:
    """Send a keepalive ping every ``interval`` seconds so the socket stays open."""
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        if not await connection.send({"type": "ping"}):
            return


async def _read_filter(policies: list[Policy], principal: Any | None) -> SearchFilter[Any]:
    """The subscriber's read scope: the OR-combination of its policies' READ reductions.

    An empty policy set (or one that reduces to deny) yields a
    ``NoMatchFilter``, so a no-policy subscriber receives nothing
    (fail-closed). This mirrors the ``AuthorizedService`` union model -- a
    ``DenyAll`` contributes nothing to the union.
    """
    from resourcey.util.search_filter import NoMatchFilter, or_

    user_id = getattr(principal, "id", None)
    reductions = [await policy.to_search_filter(user_id, Action.READ) for policy in policies]
    if not reductions:
        return NoMatchFilter()
    return or_(*reductions)


def _build_subscription_filter(resource: Resource[Any, Any], raw: Any) -> SearchFilter[Any] | None:
    """Parse a subscription's ``{field__op: value}`` filter with the resource's surface.

    Mirrors the REST query path's security gate -- an unknown ``field__op``
    key (or a filter on a resource with no query surface) is an error rather
    than a silently dropped clause -- but coerces every value itself: a REST
    query string is already typed by FastAPI's per-field ``Query(...)``
    parameter before it reaches ``_coerce_filter_value``, while a subscription
    message is raw JSON, so every operator (not just ``in``) needs coercion
    here (see :func:`_coerce_subscription_value`).
    """
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise InvalidInputError("filter must be an object of <field>__<op> keys")
    surface = _filter_surface(resource)
    known = {f"{attribute}{SEPARATOR}{op}" for attribute, (_, ops) in surface.items() for op in ops}
    unknown = set(raw) - known
    if unknown:
        raise InvalidInputError(f"Unknown filter parameters {sorted(unknown)}")
    clauses = []
    for name, value in raw.items():
        attribute, sep, op = name.rpartition(SEPARATOR)
        if not (sep and attribute):
            raise InvalidInputError(f"Invalid filter parameter {name!r}")
        annotation = surface.get(attribute, (None, frozenset()))[0]
        clauses.append((attribute, op, _coerce_subscription_value(op, value, annotation)))
    return build_filter(clauses)


def _coerce_subscription_value(op: str, value: Any, annotation: Any) -> Any:
    """Coerce one subscription filter value (raw JSON) to the field's type.

    ``in`` accepts either a JSON array or the REST path's comma-separated
    string, so a client can use either form; every other operator is
    coerced the same way a REST query parameter would have been by FastAPI's
    typed ``Query(...)``. A malformed value is an ``InvalidInputError`` (which
    the caller turns into a rejected subscription), never a crash.
    """
    if annotation is None:
        return value
    adapter = TypeAdapter(annotation)
    try:
        if op == "in":
            items = (
                value
                if isinstance(value, list)
                else [part for part in (p.strip() for p in str(value).split(",")) if part]
            )
            return [adapter.validate_python(item) for item in items]
        return adapter.validate_python(value)
    except ValidationError as exc:
        raise InvalidInputError(f"Invalid value for {op!r} filter: {exc}") from exc


def _is_unscoped(search_filter: SearchFilter[Any]) -> bool:
    """Whether a filter admits every row (an ``AllFilter``)."""
    return isinstance(search_filter, AllFilter)


# Re-exported so a caller building a custom socket can name the type.
RealtimeHandler = Callable[[WebSocket], Any]
