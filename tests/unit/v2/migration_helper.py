"""A declarative base for ``v2`` migration tests, resolvable by dotted path."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class MigrationBase(DeclarativeBase):
    """A base the migration tests point ``generate_migration`` at."""
