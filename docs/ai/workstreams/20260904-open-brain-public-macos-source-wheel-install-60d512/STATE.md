# Workstream State

- ID: `20260904-open-brain-public-macos-source-wheel-install-60d512`
- Repo root: `<repo-root>`
- Remote identity SHA-256 fingerprint: `cb5e9cd7ac71c16e5109717b4bc07f01aed1bbda2b18a99bfbc76f7bd98245bc`
- Worktree: `<repo-root>`
- Branch: goal/open-brain-phase4-p4c
- Objective: Prove and document macOS source and wheel installation for v0 without a DMG
- Created date: 2026-09-04

## Active milestone

- Status: complete; implementation and repository verification passed
- Starting head: `72fbda0b1b92bafe8ff1b0da7fcb16461fc3981e`
- Supported v0 runtime: Python 3.14
- macOS path: versioned source checkout or matching app and engine wheels
- Linux path: checksummed native archive bundling Python 3.14
- Deferred: signed and notarized macOS DMG
- Preserved: frozen P4-W5 tooling and readiness snapshot

## Decisions

- Expose the required foreground process as `open-brain daemon`; do not require users to discover
  uv's private environment interpreter or invoke an internal module.
- Keep Python 3.12 only in historical P4 native replay metadata and commands. It is not a supported
  v0 runtime.
- Keep supervisor installation outside this milestone. The verified source/wheel path runs the
  daemon in the foreground and performs no host-service mutation.

## Evidence

- The daemon-launch contract failed before implementation because no public launcher existed, then
  passed after adding the CLI command.
- A clean offline Python 3.14 uv-tool installation from only the app and engine wheels initialized
  a synthetic Brain, started the foreground daemon, created a space, captured text, and retrieved
  the result.
- An isolated Python 3.14 source installation initialized a synthetic Brain, started the same
  foreground daemon, and returned live status.
- Focused result: 44 tests passed; Ruff, strict MyPy, actionlint, JSON validation, lock validation,
  and diff integrity passed before final verification.
- Repository result: `make verify` passed with Ruff, strict MyPy across 549 source files, 3,252
  tests, six rebuilt Python distribution artifacts, and artifact-policy verification.
- Preservation result: the immutable readiness snapshot remains
  `753a1635aa2be81f3ebe6b3723dbc8e46a6d6aa46f1b39003b9d9c39da769d1b`; the P4-W5
  contracts, native toolchain declaration, and native build implementation have no diff from the
  starting head.
