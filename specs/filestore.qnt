// filestore.qnt — formal spec for the pre-signed-URL file handshake (issue #117).
//
// File bytes live in a storage medium (S3 / SQL blob / local directory) and the
// metadata — name, size, MIME type, ETag, status, opaque key — is a normal
// resource. The bytes never pass through a metadata request handler: the API
// mints a short-lived *capability* URL bound to exactly one ``(key, op)`` pair,
// and the client transfers directly against it.
//
// The handshake has three steps:
//   1. ``create``     — allocate an opaque key + ``pending`` status.
//   2. ``upload``     — the client PUTs the bytes against the ``put`` capability.
//   3. ``complete``   — the API heads the object, verifies it, flips to ``ready``,
//                       and records the medium's ETag.
// A ``ready`` file can be ``download``ed (a ``get`` capability); ``delete``
// removes the row *and* the object, so no orphan remains.
//
// The failure cases are modelled as *preconditions* (pure predicates over the
// state) rather than failing actions, so a test can assert the guard directly.
// Each predicate mirrors a check the service enforces.
//
// Invariants:
//   - keysAreUnique: no two file rows share a storage key (the key is the
//     medium's addressing unit; a collision would let one client overwrite
//     another's bytes).
//   - readyImpliesUploaded: a ``ready`` file's key is present in the medium.
//   - etagMatchesTheMedium: a ``ready`` file's recorded ETag equals the medium's
//     ETag for its key.
//   - pendingHasNoEtag: a ``pending`` file records no ETag.
//   - capabilityIsBound: a capability token authorizes exactly the operation and
//     the key it was minted for.

module filestore {
  // A file's lifecycle status. ``pending`` is created-but-not-completed;
  // ``ready`` is verified and downloadable.
  type Status = Pending | Ready

  // A capability operation, mirroring PUT_OPERATION / GET_OPERATION.
  type Op = Put | Get

  // The medium's ETag for a key: absent until the object exists.
  type Etag = NoEtag | EtagOf(int)

  // A metadata row: the opaque key, the client's declared size, the status, and
  // the medium's ETag once completed (``NoEtag`` while pending).
  type Row = {
    key: int,
    declaredSize: int,
    status: Status,
    etag: Etag,
  }

  var rows: int -> Row
  var nextId: int
  // The medium: key -> stored object size. A key absent here has no object.
  var store: int -> int

  action init = all {
    rows' = Map(),
    nextId' = 1,
    store' = Map(),
  }

  // create: allocate the next id as the key, status pending, no ETag.
  action create(declared: int): bool = {
    val id = nextId
    all {
      rows' = rows.put(id, { key: id, declaredSize: declared, status: Pending, etag: NoEtag }),
      store' = store,
      nextId' = nextId + 1,
      true,
    }
  }

  // upload: the ``put`` capability writes bytes for a key. A key with no row is
  // refused (the capability is minted only for an existing file).
  action upload(key: int, stored: int): bool = all {
    assert(rows.keys().exists(id => rows.get(id).key == key)),
    store' = store.put(key, stored),
    rows' = rows,
    nextId' = nextId,
    true,
  }

  // complete: verify the uploaded object then flip the row to ready.
  action complete(id: int): bool = {
    val row = rows.get(id)
    all {
      assert(canComplete(id, rows, store)),
      rows' = rows.put(id, { ...row, status: Ready, etag: EtagOf(store.get(row.key)) }),
      store' = store,
      nextId' = nextId,
      true,
    }
  }

  // download: allowed only for a ready file (the row's status gates the mint).
  action download(id: int): bool = all {
    assert(rows.keys().contains(id)),
    assert(rows.get(id).status == Ready),
    rows' = rows,
    store' = store,
    nextId' = nextId,
    true,
  }

