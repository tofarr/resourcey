"""The framework ``MISSING`` sentinel and JSON-schema support.

``MISSING`` is a framework-specific sentinel used as the default for optional
fields in generated create / update models. It lets the service layer tell
whether a value was explicitly supplied by the REST client versus simply
unset, which is required for PATCH semantics.

It is intentionally distinct from ``dataclasses.MISSING`` and Pydantic's
internal ``PydanticUndefined`` — neither of those can be reused because both
carry framework-specific behaviour that would leak into resourcey's models.

Because ``pyproject.toml`` sets ``filterwarnings = ["error"]``, the default
JSON-schema generation cannot be used unchanged: Pydantic emits
``PydanticJsonSchemaWarning: Default value MISSING is not JSON serializable``
for fields defaulting to ``MISSING``. Rather than suppress the warning,
``MissingJsonSchema`` overrides ``get_default_value`` so ``MISSING`` defaults
are treated as "no default" — the field shows up as optional in the schema
with no ``default`` key, and the warning is never raised.
"""

from __future__ import annotations

from typing import Any, Mapping

from pydantic.json_schema import GenerateJsonSchema, NoDefault


class _Missing:
    """Singleton sentinel marking an unset value in generated models.

    Identity comparison (``value is MISSING``) is the supported way to test
    for it. It is falsy and copy-safe (``copy`` / ``deepcopy`` return the same
    singleton) so it behaves sensibly inside Pydantic's default handling.
    """

    _instance: _Missing | None = None

    def __new__(cls) -> _Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False

    def __copy__(self) -> _Missing:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _Missing:
        return self


MISSING: _Missing = _Missing()


class MissingJsonSchema(GenerateJsonSchema):
    """JSON-schema generator that omits ``MISSING`` defaults.

    A ``MISSING`` default is not a real value and must not appear in the
    generated schema. Overriding ``get_default_value`` to return ``NoDefault``
    for it makes Pydantic treat the field as having no default — it is shown
    as optional without a ``default`` key — and skips the
    ``non-serializable-default`` code path entirely, so no
    ``PydanticJsonSchemaWarning`` is raised.
    """

    def get_default_value(self, schema: Mapping[str, Any]) -> Any:
        default = schema.get("default", NoDefault)
        if default is MISSING:
            return NoDefault
        return default
