"""``NotifyingService`` — the event emitter (issue #17).

The write happens inside a :class:`~resourcey.core.service.Service`, so the
emission point is the service — and it must fire **after the storage commits /
the operation succeeds**, never before, or a subscriber is told about a write
that then rolls back. That argues for a **service decorator**, not a
``Resource.on_event`` hook: it composes with the existing wrappers
(``AuthorizedService``, ``ViewService``), it keeps ``core`` behaviour-free (its
``Resource`` is an ABC), and it keeps WebSocket / Redis out of the storage
backends.

The stack order is ``AuthorizedService(NotifyingService(inner))`` — authorize
first, then notify — assembled by the dependency builder. So a caller who is not
permitted to write never reaches the emitter, and only a *permitted, committed*
write emits.

The emitted payload is the **read-model projection** of the written DTO, never
the internal DTO / storage row, so a field hidden by a ``ResourceView`` cannot
leak through the event stream (see :mod:`resourcey.realtime.realtime_event`).

A resource emits only for the actions it declares: a ``create`` on a read-only
resource is never routed, and a ``batch_edit`` fans out one event per *applied*
edit (a ``None`` position — a delete, or a miss — emits nothing for a create /
update; a delete emits a ``deleted`` event with no item).

This module is part of ``resourcey.realtime``; it imports only lower framework
layers.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from resourcey.core.service import (
    DEFAULT_LIMIT,
    Create,
    Delete,
    Page,
    Service,
    Update,
)
from resourcey.realtime.realtime_channel import Channel
from resourcey.realtime.realtime_event import EventKind, ResourceEvent
from resourcey.util.missing import MISSING
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

T = TypeVar("T")
K = TypeVar("K")


class NotifyingService(Service[T, K], Generic[T, K]):
    """A service proxy that publishes a :class:`ResourceEvent` after a successful write.

    It is its own async context manager, delegating the storage lifetime to the
    inner service (honouring "whoever opens the storage owns its commit and
    close"). The write actions delegate first and publish only once the inner
    call returned; the read actions forward verbatim and emit nothing.

    Attributes:
        resource_name: The resource's REST path — the event's ``resource`` and the
            subscription key.
        read_model: The resource's read-response model, used to project a written
            DTO onto exactly the shape a subscriber already knows from REST.
        id_field: The DTO's identifier field name.
    """

    def __init__(
        self,
        inner: Service[T, K],
        *,
        channel: Channel,
        resource_name: str,
        read_model: type[BaseModel],
        id_field: str,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._channel = channel
        self._resource_name = resource_name
        self._read_model = read_model
        self._id_field = id_field
        self._owns_inner = False

    # ------------------------------------------------------------------
    # Lifecycle (delegated to the inner service)
    # ------------------------------------------------------------------

    async def __aenter__(self) -> NotifyingService[T, K]:
        await super().__aenter__()
        # An inner already entered (the service dependency opened it) stays owned
        # by whoever opened it — adopting it would double-close it.
        if not self._inner.entered:
            await self._inner.__aenter__()
            self._owns_inner = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_inner and self._inner.entered:
            await self._inner.__aexit__(*exc)
            self._owns_inner = False
        await super().__aexit__(*exc)

    # ------------------------------------------------------------------
    # Delegated context / privacy
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        """The inner service's serialization context (delegated, so a reveal survives)."""
        return self._inner.serialization_context()

    def response_is_private(self) -> bool:
        """The inner service's cache privacy (delegated)."""
        return self._inner.response_is_private()

    # ------------------------------------------------------------------
    # Write actions (delegate, then publish)
    # ------------------------------------------------------------------

    async def create(self, payload: T) -> T:
        result = await self._inner.create(payload)
        await self._publish(EventKind.CREATED, result)
        return result

    async def update(self, payload: T) -> T:
        result = await self._inner.update(payload)
        await self._publish(EventKind.UPDATED, result)
        return result

    async def delete(self, id: K) -> None:  # noqa: A002
        await self._inner.delete(id)
        await self._publish(EventKind.DELETED, None, id_value=id)

    async def batch_edit(self, edits: list[Create[T] | Update[T] | Delete[K]]) -> list[T | None]:
        """Apply the batch, publishing one event per applied edit.

        A create / update position that returned a DTO emits ``created`` /
        ``updated``; a delete emits a ``deleted`` event, and a missed create /
        update (a ``None`` position) emits nothing.
        """
        results = await self._inner.batch_edit(edits)
        for edit, result in zip(edits, results, strict=True):
            if isinstance(edit, Create):
                if result is not None:
                    await self._publish(EventKind.CREATED, result)
            elif isinstance(edit, Update):
                if result is not None:
                    await self._publish(EventKind.UPDATED, result)
            else:
                await self._publish(EventKind.DELETED, None, id_value=edit.id)
        return results

    # ------------------------------------------------------------------
    # Read actions (forwarded verbatim, emit nothing)
    # ------------------------------------------------------------------

    async def read(self, id: K) -> T:  # noqa: A002
        self._require_entered()
        return await self._inner.read(id)

    async def search(
        self,
        search_filter: SearchFilter[T] | None = None,
        sort_order: SortOrder[T] | None = None,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Page[T]:
        self._require_entered()
        return await self._inner.search(search_filter, sort_order, cursor, limit)

    async def count(self, search_filter: SearchFilter[T] | None = None) -> int:
        self._require_entered()
        return await self._inner.count(search_filter)

    async def batch_read(self, ids: list[K]) -> list[T | None]:
        self._require_entered()
        return await self._inner.batch_read(ids)

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    async def _publish(self, kind: EventKind, dto: Any, *, id_value: Any = MISSING) -> None:
        """Publish one event for a completed write.

        The identifier comes from the DTO (``created`` / ``updated``) or from the
        delete call; the item is the read-model projection for a create / update
        and absent for a delete.
        """
        identifier = id_value if id_value is not MISSING else getattr(dto, self._id_field, None)
        item = self._project(dto) if dto is not None else None
        event = ResourceEvent(
            resource=self._resource_name,
            kind=kind,
            id=_wire_id(identifier),
            item=item,
        )
        await self._channel.publish(event)

    def _project(self, dto: Any) -> dict[str, Any]:
        """Project a DTO onto the read model (dropping ``MISSING``), as JSON-ready.

        Uses the inner service's serialization context, so a secret-bearing field
        serializes per the convention (redacted by default) rather than leaking.
        """
        context = self._inner.serialization_context()
        values = {name: value for name, value in vars(dto).items() if value is not MISSING}
        projected = self._read_model.model_validate(values, context=context)
        dumped = projected.model_dump(mode="json", context=context)
        return dumped if isinstance(dumped, dict) else {}


def _wire_id(value: Any) -> Any:
    """The identifier in its JSON wire form (``None`` stays ``None``)."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):  # pragma: no cover - a composite id, not expected today
        return model_dump(mode="json")
    return str(value)
