"""``ListResource`` - a read-only resource backed by a list of Pydantic objects.

The third storage backend alongside :class:`~resourcey.resource.sql.SqlResource`
(SQL) and :class:`~resourcey.mongo.mongo_resource.MongoResource` (Mongo). Where
those persist to a database, a list resource serves an **application-supplied
collection of in-process objects** - static reference data (country codes,
feature flags, catalog entries) that already exists as Pydantic models and
should be exposed over REST without being copied into a table.

A list resource is deliberately **read-only**: its :attr:`actions` are narrowed
to the read set (``read``, ``search``, ``count``, ``batch_read``), so no create /
update / delete / batch_edit route is registered. The write actions remain
unimplemented on :class:`~resourcey.resource.service_base.BaseService` and are
never reachable.

The list *is* the storage. The resource reuses the same seam every backend uses:
:meth:`open_storage` yields the resolved items and :meth:`build_service` wraps
them in a :class:`~resourcey.list.list_service.ListService`. A subclass supplies
the data by overriding :meth:`get_items`; everything else (schemas, routes,
paging, sorting, filtering, cache headers) is generated as for any resource.

Example::

    class Country(ListResource):
        id: str
        name: str
        iso3: str

        def get_items(self):
            return [CountryRead(id="us", name="United States", iso3="USA"), ...]

    manifest = ResourceManifest(resources=(Country,))
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from resourcey.resource.base import BaseResource
from resourcey.resource.errors import ResourceyConfigError
from resourcey.resource.service_base import Action

# The read-only action subset: no create / update / delete / batch_edit.
_READ_ONLY_ACTIONS: frozenset[Action] = frozenset(
    {Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}
)


class ListResource(BaseResource):
    """A read-only resource declaration backed by an in-process list.

    Narrows :attr:`actions` to the read set and reuses the storage seam: the
    resolved list is the storage, wrapped by
    :class:`~resourcey.list.list_service.ListService`. There is no ORM model,
    no collection, and no migration - the data lives wherever
    :meth:`get_items` reads it from.
    """

    # ------------------------------------------------------------------
    # Action surface (read-only)
    # ------------------------------------------------------------------

    @property
    def actions(self) -> frozenset[Action]:
        """The read-only action subset (never writable).

        A list resource supports exactly ``read``, ``search``, ``count``, and
        ``batch_read``. The route builder only registers routes for the
        supported actions, so no write route is ever mounted - the narrowing is
        structural, not a runtime guard.
        """
        return _READ_ONLY_ACTIONS

    # ------------------------------------------------------------------
    # Data source
    # ------------------------------------------------------------------

    def get_items(self) -> Any:
        """Return the collection of objects this resource serves.

        Override this - it is the resource's data source. Return an iterable of
        Pydantic objects (or plain dicts); each is validated into the resource's
        read model. The method may be ``async`` (a coroutine returning the
        iterable is awaited), so data can come from any in-process source.

        Re-resolved on every request, so the collection may change between
        requests. The base implementation raises: a list resource without a
        ``get_items`` override has no data.
        """
        raise ResourceyConfigError(
            f"{type(self).__name__} must implement get_items() to serve its data."
        )

    # ------------------------------------------------------------------
    # Storage + service (reuse the shared seam)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def open_storage(self, request: Any) -> AsyncIterator[list[Any]]:
        """Yield the resolved items for ``request``.

        Calls :meth:`get_items` and awaits it when it returns a coroutine, so
        both ``def get_items`` and ``async def get_items`` are supported.
        """
        items = self.get_items()
        if inspect.isawaitable(items):
            items = await items
        yield list(items)

    def build_service(self, resource: BaseResource, storage: Any) -> Any:
        """Build a :class:`ListService` bound to ``resource`` over ``storage``.

        ``resource`` is the resource the service is *for* - a wrapper passes
        itself so the service's read model is the wrapper's projection.
        """
        from resourcey.list.list_service import ListService

        return ListService(resource, items=storage)
