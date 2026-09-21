"""``BaseResource`` and the storage-agnostic resource declaration layer.

A resource is the central unit of resourcey. ``BaseResource`` is a plain
declaration class (not a Pydantic model): a subclass declares fields with the
ordinary annotation + ``Field()`` / default syntax, and the framework
introspects that declaration to drive the generated Pydantic create / read /
update models. Every generation step is a single-purpose, overridable hook so
a subtype can replace any piece without touching the rest (progressive
enhancement / escape hatches).

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
data record: it is never instantiated, never validated, and carries no
Pydantic model machinery.

Generated models and resolved values are cached on the class so repeated calls
are cheap. The caches are non-annotated class attributes (so they are not
treated as declared fields) and are stored per-subclass.
"""

from __future__ import annotations

import types
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

from pydantic import BaseModel, Field, SecretStr, create_model, field_serializer, field_validator
from pydantic.fields import FieldInfo

from resourcey.app_context import AppContext
from resourcey.resource.errors import ResourceyConfigError
from resourcey.resource.field import ResourceyField
from resourcey.resource.missing import MISSING
from resourcey.util.naming import camel_to_kebab, pluralize
from resourcey.util.secret_serialization import dump_secret_str, load_secret_str

if TYPE_CHECKING:
    # Used only in annotations. Kept under TYPE_CHECKING (with
    # ``from __future__ import annotations``) so the ``resource`` package
    # never imports ``util.search_filter`` / ``cache`` at runtime, avoiding a
    # potential import cycle.
    from resourcey.cache.cache_strategy import CacheStrategy
    from resourcey.util.search_filter import SearchFilter


