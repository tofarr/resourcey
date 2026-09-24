// dto_defaults.qnt — formal spec for the DTO field conventions and the
// generated logical defaults (issue #94).
//
// A DTO declaration is the single source of field truth. Three conventions are
// applied to a *bare* field (one with no explicit DtoField): the identifier is
// server-generated and immutable; ``created_at`` is written once; ``updated_at``
// is re-set on every update. Beyond those, a UUID ``id`` is generated
// server-side by the application (an application factory, never a database
// default).
//
// The rule that dominates all of them: an expressed intent wins. An explicit
// DtoField is honoured verbatim — the conventions change neither its flags nor
// its defaults — and in the model-first SQL path a column-level default
// (``default=`` / ``server_default``) beats a convention-generated one.
//
// Invariants:
//   - conventionsOnlyDecorateBareFields: an explicit DtoField is the projection,
//     untouched by the conventions.
//   - uuidIdIsGeneratedNotAccepted: a bare conventional ``id: UUID`` gets an
//     application factory and is absent from create/update requests; the same
//     UUID under a *custom* identifier (a natural key) stays client-supplied.
//   - columnDefaultBeatsConvention: an explicit column default is used rather
//     than the convention's, and a backend-generated key gets no factory at all.
//   - timestampsAreFrameworkOwned: timestamps are in no request shape and every
//     response shape, and always carry a create factory.
//   - valueAndFactoryAreExclusive: a value and a factory for the same operation
//     cannot both be declared.
//
// A field is reduced to the minimal information the rules read: its name, its
// resolved type kind, whether the author wrote an explicit DtoField, and — in
// the SQL path — the column's own default intent.

module dto_defaults {
  // The type kind a field resolves to, as far as the UUID convention cares.
  type Kind = UUIDKind | OtherKind

  // Where a field's create default comes from.
  //   NoDefault          -> no default; an omitted create leaves it unset
  //   ApplicationFactory -> the application supplies the value (a DtoField factory)
  //   BackendGenerated   -> the database supplies the value (server default / autoincrement)
  type DefaultKind = NoDefault | ApplicationFactory | BackendGenerated

  // The projection a field resolves to: create/update request membership and
  // where its create default comes from. (Whether a field is on a *response* is
  // a separate concern — responses carry no defaults — so it is not modelled
  // here; every field is a response field.)
  type Projection = {
    inCreateRequest: bool,
    inUpdateRequest: bool,
    createDefault: DefaultKind,
  }

  // A field declaration, reduced to what the rules read.
  type Field = {
    name: str,
    kind: Kind,
    // The declaration's resolved identifier (``id`` unless one was chosen).
    idFieldName: str,
    // The author wrote an explicit DtoField: the conventions must not touch it.
    explicit: bool,
    // A create default is already expressed (a value or a factory).
    declaredDefault: bool,
    // SQL only: the column is backend-generated (server_default / autoincrement).
    backendGenerated: bool,
  }

  pure def isConventionalId(f: Field): bool =
    f.name == f.idFieldName

  pure def isCustomId(f: Field): bool =
    isConventionalId(f) and f.name != "id"

  pure def isTimestamp(f: Field): bool =
    f.name == "created_at" or f.name == "updated_at"

  // The identifier's create-default kind, honouring expressed intent: a declared
  // default (or a backend key) wins over the convention-generated factory. Only
  // the *conventional* ``id`` gets the UUID factory — a custom identifier (a
  // natural key) is left alone.
  pure def idCreateDefault(f: Field): DefaultKind =
    if (f.declaredDefault) ApplicationFactory
    else if (f.backendGenerated) BackendGenerated
    else if (f.name == "id" and f.kind == UUIDKind) ApplicationFactory
    else NoDefault

  // The projection for a field — the single place the conventions run. An
  // explicit declaration is the projection outright.
  pure def project(f: Field): Projection =
    if (f.explicit) {
      declaredProjection(f)
    } else if (isConventionalId(f)) {
      {
        // A conventional ``id`` is server-generated so never client input; a
        // custom identifier (a natural key) is client-supplied on create. Both
        // are immutable, so never on an update.
        inCreateRequest: isCustomId(f),
        inUpdateRequest: false,
        createDefault: idCreateDefault(f),
      }
    } else if (isTimestamp(f)) {
      // Neither timestamp is client input; both are filled on create by the
      // application. ``created_at`` is written once (no update default),
      // ``updated_at`` is re-set on every update.
      { inCreateRequest: false, inUpdateRequest: false, createDefault: ApplicationFactory }
    } else {
      {
        inCreateRequest: true,
        inUpdateRequest: true,
        createDefault: if (f.backendGenerated) BackendGenerated else NoDefault,
      }
    }

  // The projection an *explicit* field declares: client-supplied on both writes,
  // with whatever default the declaration supplied. ``project`` must return
  // exactly this for an explicit field.
  pure def declaredProjection(f: Field): Projection =
    {
      inCreateRequest: true,
      inUpdateRequest: true,
      createDefault: if (f.declaredDefault) ApplicationFactory else NoDefault,
    }

  // Invariants

  pure def conventionsOnlyDecorateBareFields(f: Field): bool =
    not(f.explicit) or project(f) == declaredProjection(f)

  pure def uuidIdIsGeneratedNotAccepted(f: Field): bool =
    val p = project(f)
    if (
      f.name == "id" and f.kind == UUIDKind and not(f.explicit) and
      not(f.declaredDefault) and not(f.backendGenerated)
    ) {
      all {
        p.createDefault == ApplicationFactory,
        not(p.inCreateRequest),
        not(p.inUpdateRequest),
      }
    } else {
      true
    }

