# NW0 parallel feasibility checkpoint

Status: bounded parallel investigation; full NW0 remains open. This checkpoint preserves B4 at
`e47d0a9`, the approved upstream-first/maintained-patch policy, four launch access paths, Claude's
C7 dispatch block and the Codex deferral. No shipping schema, dependency, native artifact or editor
implementation changes are part of this wave. No inference or desktop actions run here.

## Ownership and budget

The coordinator owns integration, public documents and git. A owns a private durable-state/privacy
proof after minimum-interface agreement. D owns read-only desktop metadata and presentation
investigation. C owns read-only direct-API contract preparation. All artifacts use separate private
directories and the cumulative A–E ledger. A receives 45 minutes, D 20 minutes and C 15 minutes;
these are reservations within existing allocations, not additional budgets.

Earlier workers were complete and no owned build or startup probe was running. A runtime thread
limit prevented a third simultaneous worker, so C reused D's worker after D finished. An independent
review of A and coordinator conclusions follows in that worker; it does not independently certify
its own D/C reports. The coordinator verifies those against source and metadata. Workers make no
shared-environment, permission or git changes. No heavy builds or startup measurements overlap this
wave. B4's timer remains closed; B5 is not started or silently treated as completed work.

## Durable-state and privacy proof

The agreed private seams bind an immutable selected snapshot to stable note IDs, accepted revisions,
body digests, policy generation and adapter identity. Owner consent is separate from immutable
capture privacy. Mutations use expected revisions and idempotency keys; inferred edges remain
separate from accepted note links. These are feasibility interfaces, not shipping APIs or schema.

Four grouped tests pass under the repository interpreter and were rerun by the coordinator. One
uses real Engine capture and portability operations; three use a clearly labelled
`proof-local-non-v1` state machine derived from B1's semantic dataset.

| Real Engine case | Observed result |
|---|---|
| Current canonical capture, export and import | Passes; original local-only capture privacy remains unchanged. |
| Same page with two known capture provenance references | Portable validation and import succeed. Authoritative reconciliation rejects it both in the live Brain and after import. Import success alone is insufficient compatibility evidence. |
| Proposed `inactive` page status | Portable validation rejects it. This tests that encoding only. |
| Existing `archived` status | Validates and imports, but the note remains searchable. This does not implement deletion from the active workspace/search/graph. |

The first compatibility run stopped at the synthetic temporary path's macOS `/var` symlink.
Resolving the newly created fixture path fixed the test without changing the Engine's path checks.
The initial source-only inference that import itself would reject multiple provenance references
was corrected by the executable result above.

The private state machine exercises identity-preserving rename, duplicate-ID refusal, retained edit
conflicts, restart, inactive deletion/restore, explicit revision-checked link acceptance, idempotency,
restriction-preserving private export/import and inactive restored consent. It also tests mixed-source
denial, adapter-bound consent, selected-input bounds, the existing redaction guard before fake secret
resolution, and revocation between resolution and fake dispatch. Queued/in-flight fake requests are
invalidated. Inference alone leaves accepted note bytes unchanged.

These results do not prove a compatible Portable representation for new history, consent,
restrictions or accepted-link provenance. No version change is selected, and the tests do not rule
out every v1-compatible representation. Automatic filesystem promotion is disabled. Durable
multi-process release/accounting, crash boundaries, complete result validation and folder-policy
semantics remain additional proof obligations. The conservative exclusion of a conflicted note in
this simulation does not change the product's ability to show its last accepted revision with a
conflict indication. A remains incomplete as a full NW0 gate.

Independent review reproduced stale-suggestion and inactive-endpoint acceptance in the initial
private draft. It also identified missing consent binding, input/redaction/freshness checks and
privacy in the idempotency signature. The final draft corrects those cases; the reviewer reran all
four grouped tests successfully. Accepted-output limits, shared reservation/uncertainty accounting
and full release/result handling remain unproven. Review does not promote this simulation into a
shipping Engine or a completed NW0-A result.

## Desktop readiness

Coordinator metadata checks confirm Obsidian 1.12.7 and UTM 4.7.5 on the arm64 Mac. Both applications
were already running and remain untouched. Bounded discovery of the default UTM location found an
arm64 guest, with no x86_64 guest in that location. This does not rule out a guest stored elsewhere;
the required Ubuntu 24.04 LTS x86_64 GNOME environment remains unverified.