class BaseResource:
    """Base class for resource declarations.

    A *declaration* class, not a data model: subclass it and declare fields
    with the ordinary annotation + ``Field()`` / default syntax, and the
    framework derives the create / read / update Pydantic models from that
    single declaration. Each public generation method is an overridable hook.

    ``BaseResource`` is storage-agnostic - it does not know about SQLAlchemy,
    sessions, or persistence. SQL-backed resources subclass
    :class:`~resourcey.resource.sql.SqlResource` for ORM model generation.

    ``BaseResource`` is intentionally not a Pydantic ``BaseModel``. Field
    metadata is collected into :attr:`model_fields` at subclass-creation time
    by :meth:`__init_subclass__`, so the generation hooks introspect a
    registry the framework owns rather than Pydantic's model machinery.
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
        cls.model_fields = _collect_field_infos(cls)

    # ------------------------------------------------------------------
    # Registration hook
    # ------------------------------------------------------------------

    def on_register(self) -> None:
        """Materialise backend artifacts (called by the manifest at construction).

        The base implementation is a no-op: a storage-agnostic resource has
        nothing to materialise. Storage-specific subclasses (e.g.
        :class:`~resourcey.resource.sql.SqlResource`) override this to eagerly
        build their backing model so it is available before migrations / table
        creation run.
        """

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

    async def __aexit__(self, *exc: object) -> None:
        """Tear down runtime state. Base implementation is a no-op."""

    # ------------------------------------------------------------------
    # Service + action surface
    # ------------------------------------------------------------------

    @classmethod
    def get_service_cls(cls) -> type[Any]:
        """The service class this resource yields from :meth:`open_service`.

        The base implementation raises: a storage-agnostic resource has no
        service. Storage-specific subclasses override this (e.g.
        :class:`~resourcey.resource.sql.SqlResource` returns
        :class:`~resourcey.resource.service.SqlService`).
        """
        raise NotImplementedError(
            f"{cls.__name__} does not declare a service class; override get_service_cls()."
        )

    @classmethod
    def get_supported_actions(cls) -> frozenset[Any]:
        """The actions this resource exposes over HTTP.

        Defaults to the service class's declared :attr:`actions`
        (``cls.get_service_cls().actions``) - the service is the single source
        of truth. A resource may override this to *narrow* (hide an action the
        service supports but should not be exposed), but must never widen
        beyond the service's ``actions``: the route builder asserts the
        subset relation at registration time.

        Returns a ``frozenset[Action]``.
        """
        return cast("frozenset[Any]", cls.get_service_cls().actions)

    def open_service(self, request: Any) -> Any:
        """Async context manager yielding a service instance for ``request``.

        The base implementation raises: a storage-agnostic resource cannot
        open a service. Storage-specific subclasses override this (e.g.
        :class:`~resourcey.resource.sql.SqlResource` opens / reuses a session
        on ``request.state`` and yields an
        :class:`~resourcey.resource.service.SqlService`). Suitable for use as
        an injected FastAPI dependency.
        """
        raise NotImplementedError(
            f"{type(self).__name__} cannot open a service; override open_service()."
        )

    # ------------------------------------------------------------------
    # Field config resolution
    # ------------------------------------------------------------------

    @classmethod
    def get_config_for_field(cls, field_name: str, field: FieldInfo) -> ResourceyField:
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
                    f"Field '{field_name}' on {cls.__name__} is a timestamp and must declare a "
                    "default_factory (e.g. default_factory=datetime.utcnow). A fixed default or no "
                    "default is ambiguous; supply an explicit ResourceyField override if intended."
                )
        # SecretStr fields are not sortable by default: allowing `sort=field`
        # against a secret lets a client infer the relative ordering of secret
        # values even when the field is excluded from the read model. Skipped
        # when an explicit ResourceyField is attached so a developer can
        # deliberately opt a secret into sortability.
        if not explicit and _resolve_scalar_type(field.annotation) is SecretStr:
            config = config.model_copy(update={"sortable": False})
        return config

    @classmethod
    def get_search_filter_type(cls) -> type[SearchFilter] | None:  # type: ignore[type-arg]
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

    @classmethod
    def get_cache_strategy(cls) -> CacheStrategy[Any]:
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
        cached = cls.__dict__.get("_cache_strategy")
        if cached is not None:
            return cast("CacheStrategy[Any]", cached)
        from resourcey.cache.cache_strategy import (
            ETagCacheStrategy,
            LastModifiedCacheStrategy,
        )

        field = cls.model_fields.get("updated_at")
        if field is not None and cls.get_config_for_field("updated_at", field).readable:
            strategy: CacheStrategy[Any] = LastModifiedCacheStrategy()
        else:
            strategy = ETagCacheStrategy()
        cls._cache_strategy = strategy
        return strategy

    @classmethod
    def get_sortable_fields(cls) -> list[str]:
        """Names of fields whose ``ResourceyField.sortable`` is ``True``.

        The single source of truth for what the search endpoint's ``sort``
        enum may contain. Cached on the class. A field is sortable unless it
        is explicitly opted out (e.g. ``SecretStr`` fields default to
        ``sortable=False`` -- see :meth:`get_config_for_field`).
        """
        cached = cls.__dict__.get("_sortable_fields")
        if cached is not None:
            return cast(list[str], cached)
        sortable = [
            name
            for name, field in cls.model_fields.items()
            if cls.get_config_for_field(name, field).sortable
        ]
        cls._sortable_fields = sortable
        return sortable

    @classmethod
    def get_id_field(cls) -> str:
        """Return the name of the primary identifier field (default: ``id``).

        Cached on the class. Raises ``ResourceyConfigError`` if no ``id``
        field exists.
        """
        cached = cls.__dict__.get("_id_field")
        if cached is not None:
            return cast(str, cached)
        if "id" in cls.model_fields:
            cls._id_field = "id"
            return "id"
        raise ResourceyConfigError(
            f"Resource {cls.__name__} has no 'id' field; override get_id_field() to specify one."
        )

    # ------------------------------------------------------------------
    # Pydantic model generation
    # ------------------------------------------------------------------

    @classmethod
    def get_create_model(cls) -> type[BaseModel]:
        """Build (and cache) the create model: only ``creatable`` fields.

        Required fields (no original default) stay required. Optional fields
        get ``Field(default=MISSING, validate_default=False)`` so the service
        can tell whether a value was explicitly supplied. Secret-bearing
        (``SecretStr``) fields gain the ``dump_secret_str`` serializer and
        ``load_secret_str`` validator so encryption / redaction happens at the
        storage boundary driven by the serialization context.
        """
        cached = cls.__dict__.get("_create_model")
        if cached is not None:
            return cast(type[BaseModel], cached)
        fields: dict[str, Any] = {}
        secret_names: set[str] = set()
        for name, field in cls.model_fields.items():
            config = cls.get_config_for_field(name, field)
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
            f"{cls.__name__}Create",
            __base__=_secret_base(secret_names),
            **fields,
        )
        cls._create_model = model
        return model

    @classmethod
    def get_read_model(cls) -> type[BaseModel]:
        """Build (and cache) the read model: only ``readable`` fields.

        Secret-bearing (``SecretStr``) fields gain the ``dump_secret_str``
        serializer and ``load_secret_str`` validator so encryption / redaction
        happens at the storage boundary driven by the serialization context.
        """
        cached = cls.__dict__.get("_read_model")
        if cached is not None:
            return cast(type[BaseModel], cached)
        fields: dict[str, Any] = {}
        secret_names: set[str] = set()
        for name, field in cls.model_fields.items():
            config = cls.get_config_for_field(name, field)
            if not config.readable:
                continue
            fields[name] = (_strip_config(field.annotation), _clean_field(field))
            if _resolve_scalar_type(field.annotation) is SecretStr:
                secret_names.add(name)
        model = create_model(
            f"{cls.__name__}Read",
            __base__=_secret_base(secret_names),
            **fields,
        )
        cls._read_model = model
        return model

    @classmethod
    def get_update_model(cls) -> type[BaseModel]:
        """Build (and cache) the PATCH-style update model: only ``updatable`` fields.

        Every field is optional: each gets
        ``Field(default=MISSING, validate_default=False)`` (original
        default / default_factory dropped) so omitted fields are
        distinguishable from explicitly-supplied ones. Secret-bearing
        (``SecretStr``) fields gain the ``dump_secret_str`` serializer and
        ``load_secret_str`` validator so encryption / redaction happens at the
        storage boundary driven by the serialization context.
        """
        cached = cls.__dict__.get("_update_model")
        if cached is not None:
            return cast(type[BaseModel], cached)
        fields: dict[str, Any] = {}
        secret_names: set[str] = set()
        for name, field in cls.model_fields.items():
            config = cls.get_config_for_field(name, field)
            if not config.updatable:
                continue
            fields[name] = (_strip_config(field.annotation), _missing_field())
            if _resolve_scalar_type(field.annotation) is SecretStr:
                secret_names.add(name)
        model = create_model(
            f"{cls.__name__}Update",
            __base__=_secret_base(secret_names),
            **fields,
        )
        cls._update_model = model
        return model

    # ------------------------------------------------------------------
    # REST path
    # ------------------------------------------------------------------

    @classmethod
    def get_resource_path(cls) -> str:
        """Derive the plural, lower-case, kebab-case REST path segment.

        Independent of the SQL table name (see
        :meth:`~resourcey.resource.sql.SqlResource.get_table_name`) so an
        override of one never silently changes the other: the SQL table name
        and the URL path are separate concerns and may legitimately diverge.
        Defaults to the plural kebab-case class name (``UserRole`` ->
        ``user-roles``). Overridable.
        """
        return pluralize(camel_to_kebab(cls.__name__)).lower()


def _missing_field() -> Any:
    """A field defaulting to ``MISSING`` without validating the sentinel."""
    return Field(default=MISSING, validate_default=False)


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
