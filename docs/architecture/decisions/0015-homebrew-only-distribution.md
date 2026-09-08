# ADR 0015: Homebrew-only Open Brain distribution

Status: accepted

Date: 2026-09-08

## Context

The first OB1-W2 design put more code into release and installation control than into the default
second-brain experience. It duplicated work already performed by Homebrew and GitHub Releases.

Three questions controlled the reduction:

1. Linux does not need a separate curl path because supported users are assumed to have Homebrew.
2. The supported Homebrew path does not normally add macOS quarantine. Apple DTS documents that
   command-line tools such as `curl` do not opt in to quarantine and that `tar` and `unzip` do not
   propagate it ([Quarantine attribute and archive utilities](https://developer.apple.com/forums/thread/706442)).
   Notarization is therefore not required for this path, although Apple Silicon code signing
   remains required.
3. The old Phase 4 release stack is merged history, while the interrupted OB1-W2 work is documentation
   only. The completed Open Brain bootstrap and local journey remain useful.

## Decision

Homebrew is a prerequisite on supported macOS and Linux hosts. The only supported install and
uninstall commands are:

```sh
brew install cbolden15/tap/open-brain
brew uninstall open-brain
```

The public product repository is `cbolden15/open-brain`. Its Homebrew formula lives in the separate
`cbolden15/homebrew-tap` repository, which Homebrew addresses as `cbolden15/tap` under its
[tap naming convention](https://docs.brew.sh/Taps#repository-naming-conventions).

GitHub Releases stores one versioned archive per platform and a small combined manifest. Each archive
contains one native executable. The external tap formula is generated from the manifest, pins the
same immutable asset URLs and SHA-256 values, and lets Homebrew own download, verification,
installation, upgrade, and uninstall.

The macOS executable is built on arm64 and must pass ad hoc signature verification before archive
hashing. There is no Developer ID, notarization, stapling, DMG, attestation, custom installer,
transaction, receipt, rollback, clean-host matrix, or release-evidence assembly.

CI has two runners. The macOS arm64 and Linux x86_64 jobs each install the just-built archive through
a temporary Homebrew formula, capture a record, search for it, produce a verified export, and exit
without a background runtime.

## Consequences

Homebrew installation is no longer a clean-operating-system promise. It is a clean Open Brain state
on a supported machine where Homebrew already works.

The formula digest is the installation trust check. The release manifest is the source used to
render the formula, not a second document fetched dynamically during installation.

An unsupported architecture fails through the platform artifact or Homebrew metadata rather than a
custom preflight script. Open Brain data stays outside the Homebrew prefix and survives uninstall.

If direct browser, Finder, curl, package, or app-bundle distribution becomes supported later, this
decision must be revisited. Browser-delivered macOS software should use Developer ID signing and
notarization rather than relying on the Homebrew quarantine behavior.

This change affects distribution only. The shared record model and record-level protected-envelope
seam in [ADR 0014](0014-shared-record-import-envelope.md) remain unchanged.
