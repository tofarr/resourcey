"""Framework exception types.

``ResourceyError`` is the base for every error raised by the framework, so
callers can catch all resourcey failures with a single ``except``. The more
specific ``ResourceyConfigError`` is raised when a resource's configuration
is invalid or incomplete — e.g. no ``id`` field can be resolved, or a
``*_id`` column is left for the framework to guess at.
"""


class ResourceyError(Exception):
    """Base class for all framework errors."""


class ResourceyConfigError(ResourceyError):
    """A resource's configuration is invalid or incomplete."""
