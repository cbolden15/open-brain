# NW0-B3 Markdown dependency isolation

- Result: **the private Markdown component passes Mac content inspection and preserves tested
  extraction behavior.** Its observed repeat-series startup penalty is 103 ms, down from B2's
  1.615 seconds. The combined native product smoke passes. NW0 packaging remains open.
- Baseline: `e2efa31`, branch `docs/ob1-native-workspace-plan`, native macOS arm64.
- Limit: 45 minutes from remaining NW0-B, synthetic fixtures, zero model calls.
- Scope: private source patch, build specifications, executables and receipts; public documentation
  only. The shipping dependency pin, lockfile, specification and auditor remain unchanged.

This continues the [B2 packaging probe](2026-09-09-ob1-nw0-b2-frozen-graphify.md) under the
[native workspace plan](../plans/2026-09-09-ob1-native-workspace.md). B1/B2 evidence and effort
remain charged. OpenAI API, Anthropic API, Gemini API and Claude subscription remain the four launch
paths. Codex is deferred, and Claude note dispatch remains closed at C7's missing capabilities.

## What had to separate

Passing the root directly is necessary but insufficient. The first two-file prototype removes
Markdown's import back into `graphify.extract`, yet `graphify.extractors.__init__` still eagerly
loads other language extractors. Metadata sanitization imports security/path utilities, and vault
lookup imports discovery code for its skip set. That intermediate combined artifact contains 36
Graphify modules, 486 Python modules and a 12,399,063-byte archive. It still fails bounded content
inspection. It is retained as failed evidence.

The final private patch changes five existing source files and adds three modules:

| Boundary | Prototype change | Compatibility evidence |
|---|---|---|
| Root-aware extraction | Add optional keyword-only `scan_root` to Markdown extraction and link resolution; the existing extraction wrapper passes its root. | Root-aware raw output and full `extract()` output match the original source on original, relocated and edited fixtures. Omitting the root preserves relative-only behavior. |
| Language registry | Move the existing eager registry into `extractors.registry`; expose the existing extractor functions and registry lazily. | All 28 registry entries retain function identity; direct Markdown use avoids the registry's language imports. |
| Metadata | Move the existing sanitization implementation to `graphify.metadata`; security re-exports its names. | Sanitizer function ASTs are unchanged. Focused tests cover escaping, types and caps. |
| Discovery rules | Move the existing skip set to `graphify.discovery_rules`; discovery and Markdown import that shared object. | Shared-set identity is preserved; generated and dot-directory pruning is exercised. |

Direct extraction now loads seven Graphify modules: the package, extractor package, Markdown,
extractor base helpers, IDs, metadata and discovery rules. It does not load tree-sitter, the full
extractor, discovery runtime, security or path utilities. The frozen inventories confirm this
closure. Existing legal/runtime metadata handling and validation remain in place.

The source copy is explicitly patched against `graphifyy==0.9.57`, source commit
`3f82bf7f837a07fb0f7668fbdbd5662801906942`. It is not the unchanged published wheel or an accepted
upstream release. Private manifests record original/patched hashes, and the patch applies cleanly
to the original source. The original installed 30-distribution closure still verifies all 2,179
hashed RECORD files. Python 3.14.4, PyInstaller 6.22.2 and hooks 2026.7 are unchanged.

## Native artifacts and behavior

| Artifact | Executable bytes | Archive bytes | Python modules | Graphify modules | Content audit |
|---|---:|---:|---:|---:|---|
| Existing B2 baseline | 9,976,800 | 9,837,828 | 218 | 0 | Passed in B2 |
| B3 combined component | 10,034,752 | 9,892,018 | 227 | 7 | Passed |
| B3 separate component helper | 8,514,368 | 8,376,818 | 140 | 7 | Passed |

Both final component archives complete the unchanged authoritative-denylist and built-in content
inspection with zero findings. No byte/object limit, exception or source-validation setting was
relaxed. Both executables pass arm64/ad-hoc signature checks and contain no forbidden product
modules. The combined candidate passes the existing product-module audit. The helper lacks required
Open Brain modules and therefore fails that full-product audit as expected; it needs a distinct
helper/release contract before it can ship. Separate baseline/helper archives total 18,214,646 bytes.

Nine native cases exercise direct extraction, self-invocation and a separate helper across the B1
original, relocated and incremental fixtures. Frozen raw nodes/edges exactly match the original
root-aware per-file extractor. Each case stays within 16 KiB selected input/accepted output and
60 seconds, preserves source bytes, reads only selected note bodies, writes no extraction cache,
and leaves no one-file temporary files. Network and unrelated user-file access are denied by the
private Mac profile; the Python hook adds bounded runtime/source access checks. This is synthetic
Markdown evidence, not hostile-code or subscription-client confinement.

The recursion limit remains 1,000 after both import and extraction in all component cases. The
self-invocation parent also remains at 1,000. The component bypasses Graphify's full extraction
orchestrator, so its cache and canonicalization are not silently inherited. Full-orchestrator
compatibility was checked separately in source mode. The tested direct caller clears the existing
link-index cache per run; a created-target regression test verifies refresh. A concurrent or
long-lived Engine adapter still needs explicit ownership of that lifecycle.

