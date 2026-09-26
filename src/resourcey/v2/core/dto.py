"""DTOs — the single field-level declaration a resource is derived from.

A :class:`DTO` is a plain *declaration* class, not a Pydantic model: a
subclass annotates its fields (ordinary Pydantic annotations, so schema
validation works) and tags each with a :class:`DtoField` that says how the
field projects into each of the six REST shapes. One declaration is the
source of field truth; the REST models are derived from it, never hand
written.

Three things are generated from a declaration, at subclass-creation time:

* the **DTO model** — a Pydantic model in which every field is widened to
  ``ann | Missing`` and defaults to :data:`MISSING`, so an omitted field is
  distinguishable from an explicitly supplied ``None``;
* the **REST models** — six field-selection views (create/update request +
  response, read response, search response) built from the :class:`DtoField`
  ``in_*`` flags. A create request carries concrete defaults; an update request
  keeps the :data:`MISSING` sentinel on the wire boundary so PATCH can tell
  "omitted" from "explicitly ``null``";
* the resolved **operation-scoped defaults** — the value used when a client
  omits a field, with precedence *client value -> default for that operation
  ->* ``MISSING``. Create applies ``default_for_create`` /
  ``default_factory_for_create``; update applies ``default_for_update`` /
  ``default_factory_for_update``; a field with no default for the operation is
  left untouched.

Conventions (``id`` is not client-supplied; ``created_at`` is created once and
never touched on update; ``updated_at`` is set on create *and* re-set on every
update; a conventional ``UUID`` ``id`` is generated server-side) are applied in
:meth:`DTO.__init_subclass__` because ``__set_name__`` does not fire for
``Annotated`` metadata. One rule governs them all: an expressed intent wins — an
explicit ``DtoField`` is honoured verbatim, and (in the SQL path) a column-level
default beats a convention-generated factory.

This module is part of the ``v2/core`` layer: it imports only ``v2/util`` (the
``Missing`` sentinel) besides Pydantic and the standard library — ``core``
imports no other project package.
"""

from __future__ import annotations

import operator
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import reduce
from typing import Annotated, Any, ClassVar, get_args, get_origin, get_type_hints
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, create_model

from resourcey.v2.util.missing import MISSING, Missing

_UNSET: Any = object()
_NO_DEFAULT: Any = object()

# The field that identifies a row / resource unless a declaration says otherwise.
DEFAULT_ID_FIELD_NAME = "id"

# Names owned by the declaration machinery, never DTO fields.
_RESERVED_NAMES = frozenset({"metadata", "id_field_name"})


