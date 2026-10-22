# W5 P0 interface contract v1

Status: specification, not implemented. Source baseline `377d238` on `feat/slack-cited-recall-w4-20261015-6d407b`. P1 requires separate authorization. Core schema/runtime/Portable stay `17/12/12`; this document freezes additive guide interfaces, not existing collector or engine DTOs.

## Commands

All three commands require these explicit absolute paths: `--data-dir BRAIN`, `--collector-state STATE`, `--workflow-state JOURNAL`, `--collector-executable EXEC`. No ambient/default Brain or PATH executable discovery. STATE has the existing collector `sources --state` meaning: live storage is `STATE.parent/live`, not STATE itself. Reject path aliasing between journal, Brain, collector state and client configuration. No directory is created by status or preflight.

```text
open-brain source guide slack COMMON
open-brain source guide status COMMON [--json]
open-brain source guide advance COMMON --decision-file FILE --json
```

COMMON means the four required flags above, in any order after the leaf command. `slack` is interactive and requires a TTY; reject `--json` and non-TTY input. `advance` is noninteractive, never prompts and performs exactly one named action. `status` reads only existing state and never updates journal timestamps. No `--yes`, reset, arbitrary command passthrough, background flag or provider inference. Unknown/duplicate flags refuse before bootstrap. Register and dispatch this command family in local_entrypoints before ordinary root selection/bootstrap: status and prerequisite failures must never enter normal engine startup. Later owner writes open existing owner services only at their explicit phase boundary; no engine/database lease survives a child launch or prompt. The existing `source` actions and outputs remain unchanged.

`FILE` is an owned regular no-follow 0600 file under an owned 0700 parent, max 65,536 UTF-8 bytes. Strict JSON rejects duplicate keys, nonfinite numbers, extra keys, excessive nesting (16), malformed Unicode and invalid IDs. Paths are not sourced from Slack text. New JSON DTOs use exact keys, nullable fields remain present, integers exclude booleans. IDs are bounded opaque strings, 1–512 characters without controls; cursors alone permit 8,192. No guessed parsing of IDs.

LaunchContext is exactly `{surface:standalone-cli,cwd:absolute_context_path,profile:string|null,setting_sources:[user,project,local],settings_file:null,overrides:[],managed_state:owner-declared-absent}` using string literals for the words shown. Profile is null except an existing supported Codex profile. Current source-pinned versions are claude-code2.1.300 and codex0.162.1; these are the finite observer supported versions, not claims about latest installed clients. Unsupported versions/contexts refuse; no real client is launched to determine them.

## Decisions and phase results

Decision envelope (all keys required):

```json
{"schema_version":1,"workflow_id":"guide:synthetic-1","expected_revision":0,"action":"start","confirmation":null,"arguments":{}}
```

`expected_revision` is a nonnegative integer. `workflow_id` is caller-chosen only for `start`, immutable afterward. The journal stores exact validated canonical bytes and their SHA-256 before any authority call. Canonical JSON means UTF-8, sorted keys, compact separators, no nonfinite numbers; preserve that byte sequence, never reconstruct it from current state. A confirmation is either null or `{ "intent_sha256": HEX64, "decision": "approve" }`, bound to a preceding owner-visible intent result. Intent includes action, exact request/preconditions, relevant executable identity and destination/provider choices. Changed content or versions invalidate it. Interactive prompts produce the same decision DTO and use the same coordinator.

The `arguments` object is closed by action; keys in parentheses are optional, every other key required:

