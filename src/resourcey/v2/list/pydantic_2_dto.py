"""Infer a ``v2`` DTO declaration from a plain Pydantic model (issue #116).

``v2`` is DTO-first internally: a backend serves a
:class:`~resourcey.v2.core.dto.DTO` declaration and the six REST models derive
from it. A plain Pydantic model is *not* a DTO, so the list backend needs a
``pydantic_2_dto`` — the read-model analogue of
:func:`~resourcey.v2.sql.sqlalchemy_2_dto.sqlalchemy_2_dto`. It maps each
Pydantic field (annotation + ``Field`` metadata) to a DTO field, keeping the
declaration order and nullability, and carries the identifier across.

A model author may override the inferred projection by attaching a
:class:`~resourcey.v2.core.dto.DtoField` to a field — either via an
``Annotated`` tag or via ``Field(json_schema_extra={"dto_field": ...})``::

    class Country(BaseModel):
        id: str
        name: str
        internal_code: Annotated[str, DtoField(in_read_response=False)]

The explicit ``DtoField`` is honoured verbatim (the ``v2/core`` conventions
leave it alone), so a model can hide a field from the read model exactly as a
SQL column does via ``info["dto_field"]``. A field with no explicit
``DtoField`` is left bare so the ``v2/core`` conventions apply (the
identifier's immutability, the ``created_at`` / ``updated_at`` rules); a list
resource is read-only, so any create / update defaults those conventions emit
are simply unused.

This module is part of ``v2/``: it imports no ``resourcey`` code outside ``v2/``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from resourcey.v2.core.dto import DTO, DtoField

# The ``Field(json_schema_extra=...)`` key under which a developer supplies an
# explicit ``DtoField`` for a field; absent it, the field is left to the
# ``v2/core`` conventions.
DTO_FIELD_INFO_KEY = "dto_field"


def pydantic_2_dto(
    model: type[BaseModel], *, name: str | None = None, id_field_name: str | None = None
) -> type[DTO]:
    """Infer a DTO declaration from a plain Pydantic model.

    The DTO's fields mirror the model's fields in declaration order. A field
    carrying an explicit :class:`DtoField` (an ``Annotated`` tag or
    ``Field(json_schema_extra={"dto_field": ...})``) uses it verbatim; every
    other field is declared bare so the ``v2/core`` conventions (identifier
    immutability, the timestamp rules) apply.

    Args:
        model: A Pydantic ``BaseModel`` subclass.
        name: The DTO class name (defaults to the model's name).
        id_field_name: The identifier field (defaults to ``"id"``). The named
            field must exist or ``DTO`` raises at declaration time.
    """
    annotations: dict[str, Any] = {}
    explicit: dict[str, DtoField] = {}
    for field_name, field in model.model_fields.items():
        annotations[field_name] = field.annotation
        config = _explicit_dto_field(field)
        if config is not None:
            explicit[field_name] = config
    namespace: dict[str, Any] = {
        "__annotations__": annotations,
        "__module__": getattr(model, "__module__", __name__),
        # A bare field (no explicit ``DtoField``) is left undeclared so the
        # ``v2/core`` conventions decorate it; only an explicit intent is set.
        **explicit,
    }
    return type(
        name or model.__name__,
        (DTO,),
        namespace,
        id_field_name=id_field_name or "id",
    )


def _explicit_dto_field(field: FieldInfo) -> DtoField | None:
    """The author-supplied ``DtoField`` for a field, or ``None``.

    An ``Annotated[T, DtoField(...)]`` tag wins; otherwise a
    ``Field(json_schema_extra={"dto_field": ...})`` entry is read. The two are
    equivalent ways to express the same intent.
    """
    for meta in field.metadata:
        if isinstance(meta, DtoField):
            return meta
    extra = field.json_schema_extra
    if isinstance(extra, dict):
        candidate = extra.get(DTO_FIELD_INFO_KEY)
        if isinstance(candidate, DtoField):
            return candidate
    return None
