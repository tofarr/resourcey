"""The ``v2`` isolation test (issues #75 / #78 / #82 / #86).

Asserts that no module under ``resourcey/v2/`` makes a **runtime** import of any
``resourcey`` module *outside* ``v2/`` — that is what pins ``v2`` as a
self-contained layer (its ``core``, ``config``, ``encryption``, ``sql``, and
``util`` packages import only each other and the wider third-party stack, never
the legacy ``v1`` packages). Imports under ``if TYPE_CHECKING:`` are allowed
(they do not execute at runtime, and the existing base relies on that to avoid
cycles), so the check is a static AST walk that tracks whether an import sits
inside a ``TYPE_CHECKING`` guard.

It also pins the **layer ranks** inside ``v2``:

    util < core < {sql, mongo, list, http, config, cache, encryption}

no module may import a strictly-higher project layer at runtime. This subsumes
both "``util`` imports nothing project-level" (it is the bottom layer) and
"``core`` imports only ``util``". ``Missing`` / ``MISSING`` live in
``v2/util/missing.py`` so that ``util`` is the true bottom and ``core`` reaches
down rather than across.

It fails if a runtime cross-layer import is added.
"""

from __future__ import annotations

import ast
import pathlib

V2_DIR = pathlib.Path(__file__).resolve().parents[3] / "src" / "resourcey" / "v2"
_V2_PREFIX = "resourcey.v2"

# The project layers of ``v2``, ordered bottom-up. A module may import from its
# own layer or any lower one, never strictly higher.
_LAYER_RANK = {
    "util": 0,
    "core": 1,
    "cache": 2,
    "config": 2,
    "encryption": 2,
    "http": 2,
    "mongo": 2,
    "list": 2,
    "sql": 2,
    "view": 2,
}


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
    """Whether a ``resourcey`` dotted module resolves outside ``v2/``."""
    if module == "resourcey":
        return True
    return not (module == _V2_PREFIX or module.startswith(_V2_PREFIX + "."))


def _runtime_resourcey_imports(path: pathlib.Path) -> list[str]:
    """Runtime ``resourcey`` imports in ``path`` (excluding ``TYPE_CHECKING`` guards)."""
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
        if _within_type_checking(node, parents):
            continue
        found.append(module)
    return sorted(set(found))


def _runtime_cross_layer_imports(path: pathlib.Path) -> list[str]:
    """Runtime imports of ``resourcey`` code *outside* ``v2/``."""
    return [m for m in _runtime_resourcey_imports(path) if _is_outside_v2(m)]


def _layer_of(path: pathlib.Path, root: pathlib.Path = V2_DIR) -> str | None:
    """The ``v2`` layer a module belongs to (its directory under ``root``)."""
    relative = path.relative_to(root)
    return relative.parts[0] if len(relative.parts) > 1 else None


def _layer_of_module(module: str) -> str | None:
    """The ``v2`` layer a dotted module belongs to, or ``None`` for non-``v2``."""
    if not (module == _V2_PREFIX or module.startswith(_V2_PREFIX + ".")):
        return None
    parts = module.split(".")
    return parts[2] if len(parts) > 2 else None


def _upward_layer_imports(path: pathlib.Path, root: pathlib.Path = V2_DIR) -> list[str]:
    """Runtime ``v2`` imports in ``path`` from a strictly-higher project layer."""
    own = _layer_of(path, root)
    if own is None:
        return []
    own_rank = _LAYER_RANK.get(own)
    if own_rank is None:
        return []
    found: list[str] = []
    for module in _runtime_resourcey_imports(path):
        target = _layer_of_module(module)
        if target is None or target == own:
            continue
        target_rank = _LAYER_RANK.get(target)
        if target_rank is not None and target_rank > own_rank:
            found.append(module)
    return found


def test_v2_has_no_runtime_cross_layer_imports():
    violations: dict[str, list[str]] = {}
    for path in _v2_modules():
        imports = _runtime_cross_layer_imports(path)
        if imports:
            violations[str(path.relative_to(V2_DIR))] = imports
    assert violations == {}, (
        "v2 must stand alone; these modules import resourcey packages outside v2 "
        f"at runtime: {violations}"
    )


def test_v2_imports_flow_upward_only():
    violations: dict[str, list[str]] = {}
    for path in _v2_modules():
        imports = _upward_layer_imports(path)
        if imports:
            violations[str(path.relative_to(V2_DIR))] = imports
    assert violations == {}, (
        "v2 layers are ranked util < core < {sql, mongo, list, http, config, cache, "
        "encryption}; these modules import a strictly-higher layer: " + str(violations)
    )


def test_layer_rank_detector_catches_an_upward_import(tmp_path):
    # A fixture tree stands in for ``v2/`` so the detector can be exercised.
    (tmp_path / "core").mkdir()
    (tmp_path / "http").mkdir()
    (tmp_path / "util").mkdir()

    # core importing http is flagged (http is a strictly-higher layer)...
    up = tmp_path / "core" / "up.py"
    up.write_text("from resourcey.v2.http.app import create_app\n")
    assert _upward_layer_imports(up, tmp_path) == ["resourcey.v2.http.app"]

    # ...while core importing util is fine (a lower layer, the true bottom).
    down = tmp_path / "core" / "down.py"
    down.write_text("from resourcey.v2.util.missing import MISSING\n")
    assert _upward_layer_imports(down, tmp_path) == []


