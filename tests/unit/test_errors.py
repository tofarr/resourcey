"""Tests for the framework error types."""

import pytest

from resourcey.resource.errors import ResourceyConfigError, ResourceyError


def test_resourcey_config_error_is_resourcey_error():
    assert issubclass(ResourceyConfigError, ResourceyError)


def test_resourcey_error_is_exception():
    assert issubclass(ResourceyError, Exception)


def test_can_catch_all_with_base():
    with pytest.raises(ResourceyError):
        raise ResourceyConfigError("bad config")


def test_message_preserved():
    err = ResourceyConfigError("no id field")
    assert str(err) == "no id field"
