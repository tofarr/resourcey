"""The ``Message`` resource — the child side of the board.

The framework is model-first: ``Message`` the ORM model (in
:mod:`webhooks_example.models`) is the schema of record, and
:class:`~resourcey.sql.sql_resource.SqlResource` infers the DTO and the REST
models from it. No trigger-specific code lives here — ``messages`` gets its
webhook trigger from the config-driven
:class:`~resourcey.triggers.trigger_config.TriggerConfig` /
:class:`~resourcey.triggers.triggered_dependency_builder.TriggeredDependencyBuilder`
rung in :mod:`webhooks_example.app`, so the resource declaration itself is
untouched.
"""

from __future__ import annotations

from typing import Any

from resourcey.sql.sql_resource import SqlResource


class MessageResource(SqlResource[Any, Any]):
    """The ``Message`` ORM model exposed with the derived query surface."""
