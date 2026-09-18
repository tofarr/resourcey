"""Helper module with a duplicate class name for TestDuplicateClassRejection.

This module defines ``Dup`` — another module (test_duplicate_helper_b.py)
defines a class with the same name, both subclassing DiscriminatedUnionMixin.
This triggers the duplicate-class detection in _get_checked_concrete_subclasses.
"""

from resourcey.util.models import DiscriminatedUnionMixin


class Dup(DiscriminatedUnionMixin):
    val: int = 1
