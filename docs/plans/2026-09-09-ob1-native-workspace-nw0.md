# OB1 native workspace NW0 decision record

- Status: open. NW0-B1 closure/import proof passed with required integration controls.
  NW0-C2 did not establish Codex isolation; Codex subscription is deferred under the subsequent
  user-authorized scope change. NW0-C6 connects the existing SDK lifecycle to supervised pre-input
  metadata and rejection on C5's Mac boundary; native completion remains closed. Four launch paths
  and frozen packaging remain unproven.
  NW0-C7 records the provider/supervisor contract and local upstream requirements.
- Authority: [native workspace plan](2026-09-09-ob1-native-workspace.md), including the user's
  implementation-efficiency guidelines and the subsequent user-authorized Codex deferral.
- Starting revision: `3468115` on `docs/ob1-native-workspace-plan`.
- Latest runtime checkpoint: NW0-C6 ran from `d6c7766` on macOS arm64. The final real-SDK/fake-process
  suite passed 15 assertions; the native sibling bridge passed ten checks after connecting C4's
  validator. SDK and native exits were zero with input withheld. A nested-sandbox launch failure
  remains recorded. Zero model calls or native note input; no real account inspection, product
  runtime changes, or VM provisioning.
- Latest design checkpoint: NW0-C7 at `77012ea` specifies the held-input contract, authority and
  budget ownership, rejection/revocation behavior, and missing native capabilities. No new native
  or model calls; no shipping interfaces or schema changes.

## Established state

Completed product work and prior planning/review remain complete. The planning workstream ended at
`ae9df14`; the review workstream ended at `b87e47a`. The current branch contains the subsequent
review corrections, Gemini addition, and Mac-host selection. Those older receipts are historical
evidence, not instructions to repeat resolved decisions. The initial preflight found no prior NW0
runtime proof; the subsequent B1 experiment below supplies the first closure/import evidence.

The current lockfile contains PyInstaller `6.22.2` and hooks `2026.7`; it contains no Graphify
dependency. Existing native audit limits remain 64 MiB input/member and 256 MiB expanded, with
additional archive-depth/object limits enforced by `tools/open_brain_dev/artifact_audit.py`.
Passing base-product CI does not establish a Graphify candidate's closure or startup performance.

Five local metadata commands completed within 20 seconds each and 64 KiB retained output each.
They identified Codex CLI `0.153.4` and Claude Code `2.1.265`. Codex's `exec --help` advertises
`--ephemeral`, `--ignore-user-config`, `--ignore-rules`, schema output, and stdin input; app-server
help advertises private stdio. This is not evidence that built-in tools or ambient instructions
are disabled. Claude's help advertises `--safe-mode`, `--tools ""`, `--strict-mcp-config`, and
`--no-session-persistence`; safe mode still permits managed settings. These remain candidate
controls requiring behavioral proof. No authentication status, account identity, credentials,
or live model availability was inspected. Private evidence retains the exact help output.

## NW0-B1 result: closure/import works; raw output needs the engine adapter

The bounded source-import experiment passed on this Mac. Direct use of the full vault as the scan
root and publication of raw upstream links failed Open Brain's selection/link requirements. These
are required adapter controls, not launch-scope exceptions. B1 does not pass the full NW0-B native
packaging gate.

| Measurement | Observed result |
|---|---|
| Pin correspondence | All 221 files inside the wheel's `graphify/` package, including 86 Python modules, matched source commit `3f82bf7f837a07fb0f7668fbdbd5662801906942` byte-for-byte. Wheel SHA-256: `f35c86410e7d92ace69a50ac8dbed568903c880482c656f43437ca657fee8c37`. |
| Dependency closure | 30 distributions resolved and installed from wheels with a hash-locked requirements file; `uv pip check` passed and installed RECORD hashes verified. Installed distribution files total 110,802,512 bytes. This excludes the interpreter and is not a frozen/archive size measurement. |
| Synthetic extraction | Six selected notes, 691 input bytes, 12 nodes and 12 edges, 6,103 graph-output bytes. Qualified links with heading/display text and cross-folder bare links resolved to the expected selected pages. |
| Process timings | First empty-Graphify-cache process: 0.792 seconds; cached process: 0.293 seconds. Relocated/shared-cache, full-vault negative control, and incremental-edit cases each completed in 0.286–0.295 seconds. These are diagnostic source-process samples, not native startup medians or five-minute acceptance evidence. |
| Verification | Five bounded process cases and 20 persisted-evidence assertions passed. Each process stayed below 60 seconds and 16 KiB response output. Graphify preserved source bytes, read only selected note bodies, made no observed Python-level network/subprocess attempts, and wrote only the private cache. Zero model calls. |

