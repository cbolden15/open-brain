# Core v0.1 T20 artifact contract

Frozen from clean `ob-new-user/m3` baseline `2ea2e66473915e7b7864bd8023b0ca42f4eb691f`.
This contract covers local T20/scoped A15 core packaging. T21 documentation and T23
new-user acceptance remain separate fresh-thread work.

## Inventory and boundary

The existing native builder creates separate base and Graphify executables and
reproducible archive envelopes. Component manifest v1 binds platform, version,
archive SHA-256, executable SHA-256 and installation destination. The base archive
contains the existing desktop-only Obsidian plugin assets. Homebrew renders a paired
formula and its guarded temporary keg-only smoke installs and removes local artifacts
without replacing the installed product. Existing tests cover canonical manifests,
platform rejection, deterministic archive metadata and guarded cleanup.

Core v0.1 ships only core CLI/stdio MCP, Graphify and existing Obsidian assets for
macOS arm64 and Linux x86_64. Optional connector/collector source packages remain
build/regression inputs, not certified artifacts under this narrowed contract.
Desktop release, semantic helper/model, providers and T10–T18/T22 remain deferred.
Preserve T09/T19 and all existing compatibility fences and desktop tests.

## Bounded implementation

1. Bring the native smoke's exact MCP tool inventories into parity with additive
   T19 catalog discovery while retaining exact grant filtering and refusal checks.
   Exercise grant-free CLI catalog discovery before Brain creation and verify its core
   compatibility coordinates and absence of optional package/public acceptance claims.
   Invoke `brain_catalog` in an existing single-grant MCP session and require its
   authorized-operation inventory to equal that session's exact tools. Preserve the
   existing refusal to start MCP without a capability grant; do not change core behavior.
2. Require catalog implementation modules in the native module audit. Keep all
   forbidden dependencies and separate Graphify packaging unchanged.
3. Execute artifact runtime probes from disposable synthetic working directories
   with synthetic HOME, a minimal system PATH, and no PYTHONPATH. Repository fixture
   reads/build orchestration may use explicit repository paths; artifact processes
   must not depend on a developer checkout. Preserve installed plugin asset validation,
   Graphify isolation, capture/search/read/history and install/remove checks.
4. Add focused regression tests proving the changed smoke assertions detect missing
   catalog/grant drift and that runtime subprocess isolation is applied. Do not weaken
   any exact-set comparison to subset membership. No core product behavior changes.

Contract clarification after independent review: the original wording "grantless
session" conflicted with the existing MCP minimum-capability startup fence. The
clarification above preserves that fence and requires actual MCP catalog invocation,
not merely tool-list membership. The original freeze remains in private evidence.

## Verification and receipts

A bounded implementation worker runs focused release tests and relevant lint/type
checks; a separate strong reviewer independently inspects the frozen contract and
patch. Repair actionable findings and obtain independent delta review where needed.
Only the coordinator integrates and runs heavy builds, serially.

Required local gates: `make -j1 verify`, `make -j1 native-audit homebrew-smoke`
(one shared native prerequisite in that invocation), `git diff --check`, and
`actionlint .github/workflows/ci.yml`. The native/Homebrew gate uses only local
candidate archives and synthetic Brain fixtures. Its disposable reserved tap/keg and
temporary synthetic directories are test outputs; no real service is installed.

Bind private receipts to the exact implementation commit, source file hashes,
platform, base/helper executable and archive hashes, component manifest hash, and
Obsidian asset hashes. Record focused/full test counts and elapsed durations, native
module audit, actual install/removal result and review disposition. Reproducibility
means existing deterministic archive construction for identical inputs; do not claim
bit-identical independently rebuilt native executables without such evidence.

macOS arm64 local evidence closes only the local scoped A15 checkpoint. Linux x86_64
exact-candidate native/Homebrew evidence, public promotion and Obsidian GUI acceptance
remain explicit open gates. They do not authorize remote execution or publication.

## Completion

Commit only scoped code/tests, this contract, coverage and bounded workstream evidence.
Keep raw prompts/logs/reviews/usage private. Final full verification must cover the
committed implementation; later checkpoint-only documentation must be identified.
Persist and validate a private exact-HEAD handoff after the checkpoint commit, with a
clean checkout, all children released and next action **start T21 in a fresh thread**.
T23 is not started here. No push, PR, merge, release or live/private source access.
