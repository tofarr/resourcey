"""Helper module with a duplicate class name for the duplicate-class test.

Defines ``Dup`` — another helper defines a class with the same name, both
subclassing the shared ``DupBase``. This triggers the duplicate-class
detection in ``_get_checked_concrete_subclasses``.
"""

from dup_base import DupBase


class Dup(DupBase):
    val: int = 1
