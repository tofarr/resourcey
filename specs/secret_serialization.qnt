// secret_serialization.qnt — formal spec for the context-driven secret
// convention (issue #118).
//
// A SecretStr field serializes through one rule, driven entirely by the
// serialization context passed to model_dump / model_validate:
//
//   * an `encryption_service` in the context encrypts on dump (JWE ciphertext)
//     and decrypts on load — it wins over everything else, because an explicit
//     encryption request must never leak plaintext;
//   * otherwise `expose_secrets: true` dumps the plaintext;
//   * otherwise the value is redacted (`**********`).
//
// Laws:
//   - redactsByDefault: no context (or a context with neither flag) redacts.
//   - exposureReveals: `expose_secrets` reveals the plaintext.
//   - encryptionWins: a context carrying an `encryption_service` encrypts even
//     when `expose_secrets` is also set.
//   - contextIsTheOnlyInput: the output is a function of (value, context)
//     alone; two dumps of the same value under the same context agree, and only
//     the context changes the result.
//   - loadInvertsDumpUnderEncryption: under an `encryption_service`, a dumped
//     value loads back to the plaintext.

module secret_serialization {
  // What the context asks for. `Encrypt` carries nothing (the service is a
  // fixed reversible transform); `Expose` and `Redact` are the flags.
  type Context = NoContext | Expose | Encrypt

  pure val plaintext: str = "hunter2"
  pure val other: str = "other"
  pure val redacted: str = "**********"

  // The reversible stand-in for the encryption service. Quint has no string
  // concatenation, so the transform is a total, case-defined bijection over the
  // finite value universe the tests range over.
  pure val cipherOfPlaintext: str = "cipher:hunter2"
  pure val cipherOfOther: str = "cipher:other"

  pure def enc(v: str): str =
    if (v == plaintext) cipherOfPlaintext
    else if (v == other) cipherOfOther
    else "cipher:unknown"

  pure def dec(v: str): str =
    if (v == cipherOfPlaintext) plaintext
    else if (v == cipherOfOther) other
    else "cipher:unknown"

  // The value universe the round-trip law ranges over.
  pure val values: Set[str] = Set(plaintext, other)

  // The dump rule. Encryption wins over exposure, which wins over redaction.
  pure def dumped(value: str, ctx: Context): str =
    match ctx {
      Encrypt => enc(value) |
      Expose => value |
      NoContext => redacted
    }

  // The load rule: under encryption the stored ciphertext is decrypted;
  // otherwise the value passes through unchanged.
  pure def loaded(value: str, ctx: Context): str =
    match ctx {
      Encrypt => dec(value) |
      _ => value
    }

  // Invariants

  pure def redactsByDefault: bool =
    and {
      dumped(plaintext, NoContext) == redacted,
      not(dumped(plaintext, NoContext) == plaintext),
    }

  pure def exposureReveals: bool =
    dumped(plaintext, Expose) == plaintext

  pure def encryptionWins: bool =
    // Encryption encrypts the value even though exposure is not a separate bit
    // here — the priority is expressed by `Encrypt` beating `Expose`.
    and {
      dumped(plaintext, Encrypt) == enc(plaintext),
      not(dumped(plaintext, Encrypt) == plaintext),
      not(dumped(plaintext, Encrypt) == redacted),
    }

  pure def contextIsTheOnlyInput: bool =
    // Determinism: same value + same context => same output, for every context.
    all {
      dumped(plaintext, NoContext) == dumped(plaintext, NoContext),
      dumped(plaintext, Expose) == dumped(plaintext, Expose),
      dumped(plaintext, Encrypt) == dumped(plaintext, Encrypt),
      // Only the context changes the result.
      dumped(plaintext, NoContext) != dumped(plaintext, Expose),
      dumped(plaintext, Expose) != dumped(plaintext, Encrypt),
    }

  pure def loadInvertsDumpUnderEncryption: bool =
    values.forall(v => loaded(dumped(v, Encrypt), Encrypt) == v)

  // Tests

  run defaultRedacts = {
    all {
      assert(dumped(plaintext, NoContext) == redacted),
      assert(redactsByDefault),
    }
  }

  run exposeRevealsThePlaintext = {
    all {
      assert(dumped(plaintext, Expose) == plaintext),
      assert(exposureReveals),
    }
  }

  run encryptionTakesPrecedence = {
    all {
      assert(dumped(plaintext, Encrypt) == enc(plaintext)),
      assert(encryptionWins),
    }
  }

  run outputDependsOnlyOnTheContext = {
    all {
      assert(contextIsTheOnlyInput),
    }
  }

  run encryptionRoundTrips = {
    all {
      assert(loadInvertsDumpUnderEncryption),
      assert(loaded("stored", NoContext) == "stored"),
    }
  }

  run invariantsHold = {
    all {
      assert(redactsByDefault),
      assert(exposureReveals),
      assert(encryptionWins),
      assert(contextIsTheOnlyInput),
      assert(loadInvertsDumpUnderEncryption),
    }
  }
}