def test_the_core_files_exist_without_an_init():
    core = V2_DIR / "core"
    names = {p.name for p in sorted(core.glob("*.py"))}
    assert names == {"dto.py", "errors.py", "manifest.py", "resource.py", "service.py"}
    assert not (core / "__init__.py").exists()


def test_the_sql_files_exist_without_an_init():
    sql = V2_DIR / "sql"
    names = {p.name for p in sorted(sql.glob("*.py"))}
    assert names == {
        "cursor.py",
        "db_config.py",
        "filter_converter.py",
        "session_manager.py",
        "sort_converter.py",
        "sql_config.py",
        "sql_resource.py",
        "sql_service.py",
        "sqlalchemy_2_dto.py",
    }
    assert not (sql / "__init__.py").exists()


def test_the_config_files_exist_without_an_init():
    config = V2_DIR / "config"
    names = {p.name for p in sorted(config.glob("*.py"))}
    assert names == {"config_base.py", "lazy_field.py"}
    assert not (config / "__init__.py").exists()


def test_the_encryption_files_exist_without_an_init():
    encryption = V2_DIR / "encryption"
    names = {p.name for p in sorted(encryption.glob("*.py"))}
    assert names == {"encryption_config.py", "encryption_service.py"}
    assert not (encryption / "__init__.py").exists()


def test_the_http_files_exist_without_an_init():
    http = V2_DIR / "http"
    names = {p.name for p in sorted(http.glob("*.py"))}
    assert names == {"app.py", "dependency_builder.py", "routes.py"}
    assert not (http / "__init__.py").exists()


def test_the_util_files_exist_without_an_init():
    util = V2_DIR / "util"
    names = {p.name for p in sorted(util.glob("*.py"))}
    assert names == {
        "cursor.py",
        "env_parser.py",
        "import_paths.py",
        "missing.py",
        "models.py",
        "naming.py",
        "search_filter.py",
        "singleton.py",
        "sort_order.py",
    }
    assert not (util / "__init__.py").exists()


def test_the_mongo_files_exist_without_an_init():
    mongo = V2_DIR / "mongo"
    names = {p.name for p in sorted(mongo.glob("*.py"))}
    assert names == {
        "embedded.py",
        "mongo_client.py",
        "mongo_config.py",
        "mongo_filter_converter.py",
        "mongo_resource.py",
        "mongo_service.py",
        "mongo_sort_converter.py",
    }
    assert not (mongo / "__init__.py").exists()


def test_the_list_files_exist_without_an_init():
    list_dir = V2_DIR / "list"
    names = {p.name for p in sorted(list_dir.glob("*.py"))}
    assert names == {
        "list_resource.py",
        "list_service.py",
        "pydantic_2_dto.py",
    }
    assert not (list_dir / "__init__.py").exists()


def _imports_module(path: pathlib.Path, module: str) -> bool:
    """Whether ``path`` imports ``module`` (or a submodule) at runtime."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        if any(name == module or name.startswith(module + ".") for name in names):
            return True
    return False


def test_v2_mongo_never_imports_sqlalchemy():
    """``v2/mongo`` is the non-SQL proof: it must not drag SQLAlchemy into its path."""
    offenders = [
        str(p.relative_to(V2_DIR))
        for p in sorted((V2_DIR / "mongo").rglob("*.py"))
        if _imports_module(p, "sqlalchemy")
    ]
    assert offenders == []


def test_v2_list_never_imports_sqlalchemy():
    """``v2/list`` is the dependency-free proof: it must not drag SQLAlchemy in."""
    offenders = [
        str(p.relative_to(V2_DIR))
        for p in sorted((V2_DIR / "list").rglob("*.py"))
        if _imports_module(p, "sqlalchemy")
    ]
    assert offenders == []


def test_the_cache_files_exist_without_an_init():
    cache = V2_DIR / "cache"
    names = {p.name for p in sorted(cache.glob("*.py"))}
    assert names == {
        "cache_defaults.py",
        "cache_header.py",
        "cache_strategy.py",
    }
    assert not (cache / "__init__.py").exists()


def _imports_module(path: pathlib.Path, module: str) -> bool:
    """Whether ``path`` imports ``module`` (or a submodule) at runtime.

    Checks actual import statements, not the raw text, so a docstring may
    legitimately *mention* a module without tripping the check.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        if any(name == module or name.startswith(module + ".") for name in names):
            return True
    return False


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


def test_detector_catches_an_added_cross_layer_import(tmp_path):
    # A runtime cross-layer import is flagged...
    bad = tmp_path / "bad.py"
    bad.write_text("from resourcey.resource.base import BaseResource\n")
    assert _runtime_cross_layer_imports(bad) == ["resourcey.resource.base"]

    # ...an intra-v2 import is fine...
    ok = tmp_path / "ok.py"
    ok.write_text("from resourcey.v2.core.dto import DTO\n")
    assert _runtime_cross_layer_imports(ok) == []

    # ...and a TYPE_CHECKING-guarded cross-layer import is allowed.
    guarded = tmp_path / "guarded.py"
    guarded.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from resourcey.resource.base import BaseResource\n"
    )
    assert _runtime_cross_layer_imports(guarded) == []
