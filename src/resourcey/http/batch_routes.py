"""The app-level ``POST /batch-edit`` route: one batch across every exposed resource.

``POST /{resource}/batch-edit`` edits **one** resource. This module adds the
cross-resource counterpart, so a client can change a graph — a thread and its
messages — in one request.

The wire contract is the per-resource one with a ``resource`` field added:
a flat, ordered list of ``Create`` / ``Update`` / ``Delete`` nodes, each naming
its target by path segment, answered by a positionally aligned list.

Two properties are deliberate:

* **Strict list order.** Nodes apply in the order given, so a later node observes
  an earlier node's write (a create followed by an update of the row it made).
  Grouping nodes by resource would reorder dependent edits, so it is not done.
* **Fail fast, best-effort rollback.** The first failure aborts the batch and
  propagates to the normal error envelope. Each service's ``__aexit__`` then
  rolls back if its backend supports it — same-connection SQL shares one
  session, so it rolls back together; other backends and other connections do
  not, and a failed request may have applied a prefix of the nodes.

The body is a **static** two-level discriminated union built at registration
from every exposed resource (``resource`` -> ``kind``), so a node naming a
resource that does not declare the requested action is rejected by validation
rather than at execution, and OpenAPI documents the whole surface.

This module is part of ``resourcey.http``; it imports no code outside the
framework.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AsyncExitStack
from enum import Enum
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel, Field, create_model

from resourcey.core.errors import InvalidInputError
from resourcey.core.manifest import Manifest
from resourcey.core.resource import Resource
from resourcey.core.service import (
    Action,
    Create,
    Edit,
    Update,
    normalize_actions,
)
from resourcey.http.dependency_builder import DependencyBuilder, OpenDependencyBuilder

# The per-resource batch route and this one build the same edit shapes and
# project the same results, so the helpers are shared rather than duplicated.
from resourcey.http.routes import (
    _batch_edit_body,
    _batch_edit_node,
    _dump,
    _id_python_type,
    _project_edit_result,
    _route,
)

DEFAULT_BATCH_PATH = "/batch-edit"

# One batch is one request's worth of writes, so an unbounded list would let a
# caller turn a single request into arbitrary work.
DEFAULT_MAX_EDITS = 500

_TAGS = ["Batch"]


def register_batch_routes(
    app_or_router: FastAPI | APIRouter,
    manifest: Manifest,
    *,
    path: str = DEFAULT_BATCH_PATH,
    prefix: str = "",
    dependency_builder: DependencyBuilder | None = None,
    max_edits: int | None = DEFAULT_MAX_EDITS,
    tags: Sequence[str] | None = None,
) -> APIRouter:
    """Mount ``POST /batch-edit`` spanning every exposed resource in ``manifest``.

    Mounted by :func:`~resourcey.http.app.create_app` /
    :func:`~resourcey.http.app.add_to_app` by default; call it directly to mount
    on a custom ``path`` / ``prefix``. Nothing is registered when the manifest has
    no exposed resource, or when no exposed resource declares ``batch_edit``.

    ``dependency_builder`` decides how each resource's service is built. Every
    node goes through it, so an authorizing builder applies its policy to each
    node exactly as it would on a single-resource route.

    ``max_edits`` caps the number of nodes in one batch (``None`` disables the
    cap); exceeding it is an ``InvalidInputError``, not a timeout.

    ``tags`` overrides the OpenAPI tag, matching ``create_app``'s app-wide
    override so the batch route groups with the rest of the app.
    """
    builder = dependency_builder if dependency_builder is not None else OpenDependencyBuilder()
    router_tags: list[str | Enum] = list(tags) if tags is not None else list(_TAGS)
    router = APIRouter(tags=router_tags)

    targets = _batch_targets(manifest)
    if not targets:
        return router

    body = _cross_batch_body(targets)
    auth_dep = builder.get_principal_dependency()
    route_deps = [Depends(auth_dep)] if auth_dep is not None else None

    async def handler(request: Request, payload: Any) -> Any:
        edits = list(payload)
        if max_edits is not None and len(edits) > max_edits:
            raise InvalidInputError(
                f"batch-edit accepts at most {max_edits} edits per request; got {len(edits)}."
            )
        return await _apply_batch(request, targets, edits, builder)

    handler.__annotations__ = {
        "request": Request,
        "payload": list[body],  # type: ignore[valid-type]
        "return": Any,
    }
    _route(
        router,
        "/" + path.strip("/"),
        ["POST"],
        handler,
        dependencies=route_deps,
        summary="Batch edit across resources",
        description=(
            "Apply an ordered list of create / update / delete edits across any exposed "
            "resource. Results align positionally with the input; a delete, an absent id, "
            "or a failed condition yields null. Nodes apply in the order given and the "
            "first failure aborts the batch. Rollback is best effort: it happens where the "
            "backend supports it (resources sharing one SQL connection roll back "
            "together), so a failed request may have applied a prefix of the edits."
        ),
    )

    app_or_router.include_router(router, prefix="" if prefix == "/" else prefix)
    return router


# ---------------------------------------------------------------------------
# Targets -- the exposed resources that admit a batch edit
# ---------------------------------------------------------------------------


class _BatchTarget:
    """One resource's contribution to the cross-resource batch surface.

    Built once at registration from the **exposed** resource, so a projection's
    models and action set are the projection's, not the hidden inner resource's.
    """

    __slots__ = ("actions", "dto_model", "id_field", "id_type", "models", "name", "resource")

    def __init__(self, resource: Resource[Any, Any], actions: frozenset[Action]) -> None:
        self.resource = resource
        self.name = resource.get_resource_path()
        self.actions = actions
        self.models = resource.get_rest_models()
        self.dto_model = resource.get_dto_type()
        self.id_field = resource.get_id_field()
        self.id_type = _id_python_type(self.models, self.id_field)


def _batch_targets(manifest: Manifest) -> dict[str, _BatchTarget]:
    """The exposed resources that admit a ``batch_edit``, keyed by path segment.

    A hidden resource contributes nothing, so the endpoint cannot reach one. A
    resource without ``batch_edit`` (after normalisation, so one with no write
    action at all) is skipped too, which is the same gate that decides whether
    its own ``/{resource}/batch-edit`` route exists.
    """
    targets: dict[str, _BatchTarget] = {}
    for resource in manifest.resources:
        exposed = resource.get_exposed_resource()
        if exposed is None:
            continue
        actions = normalize_actions(exposed.get_supported_actions())
        if Action.BATCH_EDIT not in actions:
            continue
        targets[exposed.get_resource_path()] = _BatchTarget(exposed, actions)
    return targets


# ---------------------------------------------------------------------------
# Body -- one static union, ``resource`` -> ``kind``
# ---------------------------------------------------------------------------


def _cross_batch_body(targets: dict[str, _BatchTarget]) -> Any:
    """Build the batch body: a ``resource``-discriminated union of per-resource unions.

    Each resource contributes the *same* ``kind`` union its own
    ``/{resource}/batch-edit`` accepts, wrapped with a ``resource`` tag, so a
    node's ``kind`` is checked against that resource's declared actions at
    validation time and the two routes cannot drift apart.
    """
    members = [_resource_members(target) for target in targets.values()]
    union = members[0]
    for member in members[1:]:
        union = union | member
    return Annotated[union, Field(discriminator="resource")]


def _resource_members(target: _BatchTarget) -> Any:
    """One resource's nested ``kind`` union, tagged with its path segment."""
    inner = _batch_edit_body(
        target.models.create_request,
        target.models.update_request,
        target.id_field,
        target.id_type,
        allow_create=Action.CREATE in target.actions,
        allow_update=Action.UPDATE in target.actions,
        allow_delete=Action.DELETE in target.actions,
    )
    # ``inner`` is the per-resource ``Annotated[... , Field(discriminator="kind")]``
    # union; strip the annotation so it can be re-annotated per member.
    union = inner.__origin__
    tagged: list[Any] = []
    for member in _union_members(union):
        tagged.append(
            create_model(
                f"{_model_prefix(target)}_{member.__name__}",
                __base__=member,
                resource=(Literal[target.name], ...),
            )
        )
    result = tagged[0]
    for extra in tagged[1:]:
        result = result | extra
    return Annotated[result, Field(discriminator="kind")]