One semantic dataset materialized an eligible snapshot, a relocated snapshot, and a synthetic vault
with excluded/generated canaries. A separate incremental copy added one explicit link. Cached and
uncached output matched; selected-page mappings stayed stable across relocation and the edit; the
new link appeared. No semantic connection was fabricated between the unlinked related notes.
The dependency lock, installed-file hashes, scripts, dataset, raw results, and verification receipt
are retained in the private B1 workstream. No product dependency or native build specification changed.

Required controls established by the experiment:

1. **Stage only eligible sources under the scan root.** With the full synthetic vault as `root`,
   Graphify scanned the excluded folder and resolved a link to an unselected note, despite receiving
   only selected paths. It did not read that note's body. Passing an isolated eligible snapshot
   prevented that lookup. A selected path list alone is not the exclusion boundary.
2. **Keep link resolution and publication under engine control.** `[[shared]]` selected
   `alpha/shared.md` over another same-basename note without an ambiguity diagnostic. The tested
   frontmatter alias remained unresolved. Unresolved target IDs encoded the absolute snapshot path
   and changed after relocation. Resolve supported aliases, detect ambiguity, map accepted endpoints
   to Brain IDs, and emit path-free diagnostics for unresolved/excluded targets before publication.
   Never accept an upstream tie-break or path-derived dangling ID as a permanent link.
3. **Keep the cache private and rebuildable.** B1 verified cache reuse, relocation, and a source-body
   edit; it did not establish all rename/delete/exclusion-change invalidation semantics. Cache files
   belong outside the snapshot and must not be exported or reused across an incompatible snapshot,
   policy, or adapter version. Those lifecycle checks remain in the existing NW0/NW1/NW2 contracts.
4. **Retain a process boundary as a packaging candidate.** Markdown extraction loaded 46 Graphify
   modules and raised the process recursion limit from 1,000 to 10,000. A self-invoked helper contains
   those globals while preserving the current archive shape; a separate helper gives the stronger
   module boundary at the cost of a release-contract change. In-process integration remains the
   comparison baseline. B1 selects none of these: frozen closure, audit, startup, and cleanup evidence
   must determine the architecture.

The Python audit hook recorded and denied out-of-scope Python I/O for this structural probe. It is
not an OS sandbox or proof of subscription-client confinement. Linux execution, frozen extraction,
artifact/signature audits, five cold/five warm base-startup samples, semantic quality, and actual
Obsidian journeys remain unproven. The original 45-minute limit is a stop limit; completion of this
bounded probe does not require consuming the remaining time.

## NW0-C1 result: controls mapped; live dispatch is not ready

The [control-to-test audit](../audits/2026-09-09-ob1-nw0-c1-client-isolation.md) records installed
Codex `0.153.4` and Claude Code `2.1.265` controls, three offline Claude cases, explicit unresolved
boundaries, and the staged-executor alternative. Codex generated 416 protocol schemas; source
inspection found model-catalog-driven tools that shell/feature suppression alone does not remove.

Claude accepted the combined isolation flags. Its MCP/context diagnostics reported empty staged
ambient catalogs. Adding `--json-schema` introduced a 687-token system-tool category; removing it
removed that category. Use plain JSON text plus engine validation as the next strict tool-free
candidate, subject to the existing quality gate. This diagnostic is not a serialized model request.

