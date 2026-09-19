"""Explicit resource registry (issue #3).

``register_resource`` is the imperative, discoverable way to tell the framework
about a resource. An app typically has a single module that imports every
resource class and calls ``register_resource`` for each:

.. code-block:: python

    # myapp/resources.py
    from resourcey.resource.base import BaseResource
    from resourcey.resource.registry import register_resource
    from myapp.user import User
    from myapp.widget import Widget

    register_resource(User)
    register_resource(Widget)

The module path is named on :class:`~resourcey.config.config_framework.FrameworkConfig`
(``resource_modules``). The framework imports that module before any consumer
that needs the full resource set — the REST service layer, RBAC, and
Alembic autogeneration (:func:`resourcey.migrate.migrate_runner.import_resource_modules`).

Registration validates the class is a ``BaseResource`` subclass, eagerly builds
its SQLAlchemy model (so the table lands in ``ResourceyBase.metadata``), and
records it in an ordered registry. The registry is the single source of truth
for "which resources exist" — consumers iterate it instead of walking
``BaseResource.__subclasses__()`` with a module filter.
"""

from __future__ import annotations

from resourcey.resource.base import BaseResource

# Ordered registry of registered resource classes. A dict keyed by the class
# preserves insertion order (Python 3.7+) while deduplicating re-registrations.
_registry: dict[type[BaseResource], None] = {}


def register_resource(cls: type[BaseResource]) -> type[BaseResource]:
    """Register a resource class with the framework.

    Validates ``cls`` is a :class:`BaseResource` subclass, builds its
    SQLAlchemy ORM model (registering the derived table in
    :data:`ResourceyBase.metadata`), and records it in the registry. Returns
    ``cls`` so it can be used inline::

        register_resource(User).get_resource_path()

    Re-registering the same class is a no-op (the first registration wins on
    ordering); registering a different class under a clashing table name is
    caught by SQLAlchemy at materialisation time.
    """
    if not isinstance(cls, type) or not issubclass(cls, BaseResource):
        raise TypeError(f"register_resource expects a BaseResource subclass, got {cls!r}")
    if cls not in _registry:
        cls.get_sql_alchemy_model()
        _registry[cls] = None
    return cls


def get_registered_resources() -> list[type[BaseResource]]:
    """Return the registered resource classes in registration order."""
    return list(_registry)


def clear_registry() -> None:
    """Drop every registered resource. Used for test isolation."""
    _registry.clear()