def _union_members(union: Any) -> list[type[BaseModel]]:
    """The member types of a ``A | B | C`` union, in declaration order."""
    args = getattr(union, "__args__", ())
    return list(args) if args else [union]


def _model_prefix(target: _BatchTarget) -> str:
    """A generated-model name prefix unique to the resource.

    The path, not the DTO class name: two resources may serve the same DTO type,
    and two generated models with one name collide.
    """
    return "".join(part.capitalize() for part in target.name.replace("-", "_").split("_"))


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def _apply_batch(
    request: Request,
    targets: dict[str, _BatchTarget],
    edits: list[Any],
    builder: DependencyBuilder,
) -> Any:
    """Apply ``edits`` in order, one resource's service at a time; return the aligned results.

    Consecutive edits for the same resource share one service (and so one
    transaction where the backend has one), but the *order* is the caller's: a
    run is never reordered or merged across resources.

    Every service stays open until the whole batch has applied. Closing each run
    as it finished would commit that run, so a later failure could not roll it
    back — the batch would be a sequence of independent transactions. The first
    exception exits the stack with it, so each service rolls back if it can.
    """
    results: list[dict[str, Any]] = []
    async with AsyncExitStack() as stack:
        index = 0
        while index < len(edits):
            name = edits[index].resource
            run_end = index
            while run_end < len(edits) and edits[run_end].resource == name:
                run_end += 1
            results.extend(
                await _apply_run(request, targets[name], edits[index:run_end], builder, stack)
            )
            index = run_end
    return results


async def _apply_run(
    request: Request,
    target: _BatchTarget,
    run: list[Any],
    builder: DependencyBuilder,
    stack: AsyncExitStack,
) -> list[dict[str, Any]]:
    """Apply one resource's consecutive edits through a single service held on ``stack``."""
    service = await stack.enter_async_context(await builder.build_service(request, target.resource))
    edits = [_edit_node(item, target) for item in run]
    edited = await service.batch_edit(edits)
    context = service.serialization_context()
    return [
        _result_body(target, edit, result, context)
        for edit, result in zip(edits, edited, strict=True)
    ]


def _edit_node(item: BaseModel, target: _BatchTarget) -> Any:
    """Fold a validated body item into its :class:`Edit` service node."""
    return _batch_edit_node(item, target.dto_model, target.id_field)


def _result_body(
    target: _BatchTarget,
    edit: Edit,
    result: BaseModel | None,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """One response item: the resource and kind, plus the projected result.

    Echoing ``resource`` / ``kind`` keeps the response readable without a
    second lookup by position, which matters when a batch mixes resources.
    """
    projected = _project_edit_result(edit, result, target.models, context)
    return {
        "resource": target.name,
        "kind": _edit_kind(edit),
        "result": _dump(projected, context),
    }


def _edit_kind(edit: Edit) -> str:
    """The wire ``kind`` of an :class:`Edit` node."""
    if isinstance(edit, Create):
        return "Create"
    if isinstance(edit, Update):
        return "Update"
    return "Delete"
