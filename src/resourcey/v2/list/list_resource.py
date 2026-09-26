"""``ListResource`` — the ``v2`` read-only list backend (issue #116).

A ``v2`` backend alongside :mod:`resourcey.v2.sql`: a resource built from an
application-supplied list of Pydantic models and served **read-only**. It is the
``v2`` successor to ``v1``'s ``resourcey.list.ListResource``.

The use case is in-process reference data that is already modelled — country
codes, feature flags, catalog entries, enum-like lookups — exposed over the same
REST surface (filter / sort / cursor paging / cache headers) **without** being
copied into a table. The list *is* the storage, so there is no table, no
migration, and no external dependency::

    class Country(BaseModel):
        id: str
        name: str
        iso3: str

    countries = [
        Country(id="us", name="United States", iso3="USA"),
        Country(id="ca", name="Canada", iso3="CAN"),
    ]
    resource = ListResource(countries, path="countries")
    manifest = Manifest(resources=(resource,))

The served Pydantic model is projected onto a ``v2``
:class:`~resourcey.v2.core.dto.DTO` (via
:func:`~resourcey.v2.list.pydantic_2_dto.pydantic_2_dto`) so the six REST models,
the query / sort surface, and the cache policy all derive exactly as they do for
the SQL / Mongo backends. An explicit ``dto=`` wins over inference — the
escape hatch for a hand-written projection.

Read-only by construction: :meth:`get_supported_actions` returns exactly
``{read, search, count, batch_read}``, and ``v2/http/routes.py`` mounts a route
only for a declared action, so **no write route exists** (a write is a ``405``,
not an unimplemented handler). By default the resource is **defensive**: every
object it outputs is a deep copy of the stored object, so a caller cannot mutate
the served collection through a result (``defensive=False`` opts out).

The action layer lives in :mod:`resourcey.v2.list.list_service`.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pydantic import BaseModel

from resourcey.v2.cache.cache_defaults import DefaultCacheStrategyMixin
from resourcey.v2.core.dto import DTO, RestModels
from resourcey.v2.core.errors import InvalidInputError, ResourceyConfigError
from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import STORAGE_KEY, Action, Service, ServiceError
from resourcey.v2.encryption.encryption_service import get_encryption_service
from resourcey.v2.list.list_service import ListService
from resourcey.v2.list.pydantic_2_dto import pydantic_2_dto
from resourcey.v2.util.naming import camel_to_kebab, pluralize
from resourcey.v2.util.search_filter import SearchFilter, operators_for_annotation
from resourcey.v2.util.sort_order import AttrSortOrder, SortOrder

if TYPE_CHECKING:
    from resourcey.v2.core.manifest import Manifest
    from resourcey.v2.encryption.encryption_service import EncryptionService

T = TypeVar("T", bound=BaseModel)
K = TypeVar("K")

# The read-only action subset: no create / update / delete / batch_edit.
_READ_ONLY_ACTIONS: frozenset[Action] = frozenset(
    {Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}
)


class ListResource(DefaultCacheStrategyMixin, Resource[T, K]):
    """The list backend: an in-process list of Pydantic models, served read-only.

    Args:
        models: The objects to serve. They must all be instances of one Pydantic
            model (inferred from the first item). The list is held **by
            reference**, so mutating it later changes what the resource serves.
        model: The served model, overriding inference (required when the list is
            empty).
        dto: An explicit :class:`~resourcey.v2.core.dto.DTO` declaration — the
            escape hatch, which wins over :func:`pydantic_2_dto`.
        path: An explicit REST path segment (defaults to the model name,
            pluralized / kebab-cased).
        defensive: When ``True`` (the default) every output is a deep copy of the
            stored object, so a caller cannot mutate the served collection
            through a result.
        encryption_service: The service used to encrypt/decrypt pagination
            cursors. Defaults to the process-wide
            :func:`~resourcey.v2.encryption.encryption_service.get_encryption_service`;
            pass one to override.
    """

    def __init__(
        self,
        models: Iterable[BaseModel] = (),
        *,
        model: type[BaseModel] | None = None,
        dto: type[DTO] | None = None,
        path: str | None = None,
        defensive: bool = True,
        encryption_service: EncryptionService | None = None,
    ) -> None:
        items: list[Any] = models if isinstance(models, list) else list(models)
        resolved = model if model is not None else (type(items[0]) if items else None)
        if resolved is None and dto is None:
            raise ResourceyConfigError(
                f"{type(self).__name__} needs a model: pass a non-empty models list, an "
                "explicit model=<BaseModel subclass>, or a dto=<DTO>."
            )
        if resolved is not None:
            _validate_models(type(self).__name__, resolved, items)
        self._models = items
        self._model = resolved
        self._dto = dto if dto is not None else pydantic_2_dto(cast("type[BaseModel]", resolved))
        # The already-modelled object *is* the DTO for this backend (v1's "the
        # model is the read model"): the service works with the served model
        # directly, so a non-defensive read can serve the stored object itself.
        # An explicit ``dto=`` is the escape hatch — a hand-written projection —
        # so the declaration's generated model is the item type instead.
        if dto is not None:
            self._item_type: type[BaseModel] = self._dto.get_dto_type()
        else:
            self._item_type = cast("type[BaseModel]", resolved)
        self._path = path
        self._defensive = defensive
        # Resolved eagerly so cursor pagination works out of the box; the
        # explicit argument remains the escape hatch.
        self._encryption_service = encryption_service or get_encryption_service()
        self._entered = False
        self._manifest: Manifest | None = None

    # ------------------------------------------------------------------
    # DTO / schema surface
    # ------------------------------------------------------------------

    def get_dto_type(self) -> type[T]:
        """The DTO model the service works with.

        For the list backend the already-modelled object *is* the DTO (v1's "the
        model is the read model"), so this is the served Pydantic model — which
        is what lets a non-defensive read serve the stored object itself. With
        only an explicit ``dto=`` it is the declaration's generated model.
        """
        return cast("type[T]", self._item_type)

    def get_dto_declaration(self) -> type[DTO]:
        """The DTO declaration this resource serves (the escape hatch)."""
        return self._dto

    def get_rest_models(self) -> RestModels:
        """The six REST models derived from the DTO declaration's ``in_*`` flags."""
        return self._dto.get_rest_models()

    def get_id_field(self) -> str:
        """The identifier field name (``id`` unless an explicit ``dto=`` declares another)."""
        return self._dto.id_field_name

    def get_resource_path(self) -> str:
        """The REST path segment: an explicit ``path`` else the DTO name, pluralized."""
        if self._path is not None:
            return self._path.lstrip("/")
        return pluralize(camel_to_kebab(self._dto.__name__)).lower()

    # get_cache_strategy is inherited from DefaultCacheStrategyMixin: a read-only
    # resource gets OptimisticCacheStrategy(expire_in=600, private=True), so no
    # per-backend cache code is needed.

    def get_queryable_fields(self) -> frozenset[str]:
        """Every field the read model exposes — the default query surface.

        Derived from ``read_response``, so a field projected away is not
        filterable: a model that hides ``secret`` must not leave ``?secret__eq=``
        disclosing it.
        """
        return frozenset(self.get_rest_models().read_response.model_fields)

    def get_filter_operators(self) -> Mapping[str, frozenset[str]]:
        """The derived filter surface: each queryable field's allowed operators."""
        fields = self.get_rest_models().read_response.model_fields
        return {name: operators_for_annotation(field.annotation) for name, field in fields.items()}

    def get_search_filter_type(self) -> type[SearchFilter[Any]] | None:
        """A declared object-filter class, or ``None`` (derive from the read model)."""
        return None

    def get_sortable_fields(self) -> frozenset[str]:
        """Every field the read model exposes — the default sort surface.

        Derived from ``read_response`` (the same gate as filtering), so a field
        projected away is not sortable: ``?sort=secret`` would otherwise leak the
        hidden value's relative order.
        """
        return self.get_queryable_fields()

    def get_sort_order_type(self) -> type[SortOrder[Any]] | None:
        """A declared :class:`SortOrder` class, or ``None`` (derive the surface)."""
        return None

    def resolve_sort_order(self, sort: str | None, desc: bool) -> SortOrder[Any] | None:
        """Validate ``sort`` / ``desc`` into the ordering a search will use.

        With no ``sort`` the identifier order is used and ``desc`` is ignored. An
        unknown or non-sortable field raises
        :class:`~resourcey.v2.core.errors.InvalidInputError`.
        """
        if not sort:
            return None
        declared = self.get_sort_order_type()
        if isinstance(declared, type) and issubclass(declared, AttrSortOrder):
            if sort not in self.get_sortable_fields():
                raise InvalidInputError(f"Unknown or non-sortable sort field {sort!r}")
            return declared(attribute=sort, descending=desc)
        if sort not in self.get_sortable_fields():
            raise InvalidInputError(f"Unknown or non-sortable sort field {sort!r}")
        return AttrSortOrder(attribute=sort, descending=desc)

    # ------------------------------------------------------------------
    # Actions / exposure
    # ------------------------------------------------------------------

    def get_supported_actions(self) -> frozenset[Action]:
        """The read-only action subset — no write route is ever mounted.

        A list resource supports exactly ``read`` / ``search`` / ``count`` /
        ``batch_read``. The route builder registers a route only for a supported
        action, so the narrowing is structural, not a runtime guard.
        """
        return _READ_ONLY_ACTIONS

    def get_exposed_resource(self) -> Resource[T, K] | None:
        """The resource the outside world sees (default: ``self``)."""
        return self

    # ------------------------------------------------------------------
    # Service seam
    # ------------------------------------------------------------------

    async def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Service[T, K]:
        """Build a :class:`ListService` over ``ctx`` and the served items.

        A list already seeded on ``ctx`` (under ``STORAGE_KEY``) wins — the same
        call-scoped reuse seam the SQL / Mongo services use; otherwise the
        resource's own items are served. There is no connection to resolve, so
        the async signature only matches the :class:`Resource` contract.
        """
        mapping = ctx if ctx is not None else {}
        items = mapping.get(STORAGE_KEY)
        if items is None:
            items = self._models
        return ListService(self, mapping, items)

    # ------------------------------------------------------------------
    # Registration / lifecycle
    # ------------------------------------------------------------------

    def on_register(self, manifest: Manifest) -> None:
        """Record the manifest that owns this resource."""
        self._manifest = manifest

    def get_manifest(self) -> Manifest | None:
        """The manifest that registered this resource, or ``None``."""
        return self._manifest

    async def __aenter__(self) -> Resource[T, K]:
        """Enter the resource's runtime lifecycle (guards against double entry)."""
        if self._entered:
            raise ServiceError(f"{type(self).__name__} is already entered")
        self._entered = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the resource's runtime lifecycle."""
        self._entered = False

    # ------------------------------------------------------------------
    # Defensive cloning
    # ------------------------------------------------------------------

    def clone_for_output(self, item: Any) -> Any:
        """Deep-copy ``item`` when ``defensive`` so callers cannot edit the store."""
        if not self._defensive or not isinstance(item, BaseModel):
            return item
        return item.model_copy(deep=True)

    # ------------------------------------------------------------------
    # Model surface (escape hatches)
    # ------------------------------------------------------------------

    @property
    def models(self) -> list[Any]:
        """The served items, held by reference (the escape hatch back to the list)."""
        return self._models

    @property
    def model(self) -> type[BaseModel] | None:
        """The served Pydantic model, or ``None`` when only an explicit ``dto=`` was given."""
        return self._model

    @property
    def defensive(self) -> bool:
        """Whether outputs are deep copies of the stored objects."""
        return self._defensive


def _validate_models(name: str, resolved: type[BaseModel], items: list[Any]) -> None:
    """Fail loudly unless every item is an instance of ``resolved``."""
    if not (isinstance(resolved, type) and issubclass(resolved, BaseModel)):
        raise ResourceyConfigError(
            f"{name} model must be a Pydantic BaseModel subclass, got {resolved!r}."
        )
    for item in items:
        if not isinstance(item, resolved):
            raise ResourceyConfigError(
                f"{name} models must all be {resolved.__name__} instances; "
                f"got {type(item).__name__}."
            )
