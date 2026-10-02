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

from typing import Annotated, Any

from pydantic import BaseModel, Field, SkipValidation, field_validator

from resourcey.config.config_base import BaseConfig
from resourcey.triggers.trigger import Trigger

# A nested ``Trigger`` field is validated by the ``mode="before"`` resolver
# below (which routes a wire ``dict`` through ``Trigger.model_validate`` and
# accepts an already-built instance as-is) and then accepted verbatim.
# Without ``SkipValidation`` a *parameterised* generic annotation
# (``Trigger[Any, Any]``) re-enters the discriminated-union validator on an
# already-built instance, which the mixin's ``data.pop("kind")`` path cannot
# handle -- the same reason
# :data:`~resourcey.util.search_filter.NestedFilter` needs it.
TriggerField = Annotated["Trigger[Any, Any]", SkipValidation]


def _resolve_trigger(value: Any) -> Any:
    if isinstance(value, dict):
        return Trigger.model_validate(value)
    return value


class TriggerEntry(BaseModel):
    """One configured trigger: which resource path it fires on, and the trigger itself.

    Attributes:
        resource_path: The target resource's REST path segment (matching
            :meth:`~resourcey.core.resource.Resource.get_resource_path`).
        trigger: The configured, polymorphic :class:`Trigger` instance. Typed
            ``Trigger[Any, Any]`` here -- a config entry cannot know the
            target resource's DTO / id types ahead of time (it is resolved by
            resource path, not type).
    """

    resource_path: str
    trigger: TriggerField

    @field_validator("trigger", mode="before")
    @classmethod
    def _resolve(cls, value: Any) -> Any:
        return _resolve_trigger(value)


class TriggerConfig(BaseConfig):
    """The central, env-parseable trigger config.

    Parsed under the process-wide prefix as ``APP_TRIGGERS_<n>_RESOURCE_PATH``
    / ``_TRIGGER_KIND`` (the dotted path of the trigger subclass) plus any
    kind-specific field, the same list-of-nested-polymorphic-entry shape
    :class:`~resourcey.sql.sql_config.SqlConfig` uses for its connections.
    """

    triggers: list[TriggerEntry] = Field(default_factory=list)

    def resource_triggers(self) -> dict[str, list[Trigger[Any, Any]]]:
        """Group the configured entries by ``resource_path``, in declaration order."""
        grouped: dict[str, list[Trigger[Any, Any]]] = {}
        for entry in self.triggers:
            grouped.setdefault(entry.resource_path, []).append(entry.trigger)
        return grouped