# ---------------------------------------------------------------------------
# DtoField — per-field projection metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DtoField:
    """How a DTO field projects into the six REST shapes, plus its defaults.

    Every ``in_*`` flag defaults to ``True``. They supersede the older
    ``creatable`` / ``updatable`` / ``readable`` triple: ``in_read_response``
    and ``in_search_response`` split what ``readable`` conflated, and
    ``in_create_response`` / ``in_update_response`` are new — together they
    express a one-time-reveal field (present in the create response and
    nowhere else).

    ``default_for_create`` / ``default_factory_for_create`` and
    ``default_for_update`` / ``default_factory_for_update`` are the values used
    when the client omits the field, scoped to the operation (distinct from
    ``MISSING``); precedence is client value -> default for that operation ->
    ``MISSING``. A create and an update do not want the same default: an
    omitted update field with no update default is *left unchanged*, while an
    ``in_update_request=False`` field is always omitted and so always takes its
    update default (the "always overwrite" case).

    ``metadata`` is free-form: a general-purpose store for extra data a
    downstream layer wants to attach to the field (a UI label, a column hint,
    a validation rule). ``v2/core`` never reads it; it is there so extensions
    do not need a new ``DtoField`` attribute each.
    """

    in_create_request: bool = True
    in_create_response: bool = True
    in_update_request: bool = True
    in_update_response: bool = True
    in_read_response: bool = True
    in_search_response: bool = True
    default_for_create: Any = _UNSET
    default_factory_for_create: Callable[[], Any] | None = None
    default_for_update: Any = _UNSET
    default_factory_for_update: Callable[[], Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        # A value and a factory for the same operation are mutually exclusive:
        # which one wins would be arbitrary. Both are *supplied* rather than
        # inferred, so the contradiction is a declaration error, not a
        # convention one.
        for operation in ("create", "update"):
            value, factory = self._default_parts(operation)
            if value is not _UNSET and factory is not None:
                raise ValueError(
                    f"DtoField declares both a value and a factory default for "
                    f"{operation}; declare only one"
                )

    def has_default_for(self, operation: str) -> bool:
        """Whether a default (value or factory) was declared for ``operation``."""
        value, factory = self._default_parts(operation)
        return value is not _UNSET or factory is not None

    def resolve_default(self, operation: str = "create") -> Any:
        """The default for an omitted field in ``operation``, or ``MISSING`` if none."""
        value, factory = self._default_parts(operation)
        if value is not _UNSET:
            return value
        if factory is not None:
            return factory()
        return MISSING

    def _default_parts(self, operation: str) -> tuple[Any, Callable[[], Any] | None]:
        if operation == "update":
            return self.default_for_update, self.default_factory_for_update
        return self.default_for_create, self.default_factory_for_create

    def with_overrides(self, **overrides: Any) -> DtoField:
        """A copy of this field with the given flags replaced (convention helper)."""
        return replace(self, **overrides)


def utc_now() -> datetime:
    """Default factory for the ``created_at`` / ``updated_at`` conventions."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# The six generated REST models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestModels:
    """The six REST shapes derived from a DTO declaration, all Pydantic models."""

    create_request: type[BaseModel]
    create_response: type[BaseModel]
    update_request: type[BaseModel]
    update_response: type[BaseModel]
    read_response: type[BaseModel]
    search_response: type[BaseModel]


# ---------------------------------------------------------------------------
# DTO declaration class
# ---------------------------------------------------------------------------


class DTO:
    """Base class for a DTO declaration.

    Subclass it and annotate fields; tag a field with
    ``Annotated[T, DtoField(...)]``, or leave it bare and let the conventions
    apply::

        class MyStoredKey(DTO, metadata={"table": "stored_keys"}):
            id: Annotated[UUID, DtoField(in_create_request=False, in_update_request=False,
                                         default_factory_for_create=uuid4)]
            key: Annotated[str, DtoField(in_read_response=False, metadata={"label": "Key"})]
            description: Annotated[str | None, DtoField(default_for_create=None)]
            created_at: Annotated[datetime, DtoField(in_create_request=False,
                                                     in_update_request=False,
                                                     default_factory_for_create=utc_now)]
            updated_at: Annotated[datetime, DtoField(in_create_request=False,
                                                     in_update_request=False,
                                                     default_factory_for_create=utc_now,
                                                     default_factory_for_update=utc_now)]

    ``Annotated`` is the canonical Pydantic v2 mechanism (and the one ``v1``
    already uses for ``ResourceyField``). It keeps the real field type, unlike
    the assignment form (``key: str = DtoField(...)``), which is a type error
    under ``mypy --strict``. No explicit ``| Missing`` is needed: the generator
    widens every field itself.

    ``metadata`` is free-form extra data, inherited and merged down the MRO; a
    subclass may pass ``metadata=`` as a class keyword or declare a ``metadata``
    attribute in its body. See :attr:`metadata`.
    """

    __dto_fields__: ClassVar[dict[str, tuple[Any, DtoField]]]
    __dto_model__: ClassVar[type[BaseModel]]
    __rest_models__: ClassVar[RestModels]

    metadata: ClassVar[dict[str, Any]] = {}
    """Free-form extra data attached to the declaration.

    A general-purpose store for whatever a downstream layer needs (a table
    name, a label, a feature flag). ``v2/core`` never reads it. It is inherited
    and merged across the MRO, and is *not* a DTO field.
    """

    id_field_name: ClassVar[str] = DEFAULT_ID_FIELD_NAME
    """The field that identifies a row / resource — ``id`` by default.

    Override with the ``id_field_name=`` class keyword to use another declared
    field as the identifier (e.g. a natural key)::

        class Country(DTO, id_field_name="code"):
            code: str
            name: str

    The identifier gets the ``id`` conventions (omitted from create/update
    requests) and, in the SQL backend, becomes the primary key. Validated at
    subclass creation: the named field must exist, or ``TypeError`` is raised.
    """

    def __init_subclass__(
        cls,
        *,
        metadata: dict[str, Any] | None = None,
        id_field_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        cls.metadata = _resolve_metadata(cls, metadata)
        cls.id_field_name = _resolve_id_field_name(cls, id_field_name)
        fields = _collect_dto_fields(cls)
        cls.__dto_fields__ = fields
        _assert_id_field_exists(cls, fields)
        cls.__dto_model__ = _build_dto_model(cls.__name__, fields)
        cls.__rest_models__ = _build_rest_models(cls.__name__, fields)

    # -- introspection --------------------------------------------------

    @classmethod
    def get_dto_type(cls) -> type[BaseModel]:
        """The generated Pydantic DTO model: every field ``ann | Missing = MISSING``."""
        return cls.__dto_model__

    @classmethod
    def get_fields(cls) -> dict[str, DtoField]:
        """The resolved per-field projection metadata, in declaration order."""
        return {name: config for name, (_ann, config) in cls.__dto_fields__.items()}

    @classmethod
    def get_rest_models(cls) -> RestModels:
        """The six REST models derived from this declaration's flags."""
        return cls.__rest_models__

    @classmethod
    def get_default(cls, name: str, operation: str = "create") -> Any:
        """The default for ``name`` in ``operation`` (value or factory result), else ``MISSING``."""
        return cls.__dto_fields__[name][1].resolve_default(operation)

    # -- construction ---------------------------------------------------

    @classmethod
    def new(cls, **values: Any) -> BaseModel:
        """Build a DTO instance applying the create-default precedence.

        A client-supplied value (anything but ``MISSING``) wins, then the
        create default, then ``MISSING``. An explicit ``None`` is a supplied
        value and survives alongside the sentinel. An optional annotation is
        *not* a default: only ``default_for_create`` /
        ``default_factory_for_create`` fill an omitted field.
        """
        data: dict[str, Any] = {}
        for name, (_ann, config) in cls.__dto_fields__.items():
            value = values.get(name, MISSING)
            data[name] = config.resolve_default("create") if value is MISSING else value
        return cls.__dto_model__(**data)


# ---------------------------------------------------------------------------
# Declaration introspection
# ---------------------------------------------------------------------------


def _resolve_metadata(cls: type[DTO], declared: dict[str, Any] | None) -> dict[str, Any]:
    """The class metadata: the nearest ancestor's mapping merged with the subclass's.

    Sources, lowest precedence first: the nearest base's resolved ``metadata``,
    a mapping written in this class body, and a ``metadata=`` class keyword.
    """
    parent: dict[str, Any] = {}
    for base in cls.__mro__[1:]:
        inherited = base.__dict__.get("metadata")
        if isinstance(inherited, dict):
            parent = dict(inherited)
            break
    merged = dict(parent)
    own = cls.__dict__.get("metadata")
    if isinstance(own, dict):
        merged.update(own)
    if declared:
        merged.update(declared)
    return merged


def _resolve_id_field_name(cls: type[DTO], declared: str | None) -> str:
    """The identifier field name: the class keyword, else the inherited value."""
    if declared is not None:
        resolved: Any = declared
    else:
        resolved = getattr(cls, "id_field_name", DEFAULT_ID_FIELD_NAME)
    if not isinstance(resolved, str) or not resolved:
        raise TypeError(
            f"{cls.__name__} needs id_field_name to be a non-empty string, got {resolved!r}"
        )
    return resolved


def _assert_id_field_exists(cls: type[DTO], fields: Mapping[str, tuple[Any, DtoField]]) -> None:
    """Fail at declaration time if the declared identifier is not a real field."""
    if cls.id_field_name not in fields:
        raise TypeError(
            f"{cls.__name__} declares id_field_name={cls.id_field_name!r} but has no such "
            f"field (fields: {', '.join(fields) or 'none'})"
        )


def _collect_dto_fields(cls: type[DTO]) -> dict[str, tuple[Any, DtoField]]:
    """Resolve ``cls``'s declared fields into ``(annotation, DtoField)`` pairs.

    Walks the MRO base-first so a subclass override replaces an inherited
    field while preserving declaration order. Underscore-prefixed names,
    ``ClassVar`` attributes, and the reserved declaration names are
    infrastructure, not fields.
    """
    fields: dict[str, tuple[Any, DtoField]] = {}
    for klass in reversed(cls.__mro__):
        annotations = getattr(klass, "__annotations__", {})
        if not annotations:
            continue
        try:
            hints = get_type_hints(klass, include_extras=True)
        except NameError:  # pragma: no cover - unresolved forward references
            hints = {}
        for name, raw in annotations.items():
            if name.startswith("_") or name in _RESERVED_NAMES:
                continue
            if _is_classvar(raw, hints.get(name)):
                continue
            annotation = hints.get(name, raw)
            config, explicit = _config_from(klass.__dict__.get(name, _NO_DEFAULT), annotation)
            fields[name] = (
                annotation,
                _apply_conventions(name, cls.id_field_name, annotation, config, explicit),
            )
    return fields


def _config_from(value: Any, annotation: Any) -> tuple[DtoField, bool]:
    """Build the ``DtoField`` for a field from its class attribute / annotation.

    An ``Annotated[T, DtoField(...)]`` metadata entry wins; otherwise a
    ``DtoField`` class attribute, a bare class default (shorthand for a create
    default), or a bare field with the conventions only. The second element is
    whether the author expressed the config *explicitly* (a ``DtoField``) rather
    than leaving it to a bare value or nothing; the conventions only apply to
    the latter.
    """
    for meta in _annotated_metadata(annotation):
        if isinstance(meta, DtoField):
            return meta, True
    if isinstance(value, DtoField):
        return value, True
    if value is _NO_DEFAULT:
        return DtoField(), False
    # A bare default value is shorthand for a create default.
    return DtoField(default_for_create=value), False


def _apply_conventions(
    name: str,
    id_field_name: str,
    annotation: Any,
    config: DtoField,
    explicit: bool,
    *,
    identifier_is_backend_managed: bool = False,
) -> DtoField:
    """Apply the id / timestamp conventions the author did not set explicitly.

    An explicit ``DtoField`` is honoured verbatim — the conventions never alter
    declared flags or defaults (the developer's intent wins outright). Only a
    bare field (no ``DtoField``) is decorated. ``id_field_name`` is the resolved
    identifier, so a caller that knows it (the SQL path, from the primary key)
    does not have to synthesize a declaration, and
    ``identifier_is_backend_managed`` lets that caller report a database-owned
    key so no application-side factory is generated.
    """
    if explicit:
        return config
    if name == id_field_name:
        # The conventional ``id`` is server-generated, so it is never client
        # input. A *custom* identifier is the author's own key (a natural key),
        # so it stays available on create — the client supplies it — but is
        # still immutable, so it is excluded from updates either way.
        overrides: dict[str, Any] = {"in_update_request": False}
        if name == DEFAULT_ID_FIELD_NAME:
            overrides["in_create_request"] = False
        config = config.with_overrides(**overrides)
        # A UUID identifier is generated server-side by the application (never
        # by a database default), so an omitted create gets a fresh ``uuid4``.
        # Deliberately narrow: a UUID under a *custom* ``id_field_name`` (a
        # natural key), a non-UUID id, or a column that already declares its own
        # default (the expressed intent — a column default wins) is left alone.
        if (
            name == DEFAULT_ID_FIELD_NAME
            and _is_uuid_annotation(annotation)
            and not config.has_default_for("create")
            and not identifier_is_backend_managed
        ):
            config = config.with_overrides(default_factory_for_create=uuid4)
        return config
    if name in ("created_at", "updated_at"):
        # Neither is client input. ``created_at`` is written once (no update
        # default, so an omitted update leaves it alone); ``updated_at`` is
        # re-set on every update, so it carries an update default too.
        config = config.with_overrides(in_create_request=False, in_update_request=False)
        if not config.has_default_for("create"):
            config = config.with_overrides(default_factory_for_create=utc_now)
        if name == "updated_at" and not config.has_default_for("update"):
            config = config.with_overrides(default_factory_for_update=utc_now)
    return config


# ---------------------------------------------------------------------------
# Type helpers
# ---------------------------------------------------------------------------


def _is_union(annotation: Any) -> bool:
    """Whether ``annotation`` is a ``X | Y`` / ``typing.Union`` union."""
    origin = get_origin(annotation)
    return origin is types.UnionType or origin is typing.Union


def _is_classvar(raw: Any, resolved: Any) -> bool:
    """Whether a field is ``ClassVar``-annotated (raw string or resolved type)."""
    for candidate in (raw, resolved):
        if get_origin(candidate) is ClassVar or candidate is ClassVar:
            return True
        if isinstance(candidate, str) and candidate.lstrip().startswith("ClassVar"):
            return True
    return False


def _annotated_metadata(annotation: Any) -> tuple[Any, ...]:
    """The metadata tuple of an ``Annotated[...]`` annotation, else ``()``."""
    return tuple(getattr(annotation, "__metadata__", ()))


def _strip_annotated(annotation: Any) -> Any:
    """Reduce ``Annotated[T, ...]`` to ``T`` (recursively, through unions)."""
    if isinstance(annotation, str):
        return annotation
    if _annotated_metadata(annotation):
        return _strip_annotated(annotation.__origin__)
    if _is_union(annotation):
        return reduce(operator.or_, (_strip_annotated(arg) for arg in get_args(annotation)))
    return annotation


def _is_uuid_annotation(annotation: Any) -> bool:
    """Whether ``annotation`` is exactly ``UUID`` (an ``Annotated`` wrapper allowed).

    Deliberately narrow: only the bare ``UUID`` type qualifies. ``str`` /
    ``CHR(36)`` uuid columns, unions, and ``Any`` do not — the issue scopes
    server-side generation to the ``UUID`` type only.
    """
    return _strip_annotated(annotation) is UUID


def _with_missing(annotation: Any) -> Any:
    """``annotation | Missing`` — how every field of the generated DTO type is typed."""
    if annotation is None:
        return Missing
    return annotation | Missing


def _missing_field() -> Any:
    """A field defaulting to ``MISSING`` without validating the sentinel."""
    return Field(default=MISSING, validate_default=False)


# ---------------------------------------------------------------------------
# Model generation
# ---------------------------------------------------------------------------


def _build_dto_model(name: str, fields: Mapping[str, tuple[Any, DtoField]]) -> type[BaseModel]:
    """Build the DTO model: every field ``ann | Missing`` defaulting to ``MISSING``."""
    model_fields: dict[str, Any] = {
        field_name: (_with_missing(_strip_annotated(annotation)), _missing_field())
        for field_name, (annotation, _config) in fields.items()
    }
    return create_model(name, **model_fields)


def _build_rest_models(name: str, fields: Mapping[str, tuple[Any, DtoField]]) -> RestModels:
    """Build the six REST models as field-selection views of the DTO."""
    return RestModels(
        create_request=_build_create_request_model(f"{name}CreateRequest", fields),
        create_response=_build_response_model(
            f"{name}CreateResponse", fields, "in_create_response"
        ),
        update_request=_build_update_request_model(f"{name}UpdateRequest", fields),
        update_response=_build_response_model(
            f"{name}UpdateResponse", fields, "in_update_response"
        ),
        read_response=_build_response_model(f"{name}ReadResponse", fields, "in_read_response"),
        search_response=_build_response_model(
            f"{name}SearchResponse", fields, "in_search_response"
        ),
    )


def _build_response_model(
    name: str, fields: Mapping[str, tuple[Any, DtoField]], flag: str
) -> type[BaseModel]:
    """A response model: the flagged fields, required, with concrete (never-``Missing``) types."""
    model_fields: dict[str, Any] = {
        field_name: (_strip_annotated(annotation), ...)
        for field_name, (annotation, config) in fields.items()
        if getattr(config, flag)
    }
    return create_model(name, **model_fields)


def _build_create_request_model(
    name: str, fields: Mapping[str, tuple[Any, DtoField]]
) -> type[BaseModel]:
    """A create request: the ``in_create_request`` fields with their create defaults.

    Optionality comes *only* from a declared ``default_for_create`` /
    ``default_factory_for_create`` (or the field being excluded): a nullable
    annotation with no create default is required, so a PATCH can always tell
    "not specified" from "set to ``null``". A factory default becomes Pydantic's
    ``default_factory`` (so it is actually used) rather than a concrete ``None``.
    """
    model_fields: dict[str, Any] = {}
    for field_name, (annotation, config) in fields.items():
        if not config.in_create_request:
            continue
        concrete = _strip_annotated(annotation)
        if config.default_factory_for_create is not None:
            model_fields[field_name] = (
                concrete,
                Field(default_factory=config.default_factory_for_create),
            )
        elif config.default_for_create is not _UNSET:
            model_fields[field_name] = (concrete, config.default_for_create)
        else:
            model_fields[field_name] = (concrete, ...)
    return create_model(name, **model_fields)


def _build_update_request_model(
    name: str, fields: Mapping[str, tuple[Any, DtoField]]
) -> type[BaseModel]:
    """An update request: the ``in_update_request`` fields, every one optional.

    Each field defaults to the :data:`MISSING` sentinel so a PATCH that omits it
    is distinguishable from one that sends an explicit ``None`` — the sentinel
    lives on the wire boundary here, unlike the create request's concrete
    defaults. A non-nullable field still rejects ``null``. Defaults for update
    are applied by the service (operation-scoped), not materialised here.
    """
    model_fields: dict[str, Any] = {
        field_name: (_strip_annotated(annotation), _missing_field())
        for field_name, (annotation, config) in fields.items()
        if config.in_update_request
    }
    return create_model(name, **model_fields)


def derive_dto(
    dto: type[DTO],
    *,
    field_overrides: Mapping[str, Mapping[str, Any]] | None = None,
    name: str | None = None,
) -> type[DTO]:
    """Derive a new DTO declaration from ``dto`` with per-field overrides applied.

    Every field of ``dto`` is re-declared with its *resolved* :class:`DtoField`
    (from :meth:`DTO.get_fields`) made explicit, then merged with that field's
    override mapping via :meth:`DtoField.with_overrides`. Because each field is
    now explicit, the :class:`DTO` conventions do **not** re-run — a bare ``id``
    is not given a fresh ``uuid4`` factory and timestamps are not re-decorated,
    so the derived declaration projects exactly what the source declared plus
    the requested changes. This is the mechanism behind a ``ResourceView``'s
    ``exposed_field_overrides``.

    Args:
        dto: The declaration to derive from.
        field_overrides: ``{field_name: {DtoField attribute: value}}``. A partial
            mapping: unmentioned attributes keep the source field's value, so an
            override can never silently re-widen a flag the source had turned
            off (a full ``DtoField(...)`` replacement would reset the rest to
            their ``True`` defaults). An unknown field name raises ``KeyError``.
        name: The derived class name (defaults to ``<dto name>View``).

    The identifier (``id_field_name``) is carried across unchanged.
    """
    overrides = field_overrides or {}
    resolved = dto.__dto_fields__
    unknown = sorted(set(overrides) - set(resolved))
    if unknown:
        raise KeyError(f"{dto.__name__} has no field(s) {unknown} to override")
    namespace: dict[str, Any] = {
        "__annotations__": {
            field_name: Annotated[
                annotation, config.with_overrides(**overrides.get(field_name, {}))
            ]
            for field_name, (annotation, config) in resolved.items()
        },
        "__module__": dto.__module__,
    }
    return type(name or f"{dto.__name__}View", (DTO,), namespace, id_field_name=dto.id_field_name)


def apply_operation_defaults(
    dto: type[DTO], data: dict[str, Any], operation: str
) -> dict[str, Any]:
    """Fill omitted fields of ``data`` from the DTO's defaults for ``operation``.

    A field the storage owns has no default for the operation, so nothing is
    supplied and the storage generates it; a field with a factory default is
    filled by the application. An omitted field with no default for the
    operation is left untouched (so an update with no update default preserves
    the stored value). Shared by every backend's create / update path so the
    default precedence is defined once.
    """
    for name, (_ann, config) in dto.__dto_fields__.items():
        if name in data:
            continue
        default = config.resolve_default(operation)
        if default is not MISSING:
            data[name] = default
    return data


def request_to_dto(dto_model: type[BaseModel], payload: BaseModel) -> BaseModel:
    """Convert a request model instance into a DTO instance — the sanctioned hop.

    Uses ``model_dump(exclude_unset=True)`` so only client-supplied fields carry
    over and everything else stays :data:`MISSING`. This form is load-bearing:
    a plain ``model_dump`` raises (or leaks the sentinel) because the request
    field is concretely typed, and ``exclude_none=True`` would drop an explicit
    ``None`` (so a PATCH could never clear a field).
    """
    return dto_model.model_validate(payload.model_dump(exclude_unset=True))
