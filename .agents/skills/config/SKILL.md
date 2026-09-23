---
name: config
description: Typed environment-variable parsing via resourcey.util.env_parser (and its v2 copy), BaseConfig's per-class instance cache, and polymorphic models via DiscriminatedUnionMixin. Load when working on configuration or polymorphic models.
version: "1.0.0"
---

# Configuration

## `v2` layout (issue #82)

New configuration work goes in `v2`, which runs parallel to v1:

* `resourcey.v2.util.env_parser` — the env parser (copy of the v1 module).
* `resourcey.v2.util.models` — `DiscriminatedUnionMixin`.
* `resourcey.v2.util.import_paths` — dotted-path resolution.
* `resourcey.v2.config.config_base` — `BaseConfig`.
* `resourcey.v2.config.config_loader` — `load_dotenv`.
* `resourcey.v2.config.lazy_field` — `LazyField`.
* `resourcey.v2.core.errors` — `ResourceyError`, `ResourceyConfigError`.

`v2/util` imports the `Missing` sentinel from `v2/core/dto`; there is exactly
one sentinel in `v2` (`v2.util.env_parser.MISSING is v2.core.dto.MISSING`).
Nothing under `v2/` may import `resourcey` code outside `v2/` at runtime.

There is no `FrameworkConfig` / `DbConfig` / `DependencyBuilder` in `v2` yet,
and no `config_runtime` — those come in a later PR.

## BaseConfig

```python
class MyAppConfig(BaseConfig):
    database_url: str = "sqlite+aiosqlite:///app.db"


config = MyAppConfig.get_instance()
```

* `get_instance()` reads `load_dotenv()` then `from_env(cls, prefix=cls.get_prefix())`.
* The cache is **per class** and typed to the owning class:
  `MyAppConfig.get_instance()` returns a `MyAppConfig`. Repeated calls return
  the same instance.
* A subclass and its base cache independently — there is no super/subclass
  acceptance check, so an app config may extend a framework config and both
  stay resolvable. Framework code calls `FrameworkConfig.get_instance()`; the
  app entry point calls `MyAppConfig.get_instance()`.
* `clear_instance_cache()` clears only the class it is called on.
* `get_prefix()` defaults to the top-level module name upper-cased
  (`RESOURCEY` for `resourcey.v2.config.*`).
* A build/parse failure raises `ResourceyConfigError` (a `ResourceyError`).
* `generate_env_template()` emits a commented, valued `.env` template.

Configuration is read from the environment, and env vars beat `.env` file
values. There is no `set_config` / `get_config_as` / `RESOURCEY_CONFIG_CLASS`
in v2.

## env_parser

`resourcey.util.env_parser` (and the `v2` copy) converts environment variables
into typed pydantic models. It supports complex nested types and polymorphism
that `pydantic-settings` cannot express. Vendored from the OpenHands Software
Agent SDK — **no runtime dependency on the SDK**.

```python
from resourcey.v2.util.env_parser import from_env, to_env

db_config = from_env(DatabaseConfig, prefix="DB")
template = to_env(DatabaseConfig(host="localhost", port=5432), prefix="DB")
```

* `from_env(Type, prefix="DB")` reads `DB_HOST`, `DB_PORT`, etc. into a typed
  model.
* `to_env(value, prefix="DB")` emits a `.env` template for the model.
* Discriminated unions are keyed by a `KIND` env var
  (`DB_FEATURE_KIND=MyFeature`).

## DiscriminatedUnionMixin

`resourcey.util.models.DiscriminatedUnionMixin` (and the `v2` copy) adds a
`kind` discriminator to a pydantic model. Subclasses are serialized with
`kind = ClassName`; on deserialization the correct subclass is chosen. Abstract
bases (those extending `abc.ABC`) are never instantiated directly. Vendored
from the SDK — **no runtime dependency**.

## Rules

* Never introduce an `openhands` import into these modules — they must remain
  self-contained.
* When upgrading behaviour from upstream, copy the logic; do not add a
  dependency.
