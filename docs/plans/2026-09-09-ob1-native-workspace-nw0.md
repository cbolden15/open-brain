# OB1 native workspace NW0 decision record

- Status: open. Metadata preflight completed; no runtime feasibility gate has passed.
- Authority: [native workspace plan](2026-09-09-ob1-native-workspace.md), including the user's
  implementation-efficiency guidelines and all five required model-access paths.
- Starting revision: `3468115` on `docs/ob1-native-workspace-plan`.
- Scope of this checkpoint: source/evidence inventory, bounded installed-client version/help probes,
  and remaining experiment contracts. No model calls, product implementation, or VM provisioning.

## Established state

Completed product work and prior planning/review remain complete. The planning workstream ended at
`ae9df14`; the review workstream ended at `b87e47a`. The current branch contains the subsequent
review corrections, Gemini addition, and Mac-host selection. Those older receipts are historical
evidence, not instructions to repeat resolved decisions. No NW0 runtime proof was found in the
native-workspace workstreams inspected at this checkpoint.

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

## Unproven gates and pass/fail criteria

| Gate | Evidence required to pass | Fail or incomplete condition |
|---|---|---|
| NW0-B: native Graphify packaging | Pin source `3f82bf7f837a07fb0f7668fbdbd5662801906942` / `graphifyy==0.9.57` and verify their correspondence. Record dependency versions/hashes and full frozen closure on macOS arm64 and Linux x86_64. Execute root-aware extraction on selected synthetic Markdown, resolve IDs/links, bound output/cache/process lifetime, and audit final artifacts. Compare in-process, self-invoked, and separate-helper boundaries with five cold/five warm baseline/candidate samples. Meet existing archive/audit limits and additional median startup budgets of 500 ms cold/200 ms warm. | Missing dependency/resource, unresolved pin mismatch, incorrect links, forbidden modules, audit failure, orphan work, or exceeded startup/size limit rejects the candidate. Source import alone leaves frozen packaging incomplete. Missing Linux candidate evidence leaves the gate incomplete. |
| NW0-C: Codex subscription isolation | Unmodified official client uses explicit subscription selection and private stdio, returns schema-valid edges/evidence and recorded model attribution, and meets common privacy/budget bounds. Controlled hostile notes and ambient canaries establish no model tool access/effects, unrelated context loading, extra egress, or persistent prompt history. Verify managed-policy behavior and process-tree cancellation. | Read-only sandbox or successful login alone is insufficient. Any missing confinement control stops note dispatch; tool activity, retained prompt, unrelated context, credential relay, or API fallback fails the probe. |
| NW0-C: Claude subscription isolation | Same contract as Codex, through the unmodified official Claude client. Verify safe-mode/tool/MCP/persistence controls together while retaining client-owned subscription authentication; test managed settings explicitly. | Agent SDK configuration, CLI help, or bare mode alone is insufficient. The same isolation failures stop dispatch; no OAuth-token handoff or silent API fallback is allowed. |
| NW0-C: all five access paths | OpenAI API, Codex subscription, Anthropic API, Claude subscription, and Gemini API each run three initial samples and one incremental sample per platform through the common schema and selection boundary. All accepted edges resolve to input IDs/evidence; the known related connection appears in at least two of three initial samples, with no accepted distractor edge. Prove denied input causes zero inference, plus explicit access/model attribution and provider-specific output validation. | A missing path/platform, schema/provenance error, privacy failure, silent fallback, or unmet quality criterion leaves that path failed/incomplete. A deterministic fake does not pass a real-provider gate. |
| Vertical slice and desktop integration | Capture → infer connection → display evidence → explicit permanent-link acceptance → verified export. Run deterministic fake responses first, then all real adapters through the same operations. Inference preserves note bytes; acceptance checks current revisions, is idempotent, and exports durable provenance. Consent denial and exclusions prevent selection/dispatch. Actual Obsidian activation/navigation runs on both desktops at the planned checkpoints. | A graph screenshot without source evidence, direct note writes during inference, missing exported link/provenance, or bypassed consent fails the slice. Headless/mocked results cannot substitute for GUI evidence or the final 300-second journey. |

Failures are results, not permission to omit a path, weaken confinement, or relax artifact limits.
The complete native, subscription, and desktop proofs remain unproven after this checkpoint.

## Smallest next experiments

1. **NW0-B1: closure and import on this Mac.** Reserve at most 45 minutes from NW0-B's remaining
   eight-hour allocation. Use a disposable environment and the plan's synthetic Markdown dataset.
   Verify the source/package pin, resolve the full dependency closure, and invoke the root-aware
   `graphify.extract.extract(..., parallel=False)` facade on selected paths. Record versions/hashes,
   import/resource failures, links/IDs, and local cache effects. Bound the extraction to 60 seconds,
   16 KiB selected input, and 16 KiB accepted output. Make zero model calls. Pass B1 only if the
   reproducible import/extraction works within those bounds; it does not pass frozen packaging.
   Stop at the limit and record the precise packaging obstacle.
2. **NW0-C1: resolve official-client controls.** Reserve at most 45 minutes from NW0-C. Inspect
   supported configuration/protocol controls for the recorded versions and build a control-to-test
   map covering tools, ambient instructions, managed policy, egress, retention, and cancellation.
   Start with Codex's unresolved tool/context controls; check the complete Claude candidate recipe
   independently. Use a scratch directory outside all Brains/vaults. Make zero model calls until
   every boundary has a concrete enforcement mechanism. Missing controls trigger the staged-asset
   alternative assessment, not a permissive test run.
3. **First bounded real subscription probes.** Once C1 passes and the user-selected account is ready,
   run one synthetic hostile-input completion per official client. Reserve each actual attempt
   before dispatch; enforce all existing byte/time limits. Observe process/tool/network/retention
   effects, not only final text. A privacy rejection makes zero inference calls; an eligible hostile
   note testing confinement spends one reserved call. Preserve enough capacity for the required
   initial/incremental matrix; do not repeat already accepted samples under equivalent conditions.
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
and 90 seconds total per probe. The mandatory initial/incremental matrix reserves 40 calls; isolation
and recovery draw from the remaining capacity. Effort is a stop limit, never a delivery estimate.
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

Next action: run NW0-B1 within its 45-minute stop limit and record the closure/import result here.
