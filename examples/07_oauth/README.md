# Example 07 — OAuth / OIDC federation (BFF session)

A small message board that federates authentication to an **external identity
provider**, demonstrating Part 4 of the authentication roadmap
([#151](https://github.com/tofarr/resourcey/issues/151)): a pluggable OAuth /
OIDC method that plugs into the same `Authenticator` seam as the API key (03) and
the session cookie (04).

It builds on the message board of examples 01 / 04.

## The idea

A caller presents a token the provider issued; the framework verifies it, maps it
to a **local** user, and the ordinary authorization rules decide what it may do:

```
provider token ──▶ OAuthAuthenticator ──▶ ExternalIdentity ──▶ local user ──▶ policies
   (Bearer)          verify signature        (iss, sub)          users row      (roles)
                     pin alg / iss / aud     → user_id           enabled?
                     map claims → Principal
```

The local user store is **authoritative**: a valid provider token whose internal
user is missing or `enabled=False` is rejected. Disable a user and every
credential that resolves to it is revoked, however valid the token.

## What it wires

Everything lives in [`oauth_example/app.py`](oauth_example/app.py) and is built by
one call to [`configure_oauth`](../../src/resourcey/auth/auth_oauth_setup.py):

| Piece | Where | What |
| --- | --- | --- |
| Client config | `APP_OAUTH_CLIENTS_<n>_*` | `issuer` / `jwks_uri` / `audience` / `algorithms` (verification) + `auth_url` / `token_url` / `client_secret` / `scopes` (flow) |
| Identity map | `external_identities` table | `(issuer, subject) → user_id`, unique on the pair |
| Token store | `oauth_tokens` table | the encrypted refresh token the flow persists |
| Local users | `users` table | the internal principal the token resolves to |
| Inbound verifier | `OAuthAuthenticator` | issuer-keyed lookup, JWKS check, `alg` pinned, `(iss, sub)` mapped |
| Interactive flow | `register_oauth_routes` | `GET /oauth/login` → `GET /oauth/callback` → `POST /oauth/refresh` |

### Two roles per client row

The client fields split into two roles and are **not** conflated:

* **verification** (`issuer`, `jwks_uri`, `audience`, `algorithms`) — what
  `authenticate(request)` reads per request. `iss` selects the row; the token is
  then validated against **that row's** JWKS.
* **flow** (`auth_url`, `token_url`, `refresh_url`, `client_secret`,
  `redirect_uri`, `scopes`) — what the *app* reads for the interactive flow.

### No algorithm confusion

`alg` comes from the row's `algorithms` allowlist, never the token header. An
`HS256` token "signed" with the RSA public key (the classic confusion attack) is
rejected before any signature is trusted.

### BFF session posture

The callback mints **our own** session cookie — the browser presents *our*
cookie, never the provider's access token. The provider's refresh token lives
only, encrypted, in `oauth_tokens`; the cookie carries an opaque handle. Our
session `exp` matches the IdP token's expiry (the provider is authoritative), and
the browser `Max-Age` is clamped down to it.

### Ephemeral flow state

`state` and the PKCE verifier are seconds-lived and single-use, so they are
carried in a short-TTL JWE cookie — **not** a table.

## The dev identity provider

So the demo is self-contained, [`oauth_example/dev_idp.py`](oauth_example/dev_idp.py)
mints a dev JWKS and a matching dev token, and the app injects the JWKS fetcher
that serves it. The verification code path is the ordinary one — only the network
fetch is faked. A real deployment points `APP_OAUTH_CLIENTS_0_JWKS_URI` at the
provider and drops the injection.

## What to try

```bash
# reads are public — anonymous callers see the board
curl localhost:8087/threads

# mint a dev token for the seeded 'dev-user' subject, then present it
TOKEN=$(uv run --env-file .env python -c \
  "from oauth_example.dev_idp import make_dev_token; print(make_dev_token(subject='dev-user'))")
curl -H "Authorization: Bearer $TOKEN" localhost:8087/threads

# a USER may create a message and edit only its own
curl -H "Authorization: Bearer $TOKEN" -X POST localhost:8087/messages \
     -H 'Content-Type: application/json' -d '{"thread_id":1,"text":"hi"}'
```

A token for an **unmapped** subject is rejected (fail-closed), and a token whose
local user is disabled is rejected too — the local store wins.

## Run it

```bash
uv sync --extra test
uv run --env-file .env alembic upgrade head   # creates the schema, seeds users + links
uv run --env-file .env uvicorn oauth_example.app:app --port 8087
```

The framework does no `.env` loading of its own, so the `--env-file` flag is what
populates the process environment.

## Tests

```bash
uv run pytest
```

The suite drives the full request → OAuth verify → identity map → local user →
role → service stack through httpx's ASGI transport, with the dev JWKS / token
exchange injected so no network is touched.
