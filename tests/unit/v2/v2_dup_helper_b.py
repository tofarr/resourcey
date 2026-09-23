"""Helper module with a duplicate class name for the v2 duplicate-class test.

Defines ``V2Dup`` — collides with ``v2_dup_helper_a.V2Dup``. Both subclass
``V2DupBase``, so ``_get_checked_concrete_subclasses`` flags them as duplicates.
"""

from v2_dup_base import V2DupBase


class V2Dup(V2DupBase):
    val: int = 2
