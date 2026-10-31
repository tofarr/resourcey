// storage_sharing.qnt — formal spec for call-scoped storage sharing (issue #178).
//
// A resource's service opens its storage (a SQL session, a Mongo collection, a
// list, a file medium) and puts it in the call-scoped ctx so a later service in
// the same call can adopt it. The ctx is keyed by a `StorageKey`, and the key
// names the *storage* — backend plus connection identity — not the request.
//
// The rule:
//   * two services that name the same key share one storage: the first entrant
//     owns it, the rest adopt it, and the owner commits/closes/clears on exit;
//   * two services that name different keys never see each other's storage,
//     whatever their entry order — including two backends whose storages have
//     incompatible types.
//
// A single process-wide key (the pre-#178 design) collapses every key to one,
// so the second service adopts the first's storage: a cross-tenant read. The
// `singleKeyIsolationFails` test pins that the isolation law has teeth by
// exhibiting that model and showing the law is false there.
//
// Invariants:
//   - eachKeyKeepsItsOwnStorage: every key present in a ctx holds exactly the
//     storage that key names, so a service can only ever read its own rows.
//   - aKeyIsEnteredOnce: entering an already-present key never replaces the
//     storage the first entrant put there.
//   - closedKeysAreCleared: a key whose owner exited is absent from the ctx.

module storage_sharing {
  // A storage key: the backend, plus a connection token (a session maker, a
  // collection, a list, a medium). The token stands in for an object identity.
  type Key = { backend: str, token: int }

  // The rows one storage holds. A key's rows are derived from the key, so two
  // distinct keys can never hold the same rows by construction.
  type Storage = Set[int]

  pure def keyOf(backend: str, token: int): Key = { backend: backend, token: token }

  // A distinct row for every (backend, token) pair, so two keys can only
  // share rows when they are the same key.
  pure def backendRank(backend: str): int =
    if (backend == "sql") 1
    else if (backend == "mongo") 2
    else if (backend == "list") 3
    else if (backend == "file") 4
    else 5

  pure def storageFor(key: Key): Storage = Set(backendRank(key.backend) * 1000 + key.token)

  // The pre-#178 design: one process-wide key for every backend and connection.
  pure val singleKey: Key = keyOf("*", 0)

  pure val empty: Key -> Storage = Map()

  // Whether two keys name the same storage.
  pure def sameStorage(a: Key, b: Key): bool = a == b

  // Whether a ctx is isolated: every present key holds its own storage.
  pure def isolated(ctx: Key -> Storage): bool =
    ctx.keys().forall(k => ctx.get(k) == storageFor(k))

  // Drop a key from a map without needing a map-removal primitive.
  pure def without(ctx: Key -> Storage, key: Key): Key -> Storage =
    ctx.keys().exclude(Set(key)).mapBy(k => ctx.get(k))

  // The ctx produced by entering `key`: a key already present is left alone, so
  // the first entrant's storage is never replaced.
  pure def enter(ctx: Key -> Storage, key: Key): Key -> Storage =
    if (key.in(ctx.keys())) ctx else ctx.put(key, storageFor(key))

  // Whether entering `key` over `ctx` makes the entrant the owner.
  pure def owns(ctx: Key -> Storage, key: Key): bool = not(key.in(ctx.keys()))

  // The ctx produced by an entrant releasing `key`: an owner clears its
  // storage, an adopter leaves it for the owner.
  pure def release(ctx: Key -> Storage, key: Key, owned: bool): Key -> Storage =
    if (owned) without(ctx, key) else ctx

  // --- The bug, for contrast ------------------------------------------------
  // Every entrant uses `singleKey`, whatever backend / connection it addresses.
  pure def buggyEnter(ctx: Key -> Storage, key: Key): Key -> Storage =
    if (singleKey.in(ctx.keys())) ctx else ctx.put(singleKey, storageFor(key))

  // Tests

