"""``TriggeredService`` — fires edit-event triggers after a successful edit (issue #155).

The service wrapper :class:`~resourcey.triggers.triggered_resource.TriggeredResource`
yields. It forwards every action to ``inner`` verbatim and, after a **write**
action (``create`` / ``update`` / ``delete`` / ``batch_edit``) returns
successfully, invokes every configured trigger's
:meth:`~resourcey.triggers.trigger.Trigger.callback` once with that
operation's ``(edits, results)``. Reads (``read`` / ``search`` / ``count`` /
``batch_read``) never fire a trigger, and an inner exception on a write fires
none either — the trigger only sees a success.

Execution policy
----------------
* **Per-invocation isolation**: every trigger's callback runs inside its own
  ``asyncio.gather(..., return_exceptions=True)`` slot, so one raising trigger
  is caught, logged (``logger.exception``) and never stops the rest.
* **Background by default** (``background=True``): firing launches
  ``asyncio.create_task(...)`` and returns without awaiting, so webhook
  latency never blocks the edit's response. ``background=False`` awaits
  inline for reliability-critical paths.
* **In-flight tracking at the right scope**: this service is built fresh
  **per request** (see :mod:`~resourcey.triggers.trigger_runner` for why
  tracking in-flight runs *here* would cancel them before they ever get a
  turn to execute). Firing is delegated to a
  :class:`~resourcey.triggers.trigger_runner.TriggerRunner` — shared and
  app-scoped when one is injected (by
  :class:`~resourcey.triggers.triggered_resource.TriggeredResource` /
  :class:`~resourcey.triggers.triggered_dependency_builder.TriggeredDependencyBuilder`),
  owned and closed by this service itself only when none is (the standalone,
  direct-construction case, where "whoever opens it owns its close" still
  applies).
* **Best effort, at-most-once**: no redelivery / ordering / exactly-once —
  a trigger needing durability (a webhook's own retry, a pub/sub publish)
  implements that itself; the framework fires best-effort.

This module is part of ``resourcey.triggers``; it imports only lower framework
layers (``core`` / ``util``), plus its sibling :mod:`trigger_runner`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Generic, TypeVar

from resourcey.core.service import (
    Create,
    Delete,
    Page,
    Service,
    Update,
)
from resourcey.triggers.trigger import Trigger
from resourcey.triggers.trigger_runner import TriggerRunner
from resourcey.util.search_filter import SearchFilter
from resourcey.util.sort_order import SortOrder

T = TypeVar("T")
K = TypeVar("K")


class TriggeredService(Service[T, K], Generic[T, K]):
    """A service proxy that forwards to ``inner`` and fires triggers on a successful edit.

    It is its own async context manager, delegating the storage lifetime to
    the inner service (the "whoever opens the storage owns its commit and
    close" rule is unchanged). Firing — including in-flight background-run
    tracking and teardown — is delegated to a
    :class:`~resourcey.triggers.trigger_runner.TriggerRunner`, shared and
    app-scoped when one is injected, owned by this service itself otherwise
    (see the module docstring).
    """

    def __init__(
        self,
        inner: Service[T, K],
        triggers: Sequence[Trigger[T, K]],
        *,
        background: bool = True,
        runner: TriggerRunner | None = None,
    ) -> None:
        super().__init__()
        self._inner = inner
        self._triggers = list(triggers)
        self._background = background
        self._owns_inner = False
        # No injected runner -> standalone, direct-construction usage: this
        # service owns the runner it creates and closes it on its own exit
        # (see the module / trigger_runner docstrings for why an *injected*
        # runner must not be closed here).
        if runner is not None:
            self._runner = runner
            self._owns_runner = False
        else:
            self._runner = TriggerRunner()
            self._owns_runner = True

    @property
    def runner(self) -> TriggerRunner:
        """The :class:`TriggerRunner` this service fires through."""
        return self._runner

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> TriggeredService[T, K]:
        await super().__aenter__()
        # An inner that is already entered (e.g. a composed DependencyBuilder
        # opened it before handing it to this wrapper) stays owned by whoever
        # opened it -- adopting it here would double-enter / double-close it.
        if not self._inner.entered:
            await self._inner.__aenter__()
            self._owns_inner = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the inner service; close an owned runner, never a shared one.

        A shared (injected) runner outlives this single request's service —
        it is only closed by its app-scoped owner — so this only calls
        :meth:`~resourcey.triggers.trigger_runner.TriggerRunner.aclose` when
        no runner was injected (this service created and therefore owns it).
        """
        if self._owns_runner:
            await self._runner.aclose()
        if self._owns_inner and self._inner.entered:
            await self._inner.__aexit__(*exc)
            self._owns_inner = False
        await super().__aexit__(*exc)

    # ------------------------------------------------------------------
    # Serialization context / cache privacy (delegated to the inner service)
    # ------------------------------------------------------------------

    def serialization_context(self) -> dict[str, Any] | None:
        """The inner service's serialization context (delegated verbatim).

        A trigger changes nothing observable about a response, so the
        one-time secret-reveal context an inner service supplies must survive
        wrapping unchanged.
        """
        return self._inner.serialization_context()

    def response_is_private(self) -> bool:
        """The inner service's cache privacy (delegated verbatim)."""
        return self._inner.response_is_private()

    # ------------------------------------------------------------------
    # Reads (forwarded; never fire a trigger)
    # ------------------------------------------------------------------

    async def read(self, id: K) -> T:  # noqa: A002
        self._require_entered()
        return await self._inner.read(id)

    async def search(
        self,
        search_filter: SearchFilter[T] | None = None,
        sort_order: SortOrder[T] | None = None,
        cursor: str | None = None,
        limit: int = 20,
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
    # Writes (forwarded, then fire on success)
    # ------------------------------------------------------------------

    async def create(self, payload: T) -> T:
        self._require_entered()
        result = await self._inner.create(payload)
        await self._fire([Create(item=payload)], [result])
        return result

    async def update(self, payload: T) -> T:
        self._require_entered()
        result = await self._inner.update(payload)
        await self._fire([Update(item=payload)], [result])
        return result

    async def delete(self, id: K) -> None:  # noqa: A002
        self._require_entered()
        await self._inner.delete(id)
        await self._fire([Delete(id=id)], [None])

    async def batch_edit(self, edits: list[Create[T] | Update[T] | Delete[K]]) -> list[T | None]:
        """Forward ``batch_edit`` then fire each trigger once with the whole batch.

        Triggers see the operation's full ``(edits, results)`` lists — one
        invocation per trigger, not one per item.
        """
        self._require_entered()
        results = await self._inner.batch_edit(edits)
        await self._fire(edits, results)
        return results

    # ------------------------------------------------------------------
    # Firing
    # ------------------------------------------------------------------

    async def _fire(
        self, edits: list[Create[T] | Update[T] | Delete[K]], results: list[T | None]
    ) -> None:
        """Fire every configured trigger once, through this service's runner.

        Delegated to :meth:`~resourcey.triggers.trigger_runner.TriggerRunner.fire`
        (shared and app-scoped when a runner was injected, owned by this
        service otherwise) — see the module docstring for why firing /
        in-flight tracking cannot live directly on this per-request service.
        """
        await self._runner.fire(self._triggers, edits, results, background=self._background)
