"""Helper module with a duplicate class name for the v2 duplicate-class test.

Defines ``V2Dup`` — another v2 helper defines a class with the same name, both
subclassing the shared ``V2DupBase``. This triggers the duplicate-class
detection in ``_get_checked_concrete_subclasses``.
"""

from v2_dup_base import V2DupBase


class V2Dup(V2DupBase):
    val: int = 1
