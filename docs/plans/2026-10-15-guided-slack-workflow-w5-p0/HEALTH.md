# W5 P0 read-only health contract v1

Status: feasible specification with deliberately limited claims. Existing stores have per-file atomic replacement, independent locks and sequential multi-file writes. No shared committed generation exists. Configuration `generation` hashes selection/options/binding; it is not a transaction number. Global atomic consistency and exact retained provider retry deadlines are unavailable under the existing formats. P1 must not add a manifest, lock creation, migration or stronger binary fence to disguise those limits.

## Reader and feasibility decision

New collector-owned `read_source_metadata(*,live_root:Path,brain_root:Path,source_id:str|None=None,selection:SourceResourceSelection|None=None)->HealthV1` in `packages/collector/src/open_brain_collector/source_metadata.py`. Exactly one source locator is required. Before configure, a validated Slack selection lets this collector-owned reader derive the existing default source key using the existing pure _source_id algorithm and report existing/absent state; it never treats an absent source as configured. Nondefault advanced collector sources are not discovered/adopted by the guide. Add early `guide-metadata` CLI dispatch as frozen in INTERFACE.md, before existing manager construction, control IPC or ordinary engine bootstrap. This helper is a projection/validation boundary only. No `LiveSourceManager`, `PriorityRuntime`, `LiveCaptureService`, `PrivateJsonStore`, credential helper, network transport, engine bootstrap, processing workflow or lock constructor may execute. Pure existing validators may be extracted without changing their existing callers' receipt semantics.

1. Walk explicit existing paths through directory descriptors with `O_DIRECTORY|O_NOFOLLOW`. No resolve/mkdir/repair fallback. Reject symlink leaves/ancestors, nonregular files, hardlinks, wrong owner/private modes, FIFO/device and invalid bounded names. Collector private files use current owned0600/private-directory0700 requirements. For this guide only, Brain root and manifest must also be owner-private; otherwise return unavailable/unsafe_path with no chmod. This is a conservative guide prerequisite, not a change to engine admission or installed binary compatibility. Normal canonical OS path aliases must follow existing explicit platform allowlist, not arbitrary user symlinks.
2. Read existing `brain.toml` (16KiB maximum) and verify current identity/profile syntax without engine initialization. Reproduce capture binding from canonical explicit root, root device/inode and tenant using existing `live_capture.py:62` algorithm; require existing `live/capture/brain.json` equality. Missing binding is uninitialized, never invented. Do not put tenant, raw path or binding preimages in ordinary health output.
3. Read selected source, selected account, current pending preview if referenced and custody only. Per-file limit4MiB, total bytes read16MiB per pass, at most five files plus manifest/marker. The referenced account/source names are derived only by existing filename algorithms; never concatenate provider-controlled paths. Refuse oversized/broken records rather than truncate. Validate full existing schema/digests, then immediately discard content-bearing fields from output. A missing optional file is unavailable, not an empty healthy store.
4. Record each file's fd/path identity, size, mtime_ns, ctime_ns and content SHA; fstat before/after read, compare current path to fd, retain directory identity. After all reads, re-open/re-hash every participating file once using the same safe traversal, with the same byte budget per pass. Replacement, changed bytes, disappearance or ancestor replacement yields inconsistent and no cross-store joins. No sleeps/retry loop, directory enumeration or new lock. Tokens are ephemeral opaque digests over metadata commitments, not exposed source-content hashes.
5. Validate source selection/Brain/account relation, generation, pending before-checkpoint/preview digest and custody source/binding/observation evidence. Stable mismatches yield inconsistent/unavailable; never repair. Stable interrupted multi-file work may be indistinguishable from a normal intermediate state, so all cross-store global claims remain unknown. Per-store evidence can be shown separately; a matching receipt relation is a checked relation, not a global transaction snapshot. Status never authorizes owner mutations; those use current engine/collector checks again.

The reader can deterministically detect changes it observes. It cannot prove that no writer changed A→B→A between observations or that separate files form one committed transaction. No v1 overall healthy/collecting/caught_up flag is permitted. This resolves P0 snapshot feasibility without existing-store changes; stronger global snapshots require new scope/estimate.

## Closed HealthV1 DTO

All keys required; null denotes unknown, never synthetic zero. ID/string bounds and strict JSON rules follow INTERFACE.md. Epochs are finite nonnegative numbers, counts nonnegative integers. Arrays bounded by current producer limits; reason codes max16, coverage codes max25, receipt_bindings max768 and overall output4MiB. A selected source's retained observation quota256 and generic resolved512 remain existing authorities, not new admission limits.

```text
{
 schema_version:1,
 snapshot:{consistency:"per_store"|"inconsistent"|"unavailable"|"uninitialized",
           global_consistency:"unknown",retryable:bool,reason_codes:Reason[0..16],
           evidence_tokens:EvidenceToken[0..7]},
 account:{registration:"registered"|"absent"|"unavailable",connection_id:ID|null,
          display_name:string|null,credential_status:"unknown",
          last_access_failure_code:Failure|null,last_access_failure_epoch:number|null},
 source:{source_id:ID,generation:string|null,control_epoch:int|null,
         configured_status:"enabled"|"disabled"|"paused"|null,
         activity:"manual_idle"|"awaiting_inspection"|"paused"|"blocked"|"unknown",
         schedule_running:"unknown",stored_next_run_epoch:number|null},
 pending_preview:{status:"present"|"absent"|"unavailable"|"inconsistent",
                  preview_id:ID|null,count:int|null,has_more:bool|null,
                  coverage_codes:Coverage[0..25]},
 execution:{outcome:"completed"|"failed"|"unknown",finished_epoch:number|null,
            captured_count:int|null,duplicate_count:int|null,quarantined_count:int|null,
            last_successful_completion_epoch:number|null,failure_code:Failure|null},
 coverage:{status:"partial"|"unsupported"|"unknown",has_more:bool|null,codes:Coverage[0..25]},
 recovery:{retry_deadline_epoch:null,retry_deadline_proven:false,
           unresolved_receipt_count:int|null},
 custody:{scope:"selected_source",status:"available"|"unavailable"|"inconsistent",
          counts:{pending:int,captured:int,duplicate:int,quarantined:int,history_only:int}|null,
          retained_items:int|null},
 receipt_bindings:ReceiptBindingV1[0..768],
 processing_backlog:{scope:"global",status:"unavailable",reason_code:"not_read_by_v1"},
 release:{status:"unavailable",reason_code:"app_workflow_owned"}
}
EvidenceToken={store:"brain"|"marker"|"account"|"source"|"preview"|"custody",token:HEX64}
```

