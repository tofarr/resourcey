"""``TriggeredDependencyBuilder`` — config-driven triggers, no resource edits (issue #155).

``TriggeredResource(inner, on_edit=[...])`` is the core seam, applied one
resource at a time in app code. ``TriggeredDependencyBuilder`` is the opt-in
*rung* on top: a
:class:`~resourcey.http.dependency_builder.DependencyBuilder` (the
:class:`~resourcey.auth.auth_authorized_dependency.AuthorizedDependencyBuilder`
composition shape) that resolves configured triggers **per resource path**
and wraps that resource's service in a
:class:`~resourcey.triggers.triggered_service.TriggeredService` — so a
deployment can attach a webhook to an existing app with no resource change.

It **composes with an inner builder** rather than replacing it (the app's
choice: plug it in front of an :class:`AuthorizedDependencyBuilder`, or stand
alone over the plain
:class:`~resourcey.http.dependency_builder.OpenDependencyBuilder` default).
A resource with no configured triggers passes through to the inner builder's
dependency unchanged — a no-op for an app that has not opted in — and a
resource that already supplies its own ``on_edit`` (a
:class:`~resourcey.triggers.triggered_resource.TriggeredResource` instance) is
never wrapped a second time: the constructor list wins.

Background runs and app-scoped draining
----------------------------------------
``get_service_dependency(resource)`` is called **once per resource, at
registration** (``register_routes`` asserts this), so the per-resource
``dependency`` closure it returns is reused by every request that resource
receives. A :class:`~resourcey.triggers.trigger_runner.TriggerRunner` is
created there -- once, per resource -- and captured by that closure, so every
request's :class:`~resourcey.triggers.triggered_service.TriggeredService`
shares the *same* runner: a ``background=True`` run outlives the single
request's service exit (see ``trigger_runner.py`` for why that scope matters)
and is only cancelled when this builder itself is closed.

This builder is therefore also an async context manager: add the **same
instance** to ``Manifest(managers=[builder])`` (alongside passing it as
``dependency_builder=`` to ``create_app`` / ``add_to_app``) to drain every
resource's runner on app shutdown, the same graceful-shutdown guarantee
``TriggeredResource`` gives for free. Omitting it from ``managers=`` is not
unsafe -- requests still fire triggers exactly as configured -- it just means
pending background runs are not explicitly settled before the process exits
rather than being cancelled early.

This module is part of ``resourcey.triggers``; it imports ``http`` (the
``DependencyBuilder`` seam) the same way ``auth``'s builder does — both sit at
the same layer rank, so neither imports the other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

from fastapi import Depends
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

    Attributes:
        inner: The builder this one composes in front of. Defaults to
            :class:`~resourcey.http.dependency_builder.OpenDependencyBuilder`;
            pass an :class:`~resourcey.auth.auth_authorized_dependency.AuthorizedDependencyBuilder`
            to compose with auth (either order is the app's choice — this
            builder only ever wraps what ``inner`` yields).
        resource_triggers: ``resource_path -> triggers``, typically built via
            :meth:`~resourcey.triggers.trigger_config.TriggerConfig.resource_triggers`
            (see :meth:`from_config`).
        background: Whether firing happens in the background (the default) or
            inline; forwarded to every :class:`TriggeredService` this builder
            creates.
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

        The sugar the "opt-in, no resource change" promise relies on: an app
        reads its trigger config once and passes it here instead of grouping
        ``resource_path -> triggers`` by hand.
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

        No-ops (returns the inner dependency verbatim) when the resource has no
        configured triggers, or when it is already a
        :class:`~resourcey.triggers.triggered_resource.TriggeredResource` — a
        resource that already fires its own ``on_edit`` triggers is never
        double-wrapped.
        """
        inner_dependency = self.inner.get_service_dependency(resource)
        if isinstance(resource, TriggeredResource):
            return inner_dependency
        triggers = self.resource_triggers.get(resource.get_resource_path())
        if not triggers:
            return inner_dependency

        # Mirrors TriggeredResource's constructor hook (see trigger.py's
        # ResourceBoundTrigger docstring): this builder is the config-driven
        # path's equivalent construction point, called once per resource at
        # registration -- not per request.
        for trigger in triggers:
            if isinstance(trigger, ResourceBoundTrigger):
                trigger.bind_resource(resource)

        background = self.background
        # Created once, here (get_service_dependency runs once per resource,
        # at registration) and shared by every request's TriggeredService via
        # the closure below -- see the module docstring.
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
