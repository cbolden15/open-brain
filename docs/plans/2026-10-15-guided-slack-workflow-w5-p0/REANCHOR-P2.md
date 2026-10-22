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
