"""Shared naming helpers for ``v2``.

``camel_to_kebab`` inserts a boundary before an uppercase letter that follows a
lowercase letter or digit, and before a run of uppercase letters that is
followed by a lowercase letter (so ``UserRole`` -> ``User-Role`` and
``HTTPServer`` -> ``HTTP-Server``). It does not lowercase — that is applied at
the final call site (``get_resource_path``) so the cased, separated form stays
available to callers that need it.

``pluralize`` appends ``"s"`` — or ``"es"`` when the name ends in ``"s"``,
``"x"``, ``"z"``, ``"ch"``, or ``"sh"`` — a small, predictable rule that covers
the common cases without a full English pluralization table. The ending check is
case-insensitive but the input case is preserved. Both are pure functions so
they are individually testable.

This module is part of the ``v2/util`` bottom layer: it imports no project
package.
"""

from __future__ import annotations

import re

# Boundary before an uppercase letter that follows a lowercase letter or digit
# ("userRole" -> "user-role"), and before a run of 2+ uppercase letters that is
# followed by a lowercase letter ("HTTPServer" -> "HTTP-Server"). The {2}
# lookbehind avoids splitting a single leading uppercase char off a following
# word, so "OAuth2Client" -> "OAuth2-Client" (not "O-Auth2Client").
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z]{2})(?=[A-Z][a-z])")


def camel_to_kebab(name: str) -> str:
    """Insert ``-`` boundaries into a CamelCase / PascalCase identifier.

    Does not lowercase — call ``.lower()`` at the final call site so a caller
    can inspect or further transform the cased, separated form.
    """
    return _CAMEL_BOUNDARY.sub("-", name)


def pluralize(name: str) -> str:
    """Append a simple English plural suffix to ``name``, preserving its case.

    Covers the common endings (``s`` / ``x`` / ``z`` / ``ch`` / ``sh`` -> ``es``);
    otherwise appends ``s``. The ending check is case-insensitive but the input
    case is preserved — lowercasing is the caller's responsibility. Not a full
    pluralization engine — override ``get_resource_path`` for irregular cases.
    """
    if name.lower().endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    return name + "s"
