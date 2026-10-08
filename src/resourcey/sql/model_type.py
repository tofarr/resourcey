"""``ModelType`` — a SQL column whose Python value is a Pydantic model.

A column can round-trip a whole Pydantic model through a single JSON(-like)
column and be *typed* as that model: reading a row hands back the model
instance, and writing one binds its JSON form. This is the pattern a
polymorphic / structured payload column wants — e.g. a job body, an event
envelope, a settings blob — and it composes with the framework's DTO inference:

* the DTO field's annotation is the model type (see
  :func:`~resourcey.sql.sqlalchemy_2_dto.sqlalchemy_2_dto`), so the generated
  request models carry the model's own schema (for a
  :class:`~resourcey.util.models.DiscriminatedUnionMixin`, the discriminated
  union of its kinds); the stored read model keeps the raw JSON shape, since a
  read is what the column *stores*;
* a model value is never a scalar, so :func:`~resourcey.util.search_filter.operators_for_annotation`
  gives it no filter / sort operators and the transport never synthesises a
  query parameter for it.

The column's storage ``impl`` is :class:`~sqlalchemy.JSON` by default —
override it (``ModelType(Details, impl=Text)``) for a column stored as text.
No migration is needed to adopt it over an existing ``JSON`` column: the DDL is
unchanged (identified by the concrete ``impl``).

This module imports only the lower framework layers (``util``); it is part of
``sql``.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel
from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator

T = TypeVar("T", bound=BaseModel)


class ModelType(TypeDecorator[T], Generic[T]):
    """A column typed as the Pydantic model ``model``, stored as JSON.

    Args:
        model: The Pydantic model the column round-trips. A value bound is
            serialized with ``model_dump(mode="json")``; a value read back is
            validated to the model via ``model_validate``.
        impl: The underlying SQL type; :class:`~sqlalchemy.JSON` by default.
    """

    impl = JSON
    cache_ok = True

    def __init__(self, model: type[T], *, impl: Any = JSON) -> None:
        self.model = model
        super().__init__()
        if impl is not JSON:
            self.impl = impl

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        """Serialize a model (or pass a raw mapping) through for storage."""
        if value is None:
            return None
        if isinstance(value, self.model):
            return value.model_dump(mode="json")
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        """Validate a stored value back into the bound model (``None`` stays ``None``)."""
        if value is None:
            return None
        if isinstance(value, self.model):
            return value
        return self.model.model_validate(value)

    def copy(self, **kw: Any) -> ModelType[T]:
        """Preserve the model / impl when SQLAlchemy copies the column type."""
        return ModelType(self.model, impl=self.impl)