Both clients still need effective managed-policy, authenticated egress, retention, retry accounting,
and active cancellation proof. Safe mode does not suppress Claude's managed hooks. The private
test sandbox verified selected denials but excluded real credentials and local managed policy;
it proves neither subscription compatibility nor a shipping containment mechanism. No note content
or user/model turn was submitted. The existing staged-asset port is an interface and authority
check, not an implemented OS sandbox. At C1, no path was dropped and no confinement exception
was selected. The subsequent C2 scope decision below supersedes the five-path launch requirement.

## NW0-C2 result and authorized launch-scope change

The [C2 audit](../audits/2026-09-09-ob1-nw0-c2-codex-preflight.md) records two successful offline
Codex processes, two retained configuration rejections, and the probe-harness recovery. The valid
thread was ephemeral and idle, with no turns, hooks, or MCP servers. It still listed the synthetic
user-level `AGENTS.md` as an instruction source and attempted a host-skills scan that the sandbox
denied. The complete model-visible tool inventory and incompatible-managed-policy rejection remain
unproven. No model request was sent.

During C2 the user authorized deferring Codex subscription if the isolation work proved too costly.
The coordinator selected that option based on these results. Launch now requires **OpenAI API,
Anthropic API, Claude subscription, and Gemini API**. Codex subscription, its installation/login
onboarding, and its acceptance matrix move to a later implementation; no placeholder ships. Its
future isolation requirements and the C1/C2 evidence remain available. This is an explicit scope
change, not a passed gate or a relaxation of the remaining adapters' privacy requirements.

## NW0-C3 Claude policy and retention result

The [C3 audit](../audits/2026-09-09-ob1-nw0-c3-claude-preflight.md) establishes native settings
observability and no retained synthetic system-context marker in the tested startup/control paths.
Forced termination left a session-discovery metadata record, so cleanup must inspect more than
transcripts. No user turn or model call occurred; active retention/cancellation remains unproven.

The native client selected a deliberately supplied synthetic API-key source despite
`forceLoginMethod: claudeai`. Provider identity alone cannot establish subscription selection.
Valid parent policy kept permission denials but dropped hook suppression and other unsupported
parent keys. The existing SDK resolver exposed synthetic admin hooks/routing and parent precedence,
but it is from an older client version and does not execute policy helpers. The native no-auth run
did not apply a synthetic remote cache, so it did not test managed-hook execution or account policy.

Claude remains a required launch path. The next candidate combines version-matched policy rejection
with a native runtime boundary through the existing staged-execution seam. It must preserve official
client-owned login without credential relay and reject indeterminate/conflicting policy before note
bytes. C3 recommends a bounded design/prototype step; no shipping sandbox or new dependency is selected.

## NW0-C4 supervisor design and rejection result

The [C4 design/probe record](../audits/2026-09-09-ob1-nw0-c4-claude-supervisor.md) defines the app-owned
supervisor states, engine authority/consent boundary, runtime asset policy, and client-owned login
constraint. The prototype uses native `2.1.265` settings metadata rather than the older SDK resolver.
It made live rejection decisions for incomplete preflight and observed routing changes, with zero
note bytes to the client. All owned processes were terminated and waited for.

Fake-only tests exercise a positive release, stale/malformed metadata, policy and access conflicts,
exclusions/redaction/authority denial, bound permits, observed revocation, and replay rejection.
No fake witness can authorize the native path. The native executor has no completion writer.
This completes the requested design/rejection prototype, not subscription feasibility: applicable
policy completeness/freshness, full tool catalog, real login reuse, independent containment,
unobserved policy changes, active retention/cancellation, and request accounting remain unproven.

The next containment candidate preserves the official client's existing auth namespace while
isolating other runtime state. Prove its minimal auth/bootstrap reads and rotation writes with
synthetic fixtures before an authenticated no-note check. A second dedicated login is not selected
as a shortcut around the agreed login-reuse requirement.

## NW0-C5 native runtime-layout result

