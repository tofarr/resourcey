"""The app-level ``POST /batch-edit`` route: one batch across every exposed resource.

This is the **only** batch write surface: there is no per-resource
``/{resource}/batch-edit``. A client changes a graph — a thread and its
messages — in one request.

The wire contract is a flat, ordered list of ``Create`` / ``Update`` /
``Delete`` nodes, each naming its target resource by path segment, answered by a
positionally aligned list.

Two properties are deliberate:

* **Strict list order.** Nodes apply in the order given, so a later node observes
  an earlier node's write (a create followed by an update of the row it made).
  Grouping nodes by resource would reorder dependent edits, so it is not done.
* **Fail fast, best-effort rollback.** The first failure aborts the batch and
  propagates to the normal error envelope. Each service's ``__aexit__`` then
  rolls back if its backend supports it. Storage is keyed per connection
  (:func:`~resourcey.core.service.storage_key`), so SQL resources over one engine
  adopt one session and the owner rolls the whole run back; two engines, or two
  backends, do not, and a failed request may have applied a prefix of the nodes.

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
from pydantic import (
    BaseModel,
    Field,
    WithJsonSchema,
    create_model,
    field_validator,
)

from resourcey.core.dto import RestModels, request_to_dto
from resourcey.core.errors import InvalidInputError
from resourcey.core.manifest import Manifest
from resourcey.core.resource import Resource
from resourcey.core.service import (
    Action,
    Create,
    Delete,
    Edit,
    ServiceError,
    Update,
    normalize_actions,
)
from resourcey.http.dependency_builder import DependencyBuilder, OpenDependencyBuilder
from resourcey.http.http_config import RequestLimitsConfig

# Shared with the singular routes: a batch response is projected and serialised
# exactly like a single write, so the helpers live there.
from resourcey.http.routes import _dump, _id_python_type, _project, _route
from resourcey.util.search_filter import SearchFilter

DEFAULT_BATCH_PATH = "/batch-edit"

_TAGS = ["Batch"]

# The ``condition`` wire field is typed ``Any`` rather than the native
# ``SearchFilter`` union: FastAPI re-generates a JSON schema for the parameterised
# generic base and hits ``DiscriminatedUnionMixin``'s "no subclasses" guard, even
# under a ``WithJsonSchema`` override.
_WireCondition = Annotated[Any, WithJsonSchema({"type": "object", "title": "SearchFilter"})]


def _resolve_wire_condition(value: Any) -> Any:
    """Resolve a JSON condition into a :class:`SearchFilter`, else pass through.

    A condition that is neither a filter nor a JSON object is rejected here
    rather than reaching a backend, which would dereference it as a filter tree
    and raise an ``AttributeError`` (a ``500`` on malformed input).
    """
    if isinstance(value, dict):
        return SearchFilter.model_validate(value)
    if value is None or isinstance(value, SearchFilter):
        return value
    raise ValueError(
        f"condition must be a JSON object describing a SearchFilter, not {type(value).__name__}"
    )


def register_batch_routes(
    app_or_router: FastAPI | APIRouter,
    manifest: Manifest,
    *,
    path: str = DEFAULT_BATCH_PATH,
    prefix: str = "",
    dependency_builder: DependencyBuilder | None = None,
    max_edits: int | None = None,
    tags: Sequence[str] | None = None,
) -> APIRouter:
    """Mount ``POST /batch-edit`` spanning every exposed resource in ``manifest``.

    Mounted by :func:`~resourcey.http.app.create_app` /
    :func:`~resourcey.http.app.add_to_app` by default; call it directly to mount
    on a custom ``path`` / ``prefix``. Nothing is registered when the manifest has
    no exposed resource, or when no exposed resource declares a write action.

    ``dependency_builder`` decides how each resource's service is built. Every
    node goes through it, so an authorizing builder applies its policy to each
    node exactly as it would on a single-resource route.

    ``max_edits`` caps the number of nodes in one batch; ``None`` reads
    ``RequestLimitsConfig.max_batch_edits`` (``APP_MAX_BATCH_EDITS``, default
    100). Exceeding it is an ``InvalidInputError``, not a timeout. Like the
    other request caps it is read once here, at registration, and is never
    unbounded — a cross-resource batch is the easiest way to turn one request
    into arbitrary work.

    ``tags`` overrides the OpenAPI tag, matching ``create_app``'s app-wide
    override so the batch route groups with the rest of the app.
    """
    builder = dependency_builder if dependency_builder is not None else OpenDependencyBuilder()
    cap = max_edits if max_edits is not None else RequestLimitsConfig.get_instance().max_batch_edits
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
        if len(edits) > cap:
            raise InvalidInputError(
                f"batch-edit accepts at most {cap} edits per request; got {len(edits)}."
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


# The actions a resource must declare at least one of to be a batch-edit target.
_WRITE_ACTIONS: frozenset[Action] = frozenset({Action.CREATE, Action.UPDATE, Action.DELETE})


def _batch_targets(manifest: Manifest) -> dict[str, _BatchTarget]:
    """The exposed resources that admit a write, keyed by path segment.

    A hidden resource contributes nothing, so the endpoint cannot reach one. A
    resource declaring no write action at all is skipped: there is no node kind
    it could accept.
    """
    targets: dict[str, _BatchTarget] = {}
    for resource in manifest.resources:
        exposed = resource.get_exposed_resource()
        if exposed is None:
            continue
        actions = normalize_actions(exposed.get_supported_actions())
        if actions.isdisjoint(_WRITE_ACTIONS):
            continue
        targets[exposed.get_resource_path()] = _BatchTarget(exposed, actions)
    return targets


# ---------------------------------------------------------------------------
# Body -- one static union, ``resource`` -> ``kind``
# ---------------------------------------------------------------------------


def _cross_batch_body(targets: dict[str, _BatchTarget]) -> Any:
    """Build the batch body: a ``resource``-discriminated union of per-resource unions.

    Each resource contributes its own ``kind`` union, narrowed to the actions it
    declares and wrapped with a ``resource`` tag, so a node's ``kind`` is checked
    against that resource's declared actions at validation time rather than at
    execution.
    """
    members = [_resource_members(target) for target in targets.values()]
    union = members[0]
    for member in members[1:]:
        union = union | member
    return Annotated[union, Field(discriminator="resource")]


def _resource_members(target: _BatchTarget) -> Any:
    """One resource's nested ``kind`` union, tagged with its path segment."""
    inner = _resource_edit_body(
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
    edited = await service.apply_edits(edits)
    context = service.serialization_context()
    return [
        _result_body(target, edit, result, context)
        for edit, result in zip(edits, edited, strict=True)
    ]


def _edit_node(item: BaseModel, target: _BatchTarget) -> Any:
    """Fold a validated body item into its :class:`Edit` service node."""
    kind = item.kind  # type: ignore[attr-defined]
    if kind == "Create":
        return Create(item=request_to_dto(target.dto_model, item.item))  # type: ignore[attr-defined]
    condition = getattr(item, "condition", None)
    if kind == "Update":
        return Update(item=request_to_dto(target.dto_model, item.item), condition=condition)  # type: ignore[attr-defined]
    return Delete(id=getattr(item, target.id_field), condition=condition)


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


def _resource_edit_body(
    create_request: type[BaseModel],
    update_request: type[BaseModel],
    id_field: str,
    id_type: Any,
    allow_create: bool,
    allow_update: bool,
    allow_delete: bool,
) -> Any:
    """Build the ``batch-edit`` body: a ``kind``-discriminated union of edits.

    The wire shapes match the ``Create`` / ``Update`` / ``Delete`` service
    nodes. ``Delete`` carries the identifier directly rather than nesting it
    under ``item``: there is no payload to nest.

    The ``allow_*`` flags narrow the union to the declared actions. A resource
    allowing none is not a batch target at all, so this guard is unreachable
    from :func:`register_batch_routes` and only catches a direct caller.
    """
    members: list[Any] = []
    if allow_create:
        members.append(
            create_model(
                f"{create_request.__name__}CreateEdit",
                kind=(Literal["Create"], ...),
                item=(create_request, ...),
            )
        )
    if allow_update:
        update_fields = {
            name: (field.annotation, field) for name, field in update_request.model_fields.items()
        }
        update_item = create_model(  # type: ignore[call-overload]
            f"{update_request.__name__}BatchUpdateItem",
            **{id_field: (id_type, ...)},  # the id is required on each update
            **update_fields,
        )
        members.append(
            create_model(
                f"{update_request.__name__}UpdateEdit",
                kind=(Literal["Update"], ...),
                item=(update_item, ...),
                condition=(_WireCondition, None),  # optional; applied only if the row matches
                __validators__={
                    "resolve_condition": field_validator("condition", mode="before")(
                        _resolve_wire_condition
                    )
                },
            )
        )
    if allow_delete:
        members.append(
            create_model(  # type: ignore[call-overload]
                f"{create_request.__name__}DeleteEdit",
                kind=(Literal["Delete"], ...),
                **{id_field: (id_type, ...)},
                condition=(_WireCondition, None),  # optional; applied only if the row matches
                __validators__={
                    "resolve_condition": field_validator("condition", mode="before")(
                        _resolve_wire_condition
                    )
                },
            )
        )
    if not members:
        raise ServiceError(
            "batch-edit body has no members: a resource with no write action is not a batch target."
        )
    edits = members[0]
    for member in members[1:]:
        edits = edits | member
    return Annotated[edits, Field(discriminator="kind")]


def _project_edit_result(
    edit: Any, result: BaseModel | None, models: RestModels, context: dict[str, Any] | None = None
) -> BaseModel | None:
    """Project a batch-edit result onto the shape for its edit.

    A delete or a miss yields ``None``; a create projects onto the create
    response (so a one-time-reveal field survives) and an update onto the update
    response.
    """
    if isinstance(edit, Delete) or result is None:
        return None
    if isinstance(edit, Create):
        return _project(result, models.create_response, context)
    return _project(result, models.update_response, context)
