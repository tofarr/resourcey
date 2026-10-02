// filestore.qnt — formal spec for the pre-signed-URL file store (issues #117, #158).
//
// File bytes live in a storage medium (S3 / SQL blob / local directory). There
// is **no metadata table and no tracked status**: "does the medium have the
// bytes" is the only source of truth for a file's existence. ``create`` only
// *allocates* an opaque key and mints an upload capability — nothing is
// persisted; the file comes into existence the moment the bytes land, with no
// separate completion call. A capability carries the client's declared size
// (and, in the real service, a checksum) as a signed claim, verified against
// the uploaded bytes before the write commits (``verify_upload``) — a mismatch
// is simply rejected, with no ``failed`` status to track: the client may retry
// against the same (still-valid) capability. ``delete`` removes the object, so
// no orphan remains.
//
// The failure cases are modelled as *preconditions* (pure predicates over the
// state) rather than failing actions, so a test can assert the guard directly.
// Each predicate mirrors a check the service enforces.
//
// Invariants:
//   - keysAreUnique: no two allocations share a storage key (the key is the
//     medium's addressing unit; a collision would let one client overwrite
//     another's bytes).
//   - fileExistsIffUploaded: a key is readable (``read`` / ``search`` finds it)
//     exactly when the medium holds bytes for it — there is no intermediate
//     "pending" state distinct from upload itself.
//   - uploadRejectsAMismatch: an upload whose byte count does not match its
//     capability's declared size leaves the medium unchanged (the state after
//     a rejected upload is identical to the state before it).
//   - capabilityIsBound: a capability token authorizes exactly the operation
//     and the key it was minted for.

module filestore {
  // A capability operation, mirroring PUT_OPERATION / GET_OPERATION.
  type Op = Put | Get

  // An allocated key's declared size claim (the capability's signed claim),
  // recorded at ``create`` time so ``upload`` can verify against it.
  var declaredSize: int -> int
  var nextId: int
  // The medium: key -> stored object size. A key absent here has no object —
  // and therefore does not exist as a file.
  var store: int -> int

  action init = all {
    declaredSize' = Map(),
    nextId' = 1,
    store' = Map(),
  }

  // create: allocate the next id as the key and record its declared-size
  // claim. Nothing is written to the medium yet.
  action create(declared: int): bool = {
    val id = nextId
    all {
      declaredSize' = declaredSize.put(id, declared),
      store' = store,
      nextId' = nextId + 1,
      true,
    }
  }

  // upload: the ``put`` capability writes bytes for an allocated key, but only
  // when the byte count matches the declared-size claim (``verify_upload``) —
  // a mismatch leaves the medium unchanged (the client may retry).
  action upload(key: int, stored: int): bool = all {
    assert(declaredSize.keys().contains(key)),
    store' = if (stored == declaredSize.get(key)) store.put(key, stored) else store,
    declaredSize' = declaredSize,
    nextId' = nextId,
    true,
  }

  // delete: remove the object, so no orphan remains. A key with no object is
  // a no-op (mirrors the medium's own delete semantics).
  action delete(key: int): bool = all {
    store' = store.keys().filter(k => k != key).fold(
      Map(),
      (acc, k) => acc.put(k, store.get(k))
    ),
    declaredSize' = declaredSize,
    nextId' = nextId,
    true,
  }

  // The preconditions, as pure predicates over the state.

  // read / download / content all gate on the same existence check: the
  // medium holds bytes for the key. No separate "ready" flag exists.
  pure def fileExists(key: int, s: int -> int): bool =
    s.keys().contains(key)

  // An upload with the given byte count would be accepted (committed) rather
  // than silently rejected.
  pure def uploadWouldSucceed(key: int, stored: int, claims: int -> int): bool =
    claims.keys().contains(key) and claims.get(key) == stored

  // A capability token authorizes exactly one (key, op) pair. Verifying a token
  // against a presented (key, op) succeeds only on an exact match.
  pure def capabilityAllows(
    mintedKey: int,
    mintedOp: Op,
    presentedKey: int,
    presentedOp: Op,
  ): bool =
    mintedKey == presentedKey and mintedOp == presentedOp

  // Tests

  // The happy path: create -> the file does not exist yet -> upload the
  // declared size -> the file exists.
  run happyPath = {
    init
      .then(create(5))
      .then(all {
        assert(not(fileExists(1, store))),
        declaredSize' = declaredSize,
        nextId' = nextId,
        store' = store,
      })
      .then(upload(1, 5))
      .then(all {
        assert(fileExists(1, store)),
        assert(store.get(1) == 5),
        declaredSize' = declaredSize,
        nextId' = nextId,
        store' = store,
      })
  }

  // A size mismatch is rejected: the medium is left exactly as it was, so a
  // retry against the same capability can still succeed.
  run uploadRejectsAMismatch = {
    init
      .then(create(5))
      .then(upload(1, 3))
      .then(all {
        assert(not(uploadWouldSucceed(1, 3, declaredSize))),
        assert(not(fileExists(1, store))),
        declaredSize' = declaredSize,
        nextId' = nextId,
        store' = store,
      })
      // The client retries with the correct size against the same capability.
      .then(upload(1, 5))
      .then(all {
        assert(fileExists(1, store)),
        declaredSize' = declaredSize,
        nextId' = nextId,
        store' = store,
      })
  }

  // An already-uploaded key can be uploaded again (the claim still matches) —
  // there is no "no double-upload" guard, since there is no status to track.
  run uploadIsNotOneShot = {
    init
      .then(create(5))
      .then(upload(1, 5))
      .then(upload(1, 5))
      .then(all {
        assert(fileExists(1, store)),
        declaredSize' = declaredSize,
        nextId' = nextId,
        store' = store,
      })
  }

  // Two creates allocate distinct keys.
  run keysAreUnique = {
    init
      .then(create(5))
      .then(create(5))
      .then(all {
        assert(declaredSize.keys().size() == 2),
        assert(declaredSize.keys().contains(1)),
        assert(declaredSize.keys().contains(2)),
        declaredSize' = declaredSize,
        nextId' = nextId,
        store' = store,
      })
  }

  // A capability is bound to both its operation and its key.
  run capabilityIsBound = {
    all {
      assert(capabilityAllows(7, Put, 7, Put)),
      // Wrong operation: a put token cannot get.
      assert(not(capabilityAllows(7, Put, 7, Get))),
      // Wrong key: a token for key A cannot address key B.
      assert(not(capabilityAllows(7, Put, 8, Put))),
    }
  }

  // delete removes the object; the file no longer exists.
  run deleteRemovesTheObject = {
    init
      .then(create(5))
      .then(upload(1, 5))
      .then(delete(1))
      .then(all {
        assert(not(fileExists(1, store))),
        declaredSize' = declaredSize,
        nextId' = nextId,
        store' = store,
      })
  }

  // The invariants as explicit assertions after each transition.
  run invariantsHold = {
    init
      .then(create(5))
      .then(all {
        assert(declaredSize.keys().size() == 1),
        assert(not(fileExists(1, store))),
        declaredSize' = declaredSize,
        nextId' = nextId,
        store' = store,
      })
      .then(upload(1, 5))
      .then(all {
        assert(fileExists(1, store)),
        assert(store.get(1) == 5),
        declaredSize' = declaredSize,
        nextId' = nextId,
        store' = store,
      })
  }
}
