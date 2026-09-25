// sorting.qnt — laws for v2 sorting (issue #97).
//
// Four laws are pinned here, all storage-agnostic:
//
//   Law 1 (total order): compare is antisymmetric and transitive on sort keys,
//   and equal keys fall back to the identifier, so the (key, id) order the
//   backend builds is total — which is what makes keyset paging deterministic.
//
//   Law 2 (keyset agreement): the pushed-down keyset predicate keeps exactly
//   the rows that come after the cursor in compare order (ascending).
//
//   Law 3 (descending mirrors the key, not the tie-breaker): descending
//   reverses the *sort-key* comparison while the identifier tie-breaker stays
//   ascending, matching `ORDER BY key DESC, id ASC`.
//
//   Law 4 (cursor/sort binding): a cursor is accepted only under the
//   (sort_field, ascending) it was built for.
//
// Quint rejects recursive types (QNT099), so this models a single-attribute
// sort over a finite row universe, matching how filtering.qnt unrolls its tree.
module sorting {
  // A row: a sort key and the identifier tie-breaker.
  type Row = { key: int, id: int }

  // The reference semantics: compare a single attribute ascending. This is what
  // SortOrder.compare must implement for AttrSortOrder(descending=false).
  pure def compareKey(a: int, b: int): int =
    if (a < b) -1 else if (a > b) 1 else 0

  // The total (key, id) order: the sort key first, then the identifier.
  pure def compareRow(a: Row, b: Row): int =
    val byKey = compareKey(a.key, b.key)
    if (byKey == 0) compareKey(a.id, b.id) else byKey

  pure def ascending(a: Row, b: Row): bool = compareRow(a, b) < 0

  // Descending order as the backend builds it: `ORDER BY key DESC, id ASC`.
  // Only the key comparison mirrors; the identifier tie-breaker stays ascending.
  pure def descending(a: Row, b: Row): bool =
    if (a.key != b.key) a.key > b.key else a.id < b.id

  // A small universe with deliberate key collisions, so the tie-breaker matters.
  pure val rows: List[Row] = List(
    { key: 0, id: 1 },
    { key: 1, id: 1 },
    { key: 1, id: 2 },
    { key: 2, id: 1 }
  )

  // ---- Law 1: total order ------------------------------------------------
  // Antisymmetry: exactly one of "a before b", "b before a", "same" holds.
  pure def antisymmetric(a: Row, b: Row): bool =
    val c = compareRow(a, b)
    if (c < 0) compareRow(b, a) > 0
    else if (c > 0) compareRow(b, a) < 0
    else compareRow(b, a) == 0

  // Transitivity: a <= b and b <= c implies a <= c.
  pure def transitive(a: Row, b: Row, c: Row): bool =
    if (compareRow(a, b) <= 0 and compareRow(b, c) <= 0) compareRow(a, c) <= 0 else true

  // Equal keys fall back to the identifier (the ordering is total, never a tie).
  pure def tiesBreakOnId(a: Row, b: Row): bool =
    if (a.key == b.key and a.id != b.id) compareRow(a, b) != 0 else true

  run law1_totalOrder =
    rows.foldl(
      true,
      (acc, a) =>
        acc and
        rows.foldl(true, (acc2, b) => acc2 and antisymmetric(a, b) and tiesBreakOnId(a, b))
    ) and
    rows.foldl(
      true,
      (acc, a) =>
        acc and
        rows.foldl(
          true,
          (acc2, b) =>
            acc2 and rows.foldl(true, (acc3, c) => acc3 and transitive(a, b, c))
        )
    )

  // ---- Law 2: keyset agreement (ascending) -------------------------------
  // The pushed-down predicate `(key, id) > (cursor_key, cursor_id)` keeps
  // exactly the rows the in-memory order places after the cursor.
  pure def keysetKeeps(row: Row, c: Row): bool =
    if (row.key > c.key) true
    else if (row.key == c.key) row.id > c.id
    else false

  pure def keysetAgrees(c: Row): bool =
    rows.foldl(true, (acc, row) => acc and (keysetKeeps(row, c) == ascending(c, row)))

  run law2_keysetAgreement = rows.foldl(true, (acc, c) => acc and keysetAgrees(c))

  // ---- Law 3: descending mirrors the key, not the tie-breaker ------------
  // The descending predicate keeps exactly the rows that follow the cursor in
  // descending order: a smaller key, or an equal key with a *greater* id (the
  // tie-breaker never mirrors).
  pure def descendingKeeps(row: Row, c: Row): bool =
    if (row.key < c.key) true
    else if (row.key == c.key) row.id > c.id
    else false

  pure def descendingIsMirror(c: Row): bool =
    rows.foldl(
      true,
      (acc, row) => acc and (descendingKeeps(row, c) == descending(c, row))
    )

  run law3_descendingIsTheMirror = rows.foldl(true, (acc, c) => acc and descendingIsMirror(c))

  // ---- Law 4: cursor/sort binding ----------------------------------------
  // A cursor carries the (sort_field, ascending) it was built for; it is
  // accepted only under the matching request.
  type Cursor = { field: str, ascending: bool }
  type Request = { field: str, ascending: bool }

  pure def accepts(cursor: Cursor, request: Request): bool =
    cursor.field == request.field and cursor.ascending == request.ascending

  pure def bindingAcceptsItsOwn(c: Cursor): bool = accepts(c, { field: c.field, ascending: c.ascending })

  pure def bindingRejectsAMismatch(c: Cursor): bool =
    val other = { field: c.field, ascending: not(c.ascending) }
    val otherField = { field: "different_field", ascending: c.ascending }
    not(accepts(c, other)) and not(accepts(c, otherField))

  pure val cursors: List[Cursor] = List(
    { field: "created_at", ascending: true },
    { field: "created_at", ascending: false },
    { field: "name", ascending: true }
  )

  run law4_cursorSortBinding =
    cursors.foldl(true, (acc, c) => acc and bindingAcceptsItsOwn(c) and bindingRejectsAMismatch(c))
}
