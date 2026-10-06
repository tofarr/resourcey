"""Shared naming helpers.

``camel_to_kebab`` inserts a boundary before an uppercase letter that follows a
lowercase letter or digit, and before a run of uppercase letters that is
followed by a lowercase letter (so ``UserRole`` -> ``User-Role`` and
``HTTPServer`` -> ``HTTP-Server``). It does not lowercase — that is applied at
the final call site (``get_resource_path``) so the cased, separated form stays
available to callers that need it.

``pluralize`` appends ``"s"`` — or ``"es"`` when the name ends in ``"s"``,
``"x"``, ``"z"``, ``"ch"``, or ``"sh"`` — a small, predictable rule that covers
the common cases without a full English pluralization table. The ending check is
case-insensitive but the input case is preserved. ``singularise`` strips one trailing
``"s"`` (or ``"es"`` to nominate the plural that matches :func:`pluralize`'s rule),
so an FK's target table name can be matched back to a resource path.

``humanize`` renders an identifier as Title Case words (``"api-keys"`` ->
``"Api Keys"``, ``"ConfigApiKeyView"`` -> ``"Config Api Key View"``), for a
human-readable display name such as an OpenAPI tag. All are pure functions so
they are individually testable.

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
    """Insert ``-`` boundaries into a CamelCase / PascalCase identifier.

    Does not lowercase — call ``.lower()`` at the final call site so a caller
    can inspect or further transform the cased, separated form.
    """
    return _CAMEL_BOUNDARY.sub("-", name)


def camel_to_snake(name: str) -> str:
    """Insert ``_`` boundaries into a CamelCase / PascalCase identifier.

    The same boundaries as :func:`camel_to_kebab`, with ``_`` as the separator.
    Does not lowercase; the caller applies case as needed.
    """
    return _CAMEL_BOUNDARY.sub("_", name)


def pluralize(name: str) -> str:
    """Append a simple English plural suffix to ``name``, preserving its case.

    Covers the common endings (``s`` / ``x`` / ``z`` / ``ch`` / ``sh`` -> ``es``);
    otherwise appends ``s``. The ending check is case-insensitive but the input
    case is preserved — lowercasing is the caller's responsibility. Not a full
    pluralization engine — override ``get_resource_path`` for irregular cases.

    This is the inverse of :func:`singularise`: ``singularise(pluralize(x)) == x``
    when ``x`` does not end in a plural suffix itself.

    """
    if name.lower().endswith(("s", "x", "z", "ch", "sh")):
        return name + "es"
    return name + "s"


def singularise(name: str) -> str:
    """Strip one trailing ``s`` (or ``es`` to nominate the plural rule's form).

    The direct inverse of :func:`pluralize`: singularising a plural restores the
    unpluralized name; calling it on a name that does not end in a plural suffix
    returns it unchanged. The ending/normalisation checks are case-insensitive,
    but the result preserves the input's casing (so ``"Threads"`` -> ``"Thread"``).
    ``"es"`` takes precedence over a bare ``"s"`` so ``"statuses"`` -> ``"status"``,
    and the plural suffix must be a *complete* trailing segment (``"bus"`` -> ``"bu"``,
    not ``"b"``; a word like ``"alias"`` ends in ``"s"`` and is singularised).
    Not a full English singularization engine — used to map an FK's target table
    name back to a resource path.
    """
    lowered = name.lower()
    if lowered.endswith("es"):
        return name[:-2]
    if lowered.endswith("s"):
        return name[:-1]
    return name


def humanize(name: str) -> str:
    """Render an identifier as Title Case words.

    Splits on camel-case boundaries (via :func:`camel_to_kebab`) and on kebab /
    snake case separators, then capitalizes each word: ``"api-keys"`` ->
    ``"Api Keys"``, ``"ConfigApiKeyView"`` -> ``"Config Api Key View"``. A
    human-readable display name (e.g. an OpenAPI tag) for a value derived from a
    machine identifier. An empty or separator-only input yields ``""``.
    """
    words = re.split(r"[-_\s]+", camel_to_kebab(name.strip()))
    return " ".join(word[:1].upper() + word[1:].lower() for word in words if word)
