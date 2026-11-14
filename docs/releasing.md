# Releasing

Publication uses PyPI trusted publishing (OIDC). The workflow
[`.github/workflows/publish.yml`](https://github.com/ID42Studio/id42-work/blob/main/.github/workflows/publish.yml)
stores no token and no password. A version tag publishes to PyPI. A manual run
publishes only to TestPyPI.

Do this once, before the first upload. Repeat it only if the repository, the
workflow file or the environment name changes.

## GitHub environments

In the repository settings, create two environments. Add no secrets to either.

| Environment | Used when | Must allow |
|---|---|---|
| `pypi` | a version tag is pushed | tags matching `v[0-9]*` (for example `v0.1.0rc2`) |
| `testpypi` | someone runs the workflow by hand | the branch you will run from, usually `main` |

A rule that allows only the `main` branch blocks a tag. The deployment ref of a
tag push is the tag, not `main`. Required reviewers are optional. They delay the
upload until someone approves the deployment; they are not a credential.

## Trusted publisher on PyPI

Sign in to PyPI and open the publishing settings for the project, or add a
pending publisher if `id42-work` does not exist yet. The first successful upload
from a pending publisher creates the project. Register it there:

- Owner: `ID42Studio`
- Repository name: `id42-work`
- Workflow name: `publish.yml`
- Environment name: `pypi`

The environment name has to match the workflow exactly. PyPI checks it against
the OIDC token. Leave the API token fields empty.

## Trusted publisher on TestPyPI

TestPyPI is a separate site with its own account. Open the publishing settings
on TestPyPI and add a publisher for the same repository:

- Owner: `ID42Studio`
- Repository name: `id42-work`
- Workflow name: `publish.yml`
- Environment name: `testpypi`

Again, no API token. A publisher configured on PyPI is not valid on TestPyPI.

## Publish to PyPI

1. On `main`, set `project.version` in `pyproject.toml` to the release, and keep
   `uv.lock` in step (`uv lock`). `__version__` is read from that metadata.
2. Move the `[Unreleased]` entries in `CHANGELOG.md` to a section
   `[x.y.z] - YYYY-MM-DD` and leave `[Unreleased]` empty.
3. Merge that to `main` and wait until CI is green on the commit you will tag.
4. Tag that commit. The tag is `v` plus the version, with no extra suffix:

   ```bash
   git tag v0.1.0rc2
   git push origin v0.1.0rc2
   ```

5. The Publish workflow runs. It installs the locked dependencies, runs the
   tests, refuses the tag unless it equals `v` plus the version in
   `pyproject.toml`, builds the sdist and the wheel, and uploads them from the
   `pypi` environment.
6. Confirm the version on [pypi.org/p/id42-work](https://pypi.org/p/id42-work).
   The project page lists the files and the version. Install it in a clean
   environment and check `id42_work.__version__`.

A tag whose name is not `v` plus the version in that commit fails the check and
does not upload. If the workflow runs again for a version already on PyPI, the
upload fails: PyPI rejects a file that is already there.

## Publish to TestPyPI

Use this to rehearse the upload. It cannot publish to PyPI.

1. Open Actions, choose Publish, and run the workflow.
2. Select the branch or tag to build. The target stays `testpypi`, which is the
   only choice.
3. The job uses the `testpypi` environment and the TestPyPI publisher.
4. Confirm the version on
   [test.pypi.org/p/id42-work](https://test.pypi.org/p/id42-work).

TestPyPI also rejects a file name that was already uploaded. Pick a version that
is not there yet, or expect the run to fail on the duplicate.

The manual run does not look at a tag name. It publishes whatever version is in
the selected ref. The workflow stops if the target is anything other than
TestPyPI.
