---
name: config
description: Typed environment parsing via resourcey.util.env_parser, BaseConfig's per-class cache and process-wide prefix, LazyField, and polymorphic models via DiscriminatedUnionMixin. Load when adding or changing configuration or a polymorphic model.
version: "2.0.0"
---

# Configuration

## Where it lives

* `resourcey.util.env_parser` — the parser (`from_env` / `to_env`).
* `resourcey.util.models` — `DiscriminatedUnionMixin`.
* `resourcey.util.import_paths` — dotted-path resolution.
* `resourcey.util.missing` — the one `Missing` / `MISSING` sentinel.
* `resourcey.config.config_base` — `BaseConfig`, `get_config_prefix`,
  `set_config_prefix`, `_reset_config_prefix`.
* `resourcey.config.lazy_field` — `LazyField`.
* `resourcey.core.errors` — `ResourceyError`, `ResourceyConfigError`.

`util` is the bottom layer and imports no other project package. There is
deliberately no `config_loader` (the framework does no `.env` loading) and no
`RESOURCEY_CONFIG_CLASS` selector.

## BaseConfig

```python
class MyAppConfig(BaseConfig):
    database_url: str = "sqlite+aiosqlite:///app.db"


config = MyAppConfig.get_instance()
```

* `get_instance()` reads `os.environ` only, via `from_env`. The framework does
  **no** `.env` loading — use `uvicorn --env-file .env`, `uv run --env-file
  .env`, or a wrapper script.
* The cache is **per class** and typed to the owning class: a subclass and its
  base cache independently and there is no super/subclass acceptance check.
  `clear_instance_cache()` clears only the class it is called on.
* The env prefix is **process-wide**, not per-class: `get_prefix()` delegates to
  `get_config_prefix()` (default `APP`), so every class reads `APP_*`. The first
  read **latches**; a later `set_config_prefix(...)` raises
  `ResourceyConfigError` and clears every class's cache, because a late set
  would silently reuse configs built under the old prefix. `_reset_config_prefix`
  is the test-only reset.
* Because all classes share one flat namespace, two classes declaring the same
  field name with **different** types raise `TypeError` at class creation;
  same-name/same-type is allowed. `ClassVar` entries (`LazyField`) are not
  fields.
* A parse / build failure raises `ResourceyConfigError`.
* `generate_env_template()` emits a commented, valued `.env` skeleton.

## Composing an app config

An app composes the framework blocks by inheritance and calls the composed
config once at its entry point:

```python
class AppConfig(SqlConfig, EncryptionKeysConfig, BackgroundTasksConfig): ...


config = AppConfig.get_instance()
```

Framework internals keep resolving their own block; the per-class caches stay
independent while the values agree, because all read the one env namespace.

The blocks, each living with what it configures:

| Block | Prefix | Fields |
|---|---|---|
| `SqlConfig` | `APP_SQL_CONNECTIONS_<n>_*` | `sql_connections: list[DbConfig]` — `NAME` / `URL` / `PASSWORD` |
| `MongoConfig` | `APP_MONGO_CONNECTIONS_<n>_*` | `mongo_connections` (alias `connections`) — `NAME` / `URL` / `PASSWORD` |
| `EncryptionKeysConfig` | `APP_ENCRYPTION_KEY_*`, `APP_DECRYPTION_KEYS_<n>_*` | `encryption_key`, `decryption_keys` |
| `ApiKeysConfig` | `APP_API_KEYS_<n>_*` | `ID` / `NAME` / `KEY` / `PRINCIPAL_ID` / `ROLES_<m>` |
| `IdpConfig` | `APP_OAUTH_CLIENTS_<n>_*` | verification + flow fields, flow paths, state TTL, JWKS cache |
| `SessionCookieConfig` | `APP_SESSION_COOKIE_*` | cookie name / max-age / refresh margin |
| `FileStoreConfig` | `APP_MEDIUM_*`, `APP_DOWNLOAD_URL_TTL_SECONDS`, `APP_MAX_SIZE` | the selected medium's fields + limits |
| `RequestLimitsConfig` | `APP_MAX_SEARCH_LIMIT`, `APP_MAX_BATCH_READ_IDS` | the read surface's request caps (both default 100, both `>= 1`) |
| `RealtimeConfig` | `APP_CHANNEL_*`, `APP_HEARTBEAT_SECONDS` | the selected channel's fields |
| `BackgroundTasksConfig` | `APP_BACKGROUND_TASKS_*` | timezone, scheduler enabled, per-task entries |
| `JobsConfig` | `APP_JOBS_*` | sweep interval, concurrency, attempts, runner enabled |
| `TriggerConfig` | `APP_TRIGGERS_<n>_*` | `RESOURCE_PATH` / `TRIGGER_KIND` + kind fields |

