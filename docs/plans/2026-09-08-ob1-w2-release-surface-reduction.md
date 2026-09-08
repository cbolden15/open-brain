# OB1-W2: reduce the release surface

- Status: implemented locally; Linux x86_64 Homebrew smoke awaits the first pull-request run
- Date: 2026-09-08
- Branch: `chore/reduce-release-surface`
- Decision authority: [ADR 0015](../architecture/decisions/0015-homebrew-only-distribution.md)
- Acceptance authority: [five-minute install](../acceptance/five-minute-install.md)

## Objective

Replace the custom release and installation system with the smallest distribution path that still
tests the product a user receives: one native executable per platform, GitHub Releases, a digest
manifest, and a Homebrew tap. Preserve the completed Open Brain and Secure Node product work.

This is a release-surface reduction only. Search changes, import, schema migrations, MCP, and the
expanded contributor path remain separate ordered workstreams.

## Step 0 decisions

1. Linux has no curl installer. Supported users already have Homebrew, including Linuxbrew.
2. Notarization is not required for the supported Homebrew path. Homebrew command-line downloads do
   not normally create the quarantine state used by Gatekeeper's first-launch assessment. The
   macOS arm64 executable keeps the ad hoc signature required to execute on Apple Silicon.
3. The old Phase 4 release stack was merged. The interrupted OB1-W2 change contained documentation
   only. Completed OB1-W0 and OB1-W1 runtime behavior stays; the superseded OB1-W2 documentation and
   merged release machinery are removed rather than completed.

## Removed

- Both custom installers, preflight, transaction, rollback, receipt, and uninstall code.
- Attestation, release-candidate, compatibility, policy, and evidence-assembly code.
- The Phase 4 clean-host, timing, fault, reinstall, VM, and lifecycle harness.
- Notarization, DMG, and Developer ID release work for the unsupported direct-download path.
- CI and package-build configuration used only by those systems.

Git history is the archive. No replacement comments, compatibility copies, or `old/` directory are
kept.

## Replacement

`tools/open_brain_dev/base_native.py` owns the remaining release logic. It builds and audits the
one-file executable, runs the local product journey, writes the platform archive and manifest, and
renders the Homebrew formula. The formula repeats the final manifest digest and binds macOS to
arm64 and Linux to x86_64 through Homebrew requirements.

`.github/workflows/ci.yml` is the only workflow. It has two jobs: `ubuntu-latest` with an explicit
x86_64 assertion and `macos-latest` with an explicit arm64 assertion. Each builds the executable,
installs it from a temporary local tap, captures a record, searches it, verifies a Portable export,
checks status and doctor, then exits without a background runtime.

Publication stays owner-operated. After both jobs pass for an exact version, the two archives and
combined manifest can be uploaded to an immutable GitHub Release and the generated formula can be
committed to the external tap. This workstream does not push, publish, create a release, or change
repository settings.

## Product and upgrade boundaries

The native executable keeps the existing default CLI and direct SQLite/filesystem model. It admits
no Secure Node, server, connector, legacy, or advanced cryptography module. The record-level
encryption seam remains in [ADR 0014](../architecture/decisions/0014-shared-record-import-envelope.md):
Portable records are the upgrade input, and Secure Node wraps the same shared body rather than
copying the Open Brain SQLite database.

## Verification and exit

Run:

```sh
make verify
make homebrew-smoke
actionlint .github/workflows/ci.yml
git diff --check
```

Local verification on macOS 26.3 arm64 passed on 2026-09-08:

- `make verify`: Ruff passed, strict MyPy checked 565 source files, 3,374 tests passed, and all
  three source/wheel packages built.
- Isolated dependency checks proved that the contributor environment contains the Secure Node test
  dependencies while the native-build environment contains the base app and build tools only.
- `make homebrew-smoke`: the one-file ad hoc-signed arm64 executable installed from the temporary
  tap and passed capture, search, verified export, status, doctor, and self-check.
- `actionlint`, `git diff --check`, and `uv lock --check` passed. The temporary formula, tap, trust
  entry, and working directory were absent afterward.

The workstream closes after the same commit passes both CI jobs. Local macOS verification cannot
stand in for the Linux x86_64 job. Do not start OB1-W3 search work before that gate is green.
