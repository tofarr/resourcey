"""The ``v2`` isolation test (issues #75 / #78 / #82).

Asserts that no module under ``resourcey/v2/`` makes a **runtime** import of any
``resourcey`` module *outside* ``v2/`` — that is what lets the whole of ``v2``
be layered and eventually replace v1 without a hidden dependency on the code it
replaces. Imports under ``if TYPE_CHECKING:`` are allowed (they do not execute
at runtime, and the existing base relies on that to avoid cycles), so the check
is a static AST walk that tracks whether an import sits inside a
``TYPE_CHECKING`` guard.

``v2/util`` depends on ``v2/core`` (the ``Missing`` sentinel is defined in core
and consumed by util); that is a one-way edge inside ``v2`` and is allowed.
"""

from __future__ import annotations

import ast
import pathlib

V2_DIR = pathlib.Path(__file__).resolve().parents[3] / "src" / "resourcey" / "v2"
CORE_DIR = V2_DIR / "core"
_V2_PREFIX = "resourcey.v2"


def _core_modules() -> list[pathlib.Path]:
    return sorted(p for p in CORE_DIR.glob("*.py"))


def _v2_modules() -> list[pathlib.Path]:
    return sorted(p for p in V2_DIR.rglob("*.py"))


def _is_type_checking_guard(node: ast.If) -> bool:
    """Whether an ``if`` tests ``TYPE_CHECKING``."""
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _within_type_checking(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Whether ``node`` is nested inside a ``TYPE_CHECKING`` ``if`` block."""
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.If) and _is_type_checking_guard(current):
            return True
        current = parents.get(current)
    return False


def _is_outside_v2(module: str) -> bool:
    """Whether a ``resourcey`` dotted module resolves outside ``v2``."""
    if module == "resourcey":
        return True
    return not (module == _V2_PREFIX or module.startswith(_V2_PREFIX + "."))


def _runtime_cross_package_imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found: list[str] = []
    for node in ast.walk(tree):
        module: str | None = None
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "resourcey" or alias.name.startswith("resourcey."):
                    found.append(alias.name)
            continue
        if isinstance(node, ast.ImportFrom):
            module = node.module
        if module is None:
            continue
        if not (module == "resourcey" or module.startswith("resourcey.")):
            continue
        if not _is_outside_v2(module):
            continue
        if _within_type_checking(node, parents):
            continue
        found.append(module)
    return sorted(set(found))


def test_v2_has_no_runtime_cross_package_imports():
    violations: dict[str, list[str]] = {}
    for path in _v2_modules():
        imports = _runtime_cross_package_imports(path)
        if imports:
            violations[str(path.relative_to(V2_DIR))] = imports
    assert violations == {}, (
        "every v2 module must import only v2 code at runtime; these import "
        f"resourcey packages outside v2: {violations}"
    )


def test_the_core_files_exist_without_an_init():
    names = {p.name for p in _core_modules()}
    assert names == {"dto.py", "errors.py", "manifest.py", "resource.py", "service.py"}
    assert not (CORE_DIR / "__init__.py").exists()


def _imports_openhands(path: pathlib.Path) -> bool:
    """Whether ``path`` imports the ``openhands`` package at runtime.

    A docstring may legitimately *mention* the SDK the code was vendored from,
    so this checks actual import statements rather than the raw text.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        if any(name == "openhands" or name.startswith("openhands.") for name in names):
            return True
    return False


def test_no_v2_module_imports_openhands():
    offenders = [str(p.relative_to(V2_DIR)) for p in _v2_modules() if _imports_openhands(p)]
    assert offenders == []


def test_detector_catches_an_added_cross_package_import(tmp_path):
    # A runtime cross-package import is flagged...
    bad = tmp_path / "bad.py"
    bad.write_text("from resourcey.resource.base import BaseResource\n")
    assert _runtime_cross_package_imports(bad) == ["resourcey.resource.base"]

    # ...an intra-v2 import is fine...
    ok = tmp_path / "ok.py"
    ok.write_text("from resourcey.v2.core.dto import DTO\n")
    assert _runtime_cross_package_imports(ok) == []

    # ...and a TYPE_CHECKING-guarded cross-package import is allowed.
    guarded = tmp_path / "guarded.py"
    guarded.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from resourcey.resource.base import BaseResource\n"
    )
    assert _runtime_cross_package_imports(guarded) == []
