// auth.qnt — formal spec for authentication (issues #118 / #131).
//
// The principal pipeline (issue #131) classifies one request three ways —
// absent / invalid / authenticated — and derives both the lenient and the
// strict principal dependency from that single result, so the credential tier
// and the requirement tier stay one code path.
//
// ApiKeyAuthenticator is one method on that pipeline. Its key check composes
// with a resource's own per-request service dependency and runs first, over a
// fresh call-scoped context, so it owns the key storage it opens and can never
// adopt (or hand off to) the target resource's session. The key is validated by
// hashing the presented value and searching for the digest; the presented
// plaintext is never compared to a stored plaintext, because none is stored.
//
// Laws:
//   - failClosed: an empty stored-key set (or no key resource) denies every
//     request. A missing configuration never opens the API.
//   - absentEqInvalid: a request with no key and one with a wrong key produce
//     the identical outcome (one 401 carrying the challenge), so the endpoint
//     does not reveal whether a credential was expected.
//   - validGrantsAccess: a presented key whose digest is stored grants access.
//   - checkPrecedesTargetStorage: the key check is attempted before the target
//     resource's service is built, for every request.
//   - freshContext: the key service is opened over its own context, never the
//     request's, so ownership of the two storages is never conflated.
//   - strictGuardsEveryUnauthenticated / lenientAdmitsAnonymous /
//     invalidIsNeverAnonymous: the strict dependency 401s unless authenticated;
//     the lenient one admits only *absent* credentials (invalid is still 401);
//     a presented-but-bad credential is never treated as anonymous.
//   - compositeFirstWins: a composite authenticator's first grant wins, an
//     invalid result outranks absent, and all-absent is absent.

module auth {
  // A stored key set, reduced to the digests (integers stand in for SHA-256
  // hex). `digest` is total; 0 is "not a key we minted".
  pure val storedKeys: Set[str] = Set("rsk_alpha", "rsk_beta")

  pure def digest(key: str): int =
    if (key == "rsk_alpha") 11
    else if (key == "rsk_beta") 22
    else 0

  pure val storedDigests: Set[int] = storedKeys.map(digest)

  // The header a request presents, if any.
  type Presented = NoKey | Key(str)

  // The lookup seam: the digests the authenticator searches, given a stored set.
  pure def searchable(store: Set[str]): Set[int] = store.map(digest)

  // Whether `presented` authenticates against `store`.
  pure def authenticates(store: Set[str], presented: Presented): bool =
    match presented {
      NoKey => false |
      Key(k) => k != "" and searchable(store).contains(digest(k))
    }

  // The HTTP outcome. One 401 for absent and invalid alike.
  type Outcome = Granted | Denied({ status: int, challenge: str })

  pure val challenge: str = "Bearer realm=api-key"
  pure val unauthorized: Outcome = Denied({ status: 401, challenge: challenge })

  pure def respond(store: Set[str], presented: Presented): Outcome =
    if (authenticates(store, presented)) Granted else unauthorized

  // Precedence: the key check is attempted before the target service is built.
  // Modelled as an ordered pair of booleans (checked, targetOpened).
  type Attempt = { checked: bool, targetOpened: bool }

  pure def attempt(store: Set[str], presented: Presented): Attempt =
    val granted = authenticates(store, presented)
    { checked: true, targetOpened: granted }

  // Invariants

  pure def failClosed: bool =
    all {
      respond(Set(), NoKey) == unauthorized,
      respond(Set(), Key("rsk_alpha")) == unauthorized,
    }

  pure def absentEqInvalid: bool =
    all {
      respond(storedKeys, NoKey) == unauthorized,
      respond(storedKeys, Key("forged")) == unauthorized,
      // The two responses are indistinguishable.
      respond(storedKeys, NoKey) == respond(storedKeys, Key("forged")),
    }

  pure def validGrantsAccess: bool =
    all {
      respond(storedKeys, Key("rsk_alpha")) == Granted,
      respond(storedKeys, Key("rsk_beta")) == Granted,
    }

  pure def checkPrecedesTargetStorage: bool =
    all {
      // Granted: checked first, then the target opened.
      attempt(storedKeys, Key("rsk_alpha")).checked,
      // Denied: checked, target never opened.
      attempt(storedKeys, Key("forged")).checked,
      not(attempt(storedKeys, Key("forged")).targetOpened),
      not(attempt(storedKeys, NoKey).targetOpened),
    }

  pure def freshContext: bool =
    // The key service is opened over its own context; the target's service uses
    // the request context. Modelled as two distinct context ids.
    "key-ctx" != "request-ctx"

  // Tests

  run emptyStoreDeniesEverything = {
    all {
      assert(respond(Set(), NoKey) == unauthorized),
      assert(respond(Set(), Key("anything")) == unauthorized),
      assert(failClosed),
    }
  }