  run sameStorageSharesOneStorage = {
    // A key entered twice: the second entrant adopts, so there is one owner and
    // one storage, and both read the same rows.
    val k = keyOf("sql", 7)
    val ctx1 = enter(empty, k)
    val ctx2 = enter(ctx1, k)
    all {
      assert(owns(empty, k)),
      assert(not(owns(ctx1, k))),
      assert(ctx2 == ctx1),
      assert(ctx2.keys() == Set(k)),
      assert(ctx2.get(k) == storageFor(k)),
    }
  }

  run differentStoragesAreIsolated = {
    // Two connections, one call: each key holds only its own rows.
    val a = keyOf("sql", 1)
    val b = keyOf("sql", 2)
    val ctx1 = enter(empty, a)
    val ctx2 = enter(ctx1, b)
    all {
      assert(not(sameStorage(a, b))),
      assert(owns(ctx1, b)),                // B created its own storage
      assert(ctx2.get(a) == storageFor(a)),
      assert(ctx2.get(b) == storageFor(b)),
      assert(ctx2.get(b) != ctx2.get(a)),   // no cross-connection read
      assert(isolated(ctx2)),
    }
  }

  run crossBackendIsIsolated = {
    // The same token under two backends is two storages, in either order.
    val sql = keyOf("sql", 3)
    val list = keyOf("list", 3)
    val ctxSqlFirst = enter(enter(empty, sql), list)
    val ctxListFirst = enter(enter(empty, list), sql)
    all {
      assert(not(sameStorage(sql, list))),
      assert(isolated(ctxSqlFirst)),
      assert(isolated(ctxListFirst)),
      assert(ctxSqlFirst.keys() == Set(sql, list)),
      assert(ctxSqlFirst.get(sql) != ctxSqlFirst.get(list)),
    }
  }

  run entryOrderDoesNotChangeTheResult = {
    // For distinct keys the final ctx is the same either way round.
    val a = keyOf("mongo", 4)
    val b = keyOf("file", 5)
    val ab = enter(enter(empty, a), b)
    val ba = enter(enter(empty, b), a)
    all {
      assert(ab == ba),
      assert(isolated(ab)),
    }
  }

  run ownerReleasesAndAdopterDoesNot = {
    // The owner clears its storage on exit; an adopter's release leaves it.
    val k = keyOf("list", 9)
    val ctx1 = enter(empty, k)
    all {
      assert(owns(empty, k) and not(owns(ctx1, k))),
      assert(release(ctx1, k, true) == empty),   // owner cleared it
      assert(release(ctx1, k, false) == ctx1),   // adopter left it
    }
  }

  run singleKeyIsolationFails = {
    // The pre-#178 model: both resources claim one key, so B adopts A's rows.
    val a = keyOf("sql", 1)
    val b = keyOf("sql", 2)
    val ctx = buggyEnter(buggyEnter(empty, a), b)
    all {
      assert(not(isolated(ctx))),
      assert(ctx.get(singleKey) == storageFor(a)),
      // B's own storage is never present, so B is served A's rows.
      assert(not(b.in(ctx.keys()))),
    }
  }

  run invariantsHold = {
    // Enter a few keys, then release the first, and check the structural laws
    // after every step.
    val a = keyOf("sql", 1)
    val b = keyOf("sql", 1)       // same storage as `a`
    val c = keyOf("mongo", 2)
    val ctx1 = enter(empty, a)
    val ctx2 = enter(ctx1, b)
    val ctx3 = enter(ctx2, c)
    val ctx4 = release(ctx3, a, true)
    all {
      assert(owns(empty, a) and not(owns(ctx1, b)) and owns(ctx2, c)),
      assert(isolated(ctx1) and isolated(ctx2) and isolated(ctx3)),
      assert(ctx4.keys() == Set(c)),          // the owner cleared `a` / `b`
      assert(isolated(ctx4)),
      // Release by a non-owner never drops another entrant's storage.
      assert(release(ctx3, b, false) == ctx3),
      assert(release(ctx4, c, true) == empty),
    }
  }
}
