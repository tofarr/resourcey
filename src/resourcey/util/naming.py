"""Shared, dependency-free naming helpers.

This module is part of the ``util`` bottom layer: it imports no project
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
    """Insert ``-`` boundaries into a CamelCase / PascalCase identifier, preserving case."""
    return _CAMEL_BOUNDARY.sub("-", name)


def camel_to_snake(name: str) -> str:
    """Like :func:`camel_to_kebab`, with ``_`` as the separator."""
    return _CAMEL_BOUNDARY.sub("_", name)


def pluralize(name: str) -> str:
    """Append a simple English plural suffix, preserving case.

    ``es`` after ``s`` / ``x`` / ``z`` / ``ch`` / ``sh``, else ``s``. Not a full
    pluralization engine — override ``get_resource_path`` for irregular cases.
    """
    if name.lower().endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    return name + "s"


def singularise(name: str) -> str:
    """Strip a trailing ``es`` (else ``s``), the inverse of :func:`pluralize`.

    ``"es"`` takes precedence so ``"statuses"`` -> ``"status"``; a name with no
    plural suffix is returned unchanged.
    """
    lowered = name.lower()
    if lowered.endswith("es"):
        return name[:-2]
    if lowered.endswith("s"):
        return name[:-1]
    return name


def humanize(name: str) -> str:
    """Render an identifier as Title Case words (``"api-keys"`` -> ``"Api Keys"``)."""
    words = re.split(r"[-_\s]+", camel_to_kebab(name.strip()))
    return " ".join(word[:1].upper() + word[1:].lower() for word in words if word)
