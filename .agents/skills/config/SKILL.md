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
* `resourcey.v2.config.config_base` — `BaseConfig`, `get_config_prefix`,
  `set_config_prefix`, `_reset_config_prefix`.
* `resourcey.v2.config.lazy_field` — `LazyField`.
* `resourcey.v2.core.errors` — `ResourceyError`, `ResourceyConfigError`.

`v2/util` imports the `Missing` sentinel from `v2/util/missing`; there is
exactly one sentinel in `v2`. Nothing under `v2/` may import `resourcey` code
outside `v2/` at runtime.

There is no `FrameworkConfig` / `MigrationConfig` / `DependencyBuilder` in `v2`
yet, and no `config_runtime` / `RESOURCEY_CONFIG_CLASS` — those come in a later
PR. The framework config *blocks* that do exist live with what they configure:
`resourcey.v2.sql.sql_config.SqlConfig` and
`resourcey.v2.encryption.encryption_config.EncryptionKeysConfig`.

## Composing an app config

An app composes the framework blocks by inheritance and calls the composed
config once at its entry point:

```python
class AppConfig(SqlConfig, EncryptionKeysConfig):
    """The app's single config object, composed from the framework blocks."""


config = AppConfig.get_instance()
```

Per-class caches are independent, so `AppConfig.get_instance()` and
`SqlConfig.get_instance()` are distinct instances whose values agree (both read
the same env namespace). `AppConfig(...).generate_env_template()` emits a
skeleton covering every block. Framework internals keep resolving their own
block.

## BaseConfig

```python
class MyAppConfig(BaseConfig):
    database_url: str = "sqlite+aiosqlite:///app.db"


config = MyAppConfig.get_instance()
```

* `get_instance()` reads `os.environ` only, via
  `from_env(cls, prefix=cls.get_prefix())`. `v2` does **no** `.env` loading
  (there is no `config_loader` module) — use `uvicorn --env-file` or a wrapper
  script to populate the environment.
* The cache is **per class** and typed to the owning class:
  `MyAppConfig.get_instance()` returns a `MyAppConfig`. Repeated calls return
  the same instance.
* A subclass and its base cache independently — there is no super/subclass
  acceptance check, so an app config may extend a framework config and both
  stay resolvable. Framework code calls `FrameworkConfig.get_instance()`; the
  app entry point calls `MyAppConfig.get_instance()`.
* `clear_instance_cache()` clears only the class it is called on.
* The env prefix is **process-wide**, not per-class: `get_prefix()` delegates
  to `get_config_prefix()` (default `APP`), so every class reads
  `APP_*`. Set it before the first read with `set_config_prefix("MYAPP")`; the
  first `get_config_prefix()` call latches, and a later set raises
  `ResourceyConfigError` (and clears every class's instance cache).
  `_reset_config_prefix()` is the test-only reset.
* Because all classes share one flat namespace, two classes declaring the same
  field name with **different** types raise `TypeError` at class creation;
  same-name/same-type is allowed. `ClassVar` (`LazyField`) entries are not
  fields.
* A build/parse failure raises `ResourceyConfigError` (a `ResourceyError`).
* `generate_env_template()` emits a commented, valued `.env` template.

Configuration is read from the environment. There is no `set_config` /
`get_config_as` / `RESOURCEY_CONFIG_CLASS` in v2.

## SQL connections (issue #74)

`resourcey.v2.sql.sql_config.SqlConfig` holds `sql_connections: list[DbConfig]`,
parsed as `APP_SQL_CONNECTIONS_<n>_NAME` / `_URL` / `_PASSWORD`. `DbConfig`
requires `name` (unique, non-empty) and carries an optional `SecretStr`
`password` spliced into `url` by `database_url`. `SqlSessionManager(config)`
resolves a connection by name (default: the first) and hands out an async
session maker, building engines lazily and disposing them on exit; an unknown
name or empty list raises `ResourceyConfigError`. `SqlResource(Model, name=...)`
uses it (or an explicit `session_factory=` escape hatch).

## Encryption keys (issue #111)

`resourcey.v2.encryption.encryption_config.EncryptionKeysConfig` (a
`BaseConfig`) parses `APP_ENCRYPTION_KEY_ID` / `_VALUE` and
`APP_DECRYPTION_KEYS_<n>_ID` / `_VALUE`. The `encryption_key` field has a
default factory, so an absent key degrades to a loud dev default
(`EncryptionKeyConfig(value="changeme")` plus a warning naming
`APP_ENCRYPTION_KEY_VALUE`); a *partially* specified key (an id with no value)
raises `ResourceyConfigError` whose message names `encryption_key.value`.
`EncryptionKeyConfig` stays a nested `BaseModel` (mirroring `DbConfig`).

`get_encryption_service()` / `clear_encryption_service_cache()` (in
`encryption_service.py`) are the process-wide accessor pair, symmetric with
`get_sql_session_manager()` / `clear_sql_session_manager_cache()`.
`SqlResource` resolves the service at construction, so cursor pagination works
without wiring one in; `encryption_service=` overrides.

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
