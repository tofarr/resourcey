"""``TriggeredDependencyBuilder`` — config-driven triggers, no resource edits (issue #155).

``TriggeredResource(inner, on_edit=[...])`` is the core seam. This is the opt-in
*rung* on top: a ``DependencyBuilder`` that resolves configured triggers **per
resource path** and wraps that resource's service in a ``TriggeredService``, so a
deployment can attach a webhook to an existing app with no resource change.

It **composes with an inner builder** rather than replacing it. A resource with
no configured triggers passes through to the inner builder's dependency
unchanged, and a resource that already supplies its own ``on_edit`` is never
wrapped a second time.

``get_service_dependency(resource)`` is called **once per resource, at
registration**, so one ``TriggerRunner`` per resource is created there and
shared by every request's service. Add the same builder to
``Manifest(managers=[...])`` to drain every runner on shutdown.

This module is part of ``resourcey.triggers``; it imports only lower framework
layers (``core`` / ``util`` / ``http``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

from fastapi import Depends, Request
from pydantic import Field, PrivateAttr, SkipValidation, field_validator

from resourcey.core.resource import Resource
from resourcey.core.service import Service
from resourcey.http.dependency_builder import DependencyBuilder, OpenDependencyBuilder
from resourcey.triggers.trigger import ResourceBoundTrigger, Trigger
from resourcey.triggers.trigger_config import TriggerConfig
from resourcey.triggers.trigger_runner import TriggerRunner
from resourcey.triggers.triggered_resource import TriggeredResource
from resourcey.triggers.triggered_service import TriggeredService

# See ``trigger_config.TriggerField`` -- the same ``SkipValidation`` +
# ``mode="before"`` resolution a nested, parameterised ``Trigger[Any, Any]``
# annotation needs so an already-built instance is accepted as-is.
ResourceTriggersField = Annotated["dict[str, list[Trigger[Any, Any]]]", SkipValidation]


def _resolve_resource_triggers(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {
        path: [
            Trigger.model_validate(entry) if isinstance(entry, dict) else entry
            for entry in triggers
        ]
        for path, triggers in value.items()
    }


class TriggeredDependencyBuilder(DependencyBuilder):
    """Wrap a resource's service in a ``TriggeredService`` when triggers are configured.

    ``inner`` is the builder whose dependency is wrapped, so this composes in
    front of an authorization builder without replacing it.
    """

    inner: DependencyBuilder = OpenDependencyBuilder()
    resource_triggers: ResourceTriggersField = Field(default_factory=dict)
    background: bool = True

    _runners: list[TriggerRunner] = PrivateAttr(default_factory=list)

    @field_validator("resource_triggers", mode="before")
    @classmethod
    def _resolve_resource_triggers(cls, value: Any) -> Any:
        return _resolve_resource_triggers(value)

    @classmethod
    def from_config(
        cls,
        config: TriggerConfig,
        *,
        inner: DependencyBuilder | None = None,
        background: bool = True,
    ) -> TriggeredDependencyBuilder:
        """Build a resolver from an env-parsed :class:`TriggerConfig`.

        An app reads its trigger config once and passes it here instead of
        grouping ``resource_path -> triggers`` by hand.
        """
        return cls(
            inner=inner if inner is not None else OpenDependencyBuilder(),
            resource_triggers=config.resource_triggers(),
            background=background,
        )

    def get_principal_dependency(self) -> Callable[..., Any] | None:
        """Delegated to ``inner`` — triggers are an authorization-neutral concern."""
        return self.inner.get_principal_dependency()

    def get_service_dependency(self, resource: Resource[Any, Any]) -> Callable[..., Any]:
        """The inner builder's dependency, wrapped in a ``TriggeredService`` when configured.

        No-ops when the resource has no configured triggers, or is already a
        ``TriggeredResource`` — a resource that already fires its own triggers
        is never double-wrapped.
        """
        inner_dependency = self.inner.get_service_dependency(resource)
        triggers = self._triggers_for(resource)
        if not triggers:
            return inner_dependency

        background = self.background
        runner = TriggerRunner()
        self._runners.append(runner)

        async def dependency(
            inner_service: Service[Any, Any] = Depends(inner_dependency),  # noqa: B008
        ) -> AsyncIterator[Service[Any, Any]]:
            service: Service[Any, Any] = TriggeredService(
                inner_service, triggers, background=background, runner=runner
            )
            async with service:
                yield service

        return dependency

    async def build_service(
        self, request: Request, resource: Resource[Any, Any]
    ) -> Service[Any, Any]:
        """The inner builder's service, wrapped in a ``TriggeredService`` when configured.

        Mirrors :meth:`get_service_dependency` so a runtime-selected resource
        (the cross-resource ``batch-edit``) fires its triggers too. The runner is
        created here rather than at registration — this route resolves a resource
        per request, so there is no registration-time point to hang it off. It is
        still drained, because every runner is appended to the same ``_runners``
        list this builder drains on ``__aexit__``.
        """
        inner_service = await self.inner.build_service(request, resource)
        triggers = self._triggers_for(resource)
        if not triggers:
            return inner_service

        runner = TriggerRunner()
        self._runners.append(runner)
        service: Service[Any, Any] = TriggeredService(
            inner_service, triggers, background=self.background, runner=runner
        )
        return service

    def _triggers_for(self, resource: Resource[Any, Any]) -> list[Trigger[Any, Any]]:
        """The configured triggers for ``resource``, or ``[]`` when it must pass through.

        Empty when the resource has no configured triggers, or already fires its
        own — a ``TriggeredResource`` is never double-wrapped.
        """
        if isinstance(resource, TriggeredResource):
            return []
        triggers = self.resource_triggers.get(resource.get_resource_path())
        if not triggers:
            return []
        # Binding is idempotent, so calling it from both the registration-time
        # path and the runtime-selected one is safe.
        for trigger in triggers:
            if isinstance(trigger, ResourceBoundTrigger):
                trigger.bind_resource(resource)
        return list(triggers)

    # ------------------------------------------------------------------
    # Lifecycle -- an async context manager so an app can register this
    # builder with ``Manifest(managers=[...])`` and drain every resource's
    # runner on shutdown (see the module docstring).
    # ------------------------------------------------------------------

    async def __aenter__(self) -> TriggeredDependencyBuilder:
        return self

    async def __aexit__(self, *exc: object) -> None:
        for runner in self._runners:
            await runner.aclose()
