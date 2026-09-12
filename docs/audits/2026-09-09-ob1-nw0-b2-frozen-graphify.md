# NW0-B2 frozen Graphify packaging probe

- Result: **frozen Markdown extraction works on this Mac; the tested Graphify candidates fail
  packaging acceptance.** Content inspection and measured startup overhead remain blockers.
- Baseline: `72dd1fc`, branch `docs/ob1-native-workspace-plan`, native macOS arm64.
- Limit: 45 minutes from remaining NW0-B, synthetic B1 fixtures, zero model calls.
- Scope: private environments, build specifications, executables, archives, and receipts; public
  documentation only. The shipping lockfile, native specification, audit code, and dependencies
  remain unchanged. Nothing was pushed or published.

This extends the [B1 closure/import evidence](../plans/2026-09-09-ob1-native-workspace-nw0.md#nw0-b1-result-closureimport-works-raw-output-needs-the-engine-adapter).
It preserves the [native workspace scope](../plans/2026-09-09-ob1-native-workspace.md), including
the three direct API paths and Claude subscription. Codex remains deferred. Structural extraction
is not semantic inference or a completed five-minute Obsidian journey.

## Build and functional scope

The disposable environment reused B1's hash-locked 30-distribution closure and synthetic dataset.
Graphify remains `graphifyy==0.9.57`, corresponding to source commit
`3f82bf7f837a07fb0f7668fbdbd5662801906942`. The wheel digest remains
`f35c86410e7d92ace69a50ac8dbed568903c880482c656f43437ca657fee8c37`.
The toolchain uses Python 3.14.4, PyInstaller 6.22.2 and hooks 2026.7. Installed dependency RECORD
hashes and versions are checked separately from the frozen artifact inventory.

Five executables were built from private derivatives of the existing native specification:

| Candidate | Contents | Executable bytes | Archive bytes | Python modules |
|---|---|---:|---:|---:|
| Baseline | Existing Open Brain entry point and native configuration. | 9,976,800 | 9,837,828 | 218 |
| Broad combined | Open Brain plus Graphify extraction, installed dependency roots, and Graphify package data. | 26,280,528 | 25,181,744 | 1,028 |
| Broad helper | Graphify probe with the same broad dependency collection. | 25,052,544 | 23,956,438 | 966 |
| Markdown combined | Open Brain plus the required Graphify extraction path, without forcing unused dependency roots or unrelated package data into the bundle. | 12,880,704 | 12,726,046 | 497 |
| Markdown helper | The same narrower extraction path in a separate executable. | 11,652,480 | 11,500,782 | 435 |

The broad candidate intentionally tested collecting the installed dependency roots, including
language parsers, NumPy and NetworkX. B1's actual Markdown run loaded Graphify and tree-sitter only.
The narrower specification excludes those unused roots and retains runtime/legal metadata for the
used distributions. It still loads the unchanged `graphify.extract` entry and its 46 Graphify modules.
This is a Markdown structural-extraction candidate, not a frozen distribution of every Graphify CLI,
language parser, visualization, model adapter, or optional feature.

The existing runtime-metadata filtering and sysconfig relocation are retained. No dependency
source, audit limit, content exception, or inspected executable was patched to obtain a pass.
The separate helper's test archive is inspected independently; it is not an approved two-executable
product archive. Baseline plus Markdown-helper archives total 21,338,610 bytes, reflecting duplicated
runtime costs that a shipping layout would need to address explicitly.

## Extraction and process boundaries

Each native extraction receives only the isolated eligible snapshot: six selected notes totaling
691 bytes in the original case. The adapted B1 probe calls
`extract(selected, root=snapshot, cache_root=private_cache, parallel=False)` and retains its input,
output, source-integrity, page mapping, and Python I/O assertions. The original relocated and
incremental fixtures are copied without changing B1's evidence.

| Boundary | Executed behavior | Implication |
|---|---|---|
| Same-process extraction | Combined executable imports Graphify and extracts directly. Its recursion limit becomes 10,000. | Lazy import delays global changes until use; it does not isolate them from a resident engine. |
| Self-invoked helper | Combined executable starts its private extraction mode and waits for the child. Parent recursion remains 1,000 while the child reaches 10,000. | Isolates the observed globals while retaining one executable. The parent archive still contains Graphify and pays its unpacking cost. |
| Separate helper | Independent frozen helper processes the same snapshot. The baseline inventory contains zero Graphify modules. | Stronger module separation. Both artifacts, their invocation/lifetime, and the changed release layout need acceptance. |

Native extraction runs under a private Mac profile denying network, unrelated user-file data, and
writes outside the probe root. B1's Python hook additionally denies network/subprocess activity
inside extraction, out-of-runtime reads, and writes outside the selected cache. These controls are
specific to this synthetic structural workload; they do not prove Claude or hostile-native-code
confinement. The one-file bootloader and explicit self-invocation require process creation.

Cold Graphify-cache, cache reuse, relocated-root and incremental-edit outputs are compared to B1,
normalizing only the known snapshot prefix in upstream dangling IDs. Accepted page mappings and
qualified/cross-folder links must match; the incremental link must remain visible. All B1 adapter
requirements remain: do not publish arbitrary ambiguity resolution, unresolved aliases, or
path-derived dangling IDs as Brain links. No semantic edge is fabricated and no permanent note link
is accepted in this probe.

Warm cache hits read no note bodies through Graphify's audited read path in the targeted cases;
the probe still hashes the staged source before and after extraction and verifies the returned
graph against B1. Cache reuse therefore does not replace Engine-owned revision/eligibility checks.
The recursion change occurs during extraction, not merely on import.

## Startup measurements and limits

Five samples per candidate were recorded in each of two fresh-process series, using new private
temporary directories. The second series reuses the same executable after prior launches.
OS cache eviction was not controlled. These are **first/repeat process series, not validated
OS-cold/OS-warm medians**. The plan's true cold-start requirement therefore remains unproven.

The narrower comparison ran the existing native self-check without the extraction sandbox wrapper.
The eager variant imports Graphify before that same self-check. These measurements include one-file
startup and teardown on this development Mac; they are not Linux, emulated, GUI, or installation timings.

| Entry path | First series median | Repeat series median | Repeat increase over baseline |
|---|---:|---:|---:|
| Baseline self-check | 4.233 s | 4.299 s | — |
| Markdown combined, lazy | 5.804 s | 5.914 s | 1.615 s |
| Markdown combined, eager | 6.250 s | 6.115 s | 1.816 s |

Lazy loading avoids the eager import work, but the added archive still incurs one-file startup cost.
The measured lazy increase exceeds both the planned 500 ms cold and 200 ms warm additional-startup
budgets; it does not establish a shipping performance pass. Host/cache effects are not eliminated
by this comparison. The separate-helper design leaves the base executable unchanged, but helper
launch and full first-use latency still count toward the five-minute outcome.

The broad comparison also completed its baseline/lazy/eager startup series under the extraction
profile. Those instrumented samples remain diagnostic evidence and are not mixed into the table.
Its larger repeated-extraction batch reached the 300-second harness cap after 48 completed cases.
No owned frozen executable remained afterward. The narrower batch completed the required startup
series, then was deliberately stopped after 41 cases; its cancellation handler stopped and waited
for the current process group. Nine targeted cache/relocation/edit cases completed afterward.
Optional extraction repetitions were curtailed; no complete 69-case benchmark is claimed.

## Artifact audits

All five executables passed arm64 and strict ad-hoc signature checks. None contained the existing
forbidden product modules. Both combined executables passed the base module-boundary audit.
The helpers intentionally lack required Open Brain modules, so the base-product audit rejects
them; a separate helper needs its own explicit module contract, not a weaker base audit.

The existing bounded content auditor ran with the authoritative private denylist located by its
previously reviewed path identity. The path and terms are absent from public documents and probe
output. The baseline archive passed. Both broad and both Markdown Graphify archives failed.

The narrow candidates retain three observed path-finding locations:

| Location | Finding and interpretation |
|---|---|
| `graphify.extractors.fortran` | A home-path example in upstream documentation remains in compiled module constants. |
| `graphify.paths` | Platform-path examples in upstream documentation remain in compiled module constants. |
| `tree_sitter/_binding.cpython-314-darwin.so` | The installed wheel binary contains a home-path match. Its build provenance needs separate investigation. |

These findings are not evidence that a user's notes were bundled. They are still failures under
the current content policy. Broad collection also brings findings from unrelated package data and
additional dependency modules; narrowing collection removed those observed locations from the candidate.

Inspection stops before completion at the existing 500,000-object budget. A private diagnostic
using the unchanged scanner found the combined candidate at 499,997 decoded objects when a container
count exceeded remaining capacity. That path currently reports `artifact-invalid`. The helper
reached 500,001 objects and reports `artifact-limit-exceeded`. The corresponding expanded-byte
counters were 87,392,324 and 82,072,754, below the 256 MiB expanded limit.

The different reason labels do not make one failure safe or prove malformed packaging. Both scans
are incomplete. The three observed path locations are therefore a lower bound, not an exhaustive
finding list. No object budget, byte limit, or reviewed-content hash exception was changed.

## Verification outcome

The final evidence verifier passed 69 consistency checks, including 2,179 installed dependency
file hashes, B1 fixture/output equivalence, bounded native cases, preserved source bytes, cache
behavior, and the absence of owned frozen processes after cleanup. This verifies the recorded
results; it is not a packaging approval or independent agent review.

The existing native product smoke passed for the baseline in 212.957 seconds, covering capture,
search, export, Markdown import, and local MCP. The combined Markdown candidate reached the
240-second smoke harness cap and was terminated. Its full smoke remains incomplete; partial
execution is not counted as passage. It was not rerun with a larger timeout because the candidate
already fails artifact and startup gates.

The first evidence-verifier iteration incorrectly required every cache hit to reread every body
and checked the recursion limit after import instead of after extraction. Those assertions were
corrected against the retained receipts; graph equivalence and source-integrity requirements were
preserved. A first baseline build invocation also used a relative interpreter path against the
runner's repository working directory and failed before launch. The corrected build used an
absolute interpreter path. Neither development failure is a successful build/test result.

The shipping specification, lockfile, native builder, and auditor were checked against `72dd1fc`
and remain unchanged. Full repository verification and Homebrew installation smoke were not run
for these private prototype and documentation changes. Documentation checks accompany the commit.

## Decision and next experiment

Keep the baseline artifact and B1 evidence. None of the Graphify candidates is ready to ship.
Frozen extraction is feasible, but a working executable, acceptable compressed size, and a valid
signature do not establish content-audit or startup acceptance.

| Architecture | Assessment |
|---|---|
| Small, supported root-aware Markdown component from Graphify, with Engine-owned selection/link mapping and an auditable closure | Best fit for the launch scope. Availability remains pending: the current Markdown extractor's root lookup imports `graphify.extract`, coupling it back to the broad extraction module. Verify or prepare an upstream-compatible separation rather than silently losing root-aware links. |
| Separate Graphify helper | Strongest demonstrated base-module boundary. Still blocked by helper content/object-budget findings and an unresolved release-layout decision. It also duplicates runtime bytes in the tested layout. |
| Combined executable with a private helper mode | Preserves the one-executable archive and isolates observed globals. The measured base-startup penalty and content/object-budget findings remain failures. |

Next, run a bounded NW0-B3 architecture/closure check for the supported Markdown-only seam. Compare
an upstream-compatible separation with retaining the broader dependency plus reproducible binary
cleanup and explicitly reviewed content/budget changes. Do not replace Graphify with a homemade
parser, mutate the pinned dependency unnoticed, strip validation through optimization, raise audit
limits, or add exceptions as part of routine packaging. Preserve alias/ambiguity handling and all
semantic launch paths. Linux artifacts and GUI evidence remain separate outstanding gates.
