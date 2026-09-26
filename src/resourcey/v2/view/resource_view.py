"""``ResourceView`` - a configured, narrowing wrapper over another resource.

A view holds an inner :class:`~resourcey.v2.core.resource.Resource` and exposes
a **customized view** of it: a field-projection override (which fields appear in
each of the six REST shapes) and an action override (which actions the outside
world may reach). It is the ``v2`` successor to ``v1``'s
``WrapperResourceBase`` (issue #62), but *configuration-driven* rather than
subclass-driven, so a projection can be built as an instance::

    public_secrets = ResourceView(
        resource=secrets,
        exposed_field_overrides={
            "value": {
                "in_read_response": False,
                "in_search_response": False,
                "in_update_request": False,
                "in_update_response": False,
            },
        },
        exposed_actions=frozenset(Action) - {Action.UPDATE},
    )

It is the general-purpose **least-privilege** tool: a resource is declared once
with its full storage truth, and the public surface is narrowed without touching
the storage class.

What the view recomputes (and why)
----------------------------------
The premise "delegate everything except ``get_exposed_resource``" is *almost*
right: field overrides force the whole exposed surface to be recomputed, because
the query surface and the cache policy derive from the DTO / REST models and the
action set.

* **Field projection** - the DTO is re-derived from the inner declaration via
  :func:`~resourcey.v2.core.dto.derive_dto`, so the six REST models are the
  view's.
* **Query surface** - :meth:`get_queryable_fields`, :meth:`get_filter_operators`,
  :meth:`get_sortable_fields`, and :meth:`resolve_sort_order` are recomputed from
  the view's read model. This is load-bearing, not cosmetic: delegating them to
  the inner would let ``?secret__eq=`` / ``?sort=secret`` pass the transport's
  validation and reach the inner service, which pushes them down against the
  inner's (wider) column set - leaking the hidden value or its relative order.
* **Cache policy** - recomputed when the view narrows, so a view whose actions
  become read-only selects the read-only (optimistic, ``private``) strategy
  instead of inheriting a writable inner's shared-cacheable validator.
* **Actions** - normalized (:func:`~resourcey.v2.core.service.normalize_actions`)
  so a batch action cannot outlive its singular action.

A view may **narrow**, never widen: ``exposed_actions`` must be a subset of the
inner's, and an override cannot re-widen a flag the inner had turned off (the
override is merged onto the inner field, not substituted for it). Both are
validated at construction.

Registration
------------
Register the **view** in the manifest, not the inner: ``register_routes`` sees
``view.get_exposed_resource() is view`` and mounts the view's routes, while
:meth:`ResourceView.__aenter__` delegates to the inner so its lifecycle (e.g.
``MongoResource.ensure_indexes()``) still runs. Registering both would
double-enter the inner and mount duplicate routes.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from pydantic import BaseModel

from resourcey.v2.cache.cache_defaults import default_cache_strategy
from resourcey.v2.core.dto import DTO, RestModels, derive_dto
from resourcey.v2.core.errors import InvalidInputError, ResourceyConfigError
from resourcey.v2.core.resource import Resource
from resourcey.v2.core.service import (
    Action,
    CacheStrategy,
    Service,
    ServiceError,
    normalize_actions,
)
from resourcey.v2.util.search_filter import SearchFilter, operators_for_annotation
from resourcey.v2.util.sort_order import AttrSortOrder, SortOrder
from resourcey.v2.view.view_service import ViewService

if TYPE_CHECKING:
    from resourcey.v2.core.manifest import Manifest

T = TypeVar("T", bound=BaseModel)
K = TypeVar("K")


def _dto_declaration(resource: Resource[Any, Any]) -> type[DTO]:
    """The inner resource's DTO declaration, through its escape hatch.

    Every ``v2`` backend that derives its REST models from a DTO exposes
    ``get_dto_declaration()``. A resource that does not (a hand-written
    ``Resource``) cannot be viewed, which is reported clearly.
    """
    getter = getattr(resource, "get_dto_declaration", None)
    if getter is None:
        raise ResourceyConfigError(
            f"{type(resource).__name__} has no get_dto_declaration(); ResourceView can only "
            "wrap a resource whose REST models derive from a DTO declaration."
        )
    return cast("type[DTO]", getter())


class ResourceView(Resource[T, K], Generic[T, K]):
    """A configured wrapper exposing a narrowed view of ``resource``.

    Args:
        resource: The inner resource to view. Its DTO declaration drives the
            derived models; its storage backs the service.
        exposed_field_overrides: ``{field_name: {DtoField attribute: value}}``.
            Each value is a *partial* mapping merged onto the inner field's
            resolved :class:`~resourcey.v2.core.dto.DtoField`, so unmentioned
            flags keep the inner's value and an override can never silently
            re-widen a field. The identifier may not be overridden.
        exposed_actions: The actions the view exposes. Defaults to the inner's
            (normalized). Must be a subset of the inner's - a view narrows,
            never widens - and is normalized so a batch action without its
            singular action is dropped.
        path: An explicit REST path segment (defaults to the inner's).
        cache_strategy: An explicit cache policy - the escape hatch. When
            omitted, the policy is recomputed from the view's models and actions
            when the view narrows, and otherwise delegated to the inner.
    """

    def __init__(
        self,
        resource: Resource[T, K],
        *,
        exposed_field_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        exposed_actions: frozenset[Action] | None = None,
        path: str | None = None,
        cache_strategy: CacheStrategy | None = None,
    ) -> None:
        self._inner = resource
        self._path = path
        self._field_overrides = {
            name: dict(overrides) for name, overrides in (exposed_field_overrides or {}).items()
        }
        self._explicit_cache_strategy = cache_strategy
        self._manifest: Manifest | None = None
        self._entered = False

        inner_dto = _dto_declaration(resource)
        self._validate_field_overrides(inner_dto)
        self._validate_no_rewidening(inner_dto)
        self._dto = (
            derive_dto(inner_dto, field_overrides=self._field_overrides)
            if self._field_overrides
            else inner_dto
        )
        self._validate_declared_filter()

        inner_actions = normalize_actions(resource.get_supported_actions())
        if exposed_actions is None:
            self._actions = inner_actions
        else:
            self._actions = normalize_actions(frozenset(exposed_actions))
            widened = self._actions - inner_actions
            if widened:
                raise ResourceyConfigError(
                    f"{type(self).__name__} cannot expose actions the inner resource does not "
                    f"support: {sorted(a.value for a in widened)}"
                )

    # ------------------------------------------------------------------
    # Construction validation
    # ------------------------------------------------------------------

    def _validate_field_overrides(self, inner_dto: type[DTO]) -> None:
        """Reject an unknown field or an override of the identifier."""
        unknown = sorted(set(self._field_overrides) - set(inner_dto.get_fields()))
        if unknown:
            raise ResourceyConfigError(
                f"{type(self).__name__} got overrides for unknown field(s) {unknown}; the inner "
                f"DTO declares {sorted(inner_dto.get_fields())}"
            )
        if inner_dto.id_field_name in self._field_overrides:
            raise ResourceyConfigError(
                f"{type(self).__name__} cannot override the identifier field "
                f"{inner_dto.id_field_name!r}: the transport needs a readable identifier."
            )

    def _validate_no_rewidening(self, inner_dto: type[DTO]) -> None:
        """Reject an override that turns an inner-``False`` projection flag back on.

        A view **narrows**, never widens — the same rule ``exposed_actions``
        enforces. ``derive_dto`` merges an override onto the inner field, so an
        override naming a flag the inner had turned off would otherwise
        re-expose a field (or operation) the inner deliberately hid.
        """
        inner_fields = inner_dto.get_fields()
        for field_name, override in self._field_overrides.items():
            inner_field = inner_fields[field_name]
            for flag, value in override.items():
                if (
                    flag.startswith("in_")
                    and value is True
                    and getattr(inner_field, flag, True) is False
                ):
                    raise ResourceyConfigError(
                        f"{type(self).__name__} cannot re-widen {field_name!r}.{flag}: the inner "
                        "field turned it off and a view narrows, never widens."
                    )

    def _validate_declared_filter(self) -> None:
        """Fail loudly when field hiding meets a declared object-filter class.

        The transport builds a *declared* filter's surface from the class's own
        ``<attribute>__<op>`` fields, which still name the inner's full field
        set. Hiding a field from the read model without also narrowing that class
        would leave the hidden field filterable, so the combination is refused
        rather than silently leaking. (Widening the read model is fine: the
        declared filter already names every field.)
        """
        if not self._hides_read_fields():
            return
        if self._inner.get_search_filter_type() is not None:
            raise ResourceyConfigError(
                f"{type(self).__name__} hides fields from the read model, but the inner resource "
                "declares a get_search_filter_type(); a declared object filter still names every "
                "inner field, so a hidden field would remain filterable. Drop the declared "
                "filter (use the derived surface) or do not hide fields."
            )

    def _hides_read_fields(self) -> bool:
        """Whether the view's read model omits a field the inner's exposes."""
        inner_read = set(self._inner.get_rest_models().read_response.model_fields)
        return bool(inner_read - set(self.get_rest_models().read_response.model_fields))

    # ------------------------------------------------------------------
    # DTO / schema surface (the view's, not the inner's)
    # ------------------------------------------------------------------

    def get_dto_type(self) -> type[T]:
        """The view's DTO model (the derived declaration's generated model)."""
        return cast("type[T]", self._dto.get_dto_type())

    def get_dto_declaration(self) -> type[DTO]:
        """The view's DTO declaration (the derived one, not the inner's)."""
        return self._dto

    def get_rest_models(self) -> RestModels:
        """The six REST models derived from the view's DTO declaration."""
        return self._dto.get_rest_models()

    def get_id_field(self) -> str:
        """The identifier field name (delegated; the id is never overridden)."""
        return self._dto.id_field_name

    def get_resource_path(self) -> str:
        """The REST path segment: an explicit ``path`` else the inner's."""
        if self._path is not None:
            return self._path.lstrip("/")
        return self._inner.get_resource_path()

    # ------------------------------------------------------------------
    # Query / sort surface (recomputed from the view's read model)
    # ------------------------------------------------------------------

    def get_queryable_fields(self) -> frozenset[str]:
        """Every field the view's read model exposes - the query surface.

        Derived from the view's ``read_response``, so a field the view hides is
        not filterable or sortable: the read model *is* the query surface.
        """
        return frozenset(self.get_rest_models().read_response.model_fields)

    def get_filter_operators(self) -> Mapping[str, frozenset[str]]:
        """The derived filter surface: each queryable field's allowed operators."""
        fields = self.get_rest_models().read_response.model_fields
        return {name: operators_for_annotation(field.annotation) for name, field in fields.items()}

    def get_search_filter_type(self) -> type[SearchFilter[Any]] | None:
        """The inner's declared object filter (validated at construction).

        A declared filter is only carried across when the view does not narrow
        the read model; :meth:`_validate_declared_filter` refuses the unsafe
        combination at construction.
        """
        return self._inner.get_search_filter_type()

    def get_sortable_fields(self) -> frozenset[str]:
        """Every field the view's read model exposes - the sort surface."""
        return self.get_queryable_fields()

    def get_sort_order_type(self) -> type[SortOrder[Any]] | None:
        """The inner's declared sort order, or ``None`` to derive the surface."""
        return self._inner.get_sort_order_type()

    def resolve_sort_order(self, sort: str | None, desc: bool) -> SortOrder[Any] | None:
        """Validate ``sort`` / ``desc`` against the *view's* sortable fields.

        Re-implemented rather than delegated: the inner's own validation is
        against the inner's (wider) sortable set, so delegating would let
        ``?sort=secret`` through. An unknown or non-sortable field raises
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
        """The view's normalized action set."""
        return self._actions

    def get_exposed_resource(self) -> Resource[T, K]:
        """The view is what the outside world sees (never delegates to the inner)."""
        return self

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def get_cache_strategy(self) -> CacheStrategy | None:
        """The view's cache policy.

        An explicit ``cache_strategy`` wins; otherwise the policy is recomputed
        from the view's models and actions when the view narrows (so a view that
        becomes read-only selects the optimistic, ``private`` strategy), and
        delegated to the inner when the view is a pure pass-through.
        """
        if self._explicit_cache_strategy is not None:
            return self._explicit_cache_strategy
        if self._narrows():
            return default_cache_strategy(self.get_rest_models(), self._actions)
        return self._inner.get_cache_strategy()

    def _narrows(self) -> bool:
        """Whether the view changes fields or actions relative to the inner."""
        actions_differ = self._actions != normalize_actions(self._inner.get_supported_actions())
        return bool(self._field_overrides) or actions_differ

    # ------------------------------------------------------------------
    # Service seam
    # ------------------------------------------------------------------

    async def get_service(self, ctx: MutableMapping[Any, Any] | None = None) -> Service[T, K]:
        """The inner service, wrapped so the view's action set is enforced.

        The inner service reads/writes the inner storage and shapes results
        through the inner DTO; the transport projects those onto the view's REST
        models. The wrapper re-checks ``batch_edit`` against the view's actions,
        so a direct service caller cannot reach a batch kind the view hides.
        """
        inner = await self._inner.get_service(ctx)
        return ViewService(inner, self._actions)

    # ------------------------------------------------------------------
    # Registration / lifecycle (delegated to the inner)
    # ------------------------------------------------------------------

    def on_register(self, manifest: Manifest) -> None:
        """Record the manifest and forward registration to the inner resource."""
        self._manifest = manifest
        self._inner.on_register(manifest)

    def get_manifest(self) -> Manifest | None:
        """The manifest that registered this view, or ``None``."""
        return self._manifest

    async def __aenter__(self) -> Resource[T, K]:
        """Enter the view's lifecycle (guards against double entry)."""
        if self._entered:
            raise ServiceError(f"{type(self).__name__} is already entered")
        self._entered = True
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the view's lifecycle, forwarding to the inner resource."""
        await self._inner.__aexit__(*exc)
        self._entered = False
