"""The ``Missing`` sentinel — the single unset-value marker in ``v2``.

No ``__init__.py``: this is the bottom layer of ``v2``, a dependency-free leaf
that every other layer may import. Keeping the sentinel here (rather than in
``v2/core``) means ``v2/util`` imports no project package, so the layer ranks
are a clean ``util < core < {sql, http, config, cache, encryption}``.
"""

from __future__ import annotations

from typing import Any, ClassVar


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