The [C5 runtime-layout record](../audits/2026-09-09-ob1-nw0-c5-native-runtime-layout.md) demonstrates
a macOS file-data allowlist with selected synthetic auth/bootstrap reads, unrelated-file and adjacent
history/settings denial, narrow synthetic replacement writes, fork/spawn denial, and bounded teardown.
The pinned official client reached metadata after required loader/timezone reads were identified.
Earlier startup failures remain failures, not positive isolation evidence.

Two native sessions reused a synthetic file-backed namespace with separate runtime directories.
The separate official status command accepted nonfunctional fixture data as a local login; neither
that result nor a subscription label verifies a real account. Missing native source evidence still
rejects dispatch. Native runs kept auth files read-only; actual refresh/rotation and Keychain reuse
were not exercised. The helper's replacement operation is not a native credential-rotation proof.

Unreadable, missing, or malformed required host-policy fixtures prevented native launch. Readable
host policy appeared in native settings, and an observed routing change rejected while the client
was alive. This does not establish complete organizational policy or eliminate the check/send race.
Keep the Mac file/process candidate and check supported interfaces for the remaining client-owned
auth and complete same-process policy/capability boundaries before an authenticated no-note probe.

## NW0-C6 SDK/supervisor interface result

The [C6 interface check](../audits/2026-09-09-ob1-nw0-c6-sdk-supervisor-interface.md) verified
that the installed official TypeScript SDK can start with a held asynchronous input stream, return
public native metadata, and close on supervisor rejection before any note is yielded. A disposable
custom-process bridge connects that lifecycle to C4's unchanged validator, which rejects the missing
policy snapshot. Fake-only cases exercise structured results, single release, abort, and cleanup.

Reuse the existing adapter's lifecycle with a wider input seam and supervisor-owned launch/lifetime.
Its current immediate string prompt and cached separate-process readiness cannot implement this gate.
`accountInfo()` also caches initialization; `reinitialize()` saw a changed fake account, but does not
establish complete native policy or atomic change handling. Runtime `getSettings()` is absent from
the installed public Query interface. Missing settings remain unknown in the C4 mapping.

Nested Mac sandbox application failed; separately sandboxed SDK and native sibling processes worked.
That result keeps the SDK option viable without selecting Node distribution or weakening the native
profile. Existing-Keychain reuse, complete policy/capability evidence, and native positive dispatch
remain unproven. Do not repeat the completed lifecycle discovery or treat this as full NW0-C passage.

## NW0-C7 provider/supervisor contract

The [contract](2026-09-09-ob1-provider-supervisor-contract.md) specifies a semantic envelope around
existing ports, a private single-use release capability, Engine/supervisor ownership, and bounded
result/error projection. Direct APIs share semantic controls while Claude adds native auth/policy
and runtime evidence. Engine generation does not substitute for native policy change semantics.
The contract includes uncertain-dispatch accounting and hidden-client-attempt acceptance criteria.

The local upstream requirements brief names the missing supported capabilities and equivalent
pass criteria. It has not been sent upstream. C7 is a design checkpoint, not another runtime proof
or a completed positive subscription path. Preserve C4-C6 tests and add uncovered cases at their
owning milestones. Claude work remains stopped at those gaps while independent packaging proceeds.

## Unproven gates and pass/fail criteria

