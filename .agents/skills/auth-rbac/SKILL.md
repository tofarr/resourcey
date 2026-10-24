---
name: auth-rbac
description: Authentication and authorization — the Principal pipeline, authenticators (API key, cookie, OAuth/OIDC), the Policy vocabulary and resolvers, simple roles, stored RBAC, and the me endpoint. Load when working on auth, permissions, or identity.
version: "2.0.0"
---

# Auth & RBAC

`src/resourcey/auth/` holds both halves. It imports only lower framework layers,
and `http` must not import it (the app supplies the builder), so no cycle
exists. `auth` must never import code outside the framework at runtime (pinned
by the isolation test).

Two independent seams, composed by `AuthorizedDependencyBuilder`:

* an **`Authenticator`** — credential → `Principal`;
* a **`PolicyResolver`** — `(resource, principal)` → `list[Policy]`.

Then `AuthorizedService` enforces the resolved policies on every action.

## The principal pipeline

* **`Principal`** (frozen) — `id` (`None` = anonymous), `kind`
  (`USER` / `SERVICE` / `ANONYMOUS`), `external_id`, `roles`, `scopes`,
  `claims`. The three collections are distinct layers and must not be
  conflated: `claims` is what the *credential says* (provenance only, never a
  decision input), `scopes` is a ceiling on *this credential*, and `roles` is
  what the *principal is part of* (sourced into a policy). `roles` is populated
  only by the credential-carried (simple-roles) rung; stored RBAC leaves it
  empty and resolves per request from `id`.
* **`AuthResult`** — one attempt's outcome, keeping **absent** (no credential)
  and **invalid** (presented but rejected) distinguishable, plus
  `refresh_recommended` for a cookie past its freshness threshold.
* **`Authenticator`** — `authenticate(request) -> AuthResult`,
  `dependency()` (the request-cached FastAPI dependency), `challenge()` (the
  `WWW-Authenticate` value).
* `optional_principal(authenticator)` / `required_principal(authenticator)`
  derive the **lenient** (anonymous on absent, 401 on invalid) and **strict**
  (401 unless authenticated) dependencies from the one `AuthResult`, both
  sending the authenticator's challenge.
* **`CompositeAuthenticator`** — an ordered chain (first authenticated wins; a
  seen-but-unauthenticated credential ⇒ invalid; else absent). Its dependency
  synthesises one `Depends(child.dependency())` parameter per child so every
  child's security scheme stays visible.
* `PRINCIPAL_CTX_KEY` is the ctx key the builder publishes the authenticated
  principal under, so a resource service can read it (e.g. an `Owner`-scoped
  resource stamping the owner on a create row). It is a plain string so `core`
  need not import `auth`.
* `principal_is_active(user_resource, principal)` is the shared store check
  used by **both** the API-key and OAuth authenticators: a principal resolving
  to no live, `enabled` row is invalid, so the local store stays authoritative
  over any credential.

## Authenticators

### `ApiKeyAuthenticator`

Reads `X-API-Key` or `Authorization: Bearer`. The key is a `SecretStr` stored
**only as a SHA-256 digest**; a presented key is hashed and the digest looked up
(`find_by_key`) on the inner key resource — never through the public query
surface, so a guess-confirmation oracle is unreachable. The raw key is
disclosed exactly once, in the `201` create response, via
`serialization_context() == {"expose_secrets": True}`.

A matched row resolves to a `Principal`: the row's owner (`user_id`) wins, else
its own `principal_id` (a `SERVICE` principal), else the authenticator's fixed
`principal_id`. A present-but-unparseable id degrades to anonymous rather than
adopting the shared fixed principal. The row's `roles` are populated with no
extra lookup.

`user_resource=` makes the principal id a **stored** one: a key resolving to no
live, `enabled` row is invalid. The check is applied on both the builder path
and the standalone `api_key_dependency`, so a custom router secured by the
reusable dependency cannot accept a disabled principal. A row lacking an
`enabled` attribute counts as enabled (identity-only store); a missing row is a
rejection.

Two key resources, one declaration of storage truth:

* `StoredApiKeyResource` (built by `stored_api_key_resource(...)`) — DB-backed
  `ApiKey` ORM (mintable / revocable at runtime), `ApiKeyBase` for Alembic.
* `ConfigApiKeyResource` (built by `config_api_key_resource(config)`) — a
  read-only `ListResource` over `ApiKeysConfig`
  (`APP_API_KEYS_<n>_ID` / `_NAME` / `_KEY` / `_PRINCIPAL_ID` / `_ROLES_<m>`).
* `stored_api_key_view(inner)` / `config_api_key_view(inner)` wrap either in the
  exposed `ResourceView`; the config view hides `key`, `principal_id`, and
  `roles` from read / search.

The DB-backed key surface is a **privilege-assignment surface** (a writable
`roles` field grants access), so restrict it to administrators.

### `CookieAuthenticator`

