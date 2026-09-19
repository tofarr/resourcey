"""Tests for the shared naming helpers in :mod:`resourcey.util.naming`."""

import pytest

from resourcey.util.naming import camel_to_snake, pluralize


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("User", "user"),
        ("UserRole", "user_role"),
        ("BankAccount", "bank_account"),
        ("HTTPServer", "http_server"),
        ("HTTPSConnection", "https_connection"),
        ("simple", "simple"),
        ("already_snake", "already_snake"),
        ("OAuth2Client", "o_auth2_client"),
        ("ABC", "abc"),
        ("", ""),
    ],
)
def test_camel_to_snake(value, expected):
    assert camel_to_snake(value) == expected


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
    ],
)
def test_pluralize(value, expected):
    assert pluralize(value) == expected
