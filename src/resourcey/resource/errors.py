"""Framework exception types.

``ResourceyError`` is the base for every error raised by the framework, so
callers can catch all resourcey failures with a single ``except``. The more
specific ``ResourceyConfigError`` is raised when a resource's configuration
is invalid or incomplete — e.g. no ``id`` field can be resolved, or a
``*_id`` column is left for the framework to guess at.

``NotFoundError`` is raised by the service when a targeted entity does not
exist on read / update / delete (maps to HTTP 404). ``InvalidInputError``
covers malformed pre-validation input the framework detects itself — a
non-sortable / unknown ``sort`` field, or a ``field__op`` filter parameter
on a resource that declares no search filter (maps to HTTP 400).
"""


class ResourceyError(Exception):
    """Base class for all framework errors."""


class ResourceyConfigError(ResourceyError):
    """A resource's configuration is invalid or incomplete."""


class NotFoundError(ResourceyError):
    """A targeted entity does not exist (read / update / delete)."""

    def __init__(self, resource_name: str, id: object) -> None:  # noqa: A002
        self.resource_name = resource_name
        self.id = id
        super().__init__(f"{resource_name} {id!r} not found")


class InvalidInputError(ResourceyError):
    """Malformed input detected by the framework before validation (HTTP 400)."""


class ForbiddenError(ResourceyError):
    """The authenticated principal is not permitted to perform an action (HTTP 403).

    Raised by the secured service wrapper when a permission check denies an
    action (e.g. a ``create`` whose policy reduces to ``NoneSearchFilter``).
    """

    def __init__(self, resource_name: str, action: str) -> None:
        self.resource_name = resource_name
        self.action = action
        super().__init__(f"Permission denied: action={action} resource={resource_name}")