Existing architecture HTML files are design documents. They do not prove a product Canvas,
bundled graph viewer, Obsidian activation, source-note navigation or link acceptance. The initial
worker report overstated that distinction; the coordinator required a correction before integration.

Pinned Graphify source provides these comparison inputs, without selecting a shipping surface:

| Surface | Source-level evidence | Remaining proof |
|---|---|---|
| Canvas with plugin evidence and acceptance controls | `to_canvas` accepts a note-filename map, appends `.md`, and emits file cards and relation/confidence labels. It retains at most 200 edges. | Validate vault-relative stems, ambiguity/rename behavior, evidence display and visible handling of bounded output. Run actual Obsidian navigation. Acceptance remains an Engine operation. |
| Bundled local HTML | The upstream renderer loads vis-network 9.1.6 from unpkg. | Bundle and review the asset, prove denied-network viewing and safe navigation, and connect evidence to current revisions. |
| Dedicated plugin graph view | Fits the agreed thin plugin and Engine-backed acceptance boundary. | Viewer/toolchain packaging, evidence controls and activation cost remain unproven. This is a comparison option, not a new scope decision. |

Use an isolated Mac test account or the agreed UTM guest for subsequent synthetic GUI work.
Preserve the user's existing vaults, applications and arm64 guest. Linux GUI timings must be labelled
emulated; native Linux CI remains the build/test authority. No GUI or five-minute result is claimed.

## Direct-API preparation

The prepared common test matrix covers selection, privacy, byte/time limits, source evidence,
response-derived model attribution, cancellation, retries and provider isolation for OpenAI API,
Anthropic API and Gemini API. It is a proposed contract, not a real-provider test result.

`TextModelRequest`, `TextModelResult`, `ProviderService` and the existing closed error types supply
useful boundaries. The request's character bound does not enforce the semantic 16 KiB input limit.
The result's provider label cannot establish actual model identity. Snapshot/revision binding,
shared reservations, cancellation and semantic evidence remain missing behavior.

The legacy OpenAI adapter has explicit credentials/model, disabled storage, timeout handling and
disabled SDK retries. Reuse reviewed behavior through app composition; do not import the legacy
distribution into the shipping product. No shipping Anthropic or Gemini adapter was found.
SDK/HTTP packaging choices and current protocol/model support remain unproven.

Read-only agent-config inspection confirms reusable abort propagation, idempotent stream cleanup,
schema handling and reservation-before-dispatch patterns. Its initialization-model fallback and
OAuth-variable forwarding do not meet this product's attribution/authentication contract. No router,
orchestration runtime or new dependency is selected. Coordinator verification ran the existing
synthetic provider suites: 18 tests passed. They do not establish any new real-adapter gate.

## Gates preserved

B4's pure parser still exceeds the 200 ms warm-start budget with a measured 213 ms delta. Its Mac
content audit and native smoke remain successful historical evidence; controlled cold starts and
Linux execution remain open. No failed candidate is promoted by this parallel checkpoint.

Claude remains stopped at the missing capabilities in the
[provider/supervisor contract](../plans/2026-09-09-ob1-provider-supervisor-contract.md).
Codex remains deferred. Real-provider quality, complete durable-state compatibility, both desktop
journeys and the remaining A–E evidence still gate NW0. NW1 cannot start from this checkpoint.

The next A experiment should compare concrete portable encodings for same-page revision history,
inactive state, restrictions and consent provenance, including import followed by authoritative
reconciliation and retrieval. Stop before shipping schema changes. B5 can independently investigate
startup and reproducible packaging once measurement activity is serialized. C's next direct-API
experiment is a shared fake-transport contract harness; real calls need their own reservations and
preflight. D needs the isolated desktop environments before GUI evidence. These are remaining
NW0 obligations within the existing budgets, not permission to begin NW1.

Verification for this documentation/private-prototype checkpoint: four grouped private tests,
18 existing synthetic provider tests, independent review and bounded document/content/link checks.
No shipping source or build inputs changed, so the full product build, native/Homebrew smoke and
desktop journeys were not rerun. Their milestone requirements remain in the main plan.