**The field name is the env variable.** `sql_connections` (not `connections`)
exists because the parser derives `APP_SQL_CONNECTIONS_<n>_NAME` from it; a
`connections` field would read the wrong variable. When a field name must differ
from the conventional variable, add a read-only alias property rather than
renaming the field.

## `LazyField` — selecting a class by name

A `ClassVar` holding a dotted path that is resolved on first access, so a
deployment selects an implementation with no code change:

```python
class FileStoreConfig(BaseConfig):
    medium: ClassVar[FileStore] = LazyField(default=LocalFileStore)
```

The env variable is derived from the field name — `medium` → `APP_MEDIUM_CLASS`
— and the selected class's own fields then parse under `APP_MEDIUM_*`. A
*set-but-empty* `{NAME}_CLASS` raises rather than falling back to the default:
silently downgrading to the local medium is the wrong failure mode.
`FileStoreConfig` uses it for the medium and `RealtimeConfig` for the channel
(`APP_CHANNEL_CLASS`). Because it is a `ClassVar`, it does not participate in the
flat-namespace field collision check.

`ClassVar[list[type[SomeBase]]]` resolves a `{NAME}` / `{NAME}_0` / `{NAME}_1`
list of dotted import paths instead.

## env_parser

`resourcey.util.env_parser` converts environment variables into typed pydantic
models. It supports nested types and polymorphism that `pydantic-settings`
cannot express. Vendored from the OpenHands Software Agent SDK — **no runtime
dependency on the SDK**.

```python
from resourcey.util.env_parser import from_env, to_env

db_config = from_env(DatabaseConfig, prefix="DB")
template = to_env(DatabaseConfig(host="localhost", port=5432), prefix="DB")
```

* `from_env(Type, prefix="DB")` reads `DB_HOST`, `DB_PORT`, … into a typed
  model; with no variables present it returns `Type()`.
* `to_env(value, prefix="DB")` emits a `.env` template.
* A list field `items` reads `PREFIX_ITEMS_<n>_FIELD`; a polymorphic entry adds
  `PREFIX_ITEMS_<n>_KIND=<ClassName>`.
* A `SecretStr` field is redacted on output.

## DiscriminatedUnionMixin

`resourcey.util.models.DiscriminatedUnionMixin` adds a `kind` discriminator to a
pydantic model. Subclasses serialize with `kind = ClassName`; on deserialization
the right subclass is chosen. Abstract bases (extending `abc.ABC`) are never
instantiated directly. Vendored from the SDK — **no runtime dependency**.

Every pluggable seam in the framework is one of these: `DependencyBuilder`,
`Authenticator`, `Policy` / `PolicyResolver`, `CacheStrategy`, `CacheStore`,
`FileStore`, `Channel`, `Trigger`, `JobDetails`, `BackgroundTask`, and the retry
strategies. So a deployment selects a concrete kind by dotted path.

A parameterised generic field (`Trigger[Any, Any]`, `SearchFilter[Any]`) needs
`SkipValidation` on the annotation plus a `mode="before"` resolver, or the
mixin's "no subclasses" guard trips on an already-built instance.

## Rules

* Never introduce an `openhands` import into `util` — those modules stay
  self-contained.
* When upgrading behaviour from upstream, copy the logic; do not add a
  dependency.
* Never hand-edit `uv.lock`; use `uv add` / `uv remove` / `uv sync`.
