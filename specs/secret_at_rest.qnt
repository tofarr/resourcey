// secret_at_rest.qnt — formal spec for encrypting SecretStr fields at the
// storage boundary (issue #202).
//
// A storage backend (SQL, Mongo) encrypts every encrypted field on write and
// decrypts it on read. The store holds only what the backend wrote, so a raw
// read of the store never shows the plaintext of an encrypted field.
//
// Laws:
//   - storedIsNeverPlaintext: an encrypted field's stored value is not the
//     plaintext it was written from.
//   - readInvertsWrite: reading a written secret returns the original plaintext.
//   - nonEncryptedFieldsAreVerbatim: a field outside the encrypted set is
//     stored and read unchanged (no accidental encryption), which is also how an
//     opted-out digest field stays equality-searchable.
//   - nullStaysNull: an absent secret is stored as absent.
//   - legacyPlaintextReadsThrough: a stored value that is not ciphertext (a row
//     written before encryption was enabled) reads back unchanged.
//   - secretsAreNeverQueryable: the query surface is the read model minus its
//     secret fields, so no filter or sort can name a secret.

module secret_at_rest {
  pure val plaintext: str = "hunter2"
  pure val other: str = "other"
  pure val legacy: str = "legacy-plaintext"

  // The reversible stand-in for the encryption service; a ciphertext is
  // recognisable by shape, and every ciphertext differs from its plaintext.
  pure def enc(v: str): str =
    if (v == plaintext) "cipher:hunter2"
    else if (v == other) "cipher:other"
    else "cipher:unknown"

  pure def isCiphertext(v: str): bool =
    Set("cipher:hunter2", "cipher:other", "cipher:unknown").contains(v)

  pure def dec(v: str): str =
    if (v == "cipher:hunter2") plaintext
    else if (v == "cipher:other") other
    else "unknown"

  // A record is a field name -> optional value. `None` is an absent secret.
  type Value = Some(str) | None

  // The stored form of one field: encrypted when its name is in `encrypted`.
  pure def toStorage(name: str, v: Value, encrypted: Set[str]): Value =
    match v {
      None => None |
      Some(s) => if (encrypted.contains(name)) Some(enc(s)) else Some(s)
    }

  // The read form: ciphertext in an encrypted field decrypts; anything else
  // (a legacy plaintext row, a verbatim field) passes through.
  pure def fromStorage(name: str, v: Value, encrypted: Set[str]): Value =
    match v {
      None => None |
      Some(s) =>
        if (encrypted.contains(name) and isCiphertext(s)) Some(dec(s)) else Some(s)
    }

  // The query surface: read-model fields minus the secret ones.
  pure def queryable(readModel: Set[str], secrets: Set[str]): Set[str] =
    readModel.exclude(secrets)

  pure val encryptedSet: Set[str] = Set("api_secret")
  pure val values: Set[str] = Set(plaintext, other)

  pure def storedIsNeverPlaintext: bool =
    values.forall(v => toStorage("api_secret", Some(v), encryptedSet) != Some(v))

  pure def readInvertsWrite: bool =
    values.forall(v =>
      fromStorage("api_secret", toStorage("api_secret", Some(v), encryptedSet), encryptedSet) == Some(v)
    )

  pure def nonEncryptedFieldsAreVerbatim: bool =
    and {
      values.forall(v => toStorage("label", Some(v), encryptedSet) == Some(v)),
      values.forall(v => fromStorage("label", Some(v), encryptedSet) == Some(v)),
      // An opted-out secret (empty encrypted set) is stored verbatim too.
      values.forall(v => toStorage("api_secret", Some(v), Set()) == Some(v)),
    }

  pure def nullStaysNull: bool =
    and {
      toStorage("api_secret", None, encryptedSet) == None,
      fromStorage("api_secret", None, encryptedSet) == None,
    }

  pure def legacyPlaintextReadsThrough: bool =
    fromStorage("api_secret", Some(legacy), encryptedSet) == Some(legacy)

  pure def secretsAreNeverQueryable: bool =
    and {
      queryable(Set("id", "label", "api_secret"), Set("api_secret")) == Set("id", "label"),
      not(queryable(Set("id", "api_secret"), Set("api_secret")).contains("api_secret")),
    }

  run storedValueIsNeverThePlaintext = all { assert(storedIsNeverPlaintext) }
  run readingReturnsTheWrittenPlaintext = all { assert(readInvertsWrite) }
  run plainFieldsAreUnaffected = all { assert(nonEncryptedFieldsAreVerbatim) }
  run absentSecretStaysAbsent = all { assert(nullStaysNull) }
  run legacyRowStillReads = all { assert(legacyPlaintextReadsThrough) }
  run querySurfaceExcludesSecrets = all { assert(secretsAreNeverQueryable) }

  run invariantsHold = all {
    assert(storedIsNeverPlaintext),
    assert(readInvertsWrite),
    assert(nonEncryptedFieldsAreVerbatim),
    assert(nullStaysNull),
    assert(legacyPlaintextReadsThrough),
    assert(secretsAreNeverQueryable),
  }
}
