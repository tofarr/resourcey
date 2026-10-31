---
name: release
description: Cutting a PyPI release — bumping the version, tagging, publishing the GitHub release, and recovering from a failed run. Load when releasing, tagging, bumping the version, or debugging release.yml.
version: "1.0.0"
---

# Release

A PyPI upload happens only when a GitHub **release is published**, and only if
its tag matches the version in `pyproject.toml`. Nothing else triggers it — not
a push, not a tag alone.

## The version is in two places

`pyproject.toml` and `uv.lock` both carry it. Bump with `uv version`; it updates
both atomically:

```bash
uv version 0.0.2        # writes pyproject.toml and uv.lock
uv version --short      # print the current version
```

Editing `pyproject.toml` by hand leaves `uv.lock` stale. `uv sync` silently
repairs that, so you will not notice — but `uv lock --check`, which the release
build runs, fails. Never hand-edit the version.

## Procedure

Order matters. The version must be on `main` **before** the tag exists, because
the workflow reads `pyproject.toml` at the tagged commit.

1. **Bump the version on a branch, and merge it.**

   ```bash
   uv version 0.0.2
   git commit -am "Release v0.0.2"
   ```

   Open a PR. `uv.lock` must be in the commit — check `git status` after
   `uv version`.

2. **Confirm `main` is green.** `release.yml` calls `ci.yml` first, so a broken
   `main` cannot publish, but failing there costs a full CI run per attempt.

3. **Publish a GitHub release whose tag is `v<version>`.**

   Releases → *Draft a new release* → tag `v0.0.2`, target `main`, notes, publish.
   The tag is created at publish time from the selected target. A draft can be
   saved and published later; `published` is the event that fires the workflow.

   Or from the CLI:

   ```bash
   gh release create v0.0.2 --target main --title v0.0.2 --notes "..."
   ```

4. **Watch it, then verify.**

   ```bash
   gh run watch --repo tofarr/resourcey
   pip install resourcey==0.0.2
   ```

   On PyPI the release shows a **Provenance** section — that is the attestation
   the workflow signed. Its absence means `attestations` was disabled.

## The tag must equal the version

`v0.0.2` on a commit whose `pyproject.toml` says `0.0.1` fails immediately:

```
Error: tag v0.0.2 does not match v0.0.1 in pyproject.toml
```

This is the check doing its job — it fired before anything was built or
uploaded. The usual cause is tagging first and bumping afterwards, or tagging
`main` before the bump PR merged.

**A tag cannot be moved.** GitHub releases pin the tag; the UI has no way to
retarget one. To fix a mis-tagged release, delete the release, delete the tag,
and create a new release on the right commit:

```bash
gh release delete v0.0.2 --yes --repo tofarr/resourcey   # does NOT delete the tag
git push origin :refs/tags/v0.0.2                        # delete the remote tag
git tag -d v0.0.2                                        # and the local one
gh release create v0.0.2 --target main --title v0.0.2 --notes "..."
```

Deleting a release alone leaves the tag behind, and a leftover tag on the wrong
commit keeps failing the check. Delete both.

## Versions are permanent

PyPI refuses a filename it already has, so **a version cannot be re-uploaded**
and its number is spent. Consequences:

* A failed run is safe to retry — the failure happens before the upload, and
  `gh run rerun <id> --failed` reuses the build artifact.
* A *successful* upload cannot be undone. You cannot yank a version to re-push
  it; bump and publish again instead.
* TestPyPI also rejects duplicates. A dry run of a version already on TestPyPI
  fails with a file-exists error — that is not a configuration problem.

## Dry run (TestPyPI)

Actions → *Release* → *Run workflow* with `dry_run` enabled publishes to
TestPyPI without touching PyPI. It builds, runs `ci.yml`, and exercises the
same OIDC exchange, so it catches environment and publisher mistakes.

```bash
pip install --index-url https://test.pypi.org/simple/ \
            --extra-index-url https://pypi.org/simple/ resourcey==0.0.2
```

The extra index is required: TestPyPI does not host the dependencies.

**It does not validate the PyPI configuration.** PyPI and TestPyPI are separate
services with separate publishers, environments, and upload hosts. A green dry
run says nothing about whether `pypi` will work. There is no way to test PyPI
without publishing to it.

## Workflow shape

```
release: published (tag vX.Y.Z)
        ├── ci       ci.yml via workflow_call
        ├── build    tag == version → uv lock --check → uv build → twine check
        │            → assert contents → install wheel in a clean venv
        └── publish  id-token: write → pypa/gh-action-pypi-publish
```

Trusted publishing, not a token: PyPI verifies the workflow's OIDC identity, so
there is no secret to store or rotate. `id-token: write` is scoped to the
`publish` job. The credential is effectively the tuple (repo, workflow filename,
environment) — **renaming `release.yml` breaks publishing** until the trusted
publisher is updated to match.

Upload host is `upload.pypi.org/legacy/`, not `pypi.org/legacy/` (the latter
404s; it is the project/JSON host). TestPyPI needs no such split.

## One-time setup

Not in the repo, and required before the first tag:

* PyPI and TestPyPI → *Publishing* → pending publisher for `resourcey`:
  owner `tofarr`, repo `resourcey`, workflow `release.yml`, environment `pypi`
  (and a second with `testpypi`).
* GitHub → Settings → Environments: `pypi` and `testpypi`. Add required
  reviewers to `pypi` if a human should approve before an irreversible upload.
* GitHub → Settings → Tags: protect `v*` so a stray tag cannot publish.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `tag vX does not match vY` | version not bumped, or bump not merged before tagging |
| `invalid-publisher` | pending publisher does not match the claims printed in the error; check owner, repo, workflow filename, environment |
| `HTTPError: 404 from .../legacy/` | wrong upload host — must be `upload.pypi.org` |
| file already exists | version already uploaded; bump |
| `uv lock --check` fails | version hand-edited; re-run `uv version <same>` |

The publish log prints the OIDC claims it presented (`sub`, `repository`,
`workflow_ref`, `environment`) — compare those against the publisher rather
than guessing.