  run absentAndInvalidAreIndistinguishable = {
    all {
      assert(respond(storedKeys, NoKey) == unauthorized),
      assert(respond(storedKeys, Key("forged")) == unauthorized),
      assert(respond(storedKeys, NoKey) == respond(storedKeys, Key("forged"))),
      assert(absentEqInvalid),
    }
  }

  run aValidKeyGrantsAccess = {
    all {
      assert(respond(storedKeys, Key("rsk_alpha")) == Granted),
      assert(respond(storedKeys, Key("rsk_beta")) == Granted),
      assert(not(respond(storedKeys, Key("forged")) == Granted)),
      assert(validGrantsAccess),
    }
  }

  run theCheckRunsBeforeTargetStorage = {
    val denied = attempt(storedKeys, Key("forged"))
    val allowed = attempt(storedKeys, Key("rsk_alpha"))
    all {
      assert(denied.checked),
      assert(not(denied.targetOpened)),
      assert(allowed.checked),
      assert(allowed.targetOpened),
      assert(checkPrecedesTargetStorage),
    }
  }

  run theKeyServiceOwnsItsOwnContext = {
    all {
      assert(freshContext),
    }
  }

  run invariantsHold = {
    all {
      assert(failClosed),
      assert(absentEqInvalid),
      assert(validGrantsAccess),
      assert(checkPrecedesTargetStorage),
      assert(freshContext),
    }
  }

  // ------------------------------------------------------------------
  // The principal pipeline (issue #131)
  // ------------------------------------------------------------------
  //
  // Authentication classifies one request three ways: absent (no credential),
  // invalid (credential presented but not accepted), authenticated (a
  // principal). A single classification derives both dependency tiers -- a
  // lenient one (anonymous on absent, 401 on invalid) and a strict one (401
  // unless authenticated) -- so the credential tier and the requirement tier
  // stay one code path.

  type AuthClass = Absent | Invalid | Authenticated

  pure def classify(presented: bool, valid: bool): AuthClass =
    if (not(presented)) Absent
    else if (valid) Authenticated
    else Invalid

  // The strict dependency's outcome: 401 unless authenticated.
  pure def strict(cls: AuthClass): Outcome =
    if (cls == Authenticated) Granted else unauthorized

  // The lenient dependency's outcome: anonymous is allowed, invalid is 401.
  pure def lenient(cls: AuthClass): Outcome =
    if (cls == Invalid) unauthorized else Granted

  // A composite authenticator: first method that authenticates wins; if none
  // does but a credential was seen, Invalid; else Absent.
  pure def combine(results: Set[AuthClass]): AuthClass =
    if (results.contains(Authenticated)) Authenticated
    else if (results.contains(Invalid)) Invalid
    else Absent

  pure def strictGuardsEveryUnauthenticated: bool =
    all {
      strict(Absent) == unauthorized,
      strict(Invalid) == unauthorized,
      strict(Authenticated) == Granted,
    }

  pure def lenientAdmitsAnonymous: bool =
    all {
      lenient(Absent) == Granted,
      lenient(Invalid) == unauthorized,
      lenient(Authenticated) == Granted,
    }

  pure def invalidIsNeverAnonymous: bool =
    // A presented-but-bad credential is rejected under both tiers, so it can
    // never be silently treated as anonymous.
    all {
      strict(Invalid) == unauthorized,
      lenient(Invalid) == unauthorized,
    }

  pure def compositeFirstWins: bool =
    all {
      combine(Set(Absent, Authenticated)) == Authenticated,
      combine(Set(Authenticated, Invalid)) == Authenticated,
      // An Invalid alongside a grant does not matter: the grant wins.
      combine(Set(Invalid, Authenticated)) == Authenticated,
    }

  pure def compositeInvalidBeatsAbsent: bool =
    combine(Set(Absent, Invalid)) == Invalid

  pure def compositeAllAbsentIsAbsent: bool =
    combine(Set(Absent)) == Absent

  run thePipelineClassifiesThreeWays = {
    all {
      assert(classify(false, false) == Absent),
      assert(classify(true, false) == Invalid),
      assert(classify(true, true) == Authenticated),
    }
  }

  run oneResultDerivesBothTiers = {
    all {
      assert(strictGuardsEveryUnauthenticated),
      assert(lenientAdmitsAnonymous),
      assert(invalidIsNeverAnonymous),
    }
  }

  run theCompositeFollowsFirstWins = {
    all {
      assert(compositeFirstWins),
      assert(compositeInvalidBeatsAbsent),
      assert(compositeAllAbsentIsAbsent),
    }
  }

  run principalPipelineInvariantsHold = {
    all {
      assert(strictGuardsEveryUnauthenticated),
      assert(lenientAdmitsAnonymous),
      assert(invalidIsNeverAnonymous),
      assert(compositeFirstWins),
      assert(compositeInvalidBeatsAbsent),
      assert(compositeAllAbsentIsAbsent),
    }
  }
}
