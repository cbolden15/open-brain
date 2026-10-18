# T5.3 report: desktop bundles a flattened one-folder base

STATUS: DONE_WITH_CONCERNS (one design note below).

## Commit
- see `git log -1` on goal/offline-semantic-search (feat(desktop): bundle a flattened copy of the one-folder base)

## What changed
- New `packages/desktop/scripts/flatten-base.mjs`: inventories the base folder (lstat, readlink, SHA-256),
  resolves links (relative, contained, <= 4 hops, no cycles, no absolute/backslash/dot/empty parts),
  copies every link as a regular file or directory, then verifies the output tree.
- `prepare-runtime.mjs` no longer refuses `_internal`. It checks the EXE hash against the manifest,
  replaces only `runtime/` and the desktop manifest in the output (keeps `.gitkeep`), removes `runtime/`
  on any failure, and writes core at `runtime/libexec/open-brain/open-brain`.
- Rust (`main.rs`, `proof.rs`, `runtime.rs`) reads the core at the new path. `docs/architecture.md`,
  gotcha DESKTOP-002 (resolved) and the CLAUDE.md known-failure note updated.

## Evidence
1. TDD: `uv run --frozen pytest -q packages/desktop/tests/test_prepare_runtime.py` red first (one-folder
   fixture refused), then 44 passed. Covers flattening (file and directory links, exec bits, no symlinks
   left), refusal of escape/absolute/backslash/dot/missing/cycle/self/depth-5, four-hop acceptance, stale
   output replaced, and verifier failures (hash mismatch, extra, missing, symlink left).
2. `make desktop-test` rc=0 (typecheck, vitest, build, pytest, cargo fmt, 26 Rust tests).
   `make desktop-native` (includes desktop-runtime) rc=0, codesign verify "valid on disk"
   (log: `build/t53-desktop-native.log`).
3. Bundled EXE: `Open Brain Desktop.app/Contents/Resources/binaries/runtime/libexec/open-brain/open-brain --version`
   prints `open-brain 0.1.0` (HOME set to a temp dir). `--d0-proof-json` also ran (temp HOME):
   captureStatus captured, graphifyStatus ok, matchedSearchResults 1. 0 symlinks in the bundle.
4. `git diff --check` clean.

## Size
Base folder 24.9 MB on disk (links are tiny) becomes 40.9 MB flattened (Python.framework duplicated via
Versions/Current and the file links). Whole `runtime/` in the app is 50.3 MB (includes Graphify).

## Open items
- The v1 build writes no base inventory file (only the harness and v2 resources carry one), so the script
  derives the inventory from the source folder and verifies the copy against it, rather than a
  pre-published one. The EXE is additionally pinned to the manifest. If the owner wants a pre-published base
  inventory as the authority, that belongs with the v2 release path (T5.2 area).
- Not run: `make verify`, `make contributor-check` (per brief).

## T5.3b: review fixes (APPROVE_WITH_FIXES, 5 findings)
1. Authority: `prepare-runtime.mjs` keeps the base `archiveSha256`/`filename`, hashes
   `<manifest dir>/<filename>` against it, builds the member map from the archive's USTAR headers
   (`readArchiveInventory`), and verifies the copy against it. A `dist/` that differs from the release
   is refused (tests: altered `base_library.zip`, altered framework `Python`, tampered archive, wrong EXE).
   Extra files in `dist/` are not shipped.
2. Links outside `_internal/` (link member or resolved target) are refused; test `outside-internal`.
3. Directory link to its own ancestor (`_internal/a/loop -> ..`) is tested and refused as a cycle.
4. DESKTOP-002 records the archive authority, the `_internal/` rule and owner-only modes.
5. `installed_layout.py` docstring updated (bin branch kept; other callers still use it).
Evidence: `test_prepare_runtime.py` 50 passed; `test_installed_layout` 10 passed; `make desktop-native`
(runs desktop-test and desktop-runtime) rc=0, codesign valid (`build/t53b-desktop-native.log`);
bundled EXE `--version` prints `open-brain 0.1.0`, 0 symlinks in the bundle; `git diff --check` clean.