| Action | Argument keys and types | Confirmation |
|---|---|---|
| start | none | null; explicit invocation authorizes only creating this journal |
| preflight | `client_kind`: claude-code or codex; `scope`: project or user; `context_path`, `consent_state`, `policy_path`, `runtime_path`: absolute owner-selected paths; `provider_id`: existing provider ID; `client_version`: string(max80); `launch_context`: LaunchContext (closed existing DTO below) | null; finite config inventory, no config bodies retained |
| accounts | none | null |
| resources | `connection_id`: ID; `cursor`: string or null | null; max five pages in this advance |
| choose | `connection_id`, `resource_id`, `resource_type`: IDs; `date_floor`: timezone-aware ISO8601; `account_alias`: string or null, max80 | null; Work privacy fixed, display hints never authority |
| configure | none; exact selection/options are already in intent | required |
| preview | none | required: preview fetches/persists local state |
| import | `preview_id`: ID | required: one existing inspected preview only |
| inspect | `receipt_id`: ID | null; complete current permitted projected body, no metadata substitute |
| acknowledge-inspection | `receipt_id`: ID; `inspection_sha256`: HEX64 | required; binds the complete displayed projection/revision, never a claim of model comprehension |
| destination | `space_id`: ID | null; inspect existing dedicated Work space only |
| create-destination | `name`: safe display string max80 | required; ambiguous acknowledgement permits only exact conditional delivery replay per RECOVERY.md |
| route | `receipt_id`: ID | required; expected source versions and destination from intent |
| consent | `provider_id`: existing supported provider ID | required; explicit grant only, existing active incompatible consent refuses |
| select | `member_receipt_ids`: unique ID array1–24 | required; freezes ordered logical-source membership; per-revision inspection remains separate |
| release-preview | `receipt_id`: ID; `provider_ids`: unique ID array1 (one provider per workflow) | required; uses existing per-message preview, no bulk approval |
| approve | `preview_id`: ID | required; existing frozen sharing request from inspected intent |
| policy-preview | none; explicit policy path and chosen scope/consent/destination | null; inspect proposed existing launcher-policy.v1 payload |
| policy-apply | `policy_sha256`: HEX64 | required; create-only owner file, no overwrite |
| setup-preview | none; context/provider/space choices already frozen | required; only search/content-read, current consent and policy |
| setup-apply | `preview_id`: ID | required; no client launch |
| reconcile | `operation_sequence`: nonnegative int | required; exact saved confirmed operation only, per RECOVERY.md, never next phase |
| cancel | none | null; journal terminal marker only, no undo of completed work |

Actions can return an intent without executing their authority call: if confirmation is null where required, return `needs_confirmation` with the exact normalized proposed intent. No automatic second action. `inspect` must precede acknowledgement, routing and release for that exact current projected revision. JSON mode exposes complete inspection in its ephemeral result; bodies never enter journal/logs. Reading a body is not release approval. `select` must not silently replace a previous selection; changed membership starts new inspected intent. Corrections/revocations are explicit existing owner workflows, never automatic cleanup.

Every command emits one JSON object in JSON mode, even for input/refusal errors. No prompt, traceback or raw child stderr goes to stdout. Closed result envelope:

```text
{schema_version:1, workflow_id: ID|null, revision:int|null,
 action: Action|"status", outcome: "completed"|"needs_confirmation"|"partial"|"refused"|"unknown_outcome"|"cancelled",
 code: Code|null, intent: Intent|null, data: Data|null,
 next_actions: Action[0..5], retry_after_epoch:number|null}
Intent = {action:Action, intent_sha256:HEX64, request_sha256:HEX64,
 summary:string(max1024), canonical_request_utf8:string(max65536), expected_versions: VersionRef[0..24],execution_binding:ExecutionBinding}
ExecutionBinding={brain_id:ID,issuer_epoch:int,collector_binding:string|null,source_generation:string|null,control_epoch:int|null,executable:{path:string,sha256:HEX64,device:int,inode:int}}
VersionRef = {kind:"source"|"route"|"policy"|"consent"|"selection"|"configuration"|"collector", id:ID, version:string(max512)}
```

