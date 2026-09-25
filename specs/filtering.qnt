// filtering.qnt — laws for v2 filtering (issue #79).
//
// Two laws are pinned here, both storage-agnostic:
//
//   Law 1 (normalisation): the and_() factory's identity / annihilator
//   normalisation preserves the in-memory matches() semantics for every value.
//
//   Law 2 (NULL-safe negation): a pushed-down negated operator (SQL
//   `IS DISTINCT FROM`) agrees with the in-memory complement on every row,
//   including NULL rows, whereas a naive SQL `NOT` provably does not — the
//   NULL row is excluded from both branches under three-valued logic.
//
// Quint rejects recursive type declarations (QNT099), so a genuine recursive
// filter tree cannot be written; the tree is unrolled into a flat clause list,
// matching how resource_actions.qnt already models filters.
module filtering {
  type Entity = int

  type Clause = Eq(int) | Gt(int)

  type Filter =
    | All
    | NoMatch
    | And(List[{ attribute: str, clause: Clause }])

  // Reference semantics: the in-memory predicate. This is what matches() must
  // implement, and what any pushed-down SQL condition must agree with.
  pure def matchesClause(value: Entity, c: Clause): bool =
    match c {
      | Eq(v) => value == v
      | Gt(v) => value > v
    }

  pure def matches(value: Entity, f: Filter): bool =
    match f {
      | All => true
      | NoMatch => false
      | And(clauses) => clauses.foldl(true, (acc, c) => acc and matchesClause(value, c.clause))
    }

  // Factory normalisation identities for and_():
  //   - any NoMatch child -> NoMatch         (annihilator)
  //   - All children dropped                 (identity)
  //   - nothing left -> All                  (empty conjunction is true)
  pure def andNorm(filters: List[Filter]): Filter =
    if (filters.foldl(false, (acc, f) => acc or f == NoMatch)) NoMatch
    else {
      val clauses = filters.foldl(
        List(),
        (acc, f) =>
          match f {
            | All => acc
            | NoMatch => acc
            | And(cs) => acc.concat(cs)
          }
      )
      if (clauses.length() == 0) All else And(clauses)
    }

  // Law 1: normalisation preserves the reference semantics, for every value.
  pure def andNormPreserves(filters: List[Filter]): bool =
    entityValues.foldl(
      true,
      (acc, v) => acc and (matches(v, andNorm(filters)) == filters.foldl(true, (a, f) => a and matches(v, f)))
    )

  // ---- Law 2: SQL three-valued agreement ------------------------------
  // Pushing a filter down to SQL is only sound if the SQL condition and
  // matches() agree on EVERY row — including NULL rows. Model SQL's
  // three-valued logic explicitly, because that is where Not and the negated
  // operators diverge from the two-valued in-memory predicate.

  type Tri = T | F | Unknown

  // A nullable column value: Val(v) or Null.
  type Row = Val(int) | Null

  pure def sqlEq(row: Row, v: int): Tri =
    match row {
      | Val(r) => if (r == v) T else F
      | Null => Unknown
    }

  // SQL `NOT (c = v)` for a nullable column: NOT Unknown = Unknown, so the row
  // is excluded from BOTH the positive and the negated branch.
  pure def sqlNot(t: Tri): Tri =
    match t {
      | T => F
      | F => T
      | Unknown => Unknown
    }

  // SQL `c IS DISTINCT FROM v`: NULL-safe inequality — the row lands in the
  // negated branch instead of vanishing.
  pure def sqlIsDistinctFrom(row: Row, v: int): Tri =
    match row {
      | Val(r) => if (r == v) F else T
      | Null => T
    }

  pure def memNegEq(row: Row, v: int): bool =
    match row {
      | Val(r) => r != v
      | Null => true
    }

  // Law 2: the NULL-safe negated operator agrees with the in-memory complement
  // on every row, whereas the naive NOT does not (hence the counterexample).
  pure def nullSafeNegationAgrees(v: int): bool =
    rows.foldl(true, (acc, row) => acc and ((sqlIsDistinctFrom(row, v) == T) == memNegEq(row, v)))

  pure def naiveNegationDisagrees(v: int): bool =
    rows.foldl(false, (acc, row) => acc or ((sqlNot(sqlEq(row, v)) == T) != memNegEq(row, v)))

  // Small finite universes so the laws can be run as tests.
  pure val entityValues = List(0, 1, 2)
  pure val rows: List[Row] = List(Val(0), Val(1), Val(2), Null)

  // The sample filters the identities are checked against: All is dropped,
  // NoMatch is an annihilator, and a clause filters a value.
  pure val sampleClause = { attribute: "v", clause: Eq(1) }
  pure val sampleFilters: List[Filter] = List(All, NoMatch, And(List(sampleClause)))

  run law1_normalisationPreserves =
    andNormPreserves(sampleFilters) and andNormPreserves(List(All, All))

  run law2_nullSafeNegation =
    nullSafeNegationAgrees(1) and naiveNegationDisagrees(1)
}
