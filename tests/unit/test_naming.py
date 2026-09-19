"""Tests for the shared naming helpers in :mod:`resourcey.util.naming`."""

import pytest

from resourcey.util.naming import camel_to_kebab, camel_to_snake, pluralize


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("User", "User"),
        ("UserRole", "User_Role"),
        ("BankAccount", "Bank_Account"),
        ("HTTPServer", "HTTP_Server"),
        ("HTTPSConnection", "HTTPS_Connection"),
        ("simple", "simple"),
        ("already_snake", "already_snake"),
        ("OAuth2Client", "O_Auth2_Client"),
        ("ABC", "ABC"),
        ("", ""),
    ],
)
def test_camel_to_snake(value, expected):
    assert camel_to_snake(value) == expected


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
        ("OAuth2Client", "O-Auth2-Client"),
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
