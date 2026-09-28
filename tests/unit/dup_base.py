"""Shared abstract base for the duplicate-class-name test.

Concentrating the duplicate ``Dup`` subclasses under a dedicated base keeps
the duplicate-name check isolated from every other ``DiscriminatedUnionMixin``
subclass loaded by the test session (sorting the whole hierarchy's modules
would otherwise decide which error surfaces first).
"""

from abc import ABC

from resourcey.util.models import DiscriminatedUnionMixin


class DupBase(DiscriminatedUnionMixin, ABC):
    """Abstract base whose two concrete subclasses collide on ``__name__``."""
