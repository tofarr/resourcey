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
	quint typecheck specs/exposure.qnt
	quint typecheck specs/api_key.qnt
	quint typecheck specs/dto_defaults.qnt
	quint test specs/resource_actions.qnt --main=resource_actions --match "^(createThenRead|createUpdateDelete|batchReadAndEdit|searchEmptyFilterMatchesAll|searchEqualityPredicate|searchInPredicate|searchEmptyInMatchesNothing|searchAndCombinesPredicates|searchNoFilterDeclaredRejectsNonEmptyFilter|countEmptyFilterCountsAll|countEqualityPredicate|countNoFilterDeclaredRejectsNonEmptyFilter|readOnlyResourceExposesReadSubset|writableResourceExposesAllActions|readOnlyResourceServesReads|defensiveReadIsolated|nonDefensiveServesStore)$$"
	quint test specs/exposure.qnt --main=exposure --match "^(selfIsTheDefault|hiddenRegistersNoRoutesTest|wrapperHidesAnExposedResource|wrapperNarrowsTheQuerySurface|delegatedWrapperWouldLeak|builderDoesNotChangeExposure|narrowingIsAllowedWideningIsRejected|batchEditAdmitsOnlyDeclaredActions|defaultBuilderIsIdentityTest|wrappedBuilderPreservesTheActionContractTest|invariantsHold)$$"
	quint test specs/api_key.qnt --main=api_key --match "^(keyIsAbsentFromTheWriteModels|keyIsAbsentFromTheReadModel|onlyCreateDisclosesTheKey|querySurfaceExcludesTheKey|createRevealsTheStoredKey|updateCannotRotateTheKey|invariantsHold)$$"
	quint test specs/dto_defaults.qnt --main=dto_defaults --match "^(bareUuidIdIsGenerated|customIdentifierStaysClientSupplied|explicitIntentIsHonouredVerbatim|columnDefaultWins|timestampsAreFrameworkOwnedTest|contradictoryDefaultsAreRejected|invariantsHold)$$"
	quint typecheck specs/filtering.qnt
	quint test specs/filtering.qnt --main=filtering --match "^(law1_normalisationPreserves|law2_nullSafeNegation|law3_nullSafeNegationGeneralises|law4_positivePushdownIsSound)$$"
	quint typecheck specs/sorting.qnt
	quint test specs/sorting.qnt --main=sorting --match "^(law1_totalOrder|law2_keysetAgreement|law3_descendingIsTheMirror|law4_cursorSortBinding)$$"
	quint typecheck specs/cache_defaults.qnt
	quint test specs/cache_defaults.qnt --main=cache_defaults --match "^(readOnlyIsOptimistic|writeActionForcesValidator|writableWithUpdatedAtGetsLastModified|writableWithoutUpdatedAtGetsEtag|readOnlyDefaultHasPositiveWindow|readOnlyWindowIsPrivate|selectionIsTotal|invariantsHold)$$"

clean:
	rm -rf .mypy_cache .ruff_cache .pytest_cache .coverage htmlcov coverage.xml
