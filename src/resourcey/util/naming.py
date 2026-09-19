"""Shared naming helpers for resourcey.

``camel_to_snake`` converts a CamelCase / PascalCase identifier to
snake_case (the SQL / URL convention), inserting a boundary before an
uppercase letter that follows a lowercase letter or digit, and before a
run of uppercase letters that is followed by a lowercase letter (so
``UserRole`` -> ``user_role`` and ``HTTPServer`` -> ``http_server``).

``pluralize`` appends ``"s"`` — or ``"es"`` when the name ends in ``"s"``,
``"x"``, ``"z"``, ``"ch"``, or ``"sh"`` — a small, predictable rule that
covers the common cases without a full English pluralization table. Both
are pure functions so they are individually testable.
"""

from __future__ import annotations

import re

# Boundary before an uppercase letter that follows a lowercase letter or
# digit ("userRole" -> "user_role"), and before a run of uppercase letters
# that is followed by a lowercase letter ("HTTPServer" -> "http_server").
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def camel_to_snake(name: str) -> str:
    """Convert a CamelCase / PascalCase identifier to ``snake_case``."""
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def pluralize(name: str) -> str:
    """Append a simple English plural suffix to ``name``.

    Covers the common endings (``s`` / ``x`` / ``z`` / ``ch`` / ``sh`` -> ``es``);
    otherwise appends ``s``. Not a full pluralization engine — override
    ``get_resource_path`` / ``get_table_name`` for irregular cases.
    """
    lowered = name.lower()
    if lowered.endswith(("s", "x", "z", "ch", "sh")):
        return lowered + "es"
    return lowered + "s"