Data is a tagged, closed union: `{kind:"progress", phase:Action, source_id:ID|null, preview_id:ID|null, generation:string|null, count:int|null, policy_sha256:HEX64|null, receipt_ids:ID[0..25], selection_id:ID|null, approved_copy_ids:ID[0..24], has_more:bool|null}`; `{kind:"accounts", rows:Account[0..1000], has_more:bool}`; `{kind:"resources", rows:Resource[0..1000], next_cursor:string|null, has_more:bool}`; `{kind:"inspection", receipt_id:ID, capture_id:ID, source_id:ID, revision_id:ID, projection_sha256:HEX64, body:string, current:bool}`; `{kind:"health", report:HealthV1}; `{kind:"owner_preview",subject:"policy"|"sharing"|"setup",preview_id:ID|null,payload_utf8:string,payload_sha256:HEX64}`` as frozen in HEALTH.md. Account=`{connection_id:ID, display_name:string(max512), alias:string(max80)|null}`. Resource=`{resource_id:ID,name:string(max512),resource_type:ID}`. Hash preimages are exact: request_sha256=SHA256(UTF8(canonical_request_utf8)); intent_sha256=SHA256(canonicalJSON({action,canonical_request_utf8,request_sha256,expected_versions,execution_binding})). The display summary is not authority and is excluded. All authority-relevant request/precondition/executable fields are included in this preimage; confirmation must be invalidated if any differs. Expected versions are ordered as emitted and must not be reconstructed/reordered on resume. The exact non-content normalized request and every applicable current version/precondition must be shown, not just a hash/summary. Native authority checks still run at execution. Intent details requiring complete owner inspection are displayed from authoritative ephemeral reads, not condensed into `summary`. Owner preview payload is the complete external sharing projection, proposed policy file, or proposed Open Brain owned setup fragments only, never unrelated config secrets. Sharing preview IDs and policy hashes remain separate. Failed/unknown results return data only for already proven completed work. No model answers or raw provider responses.

Exit codes: 0 completed/cancelled; 2 invalid CLI/decision; 3 needs_confirmation/partial; 78 refused/unknown_outcome. Codes are a closed guide enum: `invalid_request`, `unsafe_path`, `collector_unavailable`, `collector_incompatible`, `collector_changed`, `state_uninitialized`, `binding_mismatch`, `revision_conflict`, `unscoped_client`, `source_reset_required`, `receipt_unresolved`, `inspection_required`, `authority_refused`, `retry_wait`, `capacity_reached`, `snapshot_inconsistent`, `operation_failed`, `operation_unknown`, `journal_incompatible`, `journal_busy`. Map each native safe failure code to this closed enum and unknown native codes to `operation_failed`. Guide v1 keeps the native code only in memory for the current invocation; the closed journal v1 evidence shape in RECOVERY.md has no native-code field, so native codes and provider strings are never written to the journal (decision recorded in REANCHOR-P2.md). `next_actions` never grants authority. A missing retry timestamp stays null.

## Optional child protocol and executable trust

Add `open-brain-collector --guide-capabilities-json` before ordinary collector initialization. It takes no Brain/state arguments, makes no file/network/credential access, and returns exactly:

```json
{"schema_version":1,"package":"open-brain-collector","guide_protocols":[1],"metadata_protocols":[1],"lifetime_guard":"inherited-flock-v1","operations":["sources.accounts","sources.resources","sources.configure","sources.preview","sources.import","sources.custody_inspect","sources.guide_metadata"],"compatibility":{"schema":17,"runtime":12,"portable":12}}
```

Require protocol1, metadata1, inherited-flock-v1, exact package and compatibility coordinates, and every listed operation; unknown versions refuse, additional operations do not expand allowlist. Existing `--boundary-json` and `0.1.0` are insufficient and remain unchanged. Capability metadata is compatibility evidence, not executable provenance.

