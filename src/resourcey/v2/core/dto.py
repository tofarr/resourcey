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
  ``in_*`` flags;
* the resolved **logical defaults** — the value used when a client omits a
  field, with precedence *client value -> logical default ->* ``MISSING``.

Conventions (``id`` is not client-supplied; ``created_at`` / ``updated_at``
are not client-supplied and get a logical default factory) are applied in
:meth:`DTO.__init_subclass__` because ``__set_name__`` does not fire for
``Annotated`` metadata.

This module is part of the ``v2/core`` bottom layer: it imports no other
``resourcey`` module (only Pydantic and the standard library).
"""

from __future__ import annotations

import operator
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import reduce
from typing import Any, ClassVar, get_args, get_origin, get_type_hints

from pydantic import BaseModel, Field, create_model

_UNSET: Any = object()
_NO_DEFAULT: Any = object()


# ---------------------------------------------------------------------------
# Missing — a usable sentinel type
# ---------------------------------------------------------------------------


class Missing:
    """Singleton sentinel marking an unset value, usable in a type annotation.

    Identity comparison (``value is MISSING``) is the supported test. The
    class carries a Pydantic core schema so ``UUID | Missing`` (or any other
    ``ann | Missing`` union) validates and serializes: only the singleton is
    accepted as a value, and it dumps to ``null`` so a sentinel never reaches
    the wire. This is what the older framework-level ``MISSING`` lacked —
    without a core schema, ``UUID | Missing`` raised
    ``PydanticSchemaGenerationError``.
    """

    _instance: ClassVar[Missing | None] = None

    def __new__(cls) -> Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> Missing:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> Missing:
        return self

    @classmethod
    def _validate(cls, value: Any) -> Missing:
        if value is MISSING:
            return cls()
        raise ValueError("Missing accepts only the MISSING singleton")

    @classmethod
    def _serialize(cls, _value: Any) -> None:
        return None

    @classmethod
    def __get_pydantic_core_schema__(cls, _source: Any, _handler: Any) -> Any:
        from pydantic_core import core_schema

        return core_schema.no_info_plain_validator_function(
            cls._validate,
            serialization=core_schema.plain_serializer_function_ser_schema(cls._serialize),
        )


MISSING: Missing = Missing()


# ---------------------------------------------------------------------------
# DtoField — per-field projection metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DtoField:
    """How a DTO field projects into the six REST shapes, plus its logical default.

    Every ``in_*`` flag defaults to ``True``. They supersede the older
    ``creatable`` / ``updatable`` / ``readable`` triple: ``in_read_response``
    and ``in_search_response`` split what ``readable`` conflated, and
    ``in_create_response`` / ``in_update_response`` are new — together they
    express a one-time-reveal field (present in the create response and
    nowhere else).

    ``logical_default_value`` / ``logical_default_value_factory`` are the
    values used when the client omits the field (distinct from ``MISSING``);
    precedence is client value -> logical default -> ``MISSING``.
    """

    in_create_request: bool = True
    in_create_response: bool = True
    in_update_request: bool = True
    in_update_response: bool = True
    in_read_response: bool = True
    in_search_response: bool = True
    logical_default_value: Any = _UNSET
    logical_default_value_factory: Callable[[], Any] | None = None

    @property
    def has_logical_default(self) -> bool:
        """Whether a logical default (value or factory) was declared."""
        return (
            self.logical_default_value is not _UNSET
            or self.logical_default_value_factory is not None
        )

    def resolve_default(self) -> Any:
        """The logical default for an omitted field, or ``MISSING`` if none."""
        if self.logical_default_value is not _UNSET:
            return self.logical_default_value
        if self.logical_default_value_factory is not None:
            return self.logical_default_value_factory()
        return MISSING

    def with_overrides(self, **overrides: Any) -> DtoField:
        """A copy of this field with the given flags replaced (convention helper)."""
        return replace(self, **overrides)


def utc_now() -> datetime:
    """Logical default factory for the ``created_at`` / ``updated_at`` conventions."""
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

    Subclass it and annotate fields; tag a field by assigning a
    :class:`DtoField` as its class-attribute value, or just leave it bare and
    let the conventions apply::

        class MyStoredKey(DTO):
            id: UUID
            key: str = DtoField(in_read_response=False)
            description: str | None = DtoField(logical_default_value=None)
            created_at: datetime = DtoField(
                in_create_request=False, logical_default_value_factory=utc_now
            )
    """

    __dto_fields__: ClassVar[dict[str, tuple[Any, DtoField]]]
    __dto_model__: ClassVar[type[BaseModel]]
    __rest_models__: ClassVar[RestModels]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        fields = _collect_dto_fields(cls)
        cls.__dto_fields__ = fields
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
    def get_logical_default(cls, name: str) -> Any:
        """The logical default for ``name`` (value or factory result), else ``MISSING``."""
        return cls.__dto_fields__[name][1].resolve_default()

    # -- construction ---------------------------------------------------

    @classmethod
    def new(cls, **values: Any) -> BaseModel:
        """Build a DTO instance applying the logical-default precedence.

        A client-supplied value (anything but ``MISSING``) wins, then the
        logical default, then ``MISSING``. An explicit ``None`` is a supplied
        value and survives alongside the sentinel.
        """
        data: dict[str, Any] = {}
        for name, (_ann, config) in cls.__dto_fields__.items():
            value = values.get(name, MISSING)
            data[name] = config.resolve_default() if value is MISSING else value
        return cls.__dto_model__(**data)


