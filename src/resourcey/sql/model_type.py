"""``ModelType`` — a SQL column whose Python value is a Pydantic model.

A column can round-trip a whole Pydantic model through a single JSON(-like)
column and be *typed* as that model: reading a row hands back the model
instance, and writing one binds its JSON form. The DTO inference projects the
model as the field's annotation, so the generated request models carry the
model's own schema, and a model value is never a scalar, so it gets no filter /
sort operators.

The storage ``impl`` is :class:`~sqlalchemy.JSON` by default — override it
(``ModelType(Details, impl=Text)``) for a column stored as text. No migration is
needed to adopt it over an existing ``JSON`` column: the DDL is unchanged.

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
        model: The Pydantic model the column round-trips.
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