Existing calls use argv `EXEC sources --state STATE --brain-root BRAIN --guide-guard-fd FD --foreground COMMAND ...`; resources/accounts include `--provider slack`, configure uses an owned 0600 temporary arguments file containing only the existing closed selection/options request. Adapter allowlist is exactly the seven operations above. Never call generic `request`, control socket, connect, control/schedule, discover, runner or background service. `sources.guide_metadata` is exposed as `EXEC sources --state STATE --brain-root BRAIN --foreground guide-metadata (--source-id ID | --selection-file FILE)` through an early read-only dispatch; no LiveSourceManager constructor. The selection file is a strict existing SourceResourceSelection object (Slack only), max64KiB/owned0600, used only before a configured source ID is known. Exactly one locator is required. The collector computes its existing deterministic default source key from the validated returned resource selection and reports whether that source exists; the app never guesses capture/logical-source identities. Status with no selected source returns state_uninitialized without creating a selection file. Its exact result is HealthV1. Inert handshake and guide-metadata use no guard and create no lock files; stateful legacy operations require the inherited guard. The new --guide-guard-fd parser option preserves the FD until process exit and never resolves a path from it. Missing/wrong guard capability refuses guided mutation, without changing legacy callers. This command can operate on missing state without creating it.

The collector is a trusted same-user program, not an OS-isolated principal. Its engine capture capability constrains application calls; it cannot contain malicious native code running as the owner. Resolve an explicitly selected installed executable; allow normal installer symlinks only after canonical target and directory validation. Target must be regular, executable, owned by owner or root, with no group/world write permission; intermediate installation directories cannot be attacker-writable. Record canonical path, device/inode, SHA-256 and metadata; recheck immediately before every launch and resume. A change requires fresh executable selection and invalidates pending intents. There remains an acknowledged same-user replacement race: hashes and handshake are not a sandbox. If this trust model is unacceptable, stop and re-scope the separately distributed owner frontend; do not invent OS sandboxing inside W5.

Spawn one argv child without shell, close inherited descriptors except the one audited lifetime-guard FD for stateful operations, stdin DEVNULL, separate process group; bounded simultaneous draining of stdout/stderr. Minimal environment: canonical owner HOME, fixed system PATH, LANG=C.UTF-8 and PYTHONNOUSERSITE=1; no ambient provider keys, PYTHONPATH or proxy variables. Existing credential references remain collector-owned. Validate supported installed collector launch under this environment; no removed-worktree editable imports. Response limits: handshake16KiB; all source operation stdout4MiB, stderr16KiB; guide JSON4MiB, decision64KiB, journal256KiB. No raw stderr echo. Reject oversized/invalid/extra JSON without assuming operation failure. Drain pipes incrementally to prevent deadlock/unbounded allocation.

Supervisory deadlines: handshake10s; metadata30s; accounts/resources120s per child; configure/preview/import/custody inspection3600s per child. These new outer bounds do not reduce existing HTTP30s default/60s maximum or credential helper15s deadlines. They are ceilings, not latency promises. Timeout/cancellation terminates child group, allows2s reap, kills/reaps remaining children and records unknown outcome if work may have begun. No retry loop, no new provider deadline override. Validate limits against real synthetic maximum fixtures; capacity refusal is preferable to truncation. Invocation budgets: max five inventory pages per advance, one preview or one import per advance, five capture pages per interactive invocation, 1000 retained references,24 release members, one child at a time. Existing custody256items/2MiB and generic resolved512 pruning remain stricter admission authorities.

## Safe display and compatibility

All untrusted names, message bodies, metadata and notices use a single safe renderer: preserve visible text and newlines, visibly escape C0/C1/DEL/ESC, OSC, bidi controls and dangerous format controls; do not execute terminal links/clipboard sequences. JSON strings use valid JSON escaping; human rendering never prints raw decoded controls. Display escaping never changes engine source bytes, projection digest or approved copy. Full content beyond output capacity refuses inspection; never mark truncated text inspected. No automatic pager/shell execution.

No new dependency on collector/connectors enters app/native core. No MCP guide/health tool, new inference provider, privilege isolation, listener, scheduler or compatibility migration. Journal v1 is the sole new app-local persistence. Existing receipt formats and setup/sharing/provider contracts remain authoritative.
