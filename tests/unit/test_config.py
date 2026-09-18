"""Tests for ``ResourceyConfig``."""

from typing import Annotated

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import Column, Integer

from resourcey.resource.config import ResourceyConfig


def test_default_values():
    cfg = ResourceyConfig()
    assert cfg.creatable is True
    assert cfg.updatable is True
    assert cfg.readable is True
    assert cfg.column is None


def test_custom_values():
    col = Column("x", Integer)
    cfg = ResourceyConfig(creatable=False, updatable=False, readable=False, column=col)
    assert cfg.creatable is False
    assert cfg.updatable is False
    assert cfg.readable is False
    assert cfg.column is col


def test_frozen_prevents_mutation():
    cfg = ResourceyConfig()
    with pytest.raises(ValidationError):
        cfg.creatable = False  # type: ignore[misc]


def test_holds_sqlalchemy_column():
    col = Column("x", Integer, primary_key=True)
    cfg = ResourceyConfig(column=col)
    assert isinstance(cfg.column, Column)


def test_attached_via_annotated_and_retrieved_from_metadata():
    cfg = ResourceyConfig(creatable=False)

    class M(BaseModel):
        x: Annotated[int, cfg]

    field = M.model_fields["x"]
    found = next(m for m in field.metadata if isinstance(m, ResourceyConfig))
    assert found is cfg


def test_model_copy_returns_frozen_config():
    cfg = ResourceyConfig()
    cfg2 = cfg.model_copy(update={"creatable": False})
    assert cfg2.creatable is False
    assert cfg.creatable is True
    with pytest.raises(ValidationError):
        cfg2.updatable = False  # type: ignore[misc]