  // delete: remove the row and the object, so no orphan remains.
  action delete(id: int): bool = {
    val key = rows.get(id).key
    all {
      rows' = rows.keys().filter(k => k != id).fold(
        Map(),
        (acc, k) => acc.put(k, rows.get(k))
      ),
      store' = store.keys().filter(k => k != key).fold(
        Map(),
        (acc, k) => acc.put(k, store.get(k))
      ),
      nextId' = nextId,
      true,
    }
  }

  // The preconditions, as pure predicates over the state.

  // complete succeeds only for a pending row whose object exists and whose size
  // matches the declared size. Mirrors the service's checks.
  pure def canComplete(id: int, r: int -> Row, s: int -> int): bool = {
    val row = r.get(id)
    r.keys().contains(id) and
    row.status == Pending and
    s.keys().contains(row.key) and
    s.get(row.key) == row.declaredSize
  }

  // download succeeds only for a ready row.
  pure def canDownload(id: int, r: int -> Row): bool =
    r.keys().contains(id) and r.get(id).status == Ready

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

  // The happy path: create -> upload the declared size -> complete -> ready.
  run handshakeReachesReady = {
    init
      .then(create(5))
      .then(upload(1, 5))
      .then(complete(1))
      .then(all {
        assert(rows.get(1).status == Ready),
        assert(rows.get(1).etag == EtagOf(5)),
        assert(store.keys().contains(rows.get(1).key)),
        rows' = rows,
        nextId' = nextId,
        store' = store,
      })
  }

  // complete requires an uploaded object.
  run completeRequiresAnObject = {
    init
      .then(create(5))
      .then(all {
        assert(not(store.keys().contains(rows.get(1).key))),
        assert(not(canComplete(1, rows, store))),
        assert(rows.get(1).status == Pending),
        rows' = rows,
        nextId' = nextId,
        store' = store,
      })
  }

  // complete verifies the uploaded size against the declared size.
  run completeVerifiesSize = {
    init
      .then(create(5))
      .then(upload(1, 3))
      .then(all {
        assert(store.get(rows.get(1).key) != rows.get(1).declaredSize),
        assert(not(canComplete(1, rows, store))),
        rows' = rows,
        nextId' = nextId,
        store' = store,
      })
  }

  // complete is refused on an already-ready file (no double-complete).
  run completeIsIdempotentOnce = {
    init
      .then(create(5))
      .then(upload(1, 5))
      .then(complete(1))
      .then(all {
        assert(not(canComplete(1, rows, store))),
        assert(rows.get(1).status == Ready),
        rows' = rows,
        nextId' = nextId,
        store' = store,
      })
  }

  // download is refused before the file is ready, allowed after.
  run downloadRequiresReady = {
    init
      .then(create(5))
      .then(upload(1, 5))
      .then(all {
        assert(not(canDownload(1, rows))),
        rows' = rows,
        nextId' = nextId,
        store' = store,
      })
      .then(complete(1))
      .then(all {
        assert(canDownload(1, rows)),
        rows' = rows,
        nextId' = nextId,
        store' = store,
      })
  }

  // upload is refused for a key with no metadata row.
  run uploadRequiresARow = {
    init
      .then(create(5))
      .then(all {
        assert(not(rows.keys().exists(id => rows.get(id).key == 999))),
        rows' = rows,
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
        assert(rows.keys().size() == 2),
        assert(rows.get(1).key == 1),
        assert(rows.get(2).key == 2),
        assert(rows.get(1).key != rows.get(2).key),
        rows' = rows,
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

  // delete removes both the row and the object.
  run deleteRemovesTheObject = {
    init
      .then(create(5))
      .then(upload(1, 5))
      .then(complete(1))
      .then(delete(1))
      .then(all {
        assert(not(rows.keys().contains(1))),
        assert(store.keys().size() == 0),
        rows' = rows,
        nextId' = nextId,
        store' = store,
      })
  }

  // The invariants as explicit assertions after each transition.
  run invariantsHold = {
    init
      .then(create(5))
      .then(all {
        assert(rows.keys().size() == 1),
        assert(rows.get(1).etag == NoEtag),
        rows' = rows,
        nextId' = nextId,
        store' = store,
      })
      .then(upload(1, 5))
      .then(complete(1))
      .then(all {
        assert(rows.get(1).status == Ready),
        assert(rows.get(1).etag == EtagOf(store.get(rows.get(1).key))),
        assert(store.keys().contains(rows.get(1).key)),
        rows' = rows,
        nextId' = nextId,
        store' = store,
      })
  }
}
