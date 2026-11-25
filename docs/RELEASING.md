# Releasing

Releases are tagged GitHub releases, and the package is installed from the tag (`pip install "agent-crashtest[local] @ git+https://github.com/munendra07-tech/agent-crashtest@vX.Y.Z"`). It is not on PyPI yet. Only the maintainer publishes a release.

## Gates

A release is published only when all of these hold:

1. The verdict tests (`tests/test_verdict_properties.py`) pass.
2. CI is green on the release commit, every job, on Ubuntu, Windows and macOS.
3. The README's install command has been run from an empty environment, followed by `crashtest selftest`.
4. Every MCP client the docs call verified has been run, in CI or by hand, and the docs say which.
5. The docs agree with the code: the README, `docs/`, `SECURITY.md` and the CLI's help.
6. Existing CLI behaviour is preserved, or each change is in the changelog as a breaking change, with what to do about it.
7. `CHANGELOG.md` has a complete entry for the version.

## Steps

1. Set `version` in `pyproject.toml`. Add the `CHANGELOG.md` entry, headed `## X.Y.Z (prepared, not released): ...`.
2. Push to `main` and wait for CI.
3. In the release commit, date the changelog heading, and point the README's install command and Action usage (`uses: munendra07-tech/agent-crashtest@vX.Y.Z`) at the new tag. Push, and wait for CI on that commit.
4. Tag and publish from that exact commit. The release notes are the changelog entry plus install and upgrade notes:

   ```bash
   gh release create vX.Y.Z --target <full commit SHA> --title "agent-crashtest X.Y.Z: ..." --notes-file notes.md --latest
   ```

5. From an empty environment, install from the tag, check `crashtest --version`, and run `crashtest selftest` (and each scenario's selftest).

Never move or delete a published tag. Fix forward with a patch release, as 0.2.1 fixed 0.2.0's Docker build.
