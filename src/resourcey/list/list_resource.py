"""``ListResource`` — the read-only list backend (issue #116).

A resource built from an application-supplied list of Pydantic models and served
**read-only**, for in-process reference data that is already modelled (country
codes, feature flags, catalog entries). The list *is* the storage, so there is no
table, no migration, and no external dependency::

    countries = [
        Country(id="us", name="United States", iso3="USA"),
        Country(id="ca", name="Canada", iso3="CAN"),
    ]
    resource = ListResource(countries, path="countries")
    manifest = Manifest(resources=(resource,))

The served Pydantic model is projected onto a
:class:`~resourcey.core.dto.DTO` so the six REST models, the query / sort surface,
and the cache policy derive exactly as they do for the SQL / Mongo backends. An
explicit ``dto=`` wins over inference.

Read-only by construction: :meth:`get_supported_actions` returns exactly
``{read, search, count, batch_read}``, so **no write route exists**. By default
the resource is **defensive**: every object it outputs is a deep copy of the
stored object.

The action layer lives in :mod:`resourcey.list.list_service`.

This module imports no code outside the framework.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pydantic import BaseModel

from resourcey.cache.cache_defaults import DefaultCacheStrategyMixin
from resourcey.core.dto import DTO, RestModels
from resourcey.core.errors import InvalidInputError, ResourceyConfigError
from resourcey.core.resource import Resource
from resourcey.core.service import Action, Service, ServiceError, StorageKey, storage_key
from resourcey.encryption.encryption_service import get_encryption_service
from resourcey.list.list_service import ListService
from resourcey.list.pydantic_2_dto import pydantic_2_dto
from resourcey.util.naming import camel_to_kebab, pluralize
from resourcey.util.search_filter import SearchFilter, operators_for_annotation
from resourcey.util.sort_order import AttrSortOrder, SortOrder

if TYPE_CHECKING:
    from resourcey.core.manifest import Manifest
    from resourcey.encryption.encryption_service import EncryptionService

T = TypeVar("T", bound=BaseModel)
K = TypeVar("K")

# The read-only action subset: no create / update / delete.
_READ_ONLY_ACTIONS: frozenset[Action] = frozenset(
    {Action.READ, Action.SEARCH, Action.COUNT, Action.BATCH_READ}
)


class ListResource(DefaultCacheStrategyMixin, Resource[T, K]):
    """The list backend: an in-process list of Pydantic models, served read-only.

    The list is held **by reference**, so mutating it later changes what the
    resource serves. ``model`` overrides inference (required when the list is
    empty) and ``dto`` is the hand-written-projection escape hatch.
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
        # The served model *is* the DTO for this backend, so a non-defensive
        # read can return the stored object itself. An explicit ``dto=`` is a
        # hand-written projection, whose generated model is the item type.
        if dto is not None:
            self._item_type: type[BaseModel] = self._dto.get_dto_type()
        else:
            self._item_type = cast("type[BaseModel]", resolved)
        self._path = path
        self._defensive = defensive
        # Resolved eagerly so cursor pagination works out of the box.
        self._encryption_service = encryption_service or get_encryption_service()
        self._entered = False
        self._manifest: Manifest | None = None

    # ------------------------------------------------------------------
    # DTO / schema surface
    # ------------------------------------------------------------------

    def get_dto_type(self) -> type[T]:
        """The DTO model the service works with.

        For the list backend the served Pydantic model *is* the DTO. With only an
        explicit ``dto=`` it is the declaration's generated model.
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
    # resource gets OptimisticCacheStrategy(expire_in=600, private=True).

    def get_queryable_fields(self) -> frozenset[str]:
        """Every field the read model exposes — the default query surface.

        Derived from ``read_response``, so a field projected away is not
        filterable.
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
        projected away is not sortable.
        """
        return self.get_queryable_fields()

    def get_sort_order_type(self) -> type[SortOrder[Any]] | None:
        """A declared :class:`SortOrder` class, or ``None`` (derive the surface)."""
        return None

    def resolve_sort_order(self, sort: str | None, desc: bool) -> SortOrder[Any] | None:
        """Validate ``sort`` / ``desc`` into the ordering a search will use.

        With no ``sort`` the identifier order is used and ``desc`` is ignored.
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

        The route builder registers a route only for a supported action, so the
        narrowing is structural.
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

        A list already seeded on ``ctx`` *under this resource's own key* wins,
        mirroring the SQL / Mongo services. The served list is the storage
        identity, so two list resources never adopt one another's items.
        """
        mapping = ctx if ctx is not None else {}
        items = mapping.get(storage_key("list", self._models))
        return self.make_service(mapping, self._models if items is None else items)

    async def get_storage_key(self) -> StorageKey:
        """The ctx key a service of this resource stores its items under."""
        return storage_key("list", self._models)

    def make_service(self, ctx: MutableMapping[Any, Any], items: list[Any]) -> Service[T, K]:
        """Build the service for this resource — the subclass seam.

        A variant adding an extra method (e.g. the auth config-key resource's
        ``find_by_key``) overrides this rather than :meth:`get_service`.
        """
        return ListService(self, ctx, items)

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
