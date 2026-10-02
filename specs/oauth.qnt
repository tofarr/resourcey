// oauth.qnt — formal spec for the OAuth / OIDC authenticator (issue #151).
//
// Part 4 of the auth roadmap adds a second credential family alongside the API
// key (#118) and the session cookie (#131): a bearer token issued by an external
// identity provider. The authenticator reduces it to the *same* `Principal`
// vocabulary, so it composes with `CompositeAuthenticator` ("OAuth **or** API
// key") and plugs into the same `AuthorizedDependencyBuilder`.
//
// The model mirrors `OAuthAuthenticator.authenticate`:
//
//   1. select the client row by the token's **unverified** `iss` claim;
//   2. verify the token against **that row's** JWKS: signature, `alg` pinned to
//      the row's allowlist, and the registered claims `iss` / `aud` / `exp`;
//   3. map `(iss, sub)` through the ExternalIdentity store to our internal user
//      id — a first-seen pair is fail-closed;
//   4. validate that internal user against the optional principal store (live +
//      `enabled`), reusing the shared check from #132.
//
// Laws (each is a `run` below):
//   - failClosedOnNoClient: a token whose `iss` names no configured client is
//     Invalid.
//   - audienceIsScopedToTheIssuer: `iss` selects the client row and `aud` is
//     checked against *that* row, so a token for one issuer carrying another
//     client's audience is Invalid.
//   - audienceMismatchRejected: a token whose `aud` is not the row's audience is
//     Invalid.
//   - expiredRejected: an expired token is Invalid.
//   - algIsPinned: a token signed with an algorithm outside the row's allowlist
//     is Invalid — the classic HMAC-confusion attack (an `HS256` token signed
//     with the RSA public key) does not authenticate.
//   - unmappedIdentityIsFailClosed: a valid token whose `(iss, sub)` is not in
//     the ExternalIdentity store is Invalid when an identity store is configured.
//   - localUserStoreIsAuthoritative: a disabled or missing local user is Invalid,
//     however valid the external credential.
//   - absentIsNotInvalid: no credential is Absent, not Invalid.

module oauth {
  // The authentication classification, mirroring AuthResult's principal field.
  type Class = Absent | Invalid | Authenticated

  // What the request presented: nothing, or a token.
  type Presented = NoCredential | WithToken(Token)

  // The claims a presented token carries.
  type Token = {
    iss: str,
    sub: str,
    aud: str,
    alg: str,
    expired: bool,
    // Whether the signature verifies under the selected client's keys *for the
    // token's own alg*. An alg-confusion token is HMAC-valid but not RSA-valid.
    signatureValid: bool,
  }

  // A configured client row: its issuer, audience, and accepted algorithms.
  type Client = { issuer: str, audience: str, algorithms: Set[str] }

  // The registry of configured clients (the inner client resource, looked up by
  // `iss`). Two issuers, one accepting only RS256.
  pure val clients: str -> Client =
    Map(
      "https://idp.example" -> { issuer: "https://idp.example", audience: "client1", algorithms: Set("RS256") },
      "https://other.example" -> { issuer: "https://other.example", audience: "client2", algorithms: Set("RS256") }
    )

  // The ExternalIdentity store: `(iss, sub) -> user_id`. An unmapped pair is a
  // first-seen external principal.
  pure val identities: (str, str) -> str =
    Map(
      ("https://idp.example", "ext-123") -> "user-1",
      ("https://idp.example", "ext-disabled") -> "user-2",
      ("https://other.example", "ext-9") -> "user-1"
    )

  // The principal store: the enabled and the disabled internal users.
  pure val enabledUsers: Set[str] = Set("user-1")
  pure val disabledUsers: Set[str] = Set("user-2")

  // A token is well-formed when it verifies under its issuer's row: the alg is in
  // the row's allowlist, the signature checks out, and the registered claims
  // agree (issuer, audience, not expired).
  pure def verifies(token: Token, client: Client): bool =
    all {
      client.algorithms.contains(token.alg),
      token.signatureValid,
      token.iss == client.issuer,
      token.aud == client.audience,
      not(token.expired),
    }