# ---------------------------------------------------------------------------
# Declaration introspection
# ---------------------------------------------------------------------------


def _collect_dto_fields(cls: type[DTO]) -> dict[str, tuple[Any, DtoField]]:
    """Resolve ``cls``'s declared fields into ``(annotation, DtoField)`` pairs.

    Walks the MRO base-first so a subclass override replaces an inherited
    field while preserving declaration order. Underscore-prefixed names and
    ``ClassVar`` attributes are infrastructure, not fields.
    """
    fields: dict[str, tuple[Any, DtoField]] = {}
    for klass in reversed(cls.__mro__):
        annotations = getattr(klass, "__annotations__", {})
        if not annotations:
            continue
        try:
            hints = get_type_hints(klass, include_extras=True)
        except Exception:  # pragma: no cover - unresolved forward references
            hints = {}
        for name, raw in annotations.items():
            if name.startswith("_") or _is_classvar(raw, hints.get(name)):
                continue
            annotation = hints.get(name, raw)
            config = _config_from(klass.__dict__.get(name, _NO_DEFAULT), annotation)
            fields[name] = (annotation, _apply_conventions(name, config))
    return fields


def _config_from(value: Any, annotation: Any) -> DtoField:
    """Build the ``DtoField`` for a field from its class attribute / annotation."""
    if isinstance(value, DtoField):
        return value
    for meta in _annotated_metadata(annotation):
        if isinstance(meta, DtoField):
            return meta
    if value is _NO_DEFAULT:
        return DtoField()
    # A bare default value is shorthand for a logical default.
    return DtoField(logical_default_value=value)


def _apply_conventions(name: str, config: DtoField) -> DtoField:
    """Apply the id / timestamp conventions the author did not set explicitly."""
    if name == "id":
        return config.with_overrides(in_create_request=False, in_update_request=False)
    if name in ("created_at", "updated_at"):
        config = config.with_overrides(in_create_request=False, in_update_request=False)
        if not config.has_logical_default:
            config = config.with_overrides(logical_default_value_factory=utc_now)
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


def _is_optional(annotation: Any) -> bool:
    """Whether ``annotation`` is a union containing ``NoneType``."""
    if _is_union(annotation):
        return type(None) in get_args(annotation)
    return annotation is type(None)


def _widen_optional(annotation: Any) -> Any:
    """``annotation`` with ``None`` added to its union if not already present."""
    if _is_optional(annotation):
        return annotation
    return annotation | None


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
        create_request=_build_request_model(f"{name}CreateRequest", fields, "in_create_request"),
        create_response=_build_response_model(
            f"{name}CreateResponse", fields, "in_create_response"
        ),
        update_request=_build_request_model(f"{name}UpdateRequest", fields, "in_update_request"),
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


def _build_request_model(
    name: str, fields: Mapping[str, tuple[Any, DtoField]], flag: str
) -> type[BaseModel]:
    """A request model: the flagged fields as plain concrete fields with real defaults.

    A field with a logical default (or an optional type) is optional with a
    real default; otherwise it stays required. The wire format never has to
    carry a sentinel — ``MISSING`` is internal to the DTO.
    """
    model_fields: dict[str, Any] = {}
    for field_name, (annotation, config) in fields.items():
        if not getattr(config, flag):
            continue
        concrete = _strip_annotated(annotation)
        if config.has_logical_default or _is_optional(concrete):
            default = (
                config.logical_default_value if config.logical_default_value is not _UNSET else None
            )
            model_fields[field_name] = (_widen_optional(concrete), default)
        else:
            model_fields[field_name] = (concrete, ...)
    return create_model(name, **model_fields)