Raw component IDs and source/target paths remain intermediate data. Engine-owned selection,
revision checks, stable-ID mapping, exclusions, alias/ambiguity handling and path-free publication
remain required. The scan root is resolution context, not a consent or filesystem authority grant.
No inferred connection or permanent note link was accepted in this structural probe.

## Startup and product smoke

Five samples per candidate were interleaved in each of two fresh-process series. Every invocation
uses a new temporary directory. Startup runs use the same instrumentation without the extraction
sandbox profile, and no build/audit runs overlap the timing series. OS cache eviction is uncontrolled.

| Startup path | First-series median | Repeat-series median | Added first / repeat |
|---|---:|---:|---:|
| Existing baseline | 4.587 s | 4.390 s | Reference |
| Combined, lazy Markdown import | 4.697 s | 4.492 s | 111 / 103 ms |
| Combined, eager Markdown import | 4.711 s | 4.456 s | 124 / 67 ms |

The observed deltas fit the 500 ms first-series and 200 ms repeat-series comparison thresholds.
These samples do not establish controlled OS-cold acceptance, Linux performance or a statistical
worst-case guarantee. B2's measured 1.615-second repeat-series penalty was from a separate run.

The existing native product smoke passes in 107.859 seconds under the unchanged 240-second cap:
self-check, capture, status, doctor, search, export, Markdown import and local MCP. This verifies
the base product with the component bundled; the Graphify structural cases above are separate.
It is not the planned capture-to-inference-to-accepted-link Obsidian vertical slice.

## Verification and the remaining frontmatter failure

The private source suite passes 21 fixture/interface checks and 62 additional compatibility,
source-integrity and refresh checks. The evidence verifier passes 73 consistency checks, covering
39 native cases/samples, hashes, source provenance, cleanup, audit receipts and native smoke.
These counts do not mean the full upstream test suite passes or NW0 packaging is approved.

The selected upstream command is `pytest tests/test_languages.py tests/test_security.py -k
'markdown or sanitize'` against the private source, with automatic plugins disabled and private
HOME/temp locations. The final run uses the existing pytest 9.1.1 test tooling, copied separately
from the runtime closure, with no network or child-process activity permitted.

**Both original and patched source report 46 passed, one failed and 420 deselected.**
`test_markdown_nested_frontmatter_survives` raises `KeyError` for `coherence_check`. The pinned
closure does not include optional PyYAML; Graphify's fallback preserves flat scalars but drops
nested structures. Per-test JUnit results match. This is an existing dependency/behavior gap,
not a regression, but it remains a failed test and must be resolved in the frontmatter contract
before a launch-capability claim. PyYAML was not added; the private frozen profile excludes optional
YAML, and the source comparison confirms its absence from the original hash-locked runtime too.

Development failures are retained: the initial private eager route kept a broad import; the smaller
helper exposed a sysconfig relocation assumption; a probe-only environment path was caught by
content inspection; and the copied test tooling initially missed its compatibility shim. The final
build removes the unnecessary probe path, makes sysconfig relocation conditional on module presence
while retaining validation when present, and uses the complete test tooling. An audit launched before
the helper existed was incomplete and was rerun after the successful build. The first verifier failed
on the upstream test; the final verifier checks original/patched parity while retaining that failure.

Full repository verification and Homebrew installation smoke were not run because repository
changes are documentation only; private candidates ran the real native builder, auditor, extraction
checks and existing native product smoke. No shipping code or dependency changed. Documentation
checks accompany the local commit. No independent agent review, push or upstream submission occurred.

## Adoption decision and next experiment

| Architecture | Assessment |
|---|---|
| Supported upstream Markdown component with explicit context and shared pure utilities | Recommended. B3 proves the local seam, but upstream acceptance, API stability, frontmatter behavior and release pin remain pending. Review legacy imports, cache ownership and the complete relevant upstream test surface before adoption. |
| Explicitly maintained, licensed component patch/fork | Technically demonstrated fallback. Requires an explicit maintenance/pin decision and the same compatibility and release gates; the private probe is not that decision. |
| Separate component helper | Strongest base-module boundary. Content inspection now passes, but duplicated runtime bytes and a helper release/lifetime contract remain costs. |
| Retain the broad unchanged Graphify dependency | Remains possible with reproducible dependency cleanup and explicit policy/budget review. B3 provides a smaller alternative without requiring those audit changes. |

Next: NW0-B4, a component-adoption and frontmatter-contract review capped at 45 minutes from
remaining NW0-B, with synthetic fixtures and zero model calls. Decide whether the supported
component API can be pursued upstream or requires an explicitly maintained patch, and establish
how nested frontmatter/aliases are preserved before adding and freezing a parser dependency.
Preserve existing audit limits. Controlled cold-start evidence, native Linux packaging, the Engine
adapter, real-provider matrix, Obsidian/UTM GUI journeys and the full NW0 gate remain open.
