"""The framework isolation / layering test (issues #75 / #78 / #82 / #86 / #144).

The framework lives directly under ``resourcey/`` (the old version prefix was folded
away in #144). This test pins the **layer ranks** inside it:

    util < core < {sql, mongo, list, http, config, cache, encryption, auth, view, tasks, triggers}

no module may import a strictly-higher project layer at runtime. This subsumes
both "``util`` imports nothing project-level" (it is the bottom layer) and
"``core`` imports only ``util``". ``Missing`` / ``MISSING`` live in
``resourcey/util/missing.py`` so that ``util`` is the true bottom and ``core``
reaches down rather than across.

It also guards a set of structural invariants: each layer's file set, that no
optional driver is imported eagerly, that ``view`` imports no backend, and that
nothing imports ``openhands``.

Imports under ``if TYPE_CHECKING:`` are allowed (they do not execute at
runtime, and the existing base relies on that to avoid cycles), so the check is
a static AST walk that tracks whether an import sits inside a ``TYPE_CHECKING``
guard.
"""

from __future__ import annotations

import ast
import pathlib

FRAMEWORK_DIR = pathlib.Path(__file__).resolve().parents[2] / "src" / "resourcey"
_PREFIX = "resourcey"

# The project layers of the framework, ordered bottom-up. A module may import
# from its own layer or any lower one, never strictly higher.
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
    "auth": 2,
    "filestore": 2,
    "tasks": 2,
    "triggers": 2,
}


def _framework_modules() -> list[pathlib.Path]:
    return sorted(p for p in FRAMEWORK_DIR.rglob("*.py") if p.name != "__init__.py")


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


