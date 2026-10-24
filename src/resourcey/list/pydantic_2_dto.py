"""Infer a DTO declaration from a plain Pydantic model (issue #116).

A backend serves a :class:`~resourcey.core.dto.DTO` declaration, so the list
backend needs the read-model analogue of ``sqlalchemy_2_dto``. It maps each
Pydantic field to a DTO field, keeping declaration order and nullability, and
carries the identifier across.

A model author may override the inferred projection by attaching a
:class:`~resourcey.core.dto.DtoField` to a field — either via an ``Annotated``
tag or via ``Field(json_schema_extra={"dto_field": ...})``::

    class Country(BaseModel):
        id: str
        name: str
        internal_code: Annotated[str, DtoField(in_read_response=False)]

A field with no explicit ``DtoField`` is left bare so the ``core`` conventions
apply; a list resource is read-only, so any create / update defaults those
conventions emit are unused.

This module imports no code outside the framework.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from resourcey.core.dto import DTO, DtoField

# The ``Field(json_schema_extra=...)`` key under which a developer supplies an
# explicit ``DtoField`` for a field; absent it, the field is left to the
# ``core`` conventions.
DTO_FIELD_INFO_KEY = "dto_field"


def pydantic_2_dto(
    model: type[BaseModel], *, name: str | None = None, id_field_name: str | None = None
) -> type[DTO]:
    """Infer a DTO declaration from a plain Pydantic model.

    A field carrying an explicit :class:`DtoField` uses it verbatim; every other
    field is declared bare so the ``core`` conventions apply. The named
    ``id_field_name`` must exist or ``DTO`` raises at declaration time.
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
        # ``core`` conventions decorate it; only an explicit intent is set.
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

    An ``Annotated[T, DtoField(...)]`` tag wins over a
    ``Field(json_schema_extra={"dto_field": ...})`` entry.
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