Mints / validates a JWE session cookie whose `sub` is the principal id and whose
`roles` claim carries the simple-roles vocabulary. The cookie's `exp` is the
**internal validation threshold** (when to re-check the principal), distinct
from the browser cookie's `Max-Age`; `refresh_after` is an optional margin
before it. `SessionCookieConfig` holds the env-driven settings
(`APP_SESSION_COOKIE_*`).

### `OAuthAuthenticator` (external IdP)

Reads `Authorization: Bearer`, decodes the **unverified** `iss` (used only to
*select* the row), looks the client up with `find_by_issuer`, and verifies
against **that row's** JWKS: signature, `iss` / `aud` / `exp` / `nbf`, and
**`alg` pinned to the row's allowlist** — the header's `alg` is never trusted
(no `none` / HMAC confusion). Claims map onto the `Principal` vocabulary
(`sub` → `external_id`, `scope` / `scp` → `scopes`, a roles claim → `roles`, the
rest → `claims`).

`(iss, sub)` resolves through the `ExternalIdentity` table to our internal
`user_id` — a first-seen pair is **fail-closed** — and the resolved id is
checked with `principal_is_active`, so a valid external credential for a
missing / disabled local user is rejected: **the local user store stays
authoritative over the IdP**. A client row's own `roles` are unioned onto the
principal's.

The JWKS fetch is cached (`jwks_cache_ttl`) and optionally host-allowlisted
(`allowed_hosts`), because `jwks_uri` from a row is an SSRF vector and
`authenticate` runs per request.

Supporting modules: `auth_oauth_config.py` (`OAuthClientConfig`, `IdpConfig` —
the *verification* fields are `issuer` / `jwks_uri` / `audience` / `algorithms`;
the *flow* fields are `auth_url` / `token_url` / `refresh_url` /
`client_secret` / `redirect_uri` / `scopes`), `auth_oauth_client.py` (the dual
config/DB client resources, `ExternalIdentity`, `oauth_client_view` hiding
`client_secret`), `auth_oauth_service.py` (`find_by_issuer`,
`find_by_issuer_subject`, `link`), `auth_oauth_token.py` (the encrypted
`OAuthToken` table and its lease-based refresh — at most one refresher per
`(principal, client)`, never `SELECT … FOR UPDATE`), `auth_oauth_provider.py`
(the outbound `CredentialProvider`), `auth_oauth_routes.py` (login / callback /
refresh, BFF posture, state + PKCE in a short-TTL JWE cookie, session `exp`
clamped to the IdP token's), and `auth_oauth_setup.py` (`configure_oauth(...)`,
which builds the whole set in one call).

