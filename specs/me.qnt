// me.qnt — formal spec for the `me` endpoint (issue #150).
//
// After #131 / #132 / #133 / #151 a caller can authenticate via an API key, our
// BFF session cookie, or a provider bearer token, and every method reduces to
// the same `Principal` vocabulary. The `me` endpoint *exposes* it, in the OIDC
// UserInfo shape (OpenID Connect Core §5.3).
//
// The model mirrors `auth_me_routes.register_me_routes` + `_principal_body` /
// `_profile_claims`:
//
//   1. the strict credential is required: an absent OR invalid credential is a
//      401 (unauthenticated), regardless of the app's posture;
//   2. an authenticated caller gets `sub == str(principal.id)` — the *internal*
//      id, not the provider `external_id` — and `sub` is always present;
//   3. `me` reads the caller's OWN row by `principal.id` through the resource's
//      own service, so no client-supplied id can make it return another user, and
//      the `users` read grant does not gate it;
//   4. a profile claim is included only when the row carries a non-null value;
//      an unavailable claim is OMITTED, never present as null.
//
// Laws (each is a `run` below):
//   - absentIsUnauthenticated / invalidIsUnauthenticated: no/an invalid
//     credential is a 401.
//   - authenticatedIsOk: a valid credential is a 200.
//   - subIsAlwaysPresent: every authenticated body carries `sub`.
//   - subIsTheInternalId: `sub` is the internal principal id, not the external
//     id.
//   - unavailableClaimIsOmitted: an unavailable profile claim is absent from the
//     body (not null).
//   - availableClaimIsIncluded: an available one is present with its value.
//   - selfOnly: the id read is the caller's own, whatever id the client names.
//   - notGatedByUsersGrant: the served body is independent of the `users` read
//     grant.

module me {
  // The authentication classification, mirroring AuthResult's principal field.
  type AuthClass = Absent | Invalid | Authenticated

  // The served HTTP status: 200 only for an authenticated caller.
  pure def status(cls: AuthClass): int =
    if (cls == Authenticated) 200 else 401

  // The authenticated caller's internal id and its provider subject. The route
  // returns the internal id as `sub`; the external id is carried alongside it.
  pure val callerId: str = "internal-user-1"
  pure val callerExternalId: str = "auth0|abc"

  // The always-present part of the body, straight off the Principal. `sub` is
  // the internal id; `external_id` is present only for an external-IdP caller.
  pure val principalBody: str -> str =
    Map(
      "sub" -> callerId,
      "kind" -> "user",
      "external_id" -> callerExternalId
    )

  // A profile attribute on the caller's own row: omitted, or carrying a value.
  type Attr = Unavailable | Available(str)

  // Merge the profile claims onto the principal body: an unavailable attribute
  // adds nothing (it is omitted), an available one adds its claim.
  pure def body(attr: Attr): str -> str =
    match attr {
      Unavailable => principalBody |
      Available(v) => principalBody.put("email", v)
    }

  // The id the route reads the profile row by, or no read at all when
  // unauthenticated. It is always the authenticated caller's own id — never the
  // client-supplied id.
  type Read = NoRead | ReadId(str)

  pure def readId(cls: AuthClass, clientSuppliedId: str): Read =
    if (cls == Authenticated) ReadId(callerId) else NoRead

  // The full served outcome for a request: the status and the body. The body is
  // empty when unauthenticated.
  pure def served(cls: AuthClass, attr: Attr): (int, str -> str) =
    if (cls == Authenticated) (status(cls), body(attr)) else (status(cls), Map())

  // The `users` read grant is deliberately NOT an input to `served` — the route
  // reads the resource's own service, not the authorized one. This helper makes
  // that explicit for the law below.
  pure def servedWithGrant(cls: AuthClass, hasUsersReadGrant: bool, attr: Attr): (int, str -> str) =
    if (hasUsersReadGrant) served(cls, attr) else served(cls, attr)

  // ------------------------------------------------------------------
  // Invariants
  // ------------------------------------------------------------------

  pure def absentIsUnauthenticated: bool = status(Absent) == 401

  pure def invalidIsUnauthenticated: bool = status(Invalid) == 401

  pure def authenticatedIsOk: bool = status(Authenticated) == 200

  pure def subIsAlwaysPresent: bool =
    Set(Unavailable, Available("x@example.com")).forall(a =>
      body(a).keys().contains("sub")
    )

  pure def subIsTheInternalId: bool =
    Set(Unavailable, Available("x@example.com")).forall(a =>
      body(a).get("sub") == callerId and body(a).get("sub") != callerExternalId
    )

  pure def unavailableClaimIsOmitted: bool =
    // The key is absent, not present-with-null (Quint maps have no null, so
    // omission is exactly `keys().contains(...) == false`).
    not(body(Unavailable).keys().contains("email"))

  pure def availableClaimIsIncluded: bool =
    body(Available("x@example.com")).get("email") == "x@example.com"

  pure def selfOnly: bool =
    // Whatever id a client names, the route reads only the caller's own id.
    Set("other-user", "admin", "", callerId).forall(requested =>
      readId(Authenticated, requested) == ReadId(callerId)
    )

  pure def unauthenticatedReadsNothing: bool =
    Set("other-user", callerId).forall(requested =>
      readId(Absent, requested) == NoRead
    )

  pure def notGatedByUsersGrant: bool =
    // The served status and body do not depend on the `users` read grant.
    all {
      Set(true, false).forall(g =>
        servedWithGrant(Authenticated, g, Available("x@example.com"))._1 == status(Authenticated)
      ),
      Set(true, false).forall(g =>
        servedWithGrant(Authenticated, g, Available("x@example.com"))._2 ==
          body(Available("x@example.com"))
      ),
      Set(true, false).forall(g =>
        servedWithGrant(Absent, g, Unavailable)._1 == 401
      ),
    }

  // ------------------------------------------------------------------
  // Tests
  // ------------------------------------------------------------------

  run absentIsUnauthenticatedTest = {
    all {
      assert(absentIsUnauthenticated),
      assert(served(Absent, Unavailable) == (401, Map())),
    }
  }

  run invalidIsUnauthenticatedTest = {
    all {
      assert(invalidIsUnauthenticated),
      assert(served(Invalid, Available("x@example.com"))._1 == 401),
    }
  }

  run authenticatedGetsOkTest = {
    all {
      assert(authenticatedIsOk),
      assert(served(Authenticated, Unavailable)._1 == 200),
    }
  }

  run subIsAlwaysPresentTest = {
    all {
      assert(subIsAlwaysPresent),
      assert(subIsTheInternalId),
    }
  }

  run profileClaimOmissionTest = {
    all {
      assert(unavailableClaimIsOmitted),
      assert(availableClaimIsIncluded),
    }
  }

  run selfOnlyTest = {
    all {
      assert(selfOnly),
      assert(unauthenticatedReadsNothing),
    }
  }

  run notGatedByUsersGrantTest = {
    all {
      assert(notGatedByUsersGrant),
    }
  }

  run invariantsHold = {
    all {
      assert(absentIsUnauthenticated),
      assert(invalidIsUnauthenticated),
      assert(authenticatedIsOk),
      assert(subIsAlwaysPresent),
      assert(subIsTheInternalId),
      assert(unavailableClaimIsOmitted),
      assert(availableClaimIsIncluded),
      assert(selfOnly),
      assert(unauthenticatedReadsNothing),
      assert(notGatedByUsersGrant),
    }
  }
}
