# Open Brain artifact characterization

Status: target release contract; artifacts are not published

Date: 2026-09-08

## Shipping artifact

Open Brain ships one PyInstaller one-file executable for each supported platform:

- `open-brain-<version>-macos-arm64.tar.gz`
- `open-brain-<version>-linux-x86_64.tar.gz`

Each archive contains only the executable named `open-brain`. The executable bundles Python 3.14,
the default app path, the base engine, SQLite support, and Portable Brain schemas. Its module audit
rejects Secure Node, server, connector, legacy, and advanced cryptography dependencies.

The macOS build must be exactly arm64. PyInstaller applies an ad hoc signature, and the build rejects
the artifact unless `codesign --verify --strict` succeeds before the archive is hashed. The Linux
build checks the ELF header for x86_64.

## Release manifest

`open-brain-release-manifest-v1.txt` contains one exact version and one row per artifact:

```text
open-brain-release-manifest-v1
version 0.1.0
artifact linux-x86_64 <64-lowercase-hex-sha256> open-brain-0.1.0-linux-x86_64.tar.gz
artifact macos-arm64 <64-lowercase-hex-sha256> open-brain-0.1.0-macos-arm64.tar.gz
```

Platform builds may first emit a one-row manifest. The `manifest` command in
`tools/open_brain_dev/base_native.py` combines the two final archives and rejects duplicate
platforms or mixed versions. There is no attestation or evidence-assembly layer.

## Homebrew

The `formula` command reads the manifest and renders the formula for the external Homebrew tap. Each
platform block uses the immutable GitHub Release URL and repeats the manifest SHA-256. The formula
does not fetch the manifest during installation. Homebrew verifies the archive digest before its
`bin.install "open-brain"` step.

CI renders the same formula with a local archive URL, installs it using Homebrew, and runs the product
smoke. The public tap is updated only after both final platform archives exist and the combined
manifest is final.

## macOS distribution decision

The supported install path is Homebrew. Command-line downloads used by Homebrew do not normally add
the quarantine attribute that triggers first-launch Gatekeeper assessment. Apple Silicon still
requires a valid code signature, so ad hoc signing remains mandatory.

Developer ID distribution, notarization, stapling, DMGs, and Gatekeeper acceptance are not part of
this release. If browser or Finder download becomes a supported path, that assumption changes and the
proper Developer ID plus notarization path must be designed before publication.

## Removed release surface

The repository has no curl installer, installer preflight, transactional activation, rollback,
installation receipt, custom uninstall, build attestation, clean-host matrix, VM provisioning,
fault-injection journey, timing watchdog, or metadata-only evidence bundle. Git history retains the
old implementation.

Source distributions remain a contributor build output. They are not the supported end-user
installation path and carry no release-evidence documents.
