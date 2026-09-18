---
name: resources
description: Resource metadata, field annotations, service generation, and how a single resource declaration derives Pydantic schemas, SQLAlchemy models, and a REST service. Load when defining or modifying resources.
version: "1.0.0"
---

# Resources

A **resource** is the central unit of `resourcey`. One declaration produces
Pydantic schemas, a SQLAlchemy model, a REST service, migrations, and
permission checks.

## Resource metadata

* Each field carries enough metadata to derive both a Pydantic field and a
  SQLAlchemy column: type, required/optional, length, uniqueness, index,
  default, and whether it is searchable/sortable.
* A resource declares which of the **standard actions** (`create`, `read`,
  `update`, `delete`, `search`, `batch_read`, `batch_edit`) are enabled.
* A resource may declare relations to other resources; these become
  SQLAlchemy relationships and nested Pydantic schemas.

## Service generation

* The framework generates a service exposing the enabled actions over REST
  (see `rest-api-routes` skill for the endpoint shapes).
* Generated services are overridable — register a custom route at the same
  path and the framework yields to it (escape hatch).
* Services layer as `routers → services → repositories → models`. Never skip
  layers.

## Progressive enhancement

* Use the generated service as-is.
* Replace one action with a custom handler, keeping the rest generated.
* Drop to the repository / session for full control.
The layers compose; none of them trap you.
