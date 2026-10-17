# W5 P0 identity contract v1

Status: frozen specification; no W5 runtime exists yet. All source citations resolve under the worktree recorded in ACCEPTANCE.md. New UI code must use owner services, never direct SQL or text search.

## Exact chain

| Identity | Producer / equality check | Refusal |
|---|---|---|
| Resource selection | `SourceResourceSelection(connector_name,connection_id,resource_id,resource_type)`; choose a returned Slack account/channel row, connector_name=`slack`. Names/aliases are display only. | Duplicate labels require explicit row selection. Unseen resource, changed account, wrong type or incomplete inventory refuses selection; pagination is not proof of absence. |
| Configured collector source | `LiveSourceManager._source_id`: `slack.` plus first24 hex SHA256 of existing `bounded_json(asdict(selection))`. Use returned source ID; formula documents semantics, not permission to guess engine/capture IDs. Before first configure, the collector-owned safe metadata locator accepts the exact returned resource selection, derives this existing default key and reports existing/absent state; configure may omit optional source_id and must agree with its returned ID. No app-side heuristic or source search. Default same account/channel selection resolves the same collector source, even with different privacy/date options. | Existing different selection, options or Brain binding never silently reset. Guide has no reset action. Existing disabled exact configuration may be inspected/reused only after explicit confirmation; enabled/paused source is not adopted automatically. |
| Generation and epoch | `LiveCaptureService.configure`: generation hashes `[selection,options,Brain binding]`; `control_epoch` separately changes control state. Existing `generation` is not a mutation counter. Options fixed to `date_floor` plus `privacy_tier:work`, validated by current provider/policy. Date window means chosen floor through existing collector scan cutoff, not a new selectable end-date feature. | Changed options returns `source_reset_required`; use separate existing owner reset workflow, then new guide/preflight. No reset, disable, cancellation cleanup or custody deletion performed by guide. |
| Preview | `LiveCaptureService.preview`: persisted source/binding/generation/before-checkpoint/intakes/checkpoint/coverage determine `preview_id`. Safe metadata helper validates exact pending document and its digest. | Count/title summary is not body inspection. Expired/missing/changed pending page or source epoch/generation halts. Never call preview just to discover pending state. |
| Custody receipt | Import returns exact `receipt_ids`. `CustodyStore.inspect` returns `receipt_id,source_id,item_id,revision_identity,intake_digest,outcome,capture_id,reason_code,control_epoch`. New metadata helper separately verifies internal binding/generation/observation evidence and emits ReceiptBindingV1. | Require literal receipt ID in this import result, selected collector source, matching Brain marker and supported Slack observation terminal result. Pending/quarantine/malformed/missing/pruned/unrelated generic ingestion receipt remains unresolved. |
| Capture revision | Only supported Slack observation branch supplies `source_receipt.capture_id`. Read literal capture with `RecordTasks.read(capture_id,expected=capture_id,history=False)`, through existing owner app services. | Generic branch may use ingestion_id: never reinterpret. Returned revision and record IDs must match exact capture read semantics; unavailable, hidden or retired evidence cannot be inferred from refusal. |
| Logical engine source | Complete projected record summary supplies `record.source_id`; inspect via `SourceTasks.inspect`. Bind Brain/issuer, current head, head_version, lifecycle_version, route_version, privacy/projection and destination for next owner intent. | Newer head is a revision conflict. Historical read is permitted only via existing explicit history path and cannot release. No current-head substitution, URL parsing, prefix manipulation or lexical identity resolution. |
| Release identities | Selection is an engine grouping of logical source IDs, not collector selection/preview. Sharing preview/approval/copy IDs come only from their frozen owner APIs. Selection membership itself is not revision approval; each sharing preview binds exact current revision/versions. | Literal preview/approval IDs go to sharing inspection; capture/source IDs cannot substitute. Pending copy is not readiness. Corrections require their own selection/approval and explicit owner revocation outside automatic guide flow. |

New `ReceiptBindingV1` is a read-only projection from existing validated custody state, not a custody format change:

```text
{receipt_id:ID,source_id:ID,generation:string,control_epoch:int,
 binding_verified:bool,current_generation:bool,
 supported_slack_observation:bool,
 outcome:"pending"|"captured"|"duplicate"|"quarantined"|"history_only",
 capture_id:ID|null}
```

Only validated known receipt outcomes are mapped; an unknown native outcome makes that receipt unavailable. Receipt membership must be checked against retained exact import receipt IDs. Matching source alone is insufficient. If native completed outcome names differ, normalize only through an explicit tested mapping, not arbitrary string acceptance. Current-generation false permits truthful retained-history display but prevents guide release. Metadata validates receipt digest/evidence internally; it does not expose source_reference, intake bodies or raw observation evidence. A legacy generic receipt never gets `supported_slack_observation=true` merely because capture_id is populated.

## Inspection and races

Before routing or previewing release, read the complete permitted projection at exact expected revision, compute its existing projection identity, and display the whole body using safe terminal rendering. Persist only revision/projection digests and owner acknowledgement. Revalidate current head/versions immediately at each authoritative operation; do not rely on journal freshness. Source edit, withdrawal, reroute, privacy/projection/policy/consent change invalidates intent. Read-snapshot and output-boundary validation remain engine/sharing responsibilities; guide orchestration must preserve those calls. No database lease across child execution or owner prompts.

Raw-source inspection and release-preview inspection are separate. Existing sharing inspect must present the entire exact proposed external payload and provider/destination scope before approve. The original source remains local-only; route is assignment, not egress authority. Dedicated Work destination is the v1 flow; broad reused spaces are refused in this guide rather than treated as selected-only client access. Existing advanced workflows remain unchanged.

## Source anchors

Baseline `377d238`; collector `live_manager.py:322,466`, `live_capture.py:62,178,271,477,498,729`; `custody.py:93,141,180,478`; connector `runtime/source_registry.py:166`, `runtime/slack_live.py:174,545,806`; engine `engine/records.py:115,177,464`, `engine/sources.py:311`, `engine/sharing_contracts.py:101,166,190`. Package roots and absolute document links are in ACCEPTANCE.md. These are source observations, not fresh runtime acceptance.
