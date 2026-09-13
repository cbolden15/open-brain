# Provider and supervisor interface contract

- Checkpoint: NW0-C7, grounded at `77012ea` on `docs/ob1-native-workspace-plan`.
- Status: proposed implementation contract, with native authorization mechanisms still unproven.
  This document adds no shipping API, dependency, schema migration, or completed feasibility gate.
- Authority: [native workspace plan](2026-09-09-ob1-native-workspace.md),
  [C4 supervisor](../audits/2026-09-09-ob1-nw0-c4-claude-supervisor.md), and
  [C6 SDK interface evidence](../audits/2026-09-09-ob1-nw0-c6-sdk-supervisor-interface.md).
- Launch paths: OpenAI API, Anthropic API, Claude subscription, Gemini API. Codex subscription and
  Ollama remain deferred. Existing-login reuse and the five-minute first-use scope are unchanged.

Reuse the existing SDK lifecycle behind an Engine-authorized, supervisor-controlled content gate.
The Engine decides which content may leave. The supervisor owns the attempt and enforces its limits.
The provider transport supplies results and observations; neither a transport response nor a UI
request can grant consent. All interface names below are proposed internal names, not existing exports.

## Ownership and existing code

| Owner | Contract responsibility | Existing boundary and required work |
|---|---|---|
| Engine | Select accepted revisions; evaluate every source, consent, exclusions, and caller authority; own request identity, shared budgets, and result publication. | Extend `EngineTaskSet` in `engine/contracts.py` through the planned shared workspace operations. It currently exposes capture, retrieval, reconciliation, and portability, but no semantic-refresh task. |
| App supervisor | Construct the selected adapter, hold content, gather evidence, authorize one release, cancel and wait, return a bounded outcome. | Compose the existing `core/ports.py` request/result types and `core/policy.py` authority checks. OS containment is an app responsibility, not an Engine import. |
| Provider transport | Perform one explicitly selected completion with bounded input/output and attributable response; report failure without fallback. | Reuse the agent-config Claude async iterator, schema options, AbortController, and idempotent close. Widen its string-only input seam; do not import the workflow orchestrator. |
| CLI, MCP, Obsidian | Request shared operations, display status/evidence, and send authorized owner actions. | No direct SDK calls, credentials, attestations, or note writes from inference. Preserve the plan's restricted MCP capability matrix. |

The existing `Provider.complete(TextModelRequest, privacy=...) -> TextModelResult` contract carries
text, timeout, output bound, and provider name. It does not carry revision bindings, requested model,
response attribution, usage, cancellation, or preflight state. Its serialized forms validate exact
keys. Compose it inside a new semantic envelope; do not add fields to its current wire format.

`StagedExecutionRequest` adds explicit assets and network hosts. Its policy wrapper checks authority
and host inclusion but does not enforce a sandbox or validate every request/output bound. Reuse
those checks for a staged runtime without making every API provider spawn a process. The current
`ProviderService` resolves an API secret for cloud construction; it cannot be the subscription
constructor. Keep the existing final-prompt redaction detector and perform it before construction.

## Minimal internal data contract

| Value | Fields and rules | Trusted producer |
|---|---|---|
| `SemanticRequest` | Existing `TextModelRequest`; explicit provider/access/model; output-schema version; snapshot digest; stable note IDs, accepted revision IDs and selected-body digests; effective privacy decision and consent reference; Engine policy generation; operation deadline; cancellation handle. The final assembled prompt includes only eligible selected context. | Engine, after authoritative selection. UI IDs are resolved and checked, not trusted as completed selection. |
| `AttemptContext` | Request/attempt IDs; nonportable budget reservation; adapter/version identity; selected access reference; deadline and byte limits; allowed endpoint/runtime profile. It holds no subscription token or note body. | Engine budget authority plus app composition. |
| `RuntimeObservation` | Bound process instance, client/SDK identity, active access source, effective policy with coverage/change semantics, capability evidence, and confinement evidence. Each field records known/unknown/conflicting status and its source. | Adapter observations combined with supervisor-owned launch and OS evidence. A client cannot attest to its own OS confinement. |
| `DispatchPermit` | Private single-use capability bound to request digest, source revisions, consent/policy generation, attempt reservation, provider/access/model, adapter instance/version, required evidence identity, and expiration. Claude additionally binds the native process instance/version. | Supervisor after all required checks. Never accepted from caller JSON, plugin, MCP, or model output. |
| `SemanticOutcome` | Success: validated edges/evidence, snapshot/revision bindings, requested and actual model with attribution source, provider/access/adapter identity, usage if reported, and attempt receipt. Failure: fixed category, dispatch certainty, consumed-attempt state, and cleanup outcome. | Supervisor validates transport output; Engine rechecks eligibility before publication. |

