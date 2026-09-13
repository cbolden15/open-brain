# Codebase vs plan audit: OB1-W4 Markdown import

**Date:** 2026-09-09

**Plan:** `docs/plans/2026-09-08-ob1-product-completion.md`, OB1-W4

**Detailed contract:** `docs/import.md`

**Product scope:** Five-minute Open Brain only

**Mode:** strict

**Commits reviewed:** `54b7d0e` through the current W4 working tree, including documentation
commits `d427de1` and `80e92b5`

**Codebase root:** repository root

## Executive summary

- **Completion:** 100% (12 of 12 implementation requirements complete)
- **Ship readiness:** READY for the pull-request gate
- **High-risk issues:** None remain after repair and rereview
- **Merge condition:** Both exact-head GitHub Actions jobs must pass before merge

W4 adds one recursive Markdown and Obsidian import path to the default local product. It preserves
source bytes through the existing immutable capture model, keeps absolute source paths in operational
SQLite only, and projects only each path's active revision into search. Reruns are idempotent. Changed
files append revisions, missing files leave search only after a complete scan, and restored content
reactivates its existing capture.

Three concurrent read-only reviews examined the CLI, filesystem boundary, and state/Portable Brain
lifecycle. Their initial findings covered early SIGINT handling, non-pollable prompt input,
deterministic bounded enumeration, normalized collisions, named filesystem races, timing wording, and
a proposed change to the frozen Portable Brain v1 schema. The repaired tree keeps Portable v1
unchanged, separates canonical review state from source confidence, and has no remaining P0 through P3
findings in the final rereviews.

## Requirement audit

### R1: Document the import contract before runtime implementation

- **Status:** COMPLETE
- **Evidence:**
  - `docs/import.md:7` defines the command, scope, and source-tree invariants.
  - `docs/import.md:71` defines root pinning, identity, overlap, and revalidation.
  - `docs/import.md:149` defines deterministic paths, eligibility, collision behavior, and bounds.
  - `docs/import.md:422` defines SIGINT safe points and completion-boundary behavior.
  - `docs/privacy-model.md` and `docs/threat-model.md` cover imported local-file content and inert
    markup in the committed documentation gate.
- **Notes:** Documentation commits preceded the runtime changes.

### R2: Pin one root identity and reject unsafe overlap or replacement

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/markdown_import_fs.py:174` independently resolves
    and descriptor-opens the root twice.
  - `packages/engine/src/open_brain_engine/engine/markdown_import.py:218` compares the candidate with
    the Brain and every registered root by path and filesystem identity.
  - `packages/engine/src/open_brain_engine/engine/markdown_import.py:259` revalidates the selected
    identity before each mutation boundary.
  - `packages/app/tests/integration/engine/test_markdown_import.py:271` covers identity aliases and
    moved roots; lines 294, 315, and 337 cover overlap and replacement boundaries.
- **Notes:** A macOS case-alias test runs on a case-insensitive volume and passed locally.

### R3: Enumerate deterministically without following filesystem links

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/markdown_import_fs.py:223` performs iterative,
    descriptor-relative traversal.
  - `packages/engine/src/open_brain_engine/engine/markdown_import_fs.py:511` collects raw names with
    incremental interruption and a global discovery bound before sorting.
  - `packages/engine/src/open_brain_engine/engine/markdown_import_fs.py:408` reopens candidates with
    no-follow flags and verifies parent and file metadata before and after reading.
  - `packages/app/tests/integration/engine/test_markdown_import_fs.py:375` and line 404 cover root and
    child-directory open races; lines 284, 307, and 336 cover candidate type and mid-read changes.
- **Notes:** NFS, FUSE, synchronized roots, and hostile same-user mutation remain explicitly outside
  the first-release guarantee.

### R4: Enforce exact aggregate and per-file bounds before capture

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/markdown_import_fs.py:90` freezes the production
    visit, selected-file, aggregate-byte, and per-file values.
  - `packages/engine/src/open_brain_engine/engine/markdown_import_fs.py:343` advances visited,
    selected, and aggregate counters at one deterministic classification safe point.
  - `packages/app/tests/integration/engine/test_markdown_import_fs.py:40` through line 130 covers each
    exact limit, one-past refusal, simultaneous gates, and the non-bypassable per-file limit.
- **Notes:** `--allow-large-vault` bypasses only total enumeration limits.

### R5: Normalize relative paths and classify every unsafe input

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/markdown_import_fs.py:542` validates UTF-8 and NFC
    path components while rejecting control and format characters.
  - `packages/engine/src/open_brain_engine/engine/markdown_import_fs.py:356` gives normalized path
    collisions precedence over candidate and link outcomes.
  - `packages/app/tests/integration/engine/test_markdown_import_fs.py:157` covers opaque invalid-path
    tokens; lines 196 and 222 cover collisions among regular files, symlinks, and hardlinks.
- **Notes:** Filesystem-dependent invalid-byte and NFC/NFD cases are skipped on the local macOS volume
  and are expected to execute on Linux CI.