ReceiptBindingV1 is frozen in IDENTITY.md. Counts include retained selected-source generations, not just current pending generation; current-generation evidence is labeled per receipt. If selected-source custody cannot be validated, counts=null and bindings empty; never infer zero from absent custody. Global processing/release are intentionally unavailable here. App status can render its existing journal progress alongside collector health, clearly labeled recorded progress rather than current approved-copy authority. Fresh selection completeness needs an ordinary authoritative owner inspect outside the no-bootstrap status path; status cannot infer it from saved approval IDs.

Reason enum: `state_absent`, `binding_absent`, `binding_mismatch`, `unsafe_path`, `invalid_state`, `size_limit`, `changed_during_read`, `relation_mismatch`, `legacy_unknown`, `not_read_by_v1`, `app_workflow_owned`. Failure enum: `access_denied`, `credentials_unavailable`, `rate_limited`, `operation_failed`; retain only a proven safe mapping from existing allowlisted source failure codes; all other native failures map to operation_failed, not raw text. Exact mapping: not_allowed→access_denied; auth_required/credential_unavailable→credentials_unavailable; rate_limited→rate_limited; every other native failure→operation_failed. Only retained known auth/access failures populate last_access_failure fields. Current source has no provable error history, so absence remains null.

Coverage enum deliberately normalizes existing notices: `partial_page`, `thread_partial`, `upstream_unknown`, `provider_limit`, `unsupported_coverage`, `legacy_unknown`. Exact current Slack notice map: partial_thread_coverage→thread_partial; message_deleted→upstream_unknown (deletion observation is not a coverage guarantee). Absence of fields→legacy_unknown; has_more=true additionally→partial_page. All other future/forged notices→upstream_unknown; unknown/forged notices become `upstream_unknown`, never interpolate raw strings. `has_more=true` implies partial_page. `has_more=false` is page/window traversal evidence only; overall status stays unknown unless a specific unsupported/partial notice applies. No complete enum exists.

## Field evidence

| Section | Authoritative evidence | Interpretation |
|---|---|---|
| account | `live/accounts/slack/slack-account-<sha256>.json`, schema1 account fields, filename matches connection ID | Registration only. Do not read credential_ref or test token. Credential status always unknown. Allowlisted latest source access error may supply last observed error/time; never claim present token health. |
| source | `live/capture/source-<sha256(source_id)>.json`, existing schema/binding/generation/epoch/status | disabled with no proven pending page => manual_idle; valid selected pending => awaiting_inspection; paused=>paused; inconsistent=>unknown; known unresolved/quarantine may be blocked. Enabled/due interval proves neither collection nor retry. |
| pending_preview | Existing referenced `preview-<digest>.json`, source generation/binding/checkpoint and preview hash | ID/count/has_more/allowlisted coverage only; bodies and source references private. New metadata entrypoint may expose existing preview identity, never fetch a page. |
| execution | Selected source `last_run`, validate interior schema separately because current `_load` only requires a dict | Completed counts/time remain execution evidence even if partial/quarantined. Failed latest run gives failure/time only. Last successful completion equals latest finished time only for validated completed outcome; if overwritten by failure, older success unknown. |
| coverage | Selected source last_run/pending page fields and supported notices, labeled with their evidence source | No runner receipt substitution. Unsupported upstream edits/reactions/absence cannot be repaired by has_more=false. If page and last_run differ, display them separately rather than merge into a single coverage claim. |
| recovery | Current custody pending/quarantine, validated selected source | Stored next_run includes fallback scheduling; no exact retained provider retry proof. Return null/false for deadline. Interactive operation may display an ephemeral proven rate-limit response before exiting; status cannot persist it into existing stores. |
| custody | Validated custody receipts filtered by selected source and Brain; existing per-receipt digest/observation evidence | Selected-source retained counts, not engine backlog or approval completeness. Old generations may be present but never qualify a current release. |

Runner `capture-health/runs.json` and processing queues are not read in v1. Existing health defaults are not used: `live_manager.py:529` reads absent top-level custody.quarantined_count as zero and is unsuitable for the guide. This discovered metric issue is recorded as a gotcha; no unrelated runtime fix is included in P0.

## Source anchors

Connector `runtime/live_storage.py:29,36,81,105,161`, `runtime/source_host.py:16`, `runtime/slack_auth.py:59,163,345`; collector `live_capture.py:62,146,178,271,408,477,551,851,924`, `custody.py:180,456,478`, `live_manager.py:105,529`, `capture_health.py:113,117,209,320`. Absolute package roots and test mapping are in ACCEPTANCE.md. No source format or permissions are modified by this specification.
