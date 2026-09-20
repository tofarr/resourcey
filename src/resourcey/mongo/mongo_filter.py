"""Translate a :class:`~resourcey.util.search_filter.SearchFilter` into a Mongo query.

The storage-agnostic contract on ``SearchFilter`` is :meth:`matches`
(in-memory predicate). SQL backends use ``filter_sql`` / ``sql_condition``;
this module provides the Mongo equivalent — a translator that walks the same
filter structure and produces a Mongo query ``dict`` (``None`` = no
restriction, matching every document).

Keeping the translator in the ``resourcey.mongo`` package (rather than adding
a ``to_query`` method to the core ``SearchFilter`` hierarchy) preserves the
principle that the core is storage-agnostic: Mongo operator names
(``$eq``, ``$ne``, ``$lt`` …) live here, not in ``resourcey.util``. The
translator introspects the filter's public structure, so it works with any
``SearchFilter`` subclass without modifying the core.

Operator mapping (mirrors the SQL ``_OPS`` table in
``resourcey.util.search_filter``):

============ =================================================================
operator     Mongo operator
============ =================================================================
eq           ``$eq`` (or a bare value for equality)
ne           ``$ne``
lt           ``$lt``
lte          ``$lte``
gt           ``$gt``
gte          ``$gte``
contains     ``$regex`` (case-insensitive substring match)
in           ``$in``
============ =================================================================
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from resourcey.util.search_filter import (
        AndSearchFilter,
        AttributeFilter,
        BaseSearchFilter,
        OrSearchFilter,
        SearchFilter,
    )


# Mongo operator for each resourcey filter operator name. ``contains`` maps
# to ``$regex`` with case-insensitive matching (``$options: "i"``), mirroring
# the SQL ``ilike`` case-insensitive substring semantics.
_MONGO_OPS: dict[str, str] = {
    "eq": "$eq",
    "ne": "$ne",
    "lt": "$lt",
    "lte": "$lte",
    "gt": "$gt",
    "gte": "$gte",
    "in": "$in",
}


def to_mongo_query(
    filter_obj: SearchFilter[Any] | None,
    *,
    id_field: str = "id",
) -> dict[str, Any] | None:
    """Translate a ``SearchFilter`` into a Mongo query ``dict``.

    Returns ``None`` when ``filter_obj`` is ``None`` or the filter imposes no
    restriction (e.g. an unset ``BaseSearchFilter`` or ``AllSearchFilter``),
    so the caller can skip the ``filter`` argument entirely.

    ``id_field`` is the resource's id field name — Mongo stores it under
    ``_id``, so a filter on the id field is rewritten to query ``_id``.
    """
    if filter_obj is None:
        return None
    return _translate(filter_obj, id_field)


def _translate(filter_obj: SearchFilter[Any], id_field: str) -> dict[str, Any] | None:
    """Dispatch on the filter's concrete type and return a Mongo query dict (or None)."""
    from resourcey.util.search_filter import (
        AllSearchFilter,
        AndSearchFilter,
        AttributeFilter,
        BaseSearchFilter,
        NoneSearchFilter,
        OrSearchFilter,
    )

    if isinstance(filter_obj, AllSearchFilter):
        return None
    if isinstance(filter_obj, NoneSearchFilter):
        # Mongo has no "match nothing" operator; ``_id: None`` never matches a
        # required ``_id``, so it is a reliable always-false predicate.
        return {"_id": None}
    if isinstance(filter_obj, AndSearchFilter):
        return _translate_and(filter_obj, id_field)
    if isinstance(filter_obj, OrSearchFilter):
        return _translate_or(filter_obj, id_field)
    if isinstance(filter_obj, AttributeFilter):
        return _translate_attribute(filter_obj, id_field)
    if type(filter_obj) is BaseSearchFilter:
        return _translate_base(filter_obj, id_field)
    if isinstance(filter_obj, BaseSearchFilter):
        # A subclass with extra clauses: translate the base clauses and rely
        # on the subclass overriding ``_active_clauses`` to expose its own.
        return _translate_base(filter_obj, id_field)
    # Unknown filter type: fall back to in-memory via ``matches`` is not
    # possible here (we have no item to test), so raise so the developer
    # extends the translator for custom filters.
    raise TypeError(
        f"Cannot translate {type(filter_obj).__name__} to a Mongo query; "
        f"extend resourcey.mongo.mongo_filter for custom SearchFilter subclasses."
    )