These are responsibility boundaries, not five new public packages or a frozen Portable schema.
`RuntimeObservation` is the subscription-specific evidence variant; API adapters carry their selected
endpoint/access binding without fabricating a native process, organizational policy, or tool catalog.
Use native types and existing canonicalization where compatible. Exact field types and durable
record compatibility belong to NW0-A/NW1. Keep cancellation handles, permits, process IDs, credentials,
and runtime paths out of portable records. Accepted links retain the agreed durable source/provenance
references; inferred graph caches remain rebuildable.

Content digests and source identifiers are local control data. Do not transmit them to the native
client during preflight. Startup gets only fixed configuration, the selected model, a content-free schema, and a
constant system prompt. In the sibling layout, the trusted supervisor retains note bodies until
release; an SDK host must not receive a string prompt early and merely promise not to forward it.

## Lifecycle calls and release ordering

The internal lifecycle is `prepare(request)`, `release(permit)`, and idempotent `stop(reason)`.
`prepare` either returns a supervisor-owned permit or a closed outcome. `release` consumes the
permit before its first content write and cannot be invoked twice. `stop` is valid in every state.
These calls run within one operation; they are not resumable public commands or a daemon protocol.

| State | Permitted action | Failure and transition |
|---|---|---|
| `CLOSED` | Engine validates current selection, authority, prompt redaction, schema and request bounds. Reserve available attempt capacity before creating a dispatch-capable workload. | Denied input causes no provider construction, credential lookup, native launch, or inference. Eligible input enters `PREFLIGHT`. |
| `PREFLIGHT` | Subscription starts the pinned client with held async input and allowlisted metadata traffic. Direct APIs validate selected credential reference, endpoint and request configuration without sending content. | Missing/conflicting evidence stops and releases a proven unused reservation. Success enters `PREPARED`. Verified no-inference metadata/status does not consume a model attempt; uncertain dispatch follows the accounting rule below. |
| `PREPARED` | Recheck current Engine generation/revisions, cancellation, deadline, access selection, runtime evidence and reservation. Atomically consume the permit and mark dispatch intent before the transport receives content. | Any failed recheck stops with no release. No lock is held across model inference. Successful handoff enters `IN_FLIGHT`. |
| `IN_FLIGHT` | One completion under the reserved limits. Treat output as untrusted; observe cancellation and relevant policy/runtime changes. | Failure, revocation, timeout, or protocol violation stops owned work. A candidate result enters `VALIDATING`; it is not yet visible graph output. |
| `VALIDATING` | Validate response schema, actual-model attribution, source IDs/evidence, output bounds, and current Engine eligibility; finish cleanup. | Publish only a complete eligible result through the Engine. Stale/revoked or cleanup-failed results are discarded; retain the last valid graph marked stale where required. |
| `STOPPED` | Finalize bounded accounting and cleanup outcome; ignore late messages; invalidate every permit. | No implicit resume, replay, or provider/access switch. A new attempt requires a new reservation and current authorization. |

Local revocation and release must share a serialized Engine/supervisor authority boundary: if
revocation wins before handoff, zero content is released. If release wins, cancellation stops the
remaining work and publication, but cannot retract bytes already sent. A revision change similarly
prevents a stale result from replacing the current graph. Durable budget state and the exact atomic
handoff mechanism are NW0-A proof obligations, not capabilities established by C4's in-memory fake.

Engine policy generation and native organizational policy identity are different values. An Engine
lock does not freeze external policy. A repeated settings read, local timestamp, or C4's one-second
test lease cannot close that race. Native authorization requires supported enforcement/change
semantics covering release and in-flight work, combined with independent containment. Where this
cannot be demonstrated, the Claude adapter stays closed rather than inventing a policy generation.

## SDK and authentication adaptation

Use the official SDK's public `query` with a held `AsyncIterable<SDKUserMessage>`. Preserve explicit
model/schema options, result iteration, cancellation, and idempotent close. A private supervisor
release resolves the held input once. Rejection or cancellation resolves pending input as finished
before closing the stream, so an iterator cannot strand cleanup or deliver a late queued message.

C6 proved metadata and live rejection using the documented custom-process hook and explicit native
executable override. Its Mac coordinator launches separately confined SDK/native sibling processes;
nested sandbox application failed. Preserve that evidence. Selecting this layout for shipping still
requires Node/SDK distribution, native artifact, startup, Linux, and complete lifetime measurements.

