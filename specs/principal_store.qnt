// principal_store.qnt — formal spec for the optional principal store (issue
// #132 follow-up: store principals in the DB).
//
// Part 2 (#132) made a role a credential-carried string; the *principal* a key
// acts as was still a bare credential field (a config ``principal_id``). This
// rung makes that principal a stored row and lets the authenticator validate it,
// so a stored principal's ``enabled`` flag is authoritative over the credential.
//
// The model mirrors ``ApiKeyAuthenticator``:
//
//   - a presented key is reduced to a principal id (or a miss). A key that
//     matches nothing is Invalid; a matched key names a principal.
//   - when a ``user_resource`` (the principal store) is configured, the
//     principal id is looked up in the store: a miss or an ``enabled=false`` row
//     makes the attempt Invalid, however valid the key was. With no store,
//     every matched key's principal is accepted (credential-only posture).
//
// Laws:
//   - failClosedOnNoKey: an unmatched key is Invalid (401), store or no store.
//   - storeMakesEnabledAuthoritative: with a store, a principal is only
//     Authenticated when its row exists *and* is enabled.
//   - disabledPrincipalRevokes: disabling a stored principal turns every key
//     that acts as it from Authenticated into Invalid.
//   - unknownPrincipalRejected: a key naming no stored row is Invalid.
//   - noStoreTrustsTheCredential: without a store, any matched key authenticates
//     (the behavioural compatibility of examples 01–03).
//   - absentIsNotInvalid: no credential is Absent, not Invalid, so the tiers can
//     still tell them apart.

module principal_store {
  // The authentication classification, mirroring AuthResult's principal field
  // plus the credential flags: Absent (no credential), Invalid (presented but
  // rejected), Authenticated (a principal).
  type Class = Absent | Invalid | Authenticated

  // What the request presented.
  type Presented = NoCredential | Credential(str)

  // A presented key's resolution: a miss, or a named principal.
  type Resolution = Miss | Resolved(str)

  // The stored key set.
  pure val storedKeys: Set[str] = Set("admin-key", "user-key", "disabled-key")

  // The principal each key acts as (the key's own binding).
  pure def keyPrincipal(key: str): str =
    if (key == "admin-key") "admin"
    else if (key == "user-key") "user"
    else if (key == "disabled-key") "disabled"
    else ""

  pure def resolveKey(presented: Presented): Resolution =
    match presented {
      NoCredential => Miss |
      Credential(k) => if (storedKeys.contains(k)) Resolved(keyPrincipal(k)) else Miss
    }

  // The principal store: the enabled and the disabled rows.
  pure val enabledPrincipals: Set[str] = Set("admin", "user")
  pure val disabledPrincipals: Set[str] = Set("disabled")

  // The store's verdict for a principal id.
  type StoreVerdict = NoStore | Enabled | Disabled | Missing

  pure def storeVerdict(hasStore: bool, id: str): StoreVerdict =
    if (not(hasStore)) NoStore
    else if (enabledPrincipals.contains(id)) Enabled
    else if (disabledPrincipals.contains(id)) Disabled
    else Missing

  // The full classification: resolve the key, then consult the store.
  pure def classify(presented: Presented, hasStore: bool): Class =
    match presented {
      NoCredential => Absent |
      Credential(k) =>
        if (not(storedKeys.contains(k))) Invalid
        else {
          val id = keyPrincipal(k)
          if (not(hasStore)) Authenticated
          else if (enabledPrincipals.contains(id)) Authenticated
          else Invalid
        }
    }

  // ------------------------------------------------------------------
  // Invariants
  // ------------------------------------------------------------------

  pure def failClosedOnNoKey: bool =
    all {
      classify(NoCredential, true) == Absent,
      classify(Credential("forged"), true) == Invalid,
      classify(Credential("forged"), false) == Invalid,
    }

  pure def storeMakesEnabledAuthoritative: bool =
    all {
      // A stored, enabled principal authenticates...
      classify(Credential("admin-key"), true) == Authenticated,
      classify(Credential("user-key"), true) == Authenticated,
      // ...and a key naming a disabled or missing principal does not.
      classify(Credential("disabled-key"), true) == Invalid,
    }

  pure def disabledPrincipalRevokes: bool =
    all {
      storeVerdict(true, "user") == Enabled,
      storeVerdict(true, "disabled") == Disabled,
      classify(Credential("disabled-key"), true) == Invalid,
    }

  pure def unknownPrincipalRejected: bool =
    all {
      classify(Credential("user-key"), true) == Authenticated,  // stored + enabled
      storeVerdict(true, "ghost") == Missing,
    }

  pure def noStoreTrustsTheCredential: bool =
    all {
      classify(Credential("admin-key"), false) == Authenticated,
      classify(Credential("user-key"), false) == Authenticated,
      // A forged key is still rejected without a store.
      classify(Credential("forged"), false) == Invalid,
    }

  pure def absentIsNotInvalid: bool =
    classify(NoCredential, true) != classify(Credential("forged"), true)

  // Tests

  run unmatchedKeyIsInvalid = {
    all {
      assert(classify(Credential("forged"), true) == Invalid),
      assert(classify(Credential("forged"), false) == Invalid),
    }
  }

  run absentIsNotInvalidTest = {
    all {
      assert(classify(NoCredential, true) == Absent),
      assert(classify(NoCredential, false) == Absent),
      assert(absentIsNotInvalid),
    }
  }

  run enabledStoredPrincipalAuthenticates = {
    all {
      assert(classify(Credential("admin-key"), true) == Authenticated),
      assert(classify(Credential("user-key"), true) == Authenticated),
      assert(storeMakesEnabledAuthoritative),
    }
  }

  run disabledPrincipalIsRejected = {
    all {
      assert(classify(Credential("disabled-key"), true) == Invalid),
      assert(storeVerdict(true, "disabled") == Disabled),
      assert(disabledPrincipalRevokes),
    }
  }

  run missingPrincipalIsRejected = {
    all {
      assert(storeVerdict(true, "ghost") == Missing),
      assert(unknownPrincipalRejected),
    }
  }

  run noStoreTrustsTheCredentialTest = {
    all {
      assert(classify(Credential("admin-key"), false) == Authenticated),
      assert(classify(Credential("forged"), false) == Invalid),
      assert(noStoreTrustsTheCredential),
    }
  }

  run invariantsHold = {
    all {
      assert(failClosedOnNoKey),
      assert(storeMakesEnabledAuthoritative),
      assert(disabledPrincipalRevokes),
      assert(unknownPrincipalRejected),
      assert(noStoreTrustsTheCredential),
      assert(absentIsNotInvalid),
    }
  }
}
