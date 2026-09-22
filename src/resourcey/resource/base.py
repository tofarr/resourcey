"""``BaseResource`` and the storage-agnostic resource declaration layer.

A resource is the central unit of resourcey. ``BaseResource`` is a plain
extension point (not a Pydantic model): a subclass declares fields with the
ordinary annotation +
``Field()`` / default syntax, and the framework introspects that declaration
to drive the generated Pydantic create / read / update models. Every
generation step is a single-purpose, overridable *instance* method so a
subtype (or a :class:`~resourcey.resource.wrapper.WrapperResourceBase`) can
replace any piece without touching the rest (progressive enhancement / escape
hatches).

``BaseResource`` is deliberately storage-agnostic: it knows nothing about
SQLAlchemy, sessions, or persistence. SQL-backed resources subclass
:class:`~resourcey.resource.sql.SqlResource`, which adds the ORM model
generation on top of this base. Non-SQL backends can subclass
``BaseResource`` directly.

Field metadata is collected at subclass-creation time into
:attr:`BaseResource.model_fields` -- a mapping of name to Pydantic
:class:`~pydantic.fields.FieldInfo`, the same type the generation hooks
already consume. Building it ourselves (rather than inheriting it from
``BaseModel``) is what lets ``BaseResource`` be a declaration rather than a
data record: it is never validated, and carries no Pydantic model machinery.

Generated models and resolved values are cached on the class so repeated calls
are cheap. The caches are non-annotated class attributes (so they are not
treated as declared fields) and are stored per-subclass.

Exposure is decided by :meth:`BaseResource.get_exposed_resource` — it returns
the resource the outside world sees (default ``self``; ``None`` means
internal-only) and is the single gate on REST presence (issue #62).

All generation hooks are **instance methods** (not classmethods). This lets a
wrapper hold a reference to an inner resource and selectively override
individual hooks while delegating the rest (the composition pattern of choice
for customisation — see :class:`~resourcey.resource.wrapper.WrapperResourceBase`).
Caching stays per-class via ``type(self).__dict__`` so non-wrapper resources
behave exactly as before; a wrapper overrides caching to be per-instance.
"""

from __future__ import annotations

import types
from abc import ABC
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import reduce
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    ClassVar,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from fastapi import Request
from pydantic import BaseModel, Field, SecretStr, create_model, field_serializer, field_validator
from pydantic.fields import FieldInfo

from resourcey.app_context import AppContext
from resourcey.resource.errors import ResourceyConfigError
from resourcey.resource.field import ResourceyField
from resourcey.resource.missing import MISSING
from resourcey.resource.service_base import Action, BaseService
from resourcey.util.naming import camel_to_kebab, pluralize
from resourcey.util.secret_serialization import dump_secret_str, load_secret_str

if TYPE_CHECKING:
    # Used only in annotations. Kept under TYPE_CHECKING (with
    # ``from __future__ import annotations``) so the ``resource`` package
    # never imports ``util.search_filter`` / ``cache`` at runtime, avoiding a
    # potential import cycle.
    from resourcey.cache.cache_strategy import CacheStrategy
    from resourcey.util.search_filter import SearchFilter