def _runtime_resourcey_imports(path: pathlib.Path) -> list[str]:
    """Runtime ``resourcey`` imports in ``path`` (excluding ``TYPE_CHECKING`` guards)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found: list[str] = []
    for node in ast.walk(tree):
        module: str | None = None
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _PREFIX or alias.name.startswith(_PREFIX + "."):
                    found.append(alias.name)
            continue
        if isinstance(node, ast.ImportFrom):
            module = node.module
        if module is None:
            continue
        if not (module == _PREFIX or module.startswith(_PREFIX + ".")):
            continue
        if _within_type_checking(node, parents):
            continue
        found.append(module)
    return sorted(set(found))


def _layer_of(path: pathlib.Path, root: pathlib.Path = FRAMEWORK_DIR) -> str | None:
    """The layer a module belongs to (its directory under ``root``)."""
    relative = path.relative_to(root)
    return relative.parts[0] if len(relative.parts) > 1 else None


def _layer_of_module(module: str) -> str | None:
    """The layer a dotted ``resourcey`` module belongs to, or ``None``."""
    if not (module == _PREFIX or module.startswith(_PREFIX + ".")):
        return None
    parts = module.split(".")
    return parts[1] if len(parts) > 1 else None


def _upward_layer_imports(path: pathlib.Path, root: pathlib.Path = FRAMEWORK_DIR) -> list[str]:
    """Runtime ``resourcey`` imports in ``path`` from a strictly-higher project layer."""
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


def test_imports_flow_upward_only():
    violations: dict[str, list[str]] = {}
    for path in _framework_modules():
        imports = _upward_layer_imports(path)
        if imports:
            violations[str(path.relative_to(FRAMEWORK_DIR))] = imports
    assert violations == {}, (
        "framework layers are ranked util < core < {sql, mongo, list, http, config, "
        "cache, encryption, auth, view}; these modules import a strictly-higher "
        "layer: " + str(violations)
    )


def test_layer_rank_detector_catches_an_upward_import(tmp_path):
    # A fixture tree stands in for the framework so the detector can be exercised.
    (tmp_path / "core").mkdir()
    (tmp_path / "http").mkdir()
    (tmp_path / "util").mkdir()

    # core importing http is flagged (http is a strictly-higher layer)...
    up = tmp_path / "core" / "up.py"
    up.write_text("from resourcey.http.app import create_app\n")
    assert _upward_layer_imports(up, tmp_path) == ["resourcey.http.app"]

    # ...while core importing util is fine (a lower layer, the true bottom).
    down = tmp_path / "core" / "down.py"
    down.write_text("from resourcey.util.missing import MISSING\n")
    assert _upward_layer_imports(down, tmp_path) == []


def test_the_core_files_exist_without_an_init():
    core = FRAMEWORK_DIR / "core"
    names = {p.name for p in sorted(core.glob("*.py"))}
    assert names == {"dto.py", "errors.py", "manifest.py", "resource.py", "service.py"}
    assert not (core / "__init__.py").exists()


def test_the_sql_files_exist_without_an_init():
    sql = FRAMEWORK_DIR / "sql"
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
    config = FRAMEWORK_DIR / "config"
    names = {p.name for p in sorted(config.glob("*.py"))}
    assert names == {"config_base.py", "lazy_field.py"}
    assert not (config / "__init__.py").exists()


def test_the_encryption_files_exist_without_an_init():
    encryption = FRAMEWORK_DIR / "encryption"
    names = {p.name for p in sorted(encryption.glob("*.py"))}
    assert names == {"encryption_config.py", "encryption_service.py"}
    assert not (encryption / "__init__.py").exists()


def test_the_http_files_exist_without_an_init():
    http = FRAMEWORK_DIR / "http"
    names = {p.name for p in sorted(http.glob("*.py"))}
    assert names == {"app.py", "dependency_builder.py", "routes.py"}
    assert not (http / "__init__.py").exists()


def test_the_util_files_exist_without_an_init():
    util = FRAMEWORK_DIR / "util"
    names = {p.name for p in sorted(util.glob("*.py"))}
    assert names == {
        "cursor.py",
        "env_parser.py",
        "import_paths.py",
        "missing.py",
        "models.py",
        "naming.py",
        "search_filter.py",
        "secret_serialization.py",
        "singleton.py",
        "sort_order.py",
    }
    assert not (util / "__init__.py").exists()


def test_the_mongo_files_exist_without_an_init():
    mongo = FRAMEWORK_DIR / "mongo"
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
    list_dir = FRAMEWORK_DIR / "list"
    names = {p.name for p in sorted(list_dir.glob("*.py"))}
    assert names == {
        "list_resource.py",
        "list_service.py",
        "pydantic_2_dto.py",
    }
    assert not (list_dir / "__init__.py").exists()


def test_the_filestore_files_exist_without_an_init():
    filestore = FRAMEWORK_DIR / "filestore"
    names = {p.name for p in sorted(filestore.glob("*.py"))}
    assert names == {
        "file_config.py",
        "file_metadata.py",
        "file_routes.py",
        "file_store.py",
        "local_file_store.py",
        "s3_file_store.py",
        "signed_url.py",
        "sql_file_store.py",
    }
    assert not (filestore / "__init__.py").exists()


def test_filestore_never_imports_boto3_at_module_scope():
    """The S3 medium imports its optional driver lazily, behind the ``s3`` extra."""
    offenders = [
        str(p.relative_to(FRAMEWORK_DIR))
        for p in sorted((FRAMEWORK_DIR / "filestore").rglob("*.py"))
        if _module_scope_import(p, "boto3")
    ]
    assert offenders == []


def test_the_view_files_exist_without_an_init():
    view = FRAMEWORK_DIR / "view"
    names = {p.name for p in sorted(view.glob("*.py"))}
    assert names == {"resource_view.py", "view_service.py"}
    assert not (view / "__init__.py").exists()


def test_the_tasks_files_exist_without_an_init():
    tasks = FRAMEWORK_DIR / "tasks"
    names = {p.name for p in sorted(tasks.glob("*.py"))}
    assert names == {"cli.py", "cron.py", "scheduler.py", "task.py"}
    assert not (tasks / "__init__.py").exists()


def test_the_triggers_files_exist_without_an_init():
    triggers = FRAMEWORK_DIR / "triggers"
    names = {p.name for p in sorted(triggers.glob("*.py"))}
    assert names == {
        "trigger.py",
        "trigger_config.py",
        "trigger_runner.py",
        "triggered_dependency_builder.py",
        "triggered_resource.py",
        "triggered_service.py",
    }
    assert not (triggers / "__init__.py").exists()


def test_the_auth_files_exist_without_an_init():
    auth = FRAMEWORK_DIR / "auth"
    names = {p.name for p in sorted(auth.glob("*.py"))}
    assert names == {
        "auth_api_key.py",
        "auth_api_key_resource.py",
        "auth_api_key_service.py",
        "auth_authorized_dependency.py",
        "auth_authorized_service.py",
        "auth_config.py",
        "auth_cookie.py",
        "auth_policy.py",
        "auth_principal.py",
        "auth_rbac.py",
        "auth_rbac_resolver.py",
        "auth_rbac_store.py",
        "auth_role.py",
    }
    assert not (auth / "__init__.py").exists()


def test_view_never_imports_a_backend():
    """``view`` is storage-agnostic: it wraps any backend but imports none."""
    backends = ("resourcey.sql", "resourcey.mongo", "resourcey.list")
    offenders = [
        str(p.relative_to(FRAMEWORK_DIR))
        for p in sorted((FRAMEWORK_DIR / "view").rglob("*.py"))
        for backend in backends
        if _imports_module(p, backend)
    ]
    assert offenders == []


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


def test_mongo_never_imports_sqlalchemy():
    """``mongo`` is the non-SQL proof: it must not drag SQLAlchemy into its path."""
    offenders = [
        str(p.relative_to(FRAMEWORK_DIR))
        for p in sorted((FRAMEWORK_DIR / "mongo").rglob("*.py"))
        if _imports_module(p, "sqlalchemy")
    ]
    assert offenders == []


def test_list_never_imports_sqlalchemy():
    """``list`` is the dependency-free proof: it must not drag SQLAlchemy in."""
    offenders = [
        str(p.relative_to(FRAMEWORK_DIR))
        for p in sorted((FRAMEWORK_DIR / "list").rglob("*.py"))
        if _imports_module(p, "sqlalchemy")
    ]
    assert offenders == []


def test_the_cache_files_exist_without_an_init():
    cache = FRAMEWORK_DIR / "cache"
    names = {p.name for p in sorted(cache.glob("*.py"))}
    assert names == {
        "cache_defaults.py",
        "cache_header.py",
        "cache_strategy.py",
    }
    assert not (cache / "__init__.py").exists()


def _module_scope_import(path: pathlib.Path, module: str) -> bool:
    """Whether ``path`` imports ``module`` at *module scope* (not inside a function).

    A lazy import (the optional-extra pattern) sits inside a function, so this
    distinguishes "the package is importable without the driver" from "the
    driver is imported eagerly".
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    functions = {
        child
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.walk(node)
    }
    for node in ast.walk(tree):
        if node in functions:
            continue
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


def test_no_framework_module_imports_openhands():
    offenders = [
        str(p.relative_to(FRAMEWORK_DIR)) for p in _framework_modules() if _imports_openhands(p)
    ]
    assert offenders == []
