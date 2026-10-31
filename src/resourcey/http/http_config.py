"""``RequestLimitsConfig`` — the request caps on the read *and* write surface.

Three client-supplied knobs are unbounded at every layer: the number of ids in
one ``batch_read``, the ``limit`` on a ``search``, and the number of nodes in one
cross-resource ``batch_edit``. A URL or body is bounded only incidentally, by
proxy / server limits that vary by deployment and are not a contract, so the cap
has to be explicit.

The caps are enforced at the **transport** boundary, not in ``Service``: ``core``
is a lower layer than ``config`` and cannot read this, and a direct service call
is trusted code — the same split that has the transport validate the rest of the
query surface.

``max_batch_read_ids`` and ``max_search_limit`` are separate fields because the
two carry different costs: a batch-read id is a primary-key lookup in an ``IN
(…)`` clause, while a ``limit`` drives rows materialised, serialised, and hashed
for the ``ETag``. They share the default of 100, which ``max_batch_edits`` also
takes: a cross-resource batch is the easiest way to turn one request into
unbounded work. All three must be ``>= 1``: a silently unbounded surface is the
footgun this config closes, so ``None`` (``FileStoreConfig.max_size``'s
"unlimited" spelling) is deliberately not accepted.

This module is part of ``resourcey.http``; it imports only lower framework
layers.
"""

from __future__ import annotations

from pydantic import Field

from resourcey.config.config_base import BaseConfig

# The ceiling on a client-supplied ``search`` limit. ``DEFAULT_LIMIT`` (20) is
# the page size used when the client omits it, and is unrelated.
DEFAULT_MAX_SEARCH_LIMIT = 100

# The ceiling on the number of ids in one ``batch_read``.
DEFAULT_MAX_BATCH_READ_IDS = 100

# The ceiling on the number of nodes in one cross-resource ``batch_edit``.
DEFAULT_MAX_BATCH_EDITS = 100


class RequestLimitsConfig(BaseConfig):
    """The request caps, parsed under the process-wide prefix.

    ``APP_MAX_SEARCH_LIMIT`` / ``APP_MAX_BATCH_READ_IDS`` / ``APP_MAX_BATCH_EDITS``
    override the defaults.
    """

    max_search_limit: int = Field(
        default=DEFAULT_MAX_SEARCH_LIMIT,
        ge=1,
        description=(
            "The largest limit a client may request on search. An omitted limit "
            "defaults to the smaller of 20 and this cap."
        ),
    )
    max_batch_read_ids: int = Field(
        default=DEFAULT_MAX_BATCH_READ_IDS,
        ge=1,
        description="The largest number of ids a client may submit to batch-read.",
    )
    max_batch_edits: int = Field(
        default=DEFAULT_MAX_BATCH_EDITS,
        ge=1,
        description="The largest number of nodes a client may submit to one batch-edit.",
    )