def _translate_base(filter_obj: BaseSearchFilter[Any], id_field: str) -> dict[str, Any] | None:
    """Translate a ``BaseSearchFilter`` by walking its active ``<attr>__<op>`` clauses."""
    clauses: dict[str, Any] = {}
    for attr, op, value in filter_obj._active_clauses():
        field = _id_field_name(attr, id_field)
        if op == "contains":
            safe = _escape_regex(str(value))
            clauses[field] = {"$regex": safe, "$options": "i"}
        else:
            mongo_op = _MONGO_OPS[op]
            clauses[field] = {mongo_op: _encode(value)}
    if not clauses:
        return None
    return clauses


def _translate_attribute(filter_obj: AttributeFilter[Any], id_field: str) -> dict[str, Any] | None:
    """Translate an ``AttributeFilter`` (runtime-specified attribute + condition)."""
    field = _id_field_name(filter_obj.attribute, id_field)
    op = filter_obj.condition.value
    value = filter_obj.value
    if op == "contains":
        safe = _escape_regex(str(value))
        return {field: {"$regex": safe, "$options": "i"}}
    mongo_op = _MONGO_OPS[op]
    return {field: {mongo_op: _encode(value)}}


def _translate_and(filter_obj: AndSearchFilter[Any], id_field: str) -> dict[str, Any] | None:
    """Translate a conjunction: ``{$and: [child_queries...]}``.

    A child that translates to ``None`` (no restriction) is dropped. If all
    children are ``None``, the conjunction is ``None`` (matches everything).
    """
    parts: list[dict[str, Any]] = []
    for child in filter_obj.filters:
        q = _translate(child, id_field)
        if q is not None:
            parts.append(q)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return {"$and": parts}


def _translate_or(filter_obj: OrSearchFilter[Any], id_field: str) -> dict[str, Any] | None:
    """Translate a disjunction: ``{$or: [child_queries...]}``.

    A child that translates to ``None`` (matches everything) makes the whole
    disjunction match everything, so the result is ``None``. If no children
    remain after dropping ``None``-condition children that match nothing, the
    disjunction matches nothing (``{_id: None}``).
    """
    parts: list[dict[str, Any]] = []
    for child in filter_obj.filters:
        q = _translate(child, id_field)
        if q is None:
            # Any child matching everything makes the OR match everything.
            return None
        parts.append(q)
    if not parts:
        return {"_id": None}
    if len(parts) == 1:
        return parts[0]
    return {"$or": parts}


def _id_field_name(attr: str, id_field: str) -> str:
    """Map the resource's id field name to Mongo's ``_id`` for query purposes."""
    return "_id" if attr == id_field else attr


def _escape_regex(value: str) -> str:
    """Escape regex metacharacters in a substring so ``$regex`` matches literally.

    Mirrors the SQL path's stripping of ``%`` / ``_`` wildcards: a user-supplied
    ``.`` or ``*`` must match literally, not as a regex quantifier.
    """
    return _regex_escape(value)


def _regex_escape(value: str) -> str:
    # ``re.escape`` escapes every non-alphanumeric; that is correct and safe
    # for a literal substring match. Imported lazily so the module import is
    # cheap.
    import re

    return re.escape(value)


def _encode(value: Any) -> Any:
    """Encode a value for a Mongo query (UUID -> string, others passthrough).

    Mirrors the encoding in :func:`resourcey.mongo.mongo_service._encode_value`:
    ``uuid.UUID`` is stored as a string for bson compatibility.
    """
    from uuid import UUID

    if isinstance(value, UUID):
        return str(value)
    return _naive(value)


def _naive(value: Any) -> Any:
    """Normalize datetimes to UTC for comparison, mirroring the SQL path.

    Mongo stores datetimes in UTC natively, but an aware datetime with a
    non-UTC offset compared against a UTC-stored value can mismatch; convert
    to UTC for parity with the SQL ``_naive`` helper.
    """
    from datetime import datetime

    if isinstance(value, datetime) and value.tzinfo is not None:
        from datetime import UTC

        return value.astimezone(UTC)
    return value