| Gate | Evidence required to pass | Fail or incomplete condition |
|---|---|---|
| NW0-B: native Graphify packaging | Pin source `3f82bf7f837a07fb0f7668fbdbd5662801906942` / `graphifyy==0.9.57` and verify their correspondence. Record dependency versions/hashes and full frozen closure on macOS arm64 and Linux x86_64. Execute root-aware extraction on selected synthetic Markdown, resolve IDs/links, bound output/cache/process lifetime, and audit final artifacts. Compare in-process, self-invoked, and separate-helper boundaries with five cold/five warm baseline/candidate samples. Meet existing archive/audit limits and additional median startup budgets of 500 ms cold/200 ms warm. | Missing dependency/resource, unresolved pin mismatch, incorrect links, forbidden modules, audit failure, orphan work, or exceeded startup/size limit rejects the candidate. Source import alone leaves frozen packaging incomplete. Missing Linux candidate evidence leaves the gate incomplete. |
| Deferred: Codex subscription isolation (not a launch gate) | Unmodified official client uses explicit subscription selection and private stdio, returns schema-valid edges/evidence and recorded model attribution, and meets common privacy/budget bounds. Controlled hostile notes and ambient canaries establish no model tool access/effects, unrelated context loading, extra egress, or persistent prompt history. Verify managed-policy behavior and process-tree cancellation. | Read-only sandbox or successful login alone is insufficient. Any missing confinement control stops note dispatch; tool activity, retained prompt, unrelated context, credential relay, or API fallback fails the probe. |
| NW0-C: Claude subscription isolation | Same contract as Codex, through the unmodified official Claude client. Verify safe-mode/tool/MCP/persistence controls together while retaining client-owned subscription authentication; test managed settings explicitly. | Agent SDK configuration, CLI help, or bare mode alone is insufficient. The same isolation failures stop dispatch; no OAuth-token handoff or silent API fallback is allowed. |
| NW0-C: four launch access paths | OpenAI API, Anthropic API, Claude subscription, and Gemini API each run three initial samples and one incremental sample per platform through the common schema and selection boundary. All accepted edges resolve to input IDs/evidence; the known related connection appears in at least two of three initial samples, with no accepted distractor edge. Prove denied input causes zero inference, plus explicit access/model attribution and provider-specific output validation. | A missing path/platform, schema/provenance error, privacy failure, silent fallback, or unmet quality criterion leaves that path failed/incomplete. A deterministic fake does not pass a real-provider gate. |
| Vertical slice and desktop integration | Capture → infer connection → display evidence → explicit permanent-link acceptance → verified export. Run deterministic fake responses first, then all real adapters through the same operations. Inference preserves note bytes; acceptance checks current revisions, is idempotent, and exports durable provenance. Consent denial and exclusions prevent selection/dispatch. Actual Obsidian activation/navigation runs on both desktops at the planned checkpoints. | A graph screenshot without source evidence, direct note writes during inference, missing exported link/provenance, or bypassed consent fails the slice. Headless/mocked results cannot substitute for GUI evidence or the final 300-second journey. |

Failures alone do not authorize omitting a required launch path, weakening confinement, or relaxing
artifact limits. Codex deferral is the separately recorded user-authorized scope exception.
The complete native, subscription, and desktop proofs remain unproven after this checkpoint.

## Smallest next experiments

1. **NW0-B1: completed.** Preserve the result above; do not rerun it without a relevant pin,
   dependency, interpreter, or fixture change. The original experiment reserved at most 45 minutes
   from NW0-B's eight-hour allocation, using a disposable environment and synthetic Markdown.
   It verified pin correspondence, resolved the full closure, and exercised
   `graphify.extract.extract(..., parallel=False)` within 60 seconds, 16 KiB selected input,
   and 16 KiB accepted output per case. Versions/hashes, links/IDs, cache effects, and the
   required integration controls are recorded above. Frozen packaging remains open.
2. **NW0-C1–C7: recorded; Codex deferred.** Preserve the audits, receipts, demonstrated SDK
   lifecycle, and provider/supervisor contract. C7 records missing upstream requirements locally.
   Resume authentication experiments only against a concrete supported mechanism. Complete native
   policy evidence and isolated existing-Keychain reuse remain unproven; native dispatch stays closed.
3. **First bounded real Claude subscription probe.** Once its C1 boundaries have concrete enforcement
   mechanisms and the user-selected account is ready, run one synthetic hostile-input completion.
   Reserve each actual attempt before dispatch; retain all byte/time limits. Observe process/tool,
   network, and retention effects. Privacy rejection makes zero inference calls; eligible hostile
   input spends a reserved call. Codex receives no calls while deferred. Preserve the mandatory
   four-path initial/incremental matrix and do not repeat accepted samples without a relevant change.