class BaseResource(ABC):
    """Base class for resource declarations.

    A *declaration* class, not a data model: subclass it and declare fields
    with the ordinary annotation + ``Field()`` / default syntax, and the
    framework derives the create / read / update Pydantic models from that
    single declaration. Each public generation method is an overridable hook.

    Subclasses :class:`~abc.ABC`, but no method is left abstract: the
    storage-specific hooks (:meth:`build_service`, ``get_orm_model``) raise at
    call time instead, so a subclass that implements only the parts it needs
    still instantiates and fails with a clear error if a missing hook is used.

    ``BaseResource`` is storage-agnostic - it does not know about SQLAlchemy,
    sessions, or persistence. SQL-backed resources subclass
    :class:`~resourcey.resource.sql.SqlResource` for ORM model generation.

    ``BaseResource`` is intentionally not a Pydantic ``BaseModel``. Field
    metadata is collected into :attr:`model_fields` at subclass-creation time
    by :meth:`__init_subclass__`, so the generation hooks introspect a
    registry the framework owns rather than Pydantic's model machinery.

    All hooks are instance methods (not classmethods) so that a
    :class:`~resourcey.resource.wrapper.WrapperResourceBase` can hold a
    reference to an inner resource and selectively override individual hooks
    while delegating the rest. Caching stays per-class via
    ``type(self).__dict__``; a wrapper overrides caching to be per-instance.
    """

    # Per-subclass field registry and caches. Non-annotated so they are not
    # treated as declared fields; stored on each subclass's own ``__dict__``
    # so subclasses don't share them.
    model_fields: dict[str, FieldInfo]
    _id_field: str
    _sortable_fields: list[str]
    _create_model: type[BaseModel]
    _read_model: type[BaseModel]
    _update_model: type[BaseModel]
    _cache_strategy: Any
    _ctx: Any

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # WrapperResourceBase proxies model_fields to its inner resource via
        # a property; skip field collection so it doesn't shadow the property.
        # Detected via a class-level marker (``_is_wrapper_base = True``) to
        # avoid an import cycle (wrapper.py imports base.py).
        if cls.__dict__.get("_is_wrapper_base") or any(
            getattr(b, "__dict__", {}).get("_is_wrapper_base") for b in cls.__mro__
        ):
            return
        cls.model_fields = _collect_field_infos(cls)

    # ------------------------------------------------------------------
    # Registration hook
    # ------------------------------------------------------------------

    def on_register(self) -> None:  # noqa: B027
        """Materialise backend artifacts (called by the manifest at construction).

        The base implementation is a no-op: a storage-agnostic resource has
        nothing to materialise. Storage-specific subclasses (e.g.
        :class:`~resourcey.resource.sql.SqlResource`) override this to eagerly
        build their backing model so it is available before migrations / table
        creation run.
        """
        # no-op: storage-agnostic resources have nothing to materialise.

    # ------------------------------------------------------------------
    # App lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self, ctx: AppContext) -> AppContext:
        """Enter runtime lifecycle: build/cache backend state.

        Called by :class:`~resourcey.manifest.ResourceManifest.__aenter__`
        with the shared :class:`~resourcey.app_context.AppContext`. A
        storage-agnostic resource has nothing to build; subclasses override
        to build connection pools and register disposal.
        """
        self._ctx = ctx
        return ctx

    async def __aexit__(self, *exc: object) -> None:  # noqa: B027
        """Tear down runtime state. Base implementation is a no-op."""
        # no-op: storage-agnostic resources have nothing to tear down.

    # ------------------------------------------------------------------
    # Exposure control
    # ------------------------------------------------------------------

    def get_exposed_resource(self) -> BaseResource | None:
        """Which resource the outside world sees (default: ``self``).

        This is the **single** gate on REST presence (issue #62):

        * ``self`` (the default) — the resource is served as declared.
        * a *different* resource (typically a
          :class:`~resourcey.resource.wrapper.WrapperResourceBase`) — that
          resource is served instead: it drives the schemas, the service, and
          the route set. This is how "the outside world sees this projection"
          is expressed on the resource itself, so an internal resource with a
          write-only secret no longer needs a second external declaration.
        * ``None`` — the resource is internal-only: the route builder registers
          nothing (the old ``is_exposed() is False`` case).

        Only ``None`` suppresses routes; a
        :class:`~resourcey.config.config_dependency.DependencyBuilder` can never
        re-expose a resource that hid itself. A wrapper does **not** override
        this (it returns ``self`` — see
        :class:`~resourcey.resource.wrapper.WrapperResourceBase`).
        """
        return self

    # ------------------------------------------------------------------
    # Service + action surface
    # ------------------------------------------------------------------

    @property
    def actions(self) -> frozenset[Action]:
        """The actions this resource supports (exposed over HTTP by default).

        The capability declaration: every action the underlying service can
        fulfill. A resource (or a wrapper) *narrows* by overriding
        :meth:`get_supported_actions`; it must never widen beyond ``actions``,
        which the route builder asserts at registration time.
        """
        return frozenset(Action)

    def get_supported_actions(self) -> frozenset[Any]:
        """The subset of :attr:`actions` this resource exposes over HTTP.

        Defaults to :attr:`actions` (expose everything the service can do).
        Override to *narrow* (hide an action) — never to widen: the route
        builder asserts ``supported ⊆ actions`` at registration time. Combined
        with :meth:`get_exposed_resource`, this gives two levels of exposure
        control: resource-level (in REST at all?) and action-level (which
        actions?).

        Returns a ``frozenset[Action]``.
        """
        return self.actions

    def get_orm_model(self) -> Any:
        """The ORM model backing this resource (instance seam, issue #62).

        Defaults to ``type(self).get_sql_alchemy_model()`` for storage-backed
        resources that expose that classmethod. Kept as an instance method (not
        a classmethod) so a wrapper can delegate it to its *inner* instance —
        the ORM model of a wrapper is the inner resource's table, which is what
        lets a wrapper back a service and have field hiding take effect on the
        response body, not merely the OpenAPI schema.
        """
        getter = getattr(type(self), "get_sql_alchemy_model", None)
        if getter is None:
            raise ResourceyConfigError(
                f"{type(self).__name__} has no SQLAlchemy model; get_orm_model() is only "
                "available on storage-backed resources (or a wrapper delegating to one)."
            )
        return getter()

    def migrate_document(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Lazily upgrade a stored document to the current shape on read (default no-op).

        Only Mongo-backed resources have documents; declared on the base (rather
        than only on :class:`~resourcey.mongo.mongo_resource.MongoResource`) so
        a wrapper delegating to a Mongo resource can back a ``MongoService``
        without the service knowing whether it holds a resource or a wrapper.

        Invoked by :meth:`MongoService._doc_to_read_model` before projecting
        a document into the read model. The default returns the document
        unchanged. An application overrides this to coordinate schema upgrades
        - most implementations carry a schema-version number on each document
        and upgrade in place, but the framework does not prescribe the
        versioning scheme, the upgrade function signatures, or the storage of
        the version field. Returning a new dict (rather than mutating) is
        safe and keeps the stored document untouched unless the override
        writes back.
        """
        return doc

    @asynccontextmanager
    async def open_storage(self, request: Request) -> AsyncIterator[Any]:
        """Yield this resource's per-request storage handle (default: ``None``).

        The storage half of the service seam: a SQL resource yields an
        ``AsyncSession``, a Mongo resource a collection. The base resource is
        storage-agnostic, so it yields ``None``. A
        :class:`~resourcey.resource.wrapper.WrapperResourceBase` delegates this
        to its inner resource so it reuses (and commits) the same storage.
        """
        yield None

    def build_service(self, resource: BaseResource, storage: Any) -> BaseService:
        """Build the service for ``resource`` over ``storage``.

        The service half of the seam. ``resource`` is passed explicitly (rather
        than read from ``self``) so a wrapper can ask its inner resource for a
        service *bound to the wrapper*, whose read model is the wrapper's
        projection — see
        :class:`~resourcey.resource.wrapper.WrapperResourceBase`. Storage
        subclasses override this (e.g.
        :class:`~resourcey.resource.sql.SqlResource` returns
        :class:`~resourcey.resource.service.SqlService`).
        """
        raise ResourceyConfigError(
            f"{type(self).__name__} does not implement build_service(); "
            "only storage-backed resources can open a service."
        )

    async def get_service_dependency(self, request: Request) -> AsyncIterator[Any]:
        """Yield the per-request service instance bound to this resource.

        The single per-request seam the route builder consumes, via the
        configured
        :class:`~resourcey.config.config_dependency.DependencyBuilder`. A bound
        async-generator method works directly as a FastAPI dependency, so this
        may be passed straight to ``Depends(...)``.
        """
        async with self.open_storage(request) as storage:
            yield self.build_service(self, storage)

    # ------------------------------------------------------------------
    # Field config resolution
    # ------------------------------------------------------------------

    def get_config_for_field(self, field_name: str, field: FieldInfo) -> ResourceyField:
        """Return the ``ResourceyField`` for a field.

        Reads it from the field metadata if an explicit ``ResourceyField`` is
        attached via ``Annotated``; otherwise builds a default config. Then
        applies the id / timestamp / secret conventions.

        The ``SecretStr`` default-off convention (``sortable`` -> ``False``)
        is applied only when no explicit ``ResourceyField`` is attached: a
        developer who wants a secret sortable must attach an explicit
        ``ResourceyField(sortable=True)`` -- a deliberate, visible opt-out
        of the safe default. (Filtering is gated per-resource by a declared
        search filter class, not by a per-field flag; see
        :meth:`get_search_filter_type`.)
        """
        explicit = False
        for meta in field.metadata:
            if isinstance(meta, ResourceyField):
                config = meta
                explicit = True
                break
        else:
            config = ResourceyField()

        if field_name == "id":
            # The DB / resource owns the id; REST methods pass the id separately
            # from the model body, so it is neither creatable nor updatable.
            config = config.model_copy(update={"creatable": False, "updatable": False})
        elif field_name in ("created_at", "updated_at"):
            if field.default_factory is not None:
                config = config.model_copy(update={"creatable": False, "updatable": False})
            else:
                raise ResourceyConfigError(
                    f"Field '{field_name}' on {type(self).__name__} is a timestamp and must "
                    "declare a default_factory (e.g. default_factory=datetime.utcnow). A fixed "
                    "default or no default is ambiguous; supply an explicit ResourceyField "
                    "override if intended."
                )
        # SecretStr fields are not sortable by default: allowing `sort=field`
        # against a secret lets a client infer the relative ordering of secret
        # values even when the field is excluded from the read model. Skipped
        # when an explicit ResourceyField is attached so a developer can
        # deliberately opt a secret into sortability.
        if not explicit and _resolve_scalar_type(field.annotation) is SecretStr:
            config = config.model_copy(update={"sortable": False})
        return config

    def get_search_filter_type(self) -> type[SearchFilter] | None:  # type: ignore[type-arg]
        """Return the declared search filter class for this resource, or ``None``.

        When ``None`` (the default) the ``search`` action exposes **no**
        filtering: any ``field__op=value`` query parameter is rejected with
        ``400 invalid_input``; the endpoint serves pure pagination + sort
        only. Filtering is opt-in per resource: a developer declares a
        :class:`~resourcey.util.search_filter.SearchFilter` subclass (typically
        a ``BaseSearchFilter[<SqlAlchemyModel>]`` whose ``<attr>__<op>`` fields
        name exactly the filterable fields/operators) and returns its type
        here. The declared class is the single source of truth for what is
        filterable -- it doubles as the validation schema for the incoming
        query params and supplies the SQL ``WHERE`` via ``filter_sql``.

        Overridable.
        """
        return None

    def get_cache_strategy(self) -> CacheStrategy[Any]:
        """Return the cache strategy for this resource (cached on the class).

        Default selection: if the resource declares an ``updated_at`` field
        and it is readable on the read model, return
        :class:`~resourcey.cache.cache_strategy.LastModifiedCacheStrategy`;
        otherwise return
        :class:`~resourcey.cache.cache_strategy.ETagCacheStrategy`. Both
        default to ``expire_in=0``.

        Overridable: a developer returns any ``CacheStrategy`` instance (e.g.
        ``OptimisticCacheStrategy(expire_in=60)``) to change the policy. This
        is the single seam for cache policy — overriding it never touches the
        service or routes.
        """
        cached = type(self).__dict__.get("_cache_strategy")
        if cached is not None:
            return cast("CacheStrategy[Any]", cached)
        from resourcey.cache.cache_strategy import (
            ETagCacheStrategy,
            LastModifiedCacheStrategy,
        )

        field = self.model_fields.get("updated_at")
        if field is not None and self.get_config_for_field("updated_at", field).readable:
            strategy: CacheStrategy[Any] = LastModifiedCacheStrategy()
        else:
            strategy = ETagCacheStrategy()
        type(self)._cache_strategy = strategy
        return strategy

    def get_queryable_fields(self) -> frozenset[str]:
        """Field names the outside world may filter / sort on (default: all).

        The single gate on the *query* surface (issue #62): ``sort=`` and
        ``field__op=`` query params naming a field outside this set are
        rejected. Defaults to the read model's fields, so a resource that does
        not project (and hides nothing) keeps its existing surface.

        Exists because hiding a field from the read model must also remove it
        from the query surface: a filterable or sortable hidden field leaks
        its value (``?secret__eq=x``) or its relative order (``?sort=secret``)
        even though it never appears in a response body. A wrapper that
        projects the read model narrows this to the surviving fields by
        default (see :class:`~resourcey.resource.wrapper.WrapperResourceBase`).

        Derived from the read model, so a field the resource marks
        ``readable=False`` is non-queryable here too, not only under a wrapper.
        """
        return frozenset(self.get_read_model().model_fields)

    def get_sortable_fields(self) -> list[str]:
        """Names of fields whose ``ResourceyField.sortable`` is ``True``.

        The single source of truth for what the search endpoint's ``sort``
        enum may contain. Cached on the class. A field is sortable unless it
        is explicitly opted out (e.g. ``SecretStr`` fields default to
        ``sortable=False`` -- see :meth:`get_config_for_field`) or is outside
        :meth:`get_queryable_fields` (a projected-away field).
        """
        cached = type(self).__dict__.get("_sortable_fields")
        if cached is not None:
            return cast(list[str], cached)
        queryable = self.get_queryable_fields()
        sortable = [
            name
            for name, field in self.model_fields.items()
            if name in queryable and self.get_config_for_field(name, field).sortable
        ]
        type(self)._sortable_fields = sortable
        return sortable

    def get_id_field(self) -> str:
        """Return the name of the primary identifier field (default: ``id``).

        Cached on the class. Raises ``ResourceyConfigError`` if no ``id``
        field exists.
        """
        cached = type(self).__dict__.get("_id_field")
        if cached is not None:
            return cast(str, cached)
        if "id" in self.model_fields:
            type(self)._id_field = "id"
            return "id"
        raise ResourceyConfigError(
            f"Resource {type(self).__name__} has no 'id' field; override get_id_field() "
            "to specify one."
        )

    # ------------------------------------------------------------------
    # Pydantic model generation
    # ------------------------------------------------------------------

    def get_create_model(self) -> type[BaseModel]:
        """Build (and cache) the create model: only ``creatable`` fields.

        Required fields (no original default) stay required. Optional fields
        get ``Field(default=MISSING, validate_default=False)`` so the service
        can tell whether a value was explicitly supplied. Secret-bearing
        (``SecretStr``) fields gain the ``dump_secret_str`` serializer and
        ``load_secret_str`` validator so encryption / redaction happens at the
        storage boundary driven by the serialization context.
        """
        cached = type(self).__dict__.get("_create_model")
        if cached is not None:
            return cast(type[BaseModel], cached)
        fields: dict[str, Any] = {}
        secret_names: set[str] = set()
        for name, field in self.model_fields.items():
            config = self.get_config_for_field(name, field)
            if not config.creatable:
                continue
            annotation = _strip_config(field.annotation)
            if field.is_required():
                fields[name] = (annotation, _clean_field(field))
            else:
                fields[name] = (annotation, _missing_field())
            if _resolve_scalar_type(field.annotation) is SecretStr:
                secret_names.add(name)
        model = create_model(
            f"{type(self).__name__}Create",
            __base__=_secret_base(secret_names),
            **fields,
        )
        type(self)._create_model = model
        return model

    def get_read_model(self) -> type[BaseModel]:
        """Build (and cache) the read model: only ``readable`` fields.

        Secret-bearing (``SecretStr``) fields gain the ``dump_secret_str``
        serializer and ``load_secret_str`` validator so encryption / redaction
        happens at the storage boundary driven by the serialization context.
        """
        cached = type(self).__dict__.get("_read_model")
        if cached is not None:
            return cast(type[BaseModel], cached)
        model = _project_read_model(
            f"{type(self).__name__}Read",
            self.model_fields,
            self,
        )
        type(self)._read_model = model
        return model

    def get_update_model(self) -> type[BaseModel]:
        """Build (and cache) the PATCH-style update model: only ``updatable`` fields.

        Every field is optional: each gets
        ``Field(default=MISSING, validate_default=False)`` (original
        default / default_factory dropped) so omitted fields are
        distinguishable from explicitly-supplied ones. Secret-bearing
        (``SecretStr``) fields gain the ``dump_secret_str`` serializer and
        ``load_secret_str`` validator so encryption / redaction happens at the
        storage boundary driven by the serialization context.
        """
        cached = type(self).__dict__.get("_update_model")
        if cached is not None:
            return cast(type[BaseModel], cached)
        fields: dict[str, Any] = {}
        secret_names: set[str] = set()
        for name, field in self.model_fields.items():
            config = self.get_config_for_field(name, field)
            if not config.updatable:
                continue
            fields[name] = (_strip_config(field.annotation), _missing_field())
            if _resolve_scalar_type(field.annotation) is SecretStr:
                secret_names.add(name)
        model = create_model(
            f"{type(self).__name__}Update",
            __base__=_secret_base(secret_names),
            **fields,
        )
        type(self)._update_model = model
        return model

    # ------------------------------------------------------------------
    # REST path
    # ------------------------------------------------------------------

    def get_resource_path(self) -> str:
        """Derive the plural, lower-case, kebab-case REST path segment.

        Independent of the SQL table name (see
        :meth:`~resourcey.resource.sql.SqlResource.get_table_name`) so an
        override of one never silently changes the other: the SQL table name
        and the URL path are separate concerns and may legitimately diverge.
        Defaults to the plural kebab-case class name (``UserRole`` ->
        ``user-roles``). Overridable.
        """
        return pluralize(camel_to_kebab(type(self).__name__)).lower()


def _missing_field() -> Any:
    """A field defaulting to ``MISSING`` without validating the sentinel."""
    return Field(default=MISSING, validate_default=False)


def _project_read_model(
    name: str,
    model_fields: dict[str, FieldInfo],
    resource: BaseResource,
    *,
    exclude: frozenset[str] = frozenset(),
) -> type[BaseModel]:
    """Build a read model from ``model_fields``, omitting ``exclude`` names.

    Used by :meth:`BaseResource.get_read_model` (with ``exclude=()``) and by
    :class:`~resourcey.resource.wrapper.WrapperResourceBase` to produce a
    read model that subtracts attributes from the inner resource's read model.
    """
    fields: dict[str, Any] = {}
    secret_names: set[str] = set()
    for field_name, field in model_fields.items():
        if field_name in exclude:
            continue
        config = resource.get_config_for_field(field_name, field)
        if not config.readable:
            continue
        fields[field_name] = (_strip_config(field.annotation), _clean_field(field))
        if _resolve_scalar_type(field.annotation) is SecretStr:
            secret_names.add(field_name)
    return create_model(name, __base__=_secret_base(secret_names), **fields)


def _collect_field_infos(cls: type[BaseResource]) -> dict[str, FieldInfo]:
    """Build ``model_fields`` for a ``BaseResource`` subclass from its declaration.

    ``BaseResource`` is a plain class, so it has no Pydantic-generated
    ``model_fields``. This walks the subclass's annotations (plus inherited
    ones, so a resource may derive from another resource) in declaration
    order and turns each declared field into a Pydantic :class:`FieldInfo` --
    the same type the generation hooks consume -- honouring class-level
    ``Field()`` / default values and ``Annotated[..., ResourceyField(...)]``
    metadata.

    String annotations (from ``from __future__ import annotations``) are
    resolved to real types via :func:`typing.get_type_hints`. Framework
    infrastructure attributes declared on ``BaseResource`` itself
    (:data:`_INFRA_ATTRS`) are excluded so they are never treated as fields.

    A field with no class attribute is required; one with a value (a plain
    default or a ``Field(...)``) is optional and the value supplies the
    default / default_factory / metadata.
    """
    resolved = _resolve_annotations(cls)
    fields: dict[str, FieldInfo] = {}
    for name, annotation in _ordered_annotations(cls):
        if name in fields:
            continue
        # ``ClassVar``-annotated attributes are config / infrastructure, not
        # model fields (e.g. ``SqlResource._session_factory``).
        # ``get_type_hints`` strips the ``ClassVar`` wrapper (returning the
        # inner type), and under ``from __future__ import annotations`` the
        # raw annotation is a string, so check both forms.
        if (
            get_origin(annotation) is ClassVar
            or annotation is ClassVar
            or (isinstance(annotation, str) and annotation.lstrip().startswith("ClassVar"))
        ):
            continue
        default = cls.__dict__.get(name, _NO_DEFAULT)
        ann_type = resolved.get(name, annotation)
        if default is _NO_DEFAULT:
            fields[name] = FieldInfo.from_annotation(ann_type)
        else:
            fields[name] = FieldInfo.from_annotated_attribute(ann_type, default)
    return fields


_NO_DEFAULT: Any = object()

# Attributes declared (with annotations) on ``BaseResource`` itself that must
# never be treated as resource fields. Because ``BaseResource`` is a plain
# class, its own ``model_fields`` / cache annotations would otherwise be
# collected by the annotation walk.
_INFRA_ATTRS: frozenset[str] = frozenset(
    {
        "model_fields",
        "_id_field",
        "_sortable_fields",
        "_create_model",
        "_read_model",
        "_update_model",
        "_sqlalchemy_model",
        "_cache_strategy",
        "_ctx",
        "_session_factory",
        "_client",
        "_database_name",
        "_db",
        "_instances",
        "_entered",
    }
)


def _resolve_annotations(cls: type[BaseResource]) -> dict[str, Any]:
    """Resolve string annotations on ``cls`` (and bases) to real types.

    Mirrors what Pydantic does for a ``BaseModel``: ``get_type_hints`` with
    ``include_extras=True`` so ``Annotated[..., ResourceyField(...)]`` metadata
    is preserved. Names that fail to resolve are left as their raw annotation.
    """
    try:
        return get_type_hints(cls, include_extras=True)
    except Exception:  # fall back to raw annotations for callers
        return {}


def _ordered_annotations(cls: type[BaseResource]) -> list[tuple[str, Any]]:
    """Merged, declaration-ordered ``(name, raw annotation)`` pairs for ``cls``.

    Walks the MRO base-first so a subclass's own annotations override an
    inherited field of the same name while preserving first-seen order.
    Framework infrastructure attributes are skipped.
    """
    seen: set[str] = set()
    ordered: list[tuple[str, Any]] = []
    for klass in reversed(cls.__mro__):
        anns = getattr(klass, "__annotations__", {})
        for name, annotation in anns.items():
            if name in seen or name in _INFRA_ATTRS:
                continue
            seen.add(name)
            ordered.append((name, annotation))
    return ordered


def _secret_base(secret_names: set[str]) -> type[BaseModel]:
    """Build a transient base class carrying secret serializers / validators.

    For each name in ``secret_names`` a ``field_serializer`` (dump via
    :func:`dump_secret_str`) and a ``before`` ``field_validator`` (load via
    :func:`load_secret_str`, rewrapped in :class:`SecretStr`) are attached.
    ``check_fields=False`` lets the decorators be defined on the base class
    before the fields exist (they are added by ``create_model``). The base
    class is fresh per call so distinct generated models never share decorator
    bindings. With no secret fields the plain :class:`BaseModel` is returned,
    preserving the #1 generation contract.
    """
    if not secret_names:
        return BaseModel
    namespace: dict[str, Any] = {}
    for name in secret_names:
        namespace[f"_serialize_secret_{name}"] = field_serializer(name, check_fields=False)(
            _make_secret_serializer(name)
        )
        namespace[f"_validate_secret_{name}"] = field_validator(
            name, mode="before", check_fields=False
        )(_make_secret_validator(name))
    return type("_SecretFieldsBase", (BaseModel,), namespace)


def _make_secret_serializer(name: str) -> Any:
    """Return a ``field_serializer`` function bound to ``name`` (closure per field)."""

    def _serialize(self: Any, value: Any, info: Any) -> str:
        if value is None or value is MISSING:
            return value  # type: ignore[return-value]
        if isinstance(value, SecretStr):
            return dump_secret_str(value, info)
        return dump_secret_str(SecretStr(str(value)), info)

    _serialize.__name__ = f"_serialize_secret_{name}"
    return _serialize


def _make_secret_validator(name: str) -> Any:
    """Return a ``field_validator`` function bound to ``name`` (closure per field)."""

    def _validate(value: Any, info: Any) -> Any:
        if value is None or value is MISSING:
            return value
        if isinstance(value, SecretStr):
            return value
        return SecretStr(load_secret_str(str(value), info))

    _validate.__name__ = f"_validate_secret_{name}"
    return _validate


def _clean_field(field: FieldInfo) -> FieldInfo:
    """Return a copy of ``field`` with ``ResourceyField`` metadata removed.

    ``ResourceyField`` carries a SQLAlchemy ``Column`` that is not
    JSON-schema serializable, so it must not appear on fields that are
    carried verbatim into generated models (e.g. required create-model
    fields and all read-model fields).
    """
    cleaned_meta = [m for m in field.metadata if not isinstance(m, ResourceyField)]
    if len(cleaned_meta) == len(field.metadata):
        return field
    new_field = field._copy()
    new_field.metadata = cleaned_meta
    return new_field


def _strip_config(annotation: Any) -> Any:
    """Remove ``ResourceyField`` (and other non-type) metadata from an annotation.

    ``ResourceyField`` carries a SQLAlchemy ``Column`` which is not
    JSON-schema serializable, so it must not leak into generated Pydantic
    models. For ``Annotated[T, ...]`` this returns the bare ``T``; unions
    are rebuilt with stripped members (preserving the ``None`` branch).
    """
    origin = get_origin(annotation)
    if origin is None:
        return annotation
    args = get_args(annotation)
    if origin is Annotated:
        return _strip_config(args[0])
    stripped = [_strip_config(a) for a in args]
    # ``types.UnionType`` (``X | Y``) cannot be subscripted; rebuild the
    # union with the ``|`` operator so the result is always a valid type.
    if origin is types.UnionType:
        return reduce(lambda x, y: x | y, stripped)
    return origin[tuple(stripped)]


def _resolve_scalar_type(annotation: Any) -> Any:
    """Reduce a (possibly Optional/Union) annotation to its concrete scalar type."""
    origin = get_origin(annotation)
    if origin is None:
        return annotation
    args = [a for a in get_args(annotation) if a is not type(None)]
    if len(args) == 1:
        return _resolve_scalar_type(args[0])
    # For multi-typed unions, prefer the first non-None arg we can map.
    return _resolve_scalar_type(args[0]) if args else annotation
