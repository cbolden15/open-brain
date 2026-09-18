# T09 collector recovery contract

Status: T09 locally verified; see [the checkpoint](../ai/workstreams/20260918-open-brain-public-m3-source-continuity-owner-control-c63538/T09-CHECKPOINT.json). Full A06 remains open for deferred T10 continuity.
Baseline: `3070e33a24ebd1e44a81b51f5d1c9cdb66b46935`. Schema 7/session 2 and T03 v1 stay unchanged.

## Observed baseline

`live_capture.apply` preserves a complete pending preview and saves committed revisions per item, but a conflict prevents later independent items. Its duplicate cache compares provider revision identity without the complete payload digest. `preview` reuses staged payloads and acknowledges the previous locally committed checkpoint before fetching. Pause/reset invalidate previews through generation/control epoch checks.

`lifecycle.sync_due` saves only intake identity metadata in `active_run`, refetches after interruption, and stops at the first capture exception. Its checkpoint save is atomic replacement but lacks file/directory fsync. Neither collector path offers replayable quarantine, quotas, or owner recovery controls.

Existing coverage: collector integration `test_live_capture.py` covers staged replay, acknowledgement, pause/reset and immutable changed-delivery refusal; `test_recovery.py` covers idempotent sink replay and checkpoints; `test_failure_modes.py` covers ownership, credentials and storage failure. These do not prove durable per-item conflict containment or quarantine quota behavior.

## Bounded implementation

1. Both live and legacy collector entrypoints must retain complete, validated intake custody before attempting capture. Use private no-follow, owner-only durable storage and bounded locks. Receipts bind the selected Brain where available, source selection/generation, control epoch, exact revision key and canonical intake digest (payload plus metadata). Pending/captured/duplicate/quarantined states are explicit. Production sink success retains actual engine capture receipt identity where supplied; never invent capture IDs. Keep injected test sink compatibility explicit, and preserve history_only when the later T10 adapter returns it. Do not change frozen T03 wire DTOs or engine schema.
2. Quarantine only a recognized typed item conflict. Introduce a ValueError-compatible typed engine delivery conflict at the existing immutable conflict site if needed; arbitrary ValueError, storage/disk/auth/lease/cancellation/infrastructure exceptions stop the batch and leave checkpoint unchanged. T03 source_revision_conflict is an item conflict; revision_changed/control-epoch or operation_pending is not automatically an item conflict.
3. When A conflicts, persist its replayable intake and receipt before continuing independent B. A checkpoint may advance only after every item has captured/duplicate receipt or durable quarantine custody. Provider acknowledgement follows durable local checkpoint commit. Crash/restart must replay staged payloads without needing provider refetch. Capture-before-receipt interruption relies on engine idempotency; receipt-before-checkpoint interruption reuses exact custody.
4. Never use revision identity alone to bypass the sink for different bytes. Different payload/metadata under the same key must reach conflict handling. Quarantine IDs are opaque deterministic digests over exact binding/item content; identical retries do not multiply custody. Bound both total retained items and UTF-8 bytes with named limits. Quota exhaustion is visible backpressure: no checkpoint advance or payload eviction. Completed ordinary payload custody can be released after durable checkpoint; quarantined custody survives until successful explicit retry.
5. Owner inspect/status/retry works through collector CLI; live sources also use shared manager/control dispatch. Default inspect/status contains only IDs, outcome, safe typed reason and counts; no body, credentials, raw upstream keys, private paths or raw exceptions. Retry targets exact receipt, revalidates current selection/generation/control epoch, and uses the same capture/idempotency path. It never silently invents ordering or edits payload. Quarantined custody from stale/reset selections stays visible but refuses replay. Pause/disable/reset must prevent any old in-flight commit after acknowledgement; no accidental source/schedule enablement.
6. Preserve optional package boundaries and foreground behavior. No services, live sources, engine compatibility relaxation or source-head promotion is part of T09; immutable intake ordering integration belongs to T10.

## Required evidence

Synthetic tests on both collector paths cover: blocked A then successful B; complete checkpoint custody; conflict and successful receipt persistence failures; capture-before-receipt and quarantine-before-checkpoint crashes; restart without refetch; acknowledgement failure/retry; same revision with changed body or metadata; inspect/retry and stale selection; item and byte quota backpressure; cancellation races; auth/disk/generic ValueError global failures that cannot be quarantined. Preserve existing package and compatibility regression tests. Independent review, focused collector/connectors/engine tests, integrated diff/actionlint and coordinator make verify are required before verified T09 acceptance. T10 still owns the routed A1/A2/B1 publication portion of A06.

## Independent review clarifications

The first candidate was not accepted. Five reproduced recovery failures require these explicit
properties before T09 integration:

- Every legacy selection/control read-modify-write and terminal/failure update shares the same
  process barrier as capture. Selection replacement cannot acknowledge before an older capture
  finishes, overwrite newer controls with stale state, or inherit an unrelated provider cursor.
- Successful retry retains intake bytes while an unfinished batch still references them.
- Checkpoint commit records a durable cleanup obligation. Cleanup is idempotent, protects its
  receipt references from pruning, and finishes before later staging can reuse deterministic IDs.
  Recovery handles interruption after checkpoint, after payload release, and before marker clear.
- Pause/resume and disable/enable transition active batch references explicitly. They preserve the
  prior checkpoint and quarantined custody without permanently stranding future collection or
  accepting old-epoch work after acknowledgement. Once cancellation is durable and the unfinished
  batch is detached, recoverably discard its unacknowledged non-quarantined pending payloads;
  preserve captured engine evidence. Repeated cancellation must not exhaust quota with unusable
  pending payloads.
- Every production path uses the same opaque root/root-identity/tenant fingerprint. An engine-created
  capture sink supplies that fingerprint, which the collector validates even with an explicit
  Brain root. No tenant-only fallback or private engine introspection substitutes for the binding.

Required proof also includes actual custody temporary-write/fsync/replace failure seams, a
separate-process reset barrier, mixed-page global failure, competing-source quota admission, the
resolved-receipt retention edge, and successful live CLI/control receipt operations. Test reports
must distinguish observed cases from inferred coverage.
