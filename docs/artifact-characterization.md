# Open Brain artifact characterization

Status: v0.1.0 release contract

Date: 2026-09-13

## Shipping artifact

Open Brain ships a paired resource set for each supported platform:

- `open-brain-<version>-macos-arm64.tar.gz`
- `open-brain-<version>-linux-x86_64.tar.gz`
- `open-brain-graphify-<version>-macos-arm64.tar.gz`
- `open-brain-graphify-<version>-linux-x86_64.tar.gz`

Each base archive contains the `open-brain` executable and exactly three desktop plugin assets under
`obsidian-plugin/`: `main.js`, `manifest.json`, and `styles.css`. The executable bundles Python 3.14,
the foreground app, the base engine, SQLite support, Portable Brain schemas, bounded direct provider
adapters, the plugin bridge, and the Graphify client. Its module audit rejects Secure Node, server,
connector, legacy, privilege, and advanced cryptography dependencies.

Each Graphify archive contains one executable named `open-brain` for Homebrew resource staging plus
the pinned Graphify and Open Brain notices under `licenses/graphify/`. Homebrew installs that payload
as `libexec/open-brain-graphify`. It is a separate PyInstaller executable with a narrow, separately
audited dependency closure. Its protocol accepts only bounded structural Markdown extraction. It has
no Open Brain app modules, provider adapter, network configuration, Brain root, or write operation.

The frozen distribution retains dependency/version metadata, entry-point metadata, and legal notices.
Installer records such as `direct_url.json`, `RECORD`, and uv caches are omitted. Generated Python
`sysconfig` data keeps its complete scalar mapping, with references to its declared build prefixes
relocated to the frozen interpreter's `sys.prefix` and `sys.exec_prefix`. Unrelated values are
preserved; an unexpected home path outside those prefixes fails the build. This runtime is not a
Python extension-building SDK. The native self-check verifies relocated library/bin paths and the
pointer ABI before the product smoke runs.

Both macOS executables must be exactly arm64. PyInstaller applies an ad hoc signature, and the build
rejects either artifact unless `codesign --verify --strict` succeeds before the archive is hashed.
Both Linux executables are checked for an x86_64 ELF header.

## Release manifest

`open-brain-component-manifest-v1.txt` contains one exact version and a base/helper pair for each
platform. Every row binds the resource role, platform, archive digest, executable digest, filename,
and final Homebrew destination:

```text
open-brain-component-manifest-v1
version 0.1.0
resource base linux-x86_64 <archive-sha256> <executable-sha256> open-brain-0.1.0-linux-x86_64.tar.gz bin/open-brain
resource graphify linux-x86_64 <archive-sha256> <executable-sha256> open-brain-graphify-0.1.0-linux-x86_64.tar.gz libexec/open-brain-graphify
resource base macos-arm64 <archive-sha256> <executable-sha256> open-brain-0.1.0-macos-arm64.tar.gz bin/open-brain
resource graphify macos-arm64 <archive-sha256> <executable-sha256> open-brain-graphify-0.1.0-macos-arm64.tar.gz libexec/open-brain-graphify
```

Platform builds first emit one two-row component manifest. The `manifest` command in
`tools/open_brain_dev/base_native.py` combines all four final archives and rejects a missing pair,
duplicate role/platform, mixed version, noncanonical order, filename mismatch, or destination
mismatch. The manifest is the reviewable digest record; there is no attestation layer.

## Owner content audit

Before accepting those provisional manifests for publication, run the owner audit on both exact
archives with the same private denylist used for source and history checks:

```sh
uv run --frozen --python 3.14 python -m tools.open_brain_dev.release_audit \
  --root /absolute/path/to/clean-source \
  --private-denylist /absolute/path/to/private-denylist.txt \
    --artifacts /absolute/path/to/open-brain-0.1.0-linux-x86_64.tar.gz \
                /absolute/path/to/open-brain-graphify-0.1.0-linux-x86_64.tar.gz \
                /absolute/path/to/open-brain-0.1.0-macos-arm64.tar.gz \
                /absolute/path/to/open-brain-graphify-0.1.0-macos-arm64.tar.gz
```

Exit zero is required. The private audit stays separate from contributor checks and CI. A successful
build, module inventory, signature check, or manifest digest does not establish content safety.

`tools/open_brain_dev/artifact_audit.py` inspects the pinned Python 3.14 / PyInstaller 6 format as
data. It checks the outer archive, ELF or Mach-O payload boundaries, CArchive members, compressed
PYZ modules, and the nested base-library ZIP. A bounded marshal reader scans code strings, names,
filenames, bytecode, and constants without constructing or executing code objects. PYZ import names
are checked as module paths ending in `.py`; their original spelling is also content-scanned.
The owner denylist and generic content rules apply to raw and decoded data. After full module
validation and scanning, only `private-ip-address` findings for the exact name/hash pairs in the
[approved standard-library policy](audits/2026-09-09-ob1-stdlib-content-policy-proposal.md) are
suppressed at that module's location. Owner terms and every other rule remain enforced. Changed
payload hashes require separate review and approval; source/history policy is unchanged.

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
platform block uses immutable GitHub Release URLs and repeats both archive SHA-256 values. The formula
does not fetch the manifest during installation. Homebrew installs the base executable in `bin`, the
Graphify helper in `libexec`, its notices under `share/open-brain/licenses/graphify`, and the plugin
assets under `share/open-brain/obsidian-plugin`.

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

## Acceptance evidence boundary

`make contributor-check` runs repository verification, including the real plugin type/build/test
suite, then the platform-native Homebrew integration smoke. That smoke builds and audits both
executables, installs the paired resources and plugin assets through a temporary keg-only formula,
and exercises CLI, MCP, managed workspace, Graphify, and plugin staging/removal without changing an
existing product installation.

Hosted CI command-line coverage does not prove Obsidian activation, modal behavior, source
navigation, provider account access, or five-minute elapsed time. Those observations belong in a
separate exact-candidate desktop acceptance record with the app, plugin, runtime, platform,
prerequisites, network conditions, provider access mode, start/end timestamps, and result.

## Removed release surface

The repository has no curl installer, installer preflight, transactional activation, rollback,
installation receipt, custom uninstall, build attestation, clean-host matrix, VM provisioning,
fault-injection journey, timing watchdog, or metadata-only evidence bundle. Git history retains the
old implementation.

Source distributions remain a contributor build output. They are not the supported end-user
installation path and carry no release-evidence documents.
