// cache_freshness.qnt — formal spec for the programmatic freshness decision.
//
// CacheStrategy.should_read(read_at, etag) answers the *server-side* freshness
// question a caching wrapper asks — "is the copy I hold still good?" — as
// distinct from the HTTP-facing validator headers. This spec pins the answer
// each shipped strategy gives.
//
// A cache entry is described by whether it is present, and (when present) how
// many seconds ago it was written (``age``) and the validator it holds. The
// decision also takes the strategy's ``expireIn`` window.
//
// Per-strategy answer:
//   * Optimistic   -> pure time check: read only once ``age >= expireIn``.
//   * LastModified -> read always: deciding needs the source's *current*
//                     updated_at, which the signature does not carry (a cheap
//                     source read, but a source contact nonetheless).
//   * Etag         -> read always: the tag is a hash of the content, so this
//                     degenerates to read-then-compare.
//   * NoPolicy     -> read always.
//
// Invariants:
//   - emptyCacheAlwaysReads: a missing read_at (an empty cache) always reads.
//   - optimisticFreshDoesNotRead / optimisticStaleReads: the time boundary.
//   - optimisticIsPureTimeCheck: the optimistic answer depends on nothing but
//     age and expireIn (no source contact), so an optimistic cache of the same
//     age always decides the same way.
//   - validatorStrategiesAlwaysRead: LastModified / Etag / NoPolicy always read.
//   - decisionIsMonotonic: for a fixed expireIn, once optimistic decides to
//     read it never decides not to read as age grows.
//   - selectionIsTotal: every (strategy, present, age, expireIn) maps to a bool.

module cache_freshness {
  // The strategies whose freshness decision this spec pins. NoPolicy is the
  // core placeholder / a resource with no strategy.
  type Strategy = Optimistic | LastModified | Etag | NoPolicy

  // Whether a fresh read from the source is required.
  //
  // ``present`` is whether the cache holds an entry; ``age`` is the entry's age
  // in seconds (ignored when absent); ``expireIn`` is the optimistic window.
  pure def shouldRead(
    strategy: Strategy,
    present: bool,
    age: int,
    expireIn: int,
  ): bool =
    if (not(present)) true
    else if (strategy == Optimistic) age >= expireIn
    else true

  // Tests

  val allStrategies: Set[Strategy] = Set(Optimistic, LastModified, Etag, NoPolicy)
  val validatorStrategies: Set[Strategy] = Set(LastModified, Etag, NoPolicy)

  run emptyCacheAlwaysReads = {
    allStrategies.forall(strategy =>
      // Absent entry: read, whatever the (stale-looking) age and window.
      shouldRead(strategy, false, 0, 600) and shouldRead(strategy, false, 99999, 600)
    )
  }

  run optimisticFreshDoesNotRead = {
    all {
      // Inside the window: no read.
      not(shouldRead(Optimistic, true, 0, 600)),
      not(shouldRead(Optimistic, true, 599, 600)),
      not(shouldRead(Optimistic, true, 1, 2)),
    }
  }

  run optimisticStaleReads = {
    all {
      // At or past the window: read.
      shouldRead(Optimistic, true, 600, 600),
      shouldRead(Optimistic, true, 601, 600),
      shouldRead(Optimistic, true, 2, 2),
    }
  }

  run optimisticIsPureTimeCheck = {
    // The decision is a function of (age, expireIn) alone: same inputs, same
    // answer — there is no source term to consult.
    all {
      shouldRead(Optimistic, true, 300, 600) == shouldRead(Optimistic, true, 300, 600),
      shouldRead(Optimistic, true, 700, 600) == shouldRead(Optimistic, true, 700, 600),
    }
  }

  run validatorStrategiesAlwaysRead = {
    validatorStrategies.forall(strategy =>
      // Even a present, just-written entry: these cannot be decided from the
      // signature alone.
      shouldRead(strategy, true, 0, 600) and shouldRead(strategy, true, 100, 600)
    )
  }

  run decisionIsMonotonic = {
    // For the optimistic window, once stale always stale as age grows.
    all {
      shouldRead(Optimistic, true, 600, 600),
      shouldRead(Optimistic, true, 1000, 600),
      // A younger entry that reads implies the older one reads too.
      (shouldRead(Optimistic, true, 601, 600) implies shouldRead(Optimistic, true, 700, 600)),
    }
  }

  run selectionIsTotal = {
    allStrategies.forall(strategy =>
      Set(true, false).forall(present =>
        Set(0, 100, 600, 1000).forall(age =>
          Set(1, 2, 600, 3600).forall(expireIn =>
            Set(true, false).contains(shouldRead(strategy, present, age, expireIn))
          )
        )
      )
    )
  }

  run invariantsHold = {
    all {
      // emptyCacheAlwaysReads
      shouldRead(Optimistic, false, 0, 600),
      shouldRead(Etag, false, 0, 600),
      // optimistic boundary
      not(shouldRead(Optimistic, true, 599, 600)),
      shouldRead(Optimistic, true, 600, 600),
      // validators
      shouldRead(LastModified, true, 0, 600),
      shouldRead(Etag, true, 0, 600),
      shouldRead(NoPolicy, true, 0, 600),
      // totality
      shouldRead(Optimistic, true, 100, 600) or not(shouldRead(Optimistic, true, 100, 600)),
    }
  }
}
