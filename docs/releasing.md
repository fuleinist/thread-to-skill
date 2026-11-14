# Releasing

The release workflow publishes to PyPI through Trusted Publishing. No token is stored in the repository.

## One-time setup

1. Create the project on PyPI and on TestPyPI. Reserve the name `thread-to-skill` on both.
2. Add a trusted publisher on PyPI:
   - Owner: `fuleinist`
   - Repository: `thread-to-skill`
   - Workflow: `release.yml`
   - Environment: `pypi`
3. Add the same publisher on TestPyPI, with the environment `testpypi`.
4. Create the GitHub environments `pypi` and `testpypi`. Add a required reviewer to `pypi` if you want a manual gate.

Until these steps are done, the publish jobs fail with an authentication error. The build job still runs, so the package and the checks stay visible.

## Dry run on TestPyPI

1. Open the Actions tab and select the `release` workflow.
2. Choose `Run workflow` on `main`.
3. Keep the target at `testpypi` and start the run.

The run builds the sdist and the wheel, checks the metadata, installs the wheel in a clean virtual environment, and uploads to TestPyPI. Confirm the result with:

```bash
python -m pip install --index-url https://test.pypi.org/simple/ thread-to-skill
t2s --version
```

## Publish a release

1. Set the version in `pyproject.toml`.
2. Move the `Unreleased` entries in `CHANGELOG.md` under the new version heading.
3. Commit both files on `main`.
4. Create and push an annotated tag:

```bash
git tag -a v0.2.0 -m "thread-to-skill 0.2.0"
git push origin v0.2.0
```

The tag starts the workflow. The version check stops the run when the tag and `pyproject.toml` disagree. After the upload, the workflow creates the GitHub release when it is missing and attaches the sdist and the wheel.

Do not publish from a local machine. The workflow is the only release path.