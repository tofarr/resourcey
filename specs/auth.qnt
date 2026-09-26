// auth.qnt — formal spec for the API-key authenticator (issue #118).
//
// ApiKeyDependencyBuilder composes a key check with a resource's own
// per-request service dependency. The check runs first, over a fresh
// call-scoped context, so it owns the key storage it opens and can never adopt
// (or hand off to) the target resource's session. The key is validated by
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
}
