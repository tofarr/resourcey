"""Helper module with a duplicate class name for TestDuplicateClassRejection.

Defines ``Dup`` — collides with test_duplicate_helper_a.Dup. Both subclass
DiscriminatedUnionMixin, so _get_checked_concrete_subclasses flags them as
duplicates.
"""

from resourcey.util.models import DiscriminatedUnionMixin


class Dup(DiscriminatedUnionMixin):
    val: int = 2
