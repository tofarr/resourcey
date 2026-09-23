"""``ListResource`` - a read-only resource backed by a list of Pydantic objects.

The third storage backend alongside :class:`~resourcey.resource.sql.SqlResource`
(SQL) and :class:`~resourcey.mongo.mongo_resource.MongoResource` (Mongo). Where
those persist to a database and *derive* their read model from a field
declaration, a list resource serves an **application-supplied collection of
in-process objects** that are already modelled — static reference data (country
codes, feature flags, catalog entries) that should be exposed over REST without
being copied into a table.

Because the objects already exist as Pydantic models, the resource does no
schema generation: **the model is the read model**. The resource is
deliberately **read-only** (its :attr:`actions` are narrowed to ``read``,
``search``, ``count``, ``batch_read``), so it needs no create or update model
either — and, having no table, no columns.

Example::

    class Country(BaseModel):
        id: str
        name: str
        iso3: str

    countries = [
        Country(id="us", name="United States", iso3="USA"),
        Country(id="ca", name="Canada", iso3="CAN"),
    ]
    resource = ListResource(models=countries)
    app = ResourceManifest(resources=(resource,)).create_app()

By default the resource is **defensive**: every object it hands out is a deep
copy of the stored object, so a caller cannot mutate the collection through a
result (``ListResource(models=countries, defensive=False)`` opts out).

The list *is* the storage, delivered through the same
:meth:`open_storage` / :meth:`build_service` seam every backend uses: the
resolved items are wrapped in a
:class:`~resourcey.list.list_service.ListService`. Everything else (routes,
paging, sorting, filtering, cache headers) is generated as for any resource.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel, SecretStr
from pydantic.fields import FieldInfo

from resourcey.resource.base import BaseResource, _resolve_scalar_type
from resourcey.resource.errors import ResourceyConfigError
from resourcey.resource.field import ResourceyField
from resourcey.resource.service_base import Action
from resourcey.util.naming import camel_to_kebab, pluralize

# The read-only action subset: no create / update / delete / batch_edit.
_READ_ONLY_ACTIONS: frozenset[Action] = frozenset(
    {Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}
)


class ListResource(BaseResource):
    """A read-only resource serving an application-supplied list of models.

    The wrapped Pydantic model *is* the read model, so the resource generates
    no schemas and declares no columns; it only narrows :attr:`actions` to the
    read set and reuses the storage seam to serve the items through a
    :class:`~resourcey.list.list_service.ListService`.
    """

    # Marker: ``model_fields`` is a property proxying the wrapped model, so
    # BaseResource.__init_subclass__ must skip field collection (for this class
    # and every subclass).
    _is_proxy_resource: bool = True

    def __init__(
        self,
        *,
        models: Iterable[BaseModel] = (),
        defensive: bool = True,
        model: type[BaseModel] | None = None,
        path: str | None = None,
    ) -> None:
        """Wrap ``models`` as a read-only REST resource.

        Args:
            models: The objects to serve. They must all be instances of a
                single Pydantic model (the model is inferred from the first
                item). A list is held by reference, so mutating it later
                updates what the resource serves.
            defensive: When ``True`` (the default) every object the resource
                outputs is a deep copy of the stored object, so a caller
                cannot mutate the served collection through a result.
            model: The model to serve, overriding inference from ``models``.
                Required only when ``models`` is empty.
            path: The REST path segment. Defaults to the model's name
                pluralized and kebab-cased (``Country`` -> ``countrys``);
                pass e.g. ``path="countries"`` for an irregular plural (or
                override :meth:`get_resource_path` on a subclass).
        """
        items: list[Any] = models if isinstance(models, list) else list(models)
        resolved = model if model is not None else (type(items[0]) if items else None)
        if resolved is None:
            raise ResourceyConfigError(
                f"{type(self).__name__} needs a model: pass a non-empty models list or an "
                "explicit model=<BaseModel subclass>."
            )
        if not (isinstance(resolved, type) and issubclass(resolved, BaseModel)):
            raise ResourceyConfigError(
                f"{type(self).__name__} model must be a Pydantic BaseModel subclass, got "
                f"{resolved!r}."
            )
        for item in items:
            if not isinstance(item, resolved):
                raise ResourceyConfigError(
                    f"{type(self).__name__} models must all be {resolved.__name__} instances; "
                    f"got {type(item).__name__}."
                )
        self._models = items
        self._model = resolved
        self._defensive = defensive
        self._path = path

    # ------------------------------------------------------------------
    # Field registry / generated models
    # ------------------------------------------------------------------

    @property
    def model_fields(self) -> dict[str, FieldInfo]:
        """The wrapped model's fields (the resource declares none of its own)."""
        return self._model.model_fields

    @model_fields.setter
    def model_fields(self, _: dict[str, FieldInfo]) -> None:
        # ``BaseResource.__init_subclass__`` assigns ``cls.model_fields`` on
        # field-declaring subclasses; it skips proxy resources (this class),
        # so this never runs. The setter exists only so the property satisfies
        # mypy's writeable-attribute override check.
        pass

    def get_read_model(self) -> type[BaseModel]:
        """The wrapped model — it already *is* the read model (no projection)."""
        return self._model

    def get_create_model(self) -> type[BaseModel]:
        """Unavailable: a list resource is read-only and generates no write model."""
        raise ResourceyConfigError(f"{type(self).__name__} is read-only; it has no create model.")

    def get_update_model(self) -> type[BaseModel]:
        """Unavailable: a list resource is read-only and generates no write model."""
        raise ResourceyConfigError(f"{type(self).__name__} is read-only; it has no update model.")

    def get_config_for_field(self, field_name: str, field: FieldInfo) -> ResourceyField:
        """Return the field's flags without the SQL-centric conventions.

        An explicit ``ResourceyField`` is honoured, and ``SecretStr`` still
        defaults to ``sortable=False``. The id / timestamp conventions from
        :meth:`BaseResource.get_config_for_field` are skipped: they exist to
        give SQL columns sane write semantics, and a list resource has neither
        columns nor write actions.
        """
        for meta in field.metadata:
            if isinstance(meta, ResourceyField):
                return meta
        config = ResourceyField()
        if _resolve_scalar_type(field.annotation) is SecretStr:
            config = config.model_copy(update={"sortable": False})
        return config

    def get_id_field(self) -> str:
        """The identifier field name (``id``).

        Not class-cached: two list resources of the same class may wrap
        different models.
        """
        if "id" in self.model_fields:
            return "id"
        raise ResourceyConfigError(
            f"Resource {type(self).__name__} has no 'id' field; override get_id_field() "
            "to specify one."
        )

    def get_sortable_fields(self) -> list[str]:
        """Sortable field names of the wrapped model (per-instance, not cached)."""
        queryable = self.get_queryable_fields()
        return [
            name
            for name, field in self.model_fields.items()
            if name in queryable and self.get_config_for_field(name, field).sortable
        ]

    def get_cache_strategy(self) -> Any:
        """The wrapped model's cache strategy (per-instance, not cached)."""
        from resourcey.cache.cache_strategy import ETagCacheStrategy, LastModifiedCacheStrategy

        field = self.model_fields.get("updated_at")
        if field is not None and self.get_config_for_field("updated_at", field).readable:
            return LastModifiedCacheStrategy()
        return ETagCacheStrategy()

    def get_resource_path(self) -> str:
        """The REST path segment: the explicit ``path`` else the model name.

        A list resource is installed by instantiating it (there is no bespoke
        resource class to name the path after), so by default the model's name
        supplies it (``Country`` -> ``countrys``). Pass ``path=...`` for an
        irregular plural, or override this on a subclass.
        """
        if self._path is not None:
            return self._path.lstrip("/")
        return pluralize(camel_to_kebab(self._model.__name__)).lower()

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
    # Registration / lifecycle
    # ------------------------------------------------------------------

    def on_register(self) -> None:
        """Validate the wrapped model exposes an id (nothing to materialise)."""
        self.get_id_field()

    # ------------------------------------------------------------------
    # Output cloning
    # ------------------------------------------------------------------

    def clone_for_output(self, item: Any) -> Any:
        """Deep-copy ``item`` when ``defensive`` so callers cannot edit the store."""
        if not self._defensive or not isinstance(item, BaseModel):
            return item
        return item.model_copy(deep=True)

    # ------------------------------------------------------------------
    # Storage + service (reuse the shared seam)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def open_storage(self, request: Any) -> AsyncIterator[list[Any]]:
        """Yield a shallow copy of the served items for ``request``."""
        yield list(self._models)

    def build_service(self, resource: BaseResource, storage: Any) -> Any:
        """Build a :class:`ListService` bound to ``resource`` over ``storage``.

        ``resource`` is the resource the service is *for* - a wrapper passes
        itself so the service's read model is the wrapper's projection.
        """
        from resourcey.list.list_service import ListService

        return ListService(resource, items=storage)