  // The full classification, mirroring the authenticator's pipeline. A token
  // runs the four steps; no credential is Absent.
  pure def classify(
    presented: Presented, hasIdentityStore: bool, hasUserStore: bool
  ): Class =
    match presented {
      NoCredential => Absent |
      WithToken(t) => classifyToken(t, hasIdentityStore, hasUserStore)
    }

  pure def classifyToken(
    token: Token, hasIdentityStore: bool, hasUserStore: bool
  ): Class = {
    // Step 1: select the client by the unverified `iss`.
    if (not(clients.keys().contains(token.iss))) Invalid
    else {
      val client = clients.get(token.iss)
      // Step 2: verify against that client's row.
      if (not(verifies(token, client))) Invalid
      else {
        val key = (token.iss, token.sub)
        // Step 3: resolve `(iss, sub)` to our internal user id.
        if (not(identities.keys().contains(key))) {
          if (hasIdentityStore) Invalid else Authenticated
        } else {
          val userId = identities.get(key)
          // Step 4: the local user store is authoritative.
          if (not(hasUserStore)) Authenticated
          else if (enabledUsers.contains(userId)) Authenticated
          else Invalid
        }
      }
    }
  }

  // A helper token builder.
  pure def token(iss: str, sub: str, aud: str, alg: str, expired: bool, signatureValid: bool): Token =
    { iss: iss, sub: sub, aud: aud, alg: alg, expired: expired, signatureValid: signatureValid }

  pure val good: Token =
    token("https://idp.example", "ext-123", "client1", "RS256", false, true)

  // ------------------------------------------------------------------
  // Invariants
  // ------------------------------------------------------------------

  pure def failClosedOnNoClient: bool =
    all {
      classifyToken(token("https://evil.example", "ext-123", "client1", "RS256", false, true), true, true) == Invalid,
      classifyToken(token("https://evil.example", "ext-123", "client1", "RS256", false, true), false, false) == Invalid,
    }

  pure def audienceIsScopedToTheIssuer: bool =
    // `iss` selects the row, and `aud` is checked against *that* row: a token
    // for `idp.example` bearing the *other* client's audience is rejected, while
    // the same audience is accepted for its own issuer.
    all {
      classifyToken(token("https://idp.example", "ext-123", "client1", "RS256", false, true), true, true) == Authenticated,
      classifyToken(token("https://idp.example", "ext-123", "client2", "RS256", false, true), true, true) == Invalid,
      classifyToken(token("https://other.example", "ext-9", "client2", "RS256", false, true), true, true) == Authenticated,
    }

  pure def audienceMismatchRejected: bool =
    classifyToken(token("https://idp.example", "ext-123", "wrong-aud", "RS256", false, true), true, true) == Invalid

  pure def expiredRejected: bool =
    classifyToken(token("https://idp.example", "ext-123", "client1", "RS256", true, true), true, true) == Invalid

  pure def algIsPinned: bool =
    // The HMAC-confusion attack: an HS256 token "signed" with the RSA public
    // key. It is outside the row's allowlist, so it never authenticates.
    all {
      classifyToken(token("https://idp.example", "ext-123", "client1", "HS256", false, true), true, true) == Invalid,
      classifyToken(token("https://idp.example", "ext-123", "client1", "none", false, true), true, true) == Invalid,
    }

  pure def unmappedIdentityIsFailClosed: bool =
    all {
      classifyToken(token("https://idp.example", "ghost", "client1", "RS256", false, true), true, true) == Invalid,
      // With no identity store, a valid external principal is a SERVICE principal.
      classifyToken(token("https://idp.example", "ghost", "client1", "RS256", false, true), false, false) == Authenticated,
    }

  pure def localUserStoreIsAuthoritative: bool =
    all {
      // A valid external credential for a disabled local user is rejected.
      classifyToken(token("https://idp.example", "ext-disabled", "client1", "RS256", false, true), true, true) == Invalid,
      // The same credential without a local store is trusted.
      classifyToken(token("https://idp.example", "ext-disabled", "client1", "RS256", false, true), true, false) == Authenticated,
    }

  pure def absentIsNotInvalid: bool =
    all {
      classify(NoCredential, true, true) == Absent,
      classify(WithToken(token("https://idp.example", "ghost", "client1", "RS256", false, true)), true, true) == Invalid,
    }

  pure def aGoodTokenAuthenticates: bool =
    classifyToken(good, true, true) == Authenticated

