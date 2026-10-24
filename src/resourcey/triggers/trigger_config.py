"""``TriggerConfig`` — the env-parseable trigger declaration (issue #155).

The core seam is ``TriggeredResource(inner, on_edit=[...])``, built directly in
app code. This module is the **opt-in rung** that makes triggers deployable from
the environment instead: each entry names the resource path it targets and carries
a polymorphic ``Trigger``::

    APP_TRIGGERS_0_RESOURCE_PATH=threads
    APP_TRIGGERS_0_TRIGGER_KIND=myapp.webhooks.NotifyWebhook
    APP_TRIGGERS_0_TRIGGER_URL=https://example.com/hook

:meth:`TriggerConfig.resource_triggers` groups the flat list by
``resource_path`` — the same "flat config, grouped at the point of use" shape
``RolePolicyResolver`` uses.

This module is part of ``resourcey.triggers``; it imports only lower framework
layers (``config`` / ``util``).
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, SkipValidation, field_validator

from resourcey.config.config_base import BaseConfig
from resourcey.triggers.trigger import Trigger

# Without ``SkipValidation`` a parameterised generic annotation re-enters the
# discriminated-union validator on an already-built instance, which the mixin
# cannot handle -- the same reason ``NestedFilter`` needs it.
TriggerField = Annotated["Trigger[Any, Any]", SkipValidation]


def _resolve_trigger(value: Any) -> Any:
    if isinstance(value, dict):
        return Trigger.model_validate(value)
    return value


class TriggerEntry(BaseModel):
    """One configured trigger: which resource path it fires on, and the trigger itself.

    ``trigger`` is typed ``Trigger[Any, Any]``: a config entry is resolved by
    resource path, not type, so it cannot know the target's DTO / id types.
    """

    resource_path: str
    trigger: TriggerField

    @field_validator("trigger", mode="before")
    @classmethod
    def _resolve(cls, value: Any) -> Any:
        return _resolve_trigger(value)


class TriggerConfig(BaseConfig):
    """The central, env-parseable trigger config.

    Parsed as ``APP_TRIGGERS_<n>_RESOURCE_PATH`` / ``_TRIGGER_KIND`` plus any
    kind-specific field.
    """

    triggers: list[TriggerEntry] = Field(default_factory=list)

    def resource_triggers(self) -> dict[str, list[Trigger[Any, Any]]]:
        """Group the configured entries by ``resource_path``, in declaration order."""
        grouped: dict[str, list[Trigger[Any, Any]]] = {}
        for entry in self.triggers:
            grouped.setdefault(entry.resource_path, []).append(entry.trigger)
        return grouped
