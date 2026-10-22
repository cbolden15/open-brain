# W5 P2 re-anchor record

Date: 2026-10-22. Branch `feat/w5-guided-slack-workflow`. Coordinates stay schema/runtime/Portable
21/16/16. The collector anchors P2 calls (`live_capture.py:271,498,729`, `live_manager.py:280,466`)
match REANCHOR-P1.md; no new anchor drift was found.

## Open decision for P4: native failure codes in journal evidence

INTERFACE.md (Codes paragraph) says to "preserve native safe failure codes in private bounded
operation evidence". RECOVERY.md "Journal v1" freezes `OperationEvidence` as a closed per-action
projection; the collector shape is exactly `{source_id,preview_id,receipt_ids}` and has no
failure-code field.

P2 follows the frozen journal shape. A native collector code is mapped to the closed guide enum
and kept only in memory (`GuideAdapterError.native`) for the current invocation; it is not
written to the journal. Refused and unknown operations therefore record only their state.

P4 must choose one of:

1. Keep the frozen shape and amend INTERFACE.md to say native codes are not retained.
2. Add a bounded, allowlisted `native_code` field to the evidence projections. This is a journal
   v1 shape change and needs a schema decision before any build writes it, because older guide
   binaries reject unknown keys.

## P2 behaviour recorded here

- A refused import, or a refused import replay, halts the workflow (journal `status: halted`)
  unless the noncreating metadata read shows the same `preview_id` still pending. A refusal never
  proves that a prior import did not commit. A halted journal refuses every action with
  `receipt_unresolved`; the owner inspects collector state and removes the journal explicitly.
- `operation_unknown` is always reported with outcome `unknown_outcome`.

## P4 decisions

Native failure codes: option 1. The frozen journal v1 shape wins and INTERFACE.md now says native
codes are not retained. A `native_code` field would change journal v1 for evidence that no
reconcile or resume rule reads, and its allowlist would have to track every collector and owner
service code. The mapped guide code still reaches the owner in the result envelope. A refused
operation records `state: refused` with null evidence; the custody quota test asserts that the
native code never appears in journal bytes.

Collector state identity: `binding.collector_binding` now hashes the store paths plus the device
and inode of the collector state root (the parent of `--collector-state`). A different collector
state moved in at the same path is a `binding_mismatch` for `status` and every `advance`. The
journal shape and every collector file are unchanged; nothing new is read from or written to the
collector. A missing state root is `state_uninitialized`. Limitation: replacing only the `live`
directory inside an unchanged root is not detected by this hash. The Brain binding in
`live/capture/brain.json` is checked by the collector itself, and a different Brain is still caught
by `brain_id` and `issuer_epoch`.

Custody quota: the 256-item retained custody quota is now reached through a guide import with real
collector components. Slack history pages hold one message and the journal is limited to 256 KiB,
so one journal cannot import 257 pages on its own. The test stages the other retained items as a
second source's pending receipts in the same real custody store. The refused import reports
`capacity_reached`, keeps the journal `active`, the page pending and every custody receipt.
