---
name: config
description: Typed environment-variable parsing via resourcey.util.env_parser and polymorphic models via DiscriminatedUnionMixin. Load when working on configuration or polymorphic models.
version: "1.0.0"
---

# Configuration

## env_parser

`resourcey.util.env_parser` converts environment variables into typed pydantic
models. It supports complex nested types and polymorphism that
`pydantic-settings` cannot express. Vendored from the OpenHands Software
Agent SDK — **no runtime dependency on the SDK**.

```python
from resourcey.util.env_parser import from_env, to_env

db_config = from_env(DatabaseConfig, prefix="DB")
template = to_env(DatabaseConfig(host="localhost", port=5432), prefix="DB")
```

* `from_env(Type, prefix="DB")` reads `DB_HOST`, `DB_PORT`, etc. into a typed
  model.
* `to_env(value, prefix="DB")` emits a `.env` template for the model.
* Discriminated unions are keyed by a `KIND` env var
  (`DB_FEATURE_KIND=MyFeature`).

## DiscriminatedUnionMixin

`resourcey.util.models.DiscriminatedUnionMixin` adds a `kind` discriminator
to a pydantic model. Subclasses are serialized with `kind = ClassName`; on
deserialization the correct subclass is chosen. Abstract bases (those
extending `abc.ABC`) are never instantiated directly. Vendored from the SDK —
**no runtime dependency**.

## Rules

* Never introduce an `openhands` import into these modules — they must remain
  self-contained.
* When upgrading behaviour from upstream, copy the logic; do not add a
  dependency.
