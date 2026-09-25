"""Tests for the ``v2`` naming helpers in :mod:`resourcey.v2.util.naming`."""

from __future__ import annotations

import pytest

from resourcey.v2.util.naming import camel_to_kebab, pluralize


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("User", "User"),
        ("UserRole", "User-Role"),
        ("BankAccount", "Bank-Account"),
        ("HTTPServer", "HTTP-Server"),
        ("HTTPSConnection", "HTTPS-Connection"),
        ("simple", "simple"),
        ("already-kebab", "already-kebab"),
        ("OAuth2Client", "OAuth2-Client"),
        ("ABC", "ABC"),
        ("", ""),
    ],
)
def test_camel_to_kebab(value, expected):
    assert camel_to_kebab(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("user", "users"),
        ("box", "boxes"),
        ("church", "churches"),
        ("brush", "brushes"),
        ("quiz", "quizes"),
        ("role", "roles"),
        ("class", "classes"),
        ("data", "datas"),
        # case is preserved, not lowercased by pluralize
        ("Box", "Boxes"),
        ("CLASS", "CLASSes"),
    ],
)
def test_pluralize(value, expected):
    assert pluralize(value) == expected