  pure def uuidCustomIdentifierStaysClientSupplied(f: Field): bool =
    val p = project(f)
    if (isCustomId(f) and f.kind == UUIDKind and not(f.explicit)) {
      all {
        p.inCreateRequest,
        not(p.inUpdateRequest),
        p.createDefault == NoDefault,
      }
    } else {
      true
    }

  pure def columnDefaultBeatsConvention(f: Field): bool =
    val p = project(f)
    if (f.name == "id" and (f.declaredDefault or f.backendGenerated)) {
      // The column's own intent resolves, never a convention-invented factory.
      if (f.backendGenerated and not(f.declaredDefault)) p.createDefault == BackendGenerated
      else p.createDefault == ApplicationFactory
    } else {
      true
    }

  pure def timestampsAreFrameworkOwned(f: Field): bool =
    val p = project(f)
    not(isTimestamp(f)) or
    all {
      not(p.inCreateRequest),
      not(p.inUpdateRequest),
      p.createDefault == ApplicationFactory,
    }

  pure def valueAndFactoryAreExclusive(declaresValue: bool, declaresFactory: bool): bool =
    not(declaresValue and declaresFactory)

  // Tests

  val bareUuidId: Field = {
    name: "id", kind: UUIDKind, idFieldName: "id", explicit: false,
    declaredDefault: false, backendGenerated: false
  }

  val uuidNaturalKey: Field = {
    name: "node_id", kind: UUIDKind, idFieldName: "node_id", explicit: false,
    declaredDefault: false, backendGenerated: false
  }

  val uuidIdWithColumnFactory: Field = {
    name: "id", kind: UUIDKind, idFieldName: "id", explicit: false,
    declaredDefault: true, backendGenerated: false
  }

  val uuidIdBackendGenerated: Field = {
    name: "id", kind: UUIDKind, idFieldName: "id", explicit: false,
    declaredDefault: false, backendGenerated: true
  }

  val explicitId: Field = {
    name: "id", kind: UUIDKind, idFieldName: "id", explicit: true,
    declaredDefault: false, backendGenerated: false
  }

  val bareCreatedAt: Field = {
    name: "created_at", kind: OtherKind, idFieldName: "id", explicit: false,
    declaredDefault: false, backendGenerated: false
  }

  val bareUpdatedAt: Field = {
    name: "updated_at", kind: OtherKind, idFieldName: "id", explicit: false,
    declaredDefault: false, backendGenerated: false
  }

  run bareUuidIdIsGenerated = {
    all {
      assert(project(bareUuidId).createDefault == ApplicationFactory),
      assert(not(project(bareUuidId).inCreateRequest)),
      assert(not(project(bareUuidId).inUpdateRequest)),
      assert(uuidIdIsGeneratedNotAccepted(bareUuidId)),
    }
  }

  run customIdentifierStaysClientSupplied = {
    all {
      assert(project(uuidNaturalKey).inCreateRequest),
      assert(not(project(uuidNaturalKey).inUpdateRequest)),
      assert(project(uuidNaturalKey).createDefault == NoDefault),
      assert(uuidCustomIdentifierStaysClientSupplied(uuidNaturalKey)),
    }
  }

  run explicitIntentIsHonouredVerbatim = {
    all {
      // The explicit field's projection is exactly its declaration...
      assert(conventionsOnlyDecorateBareFields(explicitId)),
      assert(project(explicitId) == declaredProjection(explicitId)),
      // ...whereas the bare UUID id is decorated by the conventions (its
      // projection differs from a plain declaration).
      assert(not(project(bareUuidId) == declaredProjection(bareUuidId))),
    }
  }

  run columnDefaultWins = {
    all {
      assert(project(uuidIdWithColumnFactory).createDefault == ApplicationFactory),
      assert(columnDefaultBeatsConvention(uuidIdWithColumnFactory)),
      // A backend-generated id gets no application factory.
      assert(project(uuidIdBackendGenerated).createDefault == BackendGenerated),
      assert(columnDefaultBeatsConvention(uuidIdBackendGenerated)),
    }
  }

  run timestampsAreFrameworkOwnedTest = {
    all {
      assert(project(bareCreatedAt).createDefault == ApplicationFactory),
      assert(project(bareUpdatedAt).createDefault == ApplicationFactory),
      assert(timestampsAreFrameworkOwned(bareCreatedAt)),
      assert(timestampsAreFrameworkOwned(bareUpdatedAt)),
    }
  }

  run contradictoryDefaultsAreRejected = {
    all {
      assert(valueAndFactoryAreExclusive(true, true) == false),
      assert(valueAndFactoryAreExclusive(true, false)),
      assert(valueAndFactoryAreExclusive(false, true)),
    }
  }

  run invariantsHold = {
    val declared = Set(
      bareUuidId, uuidNaturalKey, uuidIdWithColumnFactory,
      uuidIdBackendGenerated, explicitId, bareCreatedAt, bareUpdatedAt
    )
    all {
      declared.forall(f => conventionsOnlyDecorateBareFields(f)),
      declared.forall(f => uuidIdIsGeneratedNotAccepted(f)),
      declared.forall(f => uuidCustomIdentifierStaysClientSupplied(f)),
      declared.forall(f => columnDefaultBeatsConvention(f)),
      declared.forall(f => timestampsAreFrameworkOwned(f)),
      valueAndFactoryAreExclusive(true, true) == false,
    }
  }
}