  // ------------------------------------------------------------------
  // Refresh serialization (the lease / compare-and-swap)
  // ------------------------------------------------------------------

  // A stored token row: ``version`` bumps on each successful refresh, and
  // ``leaseOwner`` is empty when the refresh lease is free. The lease is a
  // compare-and-swap column, not ``FOR UPDATE`` (which SQLite drops and Mongo
  // lacks), so the whole contract is the conditional update below.
  type Row = { version: int, leaseOwner: str }

  // Claim the lease with a CAS: succeeds only when the row is unleased.
  pure def claim(row: Row, me: str): (bool, Row) =
    if (row.leaseOwner == "") (true, { ...row, leaseOwner: me })
    else (false, row)

  // The lease holder completes: bump the version (the one exchange) and release.
  pure def complete(row: Row, me: str): Row =
    if (row.leaseOwner == me) { version: row.version + 1, leaseOwner: "" } else row

  // Two concurrent callers race on the same free row. The conditional update
  // makes the claims sequential against the row: once ``a`` writes the lease,
  // ``b``'s claim sees it and fails — so **at most one** wins, i.e. at most one
  // refresh is issued.
  pure def atMostOneRefresher: bool = {
    val row0: Row = { version: 0, leaseOwner: "" }
    val (aClaimed, rowA) = claim(row0, "a")
    val (bClaimed, _) = claim(rowA, "b")
    not(aClaimed and bClaimed)
  }

  // The winner's refresh is observed by everyone: exactly one exchange runs
  // (the single-use token is never double-spent) and the row ends fresh.
  pure def refreshRunsExactlyOnce: bool = {
    val row0: Row = { version: 0, leaseOwner: "" }
    val (aClaimed, rowA) = claim(row0, "a")
    val (bClaimed, _) = claim(rowA, "b")
    val exchanges = (if (aClaimed) 1 else 0) + (if (bClaimed) 1 else 0)
    val fresh = complete(rowA, "a")
    all {
      exchanges == 1,
      fresh.version == 1,
      fresh.leaseOwner == "",
    }
  }

  // A still-leased row is not reclaimable (the loser waits, it does not refresh),
  // while a released row is.
  pure def leaseGatesTheRefresh: bool = {
    val leased: Row = { version: 0, leaseOwner: "a" }
    val free: Row = { version: 1, leaseOwner: "" }
    all {
      not(claim(leased, "b")._1),
      claim(free, "b")._1,
    }
  }

  // ------------------------------------------------------------------
  // Tests
  // ------------------------------------------------------------------

  run failClosedOnNoClientTest = {
    assert(failClosedOnNoClient)
  }

  run aValidTokenAuthenticates = {
    all {
      assert(classifyToken(good, true, true) == Authenticated),
      assert(aGoodTokenAuthenticates),
    }
  }

  run issuerAndAudienceAreChecked = {
    all {
      assert(audienceIsScopedToTheIssuer),
      assert(audienceMismatchRejected),
    }
  }

  run expiryIsChecked = {
    assert(expiredRejected)
  }

  run algorithmIsPinnedNoConfusion = {
    assert(algIsPinned)
  }

  run identityIsFailClosed = {
    assert(unmappedIdentityIsFailClosed)
  }

  run localUserStoreIsAuthoritativeTest = {
    assert(localUserStoreIsAuthoritative)
  }

  run absentIsNotInvalidTest = {
    all {
      assert(absentIsNotInvalid),
      assert(classify(NoCredential, true, true) == Absent),
    }
  }

  run refreshSerializesAcrossCallers = {
    all {
      assert(atMostOneRefresher),
      assert(refreshRunsExactlyOnce),
      assert(leaseGatesTheRefresh),
    }
  }

  run invariantsHold = {
    all {
      assert(failClosedOnNoClient),
      assert(aGoodTokenAuthenticates),
      assert(audienceIsScopedToTheIssuer),
      assert(audienceMismatchRejected),
      assert(expiredRejected),
      assert(algIsPinned),
      assert(unmappedIdentityIsFailClosed),
      assert(localUserStoreIsAuthoritative),
      assert(absentIsNotInvalid),
      assert(atMostOneRefresher),
      assert(refreshRunsExactlyOnce),
      assert(leaseGatesTheRefresh),
    }
  }
}
