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

The frozen distribution retains dependency/version metadata, entry-point metadata, and legal notices.
Installer records such as `direct_url.json`, `RECORD`, and uv caches are omitted. Generated Python
`sysconfig` data keeps its complete scalar mapping, with references to its declared build prefixes
relocated to the frozen interpreter's `sys.prefix` and `sys.exec_prefix`. Unrelated values are
preserved; an unexpected home path outside those prefixes fails the build. This runtime is not a
Python extension-building SDK. The native self-check verifies relocated library/bin paths and the
pointer ABI before the product smoke runs.

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

## Owner content audit

Before accepting those provisional manifests for publication, run the owner audit on both exact
archives with the same private denylist used for source and history checks:

```sh
uv run --frozen --python 3.14 python -m tools.open_brain_dev.release_audit \
  --root /absolute/path/to/clean-source \
  --private-denylist /absolute/path/to/private-denylist.txt \
  --artifacts /absolute/path/to/open-brain-0.1.0-linux-x86_64.tar.gz \
              /absolute/path/to/open-brain-0.1.0-macos-arm64.tar.gz
```

Exit zero is required. The private audit stays separate from contributor checks and CI. A successful
build, module inventory, signature check, or manifest digest does not establish content safety.

`tools/open_brain_dev/artifact_audit.py` inspects the pinned Python 3.14 / PyInstaller 6 format as
data. It checks the outer archive, ELF or Mach-O payload boundaries, CArchive members, compressed
PYZ modules, and the nested base-library ZIP. A bounded marshal reader scans code strings, names,
filenames, bytecode, and constants without constructing or executing code objects. PYZ import names
are checked as module paths ending in `.py`; their original spelling is also content-scanned.
The owner denylist and generic content rules apply to raw and decoded data. Packaged standard-library
runtime constants and docstrings receive the same rules as project content.

Each artifact runs in a separate worker with these limits:

| Resource | Bound |
|---|---|
| Input archive and individual expanded member | 64 MiB each |
| Total charged input, expanded members, and decoded strings | 256 MiB |
| Archive entries across all layers | 4,096 |
| Marshal bytes per record / decoded objects across the artifact | 8 MiB / 500,000 |
| Archive nesting / marshal nesting | 4 / 64 |
| ZIP central directory and native tables of contents | 1 MiB each |
| Tar extension body / total extension bytes / consecutive extensions | 64 KiB / 1 MiB / 64 |
| CPU / wall time per worker | 30 seconds / 45 seconds |
| Findings / worker response | 128 / 64 KiB |

Linux also imposes a 1 GiB address-space ceiling. macOS uses the parser's explicit byte, object,
depth, and time bounds because its address-space resource limit is unavailable. Archive data stays
in memory. Malformed data, unsupported formats, exceeded limits, timeouts, and worker failures all
produce a failing result. Native internal locations are opaque member numbers; diagnostics never
include matching content or parser exception text.

This inspector supports the current arm64 Mach-O and x86_64 ELF bundles, stored or deflated ZIP
members, and ordinary tar/gzip archives. It rejects unsupported compression, encrypted ZIPs, ZIP64,
ZIP data descriptors, and ambiguous native member layouts. It does not verify cryptographic
signatures or assess arbitrary executable behavior. The build's existing signature and module
checks remain required. Format upgrades need new fixtures and parser verification.

ZIP directories and tar extension records are counted before the standard-library readers allocate
their metadata. Tar padding must be zero and ZIP local records must cover the complete payload
region. Native archives require gzip and reject tar extensions; generic source archives may use
bounded PAX or GNU long-name records. Sparse tar metadata and binary size encodings are unsupported.

The 2 MiB source/history content limit is unchanged. Ordinary wheel and source-distribution members
also retain that limit. Large native payloads receive the separate bounded inspection described
above; their size does not grant an exemption from content rules.

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