`refresh_url` (the provider's token endpoint) is unrelated to
`CookieAuthenticator.refresh_after` (the framework's own cookie-staleness
margin).

See `specs/oauth.qnt`.

## Policies

`Policy` (a `DiscriminatedUnionMixin`) reduces itself to a `SearchFilter` via
`async to_search_filter(user_id, action)`. The principal is an argument, not
state, so a policy stays storable.

Built-ins:

| Policy | Grants |
|---|---|
| `AllowAll` | everything, every row |
| `DenyAll` | nothing |
| `ReadOnly` | read / search / count / batch_read |
| `Owner(owner_field="user_id")` | own rows for read-like and by-id writes; `AllFilter` for **create** (a new row has no owner yet — the resource stamps it); `NoMatchFilter` for an anonymous caller |
| `GroupMember(...)` | outcome selected by the principal's group membership; the resolver pre-binds `group_ids`; unbound / empty is fail-closed |
| `Acl(ids=...)` | `AttrFilter(id_field, InFilter(values=...))` — one `IN` predicate |

The built-in reductions use the unparameterized `AllFilter()` /
`NoMatchFilter()` leaves; the pydantic parameterization `AllFilter[Any](...)`
is a distinct class the SQL / Mongo registries do not know.

`PolicyResolver` (a `DiscriminatedUnionMixin`) declares
`async resolve(resource, principal) -> list[Policy]`, with `AllowAllResolver`
(the default — a valid credential grants full access) and `DenyAllResolver`.

`Policy.scopes_to_caller` (a `ClassVar`, default `True` = safe) marks whether a
policy's reduction depends on *who* is asking. `AllowAll` / `DenyAll` /
`ReadOnly` declare `False`. `AuthorizedService` combines it into
`response_is_private()`, which the transport uses to force `private` onto the
cache header.

### Enforcement

`AuthorizedService` wraps a resource's own service and enforces the reduced
filter per action:

* a denied **create** raises `ForbiddenError` (403);
* an out-of-scope **read** / **update** / **delete** raises `NotFoundError`
  (404), so existence is not leaked;
* a denied **search** / **count** `and_`-pushes the filter down and yields an
  empty page / `0` (never an error);
* **batch_read** / **batch_edit** positions are `None`;
* for **update** / **delete** (and each write node of a **batch_edit**) the
  action's filter is folded into the write's own **condition** as an additional
  (AND) constraint — scope is enforced by the *same* atomic write, and a row the
  policy does not grant is a condition miss.

It OR-combines the resolved policies (**union model**): a `DenyAll` from one
role never overrides another's grant. An empty list is deny / fail-closed.

`ForbiddenError` and the singular `normalize_action` live in `core/service.py`.

### `AuthorizedDependencyBuilder`

Composes the two seams plus a third knob:

```python
AuthorizedDependencyBuilder(
    authenticator=ApiKeyAuthenticator(key_resource=...),  # default: fail-closed
    policy_resolver=RolePolicyResolver(...),  # default: AllowAllResolver
    posture=Posture.REQUIRED,  # or Posture.OPTIONAL
)
```

`Posture.OPTIONAL` lets an **absent** credential through as anonymous (a
*presented but invalid* one is still a 401), which is what makes "anonymous
reads, authenticated writes" expressible. `with_posture(...)` is the one-line
override. `get_principal_dependency()` returns the strict / lenient dependency
the transport adds to every route.

## Simple roles (credential-carried)

`auth_role.py` is the single place an app expresses its `role -> policy` rules.
Roles are **per-app, never global** — there is deliberately no process-wide role
registry — and the credential carries the **plain string**, so the framework
never imports app code to interpret a credential.

```python
class Role(AppRole):
    ADMIN = "ADMIN"
    USER = "USER"


resolver = RolePolicyResolver(
    role_policies={Role.ADMIN: [AllowAll()]},
    resource_role_policies={"messages": {Role.USER: [ReadOnly(), Owner("author_id")]}},
    default=[],  # explicit, fail-closed
    resource_defaults={"threads": [ReadOnly()]},
)
```

`role_key` / `role_keys` / `roles_from_credential` are the string conversion
seam. An unknown role grants nothing. The static mapping is this rung; the
store-backed resolver is the next one.

See `specs/roles.qnt`.

## Stored RBAC

Three modules, all model-first:

* `auth_rbac.py` — the ORM models on a local `RbacBase`: `User`, `Group`,
  `GroupUser` (membership), `Role`, `GroupRole` (assignment), `RolePermission`
  (a `resource` name + a JSON-serialized `Policy`), and `ResourceAcl` (the
  materialized per-object grant, keyed by `(principal_id, resource_name)`).
  `RBAC_MODELS` and `rbac_resources(...)` expose the whole set as ordinary
  `SqlResource`s so it can be seeded / administered over REST.
* `auth_rbac_store.py` — `RbacStore` (the read seam: `policies_for`,
  `groups_for`, `acl_ids`, `acl_id_subquery`) and `SqlRbacStore`. Resolution
  **collapses at the store**: the query joins membership → role → permission
  filtered to the target resource, with `SELECT DISTINCT` so roles sharing a
  policy collapse. The session source is an `async_sessionmaker` or a zero-arg
  callable returning one (sync or async), so a store can be built before the app
  lifecycle. `ACL_MAX_IDS = 100` caps the enumeration path; beyond it the join /
  subquery flavour is the answer. `policy_from_rows` skips a corrupt row rather
  than crashing or over-denying.
* `auth_rbac_resolver.py` — `RbacPolicyResolver(store, cache_ttl=None)`.
  Per request it takes the principal, asks the store for its groups and the
  resource-scoped policies, binds membership onto any `GroupMember` policy, and
  returns them. An anonymous principal resolves to `[]`.
  `materialized_acl(...)` is the portable bridge. **Freshness is explicit**:
  `cache_ttl=None` resolves every request, so a membership change is immediate;
  a positive value bounds staleness to (at most) the credential's own
  validation threshold.

See `specs/rbac.qnt`.

## The `me` endpoint

`register_me_routes(app, *, authenticator, user_resource=None, path="me",
prefix="")` — mounted **after** `create_app` (a "who am I" response is not one
of the eight standard actions, and the app owns the authenticator the route must
agree with). It works uniformly across API-key, cookie, and OAuth with no
method-specific code.

The body is **OIDC UserInfo**-shaped: `sub` (the internal principal id as a
string, **always present**), plus the framework's `kind` / `roles` / `scopes`,
`external_id` for an external-IdP caller, and profile claims (`email`,
`preferred_username`, `name`, `email_verified`, `updated_at`) read from the
caller's **own** row of `user_resource`. An unavailable claim is **omitted**,
never `null`.

Authorization is the crux: the route reads the row through the resource's **own**
service, not an `AuthorizedService`, so an admin-only `users` surface does not
block a caller from seeing its own `me`; and it reads only the caller's own id,
never a client-named one, so it can only return the identity the credential
already proved (a missing row yields `sub` alone, not a 404). It depends on the
**strict** principal dependency, so an absent or invalid credential is a 401
regardless of the builder's posture.

See `specs/me.qnt`.

## Related specs

`specs/auth.qnt`, `specs/api_key.qnt`, `specs/principal_store.qnt`,
`specs/permissions.qnt`, `specs/roles.qnt`, `specs/rbac.qnt`,
`specs/oauth.qnt`, `specs/me.qnt`.

## Runnable examples

`examples/03_api_key_auth` (API key), `04_simple_roles` (roles on the
credential), `05_full_rbac` (stored RBAC), `07_oauth` (OIDC federation + BFF
session). Each README is the walkthrough.