### R6: Preserve exact bytes, bounded titles, and unverified provenance

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/markdown_import.py:466` reads a stable candidate and
    submits its exact bytes through `FilePayload` with `text/markdown`.
  - `packages/engine/src/open_brain_engine/engine/markdown_import.py:875` derives a bounded inert title
    from frontmatter, heading, or filename.
  - `packages/engine/src/open_brain_engine/engine/markdown_import.py:903` constructs the fixed
    non-owner, capture-only import context.
  - `packages/app/tests/integration/engine/test_markdown_import.py:74` verifies exact exported blobs
    plus unknown origin, automation-absent context, and unverified trust.
- **Notes:** Markup is never executed or followed.

### R7: Make reruns, updates, missing paths, and restoration durable

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/markdown_import.py:545` reserves each root, path, and
    digest revision with a deterministic delivery binding.
  - `packages/engine/src/open_brain_engine/engine/markdown_import.py:656` activates one revision per
    logical path without rewriting prior captures.
  - `packages/engine/src/open_brain_engine/engine/markdown_import.py:756` finalizes missing paths only
    after a complete scan.
  - `packages/app/tests/integration/engine/test_markdown_import.py:74` covers unchanged reruns,
    changes, restoration, missing state, search, and export history.
- **Notes:** Failed observed paths retain their active revision while unrelated missing paths can
  finalize.

### R8: Recover safely from reservations and interrupted work

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/markdown_import.py:829` permits the reserved delivery
    namespace only for a matching pending revision and exact capture request.
  - `packages/engine/src/open_brain_engine/engine/markdown_import.py:857` suppresses pending and
    inactive import captures from the live source projection.
  - `packages/app/tests/integration/engine/test_markdown_import.py:173` covers capture-before-activation
    recovery; line 486 covers complete request binding and retry.
  - `packages/app/tests/integration/engine/test_markdown_import.py:599` through line 751 covers the
    interruption safe points and post-finalization completion behavior.
- **Notes:** Completed per-file commits survive a pre-finalization interruption.

### R9: Expose a bounded, daemonless CLI contract

- **Status:** COMPLETE
- **Evidence:**
  - `packages/app/src/open_brain/services/local_entrypoints.py:117` installs the import SIGINT handler
    before root selection and Brain opening.
  - `packages/app/src/open_brain/services/local_entrypoints.py:338` wires confirmation, progress, and
    interruption into the engine task.
  - `packages/app/src/open_brain/services/local_entrypoints.py:370` polls interactive input and
    cancels a non-pollable TTY rather than blocking.
  - `packages/app/src/open_brain/services/local_entrypoints.py:402` renders bounded failure-first
    summaries without absolute source roots or content.
  - `packages/app/tests/integration/services/test_markdown_import_cli.py:83` through line 513 covers
    confirmation, reruns, errors, SIGINT, redaction, entry caps, and human output.
- **Notes:** A first non-interactive import of a new root requires `--yes`; registered reruns do not.

### R10: Preserve Portable Brain v1 and rebuild the correct search state

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/reconciliation.py:278` rebuilds source search rows
    only for active import revisions while retaining every capture for canonical provenance.
  - `packages/engine/src/open_brain_engine/engine/search_projection.py:94` separates canonical
    frontmatter review state from source-derived search trust.
  - `packages/app/tests/integration/engine/test_markdown_import.py:386` exports, validates, imports,
    reopens, and rebuilds a reviewed canonical page derived from an unverified imported capture.
  - `packages/engine/src/open_brain_engine/portable/schemas/v1/` and the frozen schema-catalog digest
    are unchanged by W4.
- **Notes:** Portable canonical Markdown records `reviewed`; the linked capture and search result
  remain `unverified`.

### R11: Exercise the installed binary with a public-safe fixture

- **Status:** COMPLETE
- **Evidence:**
  - `examples/markdown-fixture/` contains only synthetic regular files and an Obsidian metadata
    directory.
  - `tools/open_brain_dev/base_native.py:232` runs the import smoke after the existing timed journey.
  - `tools/open_brain_dev/base_native.py:612` proves first import, unchanged rerun, nested search,
    Obsidian exclusion, exact exported bytes, and unverified provenance.
  - `tests/security/test_release_audit.py:95` rejects an unsafe committed fixture type or content.
- **Notes:** Local macOS arm64 `make homebrew-smoke` passed and removed its temporary tap and formula.

### R12: Keep the default product and verification boundary small

- **Status:** COMPLETE
- **Evidence:**
  - `packages/engine/src/open_brain_engine/engine/local.py` exposes the import task through the existing
    `EngineTaskSet`; no daemon, listener, importer framework, or network path was added.
  - `tests/release/test_native_distribution.py:53` requires the two import modules in the native
    artifact while preserving the default dependency boundary.
  - `docs/acceptance/five-minute-install.md:43` keeps the five-minute timer ending at verified export;
    the W4 smoke remains post-journey.
  - Local `make verify` passed Ruff, MyPy, 3,462 tests with 5 filesystem-dependent skips, and all
    three source package builds. `actionlint .github/workflows/ci.yml`, `git diff --check`, and the
    macOS arm64 Homebrew smoke also passed.
- **Notes:** The exact W4 head still requires both remote CI jobs before merge.

## Critical gaps

None in the current implementation.

## Integration issues

None found. The command is wired through the existing local CLI, `EngineTaskSet`, writer lease,
capture path, active search projection, Portable export, native build, Homebrew smoke, and CI job
definitions.

## Quality concerns

No unresolved P0 through P3 findings remain. The only platform-dependent local skips cover filename
behaviors that the Linux CI job is expected to execute.

## Recommended fixes

Push the frozen W4 candidate, require both exact-head CI jobs to pass, then merge it into
`goal/open-brain-five-minute-install`. Do not begin OB1-W5 before that merge.

## Optional enhancements

None in W4. Additional import formats, hostile-filesystem guarantees, root relocation, destructive
pruning, inactive-revision search, embeddings, and schema redesign remain deferred.