4. **Frozen candidate and complete thin matrix.** Build the first candidate from B1, audit its full
   closure and run baseline/candidate startup checks on this Mac and existing Linux x86_64 CI.
   Compare the other packaging boundaries before selecting the architecture. Complete thin proofs
   for the three direct APIs and the remaining subscription samples on both platforms. A CI job
   proves only what it runs; GUI and interactive account evidence remain separate. Pushing to trigger
   CI uses the existing publication authorization rules.
5. **Integrate the slice, then run desktop checkpoints.** Use the shared fake provider and source
   dataset to establish durable operations in NW0-A/NW1, connect minimal evidence display early in
   NW2, and substitute real adapters through that flow. Use the agreed Ubuntu x86_64 UTM guest for
   Linux GUI work in NW0-D and later integration checkpoints. Record emulated timings accurately;
   do not require the guest before local packaging/control work can proceed. NW0-E closes only
   after all A–E evidence is present and reviewed; NW1 does not start early.

These are substeps of the existing NW0 allocations, not extra budgets or new milestones. Preserve
32 engineer-hours total (A 8, B 8, C 8, D 6, E 2), 80 actual model attempts total and 16 per access
path, 16 KiB selected input/accepted output per attempt, 60 seconds per attempt, at most two attempts
and 90 seconds total per probe. Four active launch paths allow at most 64 attempts; Codex's unused
allowance is not transferred. The mandatory initial/incremental matrix reserves 32 calls; isolation
and recovery draw from the remaining active capacity. C1/C2/C3/C4/C5/C6/C7 effort remains charged. Effort is a stop limit, never a delivery estimate.
Keep one cumulative private effort/attempt ledger across resumed sessions and workers.

## Reuse decisions and implementation guardrails

Existing `EngineTaskSet` in `engine/contracts.py` supplies capture, retrieval, reconciliation, and
portability operations. Extend that boundary for missing workspace semantics; do not invent a second
plugin-specific engine. `core/ports.py` already defines bounded `TextModelRequest`, `TextModelResult`,
and `Provider` contracts. `providers/base.py` checks cloud authority and final-prompt redaction before
secret resolution. Preserve those checks while adding the reviewed effective-consent contract.

Read-only inspection of agent-config's `packages/workflow-runtime` confirmed reusable patterns in
`src/auth.ts`, `src/adapter.ts`, `src/adapters/codex.ts`, `src/adapters/claude.ts`, and the corresponding
auth/adapter tests. `src/runtime.ts` reserves attempts before dispatch. Reuse the explicit auth-mode,
schema validation, abort propagation, and budget-reservation patterns through small Python boundaries.
Do not copy Claude OAuth environment-token forwarding, router lanes, or the whole runtime. The Codex
adapter's read-only sandbox is weaker than tool-free completion; the Claude Agent SDK adapter is
not itself the required official-client proof. Verify actual model attribution rather than merely
echoing the requested model. These findings constrain reuse without rejecting its useful contracts.

The coordinator owns edits and git state. Read-only questions/reviews may run in bounded parallel
when they save work; this small preflight used no subagents. Implementation delegation requires
agreed interfaces and disjoint file ownership. Every added abstraction/dependency/setting must cite
its requirement or measured constraint. Keep the separate-helper and shared-provider-package options
visible when evaluating stronger boundaries; record their distribution cost before choosing.

Focused tests drive iteration. Full project verification remains required at code handoff/milestone
completion, including native build and Homebrew smoke for packaging changes. Existing Linux CI
runs `make contributor-check`; UTM supplies Linux desktop evidence. Do not rerun slow desktop
journeys until an integration checkpoint or a relevant change invalidates prior evidence.

Next action: NW0-B2, build and audit a first frozen Graphify candidate on this Mac with B1's synthetic
dataset, capped at 45 minutes from remaining NW0-B and zero model calls. Compare runtime boundaries
and startup impact; leave incomplete packaging comparisons explicit. Native Linux evidence remains
required separately. Claude dispatch stays closed at C7's missing capabilities; Codex remains deferred.
