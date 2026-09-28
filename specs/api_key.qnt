// api_key.qnt — formal spec for the stored API-key resource (issue #63),
// rewritten for the port (issue #118): the key is a SecretStr stored only
// as a digest, and the one-time reveal is the serialization context.
//
// An API key is a row. Its raw value is minted by the framework, its SHA-256
// digest is what is stored, and the raw value is disclosed exactly once — in
// the response to the create that minted it, which is the only response dumped
// with `expose_secrets`. Every other surface — read, search, batch-read,
// update, and the query surface (`sort=` / `field__op=`) — must be free of it.
//
// The mechanism is the per-field configuration the resource declares:
// the key is absent from the create and update requests, absent from the read
// model, and therefore absent from the query surface (which is derived from the
// read model). The create *response* is the one deliberate widening: the read
// model plus the key, serialized with `expose_secrets` so a redacted
// `SecretStr` is dumped as plaintext.
//
// Invariants:
//   - keyNeverAccepted: the key is absent from the create and update models,
//     so a client can neither choose one nor rotate one.
//   - keyRevealedExactlyOnce: of all the responses a client can obtain, only
//     the create response is dumped with `expose_secrets` AND carries the key
//     field — so only it carries the plaintext.
//   - keyNeverQueryable: the key is outside the query surface — otherwise
//     ?key__eq= would confirm a guess and ?sort=key would leak relative order
//     without the value ever appearing in a body.
//   - digestAtRest: the value stored is the digest of the minted key, never the
//     minted key itself.
//   - revealedKeyHashesToTheStoredDigest: the digest of the revealed value is
//     exactly the stored value; otherwise the disclosed key could never
//     authenticate.
//   - presentedKeyAuthenticatesIffDigestMatches: a presented key authenticates
//     iff its digest is the stored one — validation never compares plaintext.
//   - keyIsImmutable: an update never changes the stored digest, so the
//     one-time reveal stays valid for the row's lifetime.

module api_key {
  // The actions a client can invoke on the resource.
  type Action = Create | Read | Update | Delete | Search | BatchRead

  // Per-field configuration — the single declaration that drives every
  // generated model.
  type FieldConfig = { creatable: bool, updatable: bool, readable: bool }

  // The ApiKey resource as declared: each field with its configuration.
  pure val declaredFields: Set[str] = Set("id", "name", "key", "created_at", "updated_at")

  pure def configOf(name: str): FieldConfig =
    if (name == "key") {
      // Generated, write-once, never disclosed on a read.
      { creatable: false, updatable: false, readable: false }
    } else if (name == "name") {
      { creatable: true, updatable: true, readable: true }
    } else {
      // id / timestamps: framework-owned, readable but not client-supplied.
      { creatable: false, updatable: false, readable: true }
    }

  // Generated models: each is the subset of fields whose config allows it.
  pure val createModel: Set[str] = declaredFields.filter(f => configOf(f).creatable)
  pure val updateModel: Set[str] = declaredFields.filter(f => configOf(f).updatable)
  pure val readModel: Set[str] = declaredFields.filter(f => configOf(f).readable)

  // The query surface is derived from the read model, so a field hidden from
  // responses is neither sortable nor filterable.
  pure val queryableFields: Set[str] = readModel

  // The create response is the read model widened with the key — the single,
  // deliberate disclosure.
  pure val createdModel: Set[str] = readModel.union(Set("key"))

  // The fields a client observes in the response to each action.
  pure def responseFields(a: Action): Set[str] =
    match a {
      Create   => createdModel |
      Read     => readModel |
      Update   => readModel |
      Search   => readModel |
      BatchRead => readModel |
      Delete   => Set()
    }

  // The serialization context a response is dumped with. Only the create
  // response asks for the plaintext reveal.
  type Reveal = Redacted | Exposed

  pure def revealContext(a: Action): Reveal =
    if (a == Create) Exposed else Redacted

  // The secret as it appears on the wire for a given context.
  pure def dumped(key: str, reveal: Reveal): str =
    if (reveal == Exposed) key else "**********"

  // The raw secret a response actually carries (after the context is applied).
  // A response that does not include the key field carries nothing.
  type Carried = Present(str) | Absent

  pure def carriedSecret(a: Action, minted: str): Carried =
    if (responseFields(a).contains("key"))
      Present(dumped(minted, revealContext(a)))
    else Absent

  pure val allActions: Set[Action] = Set(Create, Read, Update, Search, BatchRead, Delete)

  // A digest model: integers stand in for SHA-256 hex. `digest` is total over
  // the finite key universe and 0 marks "not a key we minted".
  pure def digest(key: str): int =
    if (key == "rsk_alpha") 11
    else if (key == "rsk_beta") 22
    else 0

  // A stored row, reduced to what the invariants talk about: a label and the
  // digest (never the raw key).
  type Row = { digest: int, name: str }

  // Creating mints a fresh raw key and stores only its digest; the client's
  // payload cannot contribute a key (it is absent from the create model).
  pure def createRow(minted: str, requestedName: str): Row =
    { digest: digest(minted), name: requestedName }

