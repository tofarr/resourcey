.PHONY: install test test-fast lint type specs clean

install:
	uv sync --all-extras

test:
	uv run pytest tests/unit --cov=resourcey --cov-report=term-missing --cov-fail-under=90 -n 0

test-fast:
	uv run pytest tests/unit -n auto

lint:
	uv run ruff check .
	uv run ruff format --check .

type:
	uv run mypy src/resourcey

specs:
	scripts/install_quint_evaluator.sh
	quint typecheck specs/resource_actions.qnt
	quint typecheck specs/permissions.qnt
	quint test specs/permissions.qnt --main=permissions --match "^(allowAllIsAllForEveryAction|denyAllIsNoneForEveryAction|readOnlyGrantsReadLikeAndDeniesTheRest|normalizeActionReducesDerivedMembers|enforcementMatrix|enforcementAllowsEverything|allowAllGrantsAccess|denyAllBlocksAccess|emptyPoliciesDenies|readOnlyGrantsReadNotCreate|denyDoesNotOverrideGrant|unionCombinesGrants|builtinsStayShared|invariantsHold)$$"
	quint typecheck specs/roles.qnt
	quint test specs/roles.qnt --main=roles --match "^(allowAllIsAllForEveryAction|denyAllIsNoneForEveryAction|readOnlyGrantsReadLikeAndDeniesTheRest|normalizeActionReducesDerivedMembers|ownerPolicyScoping|unknownRoleGrantsNothing|unroledCallerUsesTheClosedDefault|perResourceScoping|readsAllOfXOwnRowsOfY|multipleRolesUnionTheirGrants|denyRoleDoesNotOverrideAGrant|ownerResponseIsPrivate|invariantsHold)$$"
	quint typecheck specs/exposure.qnt
	quint typecheck specs/api_key.qnt
	quint typecheck specs/auth.qnt
	quint typecheck specs/secret_serialization.qnt
	quint typecheck specs/principal_store.qnt
	quint typecheck specs/dto_defaults.qnt
	quint test specs/resource_actions.qnt --main=resource_actions --match "^(createThenRead|createUpdateDelete|batchReadAndEdit|searchEmptyFilterMatchesAll|searchEqualityPredicate|searchInPredicate|searchEmptyInMatchesNothing|searchAndCombinesPredicates|searchNoFilterDeclaredRejectsNonEmptyFilter|countEmptyFilterCountsAll|countEqualityPredicate|countNoFilterDeclaredRejectsNonEmptyFilter|readOnlyResourceExposesReadSubset|writableResourceExposesAllActions|readOnlyResourceServesReads|defensiveReadIsolated|nonDefensiveServesStore)$$"
	quint typecheck specs/conditional_write.qnt
	quint test specs/conditional_write.qnt --main=conditional_write --match "^(updateWithHoldingConditionApplies|updateWithFailingConditionIsAbsentAndUnchanged|conditionMissMatchesAbsentTarget|deleteWithHoldingConditionRemoves|deleteWithFailingConditionIsFalseAndUnchanged|policyScopeGatesTheWrite|policyScopeNarrowsNeverWidens|neverConditionIsAlwaysAbsent|batchEditAlignsConditionalResults)$$"
	quint test specs/exposure.qnt --main=exposure --match "^(selfIsTheDefault|hiddenRegistersNoRoutesTest|wrapperHidesAnExposedResource|wrapperNarrowsTheQuerySurface|delegatedWrapperWouldLeak|builderDoesNotChangeExposure|narrowingIsAllowedWideningIsRejected|batchEditAdmitsOnlyDeclaredActions|batchActionsRequireTheirSingularTest|defaultBuilderIsIdentityTest|wrappedBuilderPreservesTheActionContractTest|invariantsHold)$$"
	quint test specs/api_key.qnt --main=api_key --match "^(keyIsAbsentFromTheWriteModels|keyIsAbsentFromTheReadModel|onlyCreateDisclosesTheKey|querySurfaceExcludesTheKey|storedValueIsOnlyTheDigest|createRevealsTheStoredKey|presentedKeyAuthenticatesByDigest|updateCannotRotateTheKey|invariantsHold)$$"
	quint test specs/auth.qnt --main=auth --match "^(emptyStoreDeniesEverything|absentAndInvalidAreIndistinguishable|aValidKeyGrantsAccess|theCheckRunsBeforeTargetStorage|theKeyServiceOwnsItsOwnContext|thePipelineClassifiesThreeWays|oneResultDerivesBothTiers|theCompositeFollowsFirstWins|invariantsHold|principalPipelineInvariantsHold)$$"
	quint test specs/secret_serialization.qnt --main=secret_serialization --match "^(defaultRedacts|exposeRevealsThePlaintext|encryptionTakesPrecedence|outputDependsOnlyOnTheContext|encryptionRoundTrips|invariantsHold)$$"
	quint test specs/principal_store.qnt --main=principal_store --match "^(unmatchedKeyIsInvalid|absentIsNotInvalidTest|enabledStoredPrincipalAuthenticates|disabledPrincipalIsRejected|missingPrincipalIsRejected|noStoreTrustsTheCredentialTest|invariantsHold)$$"
	quint typecheck specs/oauth.qnt
	quint test specs/oauth.qnt --main=oauth --match "^(failClosedOnNoClientTest|aValidTokenAuthenticates|issuerAndAudienceAreChecked|expiryIsChecked|algorithmIsPinnedNoConfusion|identityIsFailClosed|localUserStoreIsAuthoritativeTest|absentIsNotInvalidTest|refreshSerializesAcrossCallers|invariantsHold)$$"
	quint test specs/dto_defaults.qnt --main=dto_defaults --match "^(bareUuidIdIsGenerated|customIdentifierStaysClientSupplied|explicitIntentIsHonouredVerbatim|columnDefaultWins|timestampsAreFrameworkOwnedTest|contradictoryDefaultsAreRejected|invariantsHold)$$"
	quint typecheck specs/filtering.qnt
	quint test specs/filtering.qnt --main=filtering --match "^(law1_normalisationPreserves|law2_nullSafeNegation|law3_nullSafeNegationGeneralises|law4_positivePushdownIsSound|law5_inFilterAgreesWithEqAndOr)$$"
	quint typecheck specs/sorting.qnt
	quint test specs/sorting.qnt --main=sorting --match "^(law1_totalOrder|law2_keysetAgreement|law3_descendingIsTheMirror|law4_cursorSortBinding)$$"
	quint typecheck specs/cache_defaults.qnt
	quint test specs/cache_defaults.qnt --main=cache_defaults --match "^(readOnlyIsOptimistic|writeActionForcesValidator|writableWithUpdatedAtGetsLastModified|writableWithoutUpdatedAtGetsEtag|readOnlyDefaultHasPositiveWindow|readOnlyWindowIsPrivate|selectionIsTotal|invariantsHold)$$"
	quint typecheck specs/cache_freshness.qnt
	quint test specs/cache_freshness.qnt --main=cache_freshness --match "^(emptyCacheAlwaysReads|optimisticFreshDoesNotRead|optimisticStaleReads|optimisticIsPureTimeCheck|validatorStrategiesAlwaysRead|decisionIsMonotonic|selectionIsTotal|invariantsHold)$$"
	quint typecheck specs/rbac.qnt
	quint test specs/rbac.qnt --main=rbac --match "^(allowAllMatchesEveryRow|denyAllMatchesNothing|readOnlyMatchesReadLike|creatorScopesToOwnedRows|groupMemberMatchesOwnGroups|aclMatchesItsIds|resolutionWalksGroupsToRolesToPermissions|resourceScopingFiltersOtherResources|scopedQueryIsComplete|unknownUserIsFailClosed|emptyPermissionSetIsFailClosed|multipleRolesUnionTheirGrants|denyDoesNotOverrideAGrant|aclsUnionToOneSet|setLeafMatchesTheJoin|thresholdBoundsMembershipChange|callerScopingIsDerivedFromPolicies|invariantsHold)$$"
	quint typecheck specs/filestore.qnt
	quint test specs/filestore.qnt --main=filestore --match "^(createIsImmediatelyVisible|keysAreUnique|capabilityIsBound|deleteRemovesTheObject|deleteOfAbsentIsNoop|invariantsHold)$$"
	quint typecheck specs/background_tasks.qnt
	quint test specs/background_tasks.qnt --main=background_tasks --match "^(matchesIsFieldConjunction|domDowOrRule|matchIsUnrestrictedWhenNeitherDayFieldIsSet|scheduleNoneNeverTicks|onlyEnabledTasksTick|selectionIsPerTask|aRaisingTaskDoesNotStopTheLoop|invariantsHold)$$"
	quint typecheck specs/triggers.qnt
	quint test specs/triggers.qnt --main=triggers --match "^(editsOnlyFire|successOnly|alignmentTest|perOperationNotPerItemTest|perTriggerIsolationTest|backgroundNeverBlocksTest|exitSettlesEveryRunTest|invariantsHold)$$"
	quint typecheck specs/jobs.qnt
	quint test specs/jobs.qnt --main=jobs --match "^(claimSingleWinner|claimNeedsRunnable|terminalSticky|completionOnlyIfOwner|attemptCap|staleRecoverable|scheduleAndRunnerGates)$$"
	quint typecheck specs/realtime.qnt
	quint test specs/realtime.qnt --main=realtime --match "^(subscribableGateTest|handshakeAuthenticatedAlwaysGrantsTest|handshakeInvalidAlwaysRejectedTest|handshakeAbsentGatedByPostureTest|createdUpdatedMatchBothFiltersTest|crossResourceNeverDeliversTest|subFilterNeverWidensBeyondPolicyTest|deleteFailsClosedWhenScopedTest|noMatchDeliversNothingTest|unscopedAlwaysDeliversTest|invariantsHold)$$"

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache .coverage htmlcov coverage.xml