`initializationResult()` and `accountInfo()` are initial observations. C6's fake `reinitialize()`
returned changed account metadata; freshness for real authentication is unproven. The installed
SDK's runtime settings getter lacks a public Query method. C4's raw control experiment and an internal
request type do not fill the supported policy contract. Missing settings must remain unknown.

Subscription uses the user's selected official login namespace. Only the official client handles
its credentials and rotation. Open Brain does not pass OAuth-token variables, copy tokens, read the
Keychain itself, or silently select a fresh namespace. Build the child environment from reviewed
inputs; the existing helper's broad environment copy and retained OAuth variable are not the launch
policy. The personal router is outside this contract.

Explicit setup/login detection is a separate no-note operation. Its observations cannot create a
semantic permit, and a denied refresh must not trigger it as an implicit recovery action.

API adapters receive the explicitly selected API-key reference through app composition. They resolve
only their own key after Engine authorization, preserve session-only fallback when no approved store
exists, and do not inherit ambient credentials or substitute another provider. API responses need
semantic validation and actual-model attribution; they do not need Claude's filesystem/catalog
attestation. No new user-facing policy setting is introduced by this separation.

Actual-model attribution must come from completion response/usage evidence, not the requested model
or an initialization label. Preserve the existing adapter's response extraction while rejecting an
initialization-only fallback for this contract. NW0 pins each provider's model/alias behavior;
missing or conflicting attribution rejects the result instead of inventing a model identity.

## Attempts, cancellation, and bounded output

Preserve NW0 limits: 16 KiB selected note input and accepted semantic output per attempt, 60 seconds
per attempt, at most two attempts and 90 seconds per probe. The operation deadline includes setup,
preflight, retry delay, validation, and teardown; phase limits are clipped to its remaining time.
The C4-C6 20-second metadata and 64 KiB native diagnostic limits remain probe limits, not extra time
or semantic-output allowance. C6's 128 KiB SDK bridge limit covers framing separately. Shipping
limits must be measured and explicit; do not silently adopt an SDK's unlimited wire output.

Reserve before dispatch and record dispatch intent before writing. A proven pre-dispatch rejection
releases the reservation. A completed, failed, cancelled, or uncertain dispatch consumes capacity.
After a crash in the record/write interval, preserve an uncertain consumed attempt and never replay
automatically. Do not report an uncertain dispatch as a confirmed provider bill or confirmed zero
calls. Request counters and actual model-attempt counters remain separate.

Hidden SDK/client model retries, repairs, or fallback can defeat accounting even with one user
message or `maxTurns: 1`. Each actual attempt must be prevented or observable and reserved before it
occurs. Until that is demonstrated, a native positive attempt does not pass NW0. Explicit retry uses
the same selected provider/access, revalidates all content, and spends fresh capacity within the
overall deadline. No automatic model, provider, access-mode, or billing fallback is authorized.

Abort stops input and publication, closes the SDK stream once, and asks the supervisor to stop and
wait for the entire owned workload. Keep process/pipe escalation within the remaining deadline;
`close()` or a signal is not proof of teardown. Reject late output after cancellation. Bound native
diagnostics and SDK framing before parsing; check depth, shape, request matching, and schema without
logging note text. Unexpected tool/permission/control traffic fails the attempt.

Keep raw client errors and metadata private. Project fixed categories through the common operation
result: authority/content rejection, setup required, policy unavailable/conflicting, cancelled/stale,
timeout, output/protocol failure, or cleanup failure. Reuse existing `BoundaryErrorCode` where it
matches. NW1/NW2 must add a typed projection for the remainder; the existing generic executor wrapper
currently collapses exceptions to `implementation_failure` and must not be described as that new API.

## Missing upstream requirements, prepared locally

This table is a local interface brief, not a claim that these APIs exist or a request sent upstream.
An equivalent supported mechanism is acceptable if it passes the same requirement; exact method
names and new generation counters are not prescribed.

