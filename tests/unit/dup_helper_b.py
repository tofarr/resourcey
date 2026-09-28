"""Helper module with a duplicate class name for the duplicate-class test.

Defines ``Dup`` — collides with ``dup_helper_a.Dup``. Both subclass
``DupBase``, so ``_get_checked_concrete_subclasses`` flags them as duplicates.
"""

from dup_base import DupBase


class Dup(DupBase):
    val: int = 2
