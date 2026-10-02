"""``TriggerConfig`` — the env-parseable trigger declaration (issue #155).

The core seam is ``TriggeredResource(inner, on_edit=[...])``, built directly
in app code. This module is the **opt-in rung** that makes triggers
deployable from the environment instead: each entry names the resource path it
targets and carries a polymorphic :class:`~resourcey.triggers.trigger.Trigger`,
so a deployment can add a webhook without touching the resource declaration::

    APP_TRIGGERS_0_RESOURCE_PATH=threads
    APP_TRIGGERS_0_TRIGGER_KIND=myapp.webhooks.NotifyWebhook
    APP_TRIGGERS_0_TRIGGER_URL=https://example.com/hook

:meth:`TriggerConfig.resource_triggers` groups the flat list by
``resource_path`` — the same "flat config, grouped at the point of use" shape
:class:`~resourcey.auth.auth_role.RolePolicyResolver` uses for its
per-resource maps — ready for
:class:`~resourcey.triggers.triggered_dependency_builder.TriggeredDependencyBuilder`.

This module is part of ``resourcey.triggers``; it imports only lower framework
layers (``config`` / ``util``).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from resourcey.config.config_base import BaseConfig
from resourcey.triggers.trigger import Trigger


class TriggerEntry(BaseModel):
    """One configured trigger: which resource path it fires on, and the trigger itself.

    Attributes:
        resource_path: The target resource's REST path segment (matching
            :meth:`~resourcey.core.resource.Resource.get_resource_path`).
        trigger: The configured, polymorphic :class:`Trigger` instance.
    """

    resource_path: str
    trigger: Trigger


class TriggerConfig(BaseConfig):
    """The central, env-parseable trigger config.

    Parsed under the process-wide prefix as ``APP_TRIGGERS_<n>_RESOURCE_PATH``
    / ``_TRIGGER_KIND`` (the dotted path of the trigger subclass) plus any
    kind-specific field, the same list-of-nested-polymorphic-entry shape
    :class:`~resourcey.sql.sql_config.SqlConfig` uses for its connections.
    """

    triggers: list[TriggerEntry] = Field(default_factory=list)

    def resource_triggers(self) -> dict[str, list[Trigger]]:
        """Group the configured entries by ``resource_path``, in declaration order."""
        grouped: dict[str, list[Trigger]] = {}
        for entry in self.triggers:
            grouped.setdefault(entry.resource_path, []).append(entry.trigger)
        return grouped