| Required capability | Minimum useful evidence | Current gap and pass criterion |
|---|---|---|
| Active access identity | Same-process provider/access source and selected account identity without token values; defined behavior on expiry, refresh and account change. | Initial metadata and offline status are insufficient. Pass with client-owned existing-login reuse and observable source changes; local labels do not prove server authentication. |
| Effective policy contract | Public typed observation of applicable effective policy, source coverage, unavailable/helper outcomes, and enforcement/change semantics for the actual completion process. | Public Query lacks the demonstrated live settings getter and completeness contract. Pass when required policy cannot be skipped, unreadability rejects, and changes cannot permit an unauthorized release. |
| Completion capability boundary | Supported proof or independently enforced mechanism preventing model tools, ambient context/hooks/plugins, hidden model dispatch and unexpected egress. | Empty MCP/context lists and startup flags are insufficient. Pass hostile-input and ambient-canary tests with bounded attempts. |
| Auth/runtime separation | Official credential/bootstrap/Keychain and rotation operations separable from prompt history, settings discovery, diagnostics and unrelated files. | C5 proved synthetic file primitives only. Pass existing-login reuse, native refresh and cleanup on both platforms without token relay or broad home access. |
| Lifetime and result reporting | Supported cancellation semantics, complete workload ownership, bounded output, actual model/access attribution, and observable attempt behavior. | C6 proved idle metadata teardown and fake results. Pass active synthetic completion, cancellation/escalation, attribution and retention checks within NW0 limits. |

The preferred architecture is a small shared provider component built on these supported completion
capabilities. A separately distributed SDK helper is a viable candidate with measured packaging
cost; Python raw-control integration remains an alternative with an unresolved support contract.
Neither a dedicated second login nor an internal-method cast resolves the missing requirements.
No shared package or new dependency is selected until its distribution and maintenance benefit is
demonstrated. This preserves the strongest architecture option without imposing an untested runtime.

## Acceptance and implementation ownership

Use one semantic dataset with format-specific fixtures for engine records, Markdown, Graphify,
SDK/API messages, and export. Preserve C4/C6 passing cases; add only uncovered boundary cases.
Parameterize the shared semantic suite over all four launch paths and the deterministic fake.
Keep provider-specific authentication, schema dialect, attribution, and runtime tests focused.

| Requirement | Existing evidence | Missing acceptance and owner |
|---|---|---|
| Denied content never dispatches | C4 fake authority/redaction and native rejection; C6 held input. | Shared operations reject excluded/mixed/stale/restored-without-consent requests before adapter construction. NW0-A proves representation; NW1 implements durable authority. |
| Atomic release and accounting | C4 fake permit replay/change tests; C6 fake single yield. | Exercise revocation/release ordering, two-process reservation contention, crash before/after handoff, uncertain dispatch and hidden retry handling. NW0-A/NW0-C proof, NW1/NW2 implementation. |
| Native auth/policy gate | C5 synthetic file boundary; C6 unchanged C4 validator rejects missing auth/settings while alive. | Establish supported pre-dispatch auth/policy, confinement and attempt-limiting mechanisms before the first bounded positive probe. That probe then tests active cancellation, retention and attribution; full passage requires those results. NW0-C feasibility, NW2 transport, NW3 setup. |
| Results preserve evidence and attribution | C6 fake structured result; existing provider output-size validation. | Invalid IDs/evidence/model/schema, oversized wire output, late and stale results reject consistently across four real paths. NW0-C thin matrix, NW2 full contract suite. |
| Complete user flow and portability | Existing capture/retrieval/export operations; structural B1 evidence. | Capture → infer → display evidence → explicit permanent-link acceptance → export through shared operations. NW0-A thin fake proof, NW1/NW2 integration, NW3 desktop checks. |

Pause only stops automatic scheduling; it does not change consent or prohibit one-shot manual
refresh. Plugin unload cancels its owned work. Inference never writes permanent links. Accepting a
suggestion uses the separate owner-authorized, revision-checked operation and records portable
provenance. These remain the agreed product behavior, not transport-level features.

## Checkpoint and next work

C7 specifies and maps interfaces only. It executes no native client, auth/status command, provider,
or model call. It does not claim a new independent review or repeat C4-C6 test counts as new results.
NW0-A schema/authority, frozen Graphify packaging, real-provider matrix and desktop gates remain open.

The Claude experiment is stopped at the explicit missing-capability brief above. Preserve its
working lifecycle and resume authentication work only against a concrete supported mechanism.
C7's next independent experiment was NW0-B2. Its candidates remain failed. B3 demonstrated a
smaller component; [B4](../audits/2026-09-09-ob1-nw0-b4-component-adoption.md) records the authorized
upstream-first/maintained-patch policy and a candidate frontmatter contract. The pure parser passes
Mac content inspection and native smoke but misses the warm-start budget. NW0-B5 addresses startup
and reproducible inputs; controlled cold/Linux evidence remains open. None of this changes the
provider/supervisor contract or authorizes Claude note dispatch.
