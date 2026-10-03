// filestore.qnt — formal spec for the direct-upload file store (issues #117, #158).
//
// File bytes live in a storage medium (S3 / SQL blob / local directory).
// There is **no metadata table and no tracked status**: "does the medium have
// the bytes" is the only source of truth for a file's existence. ``create``
// *is* the upload (case 2 of the upload-design discussion, not a pre-signed-
// URL handshake the client completes separately): the bytes commit to the
// medium in the same action that allocates the key, so a file exists the
// instant ``create`` returns — there is no intermediate "allocated but not
// yet uploaded" state to model, unlike the earlier capability-mint design.
// ``delete`` removes the object, so no orphan remains.
//
// Download keeps its own short-lived capability (unaffected by the upload
// redesign — see ``capabilityAllows``): a token mints access to exactly one
// key, nothing else.
//
// Invariants:
//   - keysAreUnique: no two allocations share a storage key (the key is the
//     medium's addressing unit; a collision would let one client overwrite
//     another's bytes).
//   - fileExistsIffCreated: a key is readable (``read`` / ``search`` finds it)
//     exactly when ``create`` has run for it and ``delete`` has not — there is
//     no intermediate state between the two.
//   - capabilityIsBound: a download capability token authorizes exactly the
//     key it was minted for.

module filestore {
  var nextId: int
  // The medium: key -> stored object size. A key absent here has no object —
  // and therefore does not exist as a file.
  var store: int -> int

  action init = all {
    nextId' = 1,
    store' = Map(),
  }

  // create: allocate the next id as the key and write ``bytesWritten`` bytes
  // to the medium in the same action — the file exists the instant this
  // returns.
  action create(bytesWritten: int): bool = {
    val id = nextId
    all {
      store' = store.put(id, bytesWritten),
      nextId' = nextId + 1,
      true,
    }
  }

  // delete: remove the object, so no orphan remains. A key with no object is
  // a no-op (mirrors the medium's own delete semantics).
  action delete(key: int): bool = all {
    store' = store.keys().filter(k => k != key).fold(
      Map(),
      (acc, k) => acc.put(k, store.get(k))
    ),
    nextId' = nextId,
    true,
  }

  // The preconditions, as pure predicates over the state.

  // read / download / content all gate on the same existence check: the
  // medium holds bytes for the key.
  pure def fileExists(key: int, s: int -> int): bool =
    s.keys().contains(key)

  // A download capability token authorizes exactly the key it was minted for
  // — there is no upload counterpart to confuse it with (upload has no
  // capability of its own; ``create`` is a direct write).
  pure def capabilityAllows(mintedKey: int, presentedKey: int): bool =
    mintedKey == presentedKey

  // Tests

  // create's file exists immediately: no separate upload step, so no window
  // where the key is allocated but unreadable.
  run createIsImmediatelyVisible = {
    init
      .then(create(5))
      .then(all {
        assert(fileExists(1, store)),
        assert(store.get(1) == 5),
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
        assert(store.keys().size() == 2),
        assert(store.keys().contains(1)),
        assert(store.keys().contains(2)),
        nextId' = nextId,
        store' = store,
      })
  }

  // A download capability is bound to its key: a token minted for key A
  // cannot address key B.
  run capabilityIsBound = {
    all {
      assert(capabilityAllows(7, 7)),
      assert(not(capabilityAllows(7, 8))),
    }
  }

  // delete removes the object; the file no longer exists.
  run deleteRemovesTheObject = {
    init
      .then(create(5))
      .then(delete(1))
      .then(all {
        assert(not(fileExists(1, store))),
        nextId' = nextId,
        store' = store,
      })
  }

  // delete of a key with no object is a no-op.
  run deleteOfAbsentIsNoop = {
    init
      .then(delete(1))
      .then(all {
        assert(store.keys().size() == 0),
        nextId' = nextId,
        store' = store,
      })
  }

  // The invariants as explicit assertions after each transition.
  run invariantsHold = {
    init
      .then(create(5))
      .then(all {
        assert(store.keys().size() == 1),
        assert(fileExists(1, store)),
        assert(store.get(1) == 5),
        nextId' = nextId,
        store' = store,
      })
      .then(delete(1))
      .then(all {
        assert(not(fileExists(1, store))),
        nextId' = nextId,
        store' = store,
      })
  }
}