  // Updating applies only the updatable fields, so the digest survives untouched.
  pure def updateRow(row: Row, newName: str): Row =
    { digest: row.digest, name: newName }

  // A presented key authenticates iff its digest is the stored one. The raw
  // value is never compared; an unknown key hashes to 0 and matches nothing.
  pure def authenticates(row: Row, presented: str): bool =
    presented != "" and digest(presented) == row.digest

  // Invariants

  pure def keyNeverAccepted: bool =
    and {
      not(createModel.contains("key")),
      not(updateModel.contains("key")),
    }

  // The create response is the only one that carries a secret at all, and the
  // only one dumped with `expose_secrets` — so it is the only one whose carried
  // secret is the plaintext.
  pure def keyRevealedExactlyOnce(minted: str): bool = {
    val revealing = allActions.filter(a =>
      carriedSecret(a, minted) == Present(minted) and revealContext(a) == Exposed
    )
    revealing == Set(Create)
  }

  pure def keyNeverQueryable: bool =
    not(queryableFields.contains("key"))

  pure def digestAtRest(minted: str): bool =
    and {
      createRow(minted, "ci").digest == digest(minted),
      digest(minted) != 0,
    }

  pure def revealedKeyHashesToTheStoredDigest(minted: str): bool =
    digest(dumped(minted, Exposed)) == createRow(minted, "ci").digest

  pure def presentedKeyAuthenticatesIffDigestMatches(row: Row, presented: str): bool =
    authenticates(row, presented) == (presented != "" and digest(presented) == row.digest)

  pure def keyIsImmutable(row: Row, newName: str): bool =
    and {
      updateRow(row, newName).digest == row.digest,
      updateRow(row, newName).name == newName,
    }

  // Tests

  run keyIsAbsentFromTheWriteModels = {
    all {
      assert(createModel == Set("name")),
      assert(updateModel == Set("name")),
      assert(keyNeverAccepted),
    }
  }

  run keyIsAbsentFromTheReadModel = {
    all {
      assert(readModel == Set("id", "name", "created_at", "updated_at")),
      assert(not(readModel.contains("key"))),
    }
  }

  run onlyCreateDisclosesTheKey = {
    val minted = "rsk_alpha"
    all {
      assert(responseFields(Create).contains("key")),
      assert(not(responseFields(Read).contains("key"))),
      assert(not(responseFields(Update).contains("key"))),
      assert(not(responseFields(Search).contains("key"))),
      assert(not(responseFields(BatchRead).contains("key"))),
      assert(carriedSecret(Create, minted) == Present(minted)),
      assert(carriedSecret(Read, minted) == Absent),
      assert(keyRevealedExactlyOnce(minted)),
    }
  }

  run querySurfaceExcludesTheKey = {
    // ?sort=key leaks relative order, ?key__eq= confirms a guess — both are
    // closed by deriving the query surface from the read model.
    all {
      assert(queryableFields == readModel),
      assert(keyNeverQueryable),
    }
  }

  run storedValueIsOnlyTheDigest = {
    val minted = "rsk_alpha"
    all {
      assert(digestAtRest(minted)),
      assert(createRow(minted, "ci").digest == 11),
      assert(digest(minted) != 0),
    }
  }

  run createRevealsTheStoredKey = {
    val minted = "rsk_beta"
    all {
      assert(revealedKeyHashesToTheStoredDigest(minted)),
      assert(createRow(minted, "ci").name == "ci"),
    }
  }

  run presentedKeyAuthenticatesByDigest = {
    val row = createRow("rsk_alpha", "ci")
    all {
      assert(authenticates(row, "rsk_alpha")),
      assert(not(authenticates(row, "rsk_beta"))),
      assert(not(authenticates(row, "forged"))),
      assert(not(authenticates(row, ""))),
      assert(presentedKeyAuthenticatesIffDigestMatches(row, "rsk_alpha")),
      assert(presentedKeyAuthenticatesIffDigestMatches(row, "rsk_beta")),
      assert(presentedKeyAuthenticatesIffDigestMatches(row, "forged")),
    }
  }

  run updateCannotRotateTheKey = {
    val row = { digest: 11, name: "ci" }
    all {
      assert(keyIsImmutable(row, "renamed")),
      // The reveal stays valid: the key a client holds still matches the row.
      assert(authenticates(updateRow(row, "renamed"), "rsk_alpha")),
    }
  }

  run invariantsHold = {
    val minted = "rsk_alpha"
    val row = createRow(minted, "ci")
    all {
      assert(keyNeverAccepted),
      assert(keyRevealedExactlyOnce(minted)),
      assert(keyNeverQueryable),
      assert(digestAtRest(minted)),
      assert(revealedKeyHashesToTheStoredDigest(minted)),
      assert(presentedKeyAuthenticatesIffDigestMatches(row, "rsk_alpha")),
      assert(keyIsImmutable(row, "renamed")),
    }
  }
}
