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

This module is part of ``resourcey.triggers``; it imports ``http`` (the
``DependencyBuilder`` seam) the same way ``auth``'s builder does — both sit at
the same layer rank, so neither imports the other.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import Depends
from pydantic import Field

from resourcey.core.resource import Resource
from resourcey.core.service import Service
from resourcey.http.dependency_builder import DependencyBuilder, OpenDependencyBuilder
from resourcey.triggers.trigger import Trigger
from resourcey.triggers.trigger_config import TriggerConfig
from resourcey.triggers.triggered_resource import TriggeredResource
from resourcey.triggers.triggered_service import TriggeredService


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
    resource_triggers: dict[str, list[Trigger]] = Field(default_factory=dict)
    background: bool = True

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

        background = self.background

        async def dependency(
            inner_service: Service[Any, Any] = Depends(inner_dependency),  # noqa: B008
        ) -> AsyncIterator[Service[Any, Any]]:
            service: Service[Any, Any] = TriggeredService(
                inner_service, triggers, background=background
            )
            async with service:
                yield service

        return dependency
