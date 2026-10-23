# Portable Brain 18

The development candidate pairs Portable 18 with local schema 23 and runtime
session 16. It inherits every Portable 17 file and its bytes unchanged, and adds one
evidence family for follow-ups (commitments and open questions). Portable 1–17 validators
and migrations 1–22 keep their original bytes and meanings. Portable 17 and older readers
refuse a Portable 18 snapshot before any destination write; they do not parse its
inherited subset. Clients upgrade together under exclusive runtime admission.

| Family | Contents |
|---|---|
| `history/followups/followups-v1.json` (`ob.followups.v1`) | One canonical JSON object with `contract`, `items`, `evidence` and `decisions`. |

The file is required. `items` holds one row per follow-up sorted by `item_id`: `kind`
(`commitment` or `question`), `project`, `summary`, `owner`, `owner_name`,
`owner_confirmed`, `due`, `due_evidence_id`, `state`, `snoozed_until`, `snooze_return_state`,
`ticket_key`, `ticket_url`, `privacy_tier`, `superseded_by`, `resolution`, `version`,
`created_at`, `updated_at`, `new_evidence_since_decision` and `evidence_retracted`. `evidence`
holds one row per cited revision sorted by `evidence_id`: `item_id`, `ordinal`, `role`,
`capture_id`, `revision_id`, `quote_start`, `quote_end`, `quote_sha256` and `recorded_at`.
`decisions` is the append-only log sorted by (`item_id`, `sequence`), `sequence` contiguous
from 1 within each item, with `decision_id`, `operation_id`, `request_sha256`, `principal_id`,
`action` (`propose`, `add_evidence` or a decision action), prior and new state and version,
`evidence_ids` and `recorded_at`. The live store's `receipt_json` (the original result returned
for a replayed `operation_id`) does not travel; after restore, replaying a pre-export
`operation_id` with the same request returns the item's current state.

Quote text never travels. Evidence carries the position and SHA-256 of the quote inside the
cited revision, and readers rebuild the quote on each read against the source's current head
(`current`, `moved`, `source_changed` or `retracted` are read-time states, not archive fields). When owner
retraction has withheld a cited capture, the item is exported with `evidence_retracted`
true and `summary` null, so nothing the retracted revision said remains in the archive.
Every item has at least one `origin` evidence row and at most 50 rows; a Brain holds at
most 10,000 items. Anything outside those bounds, a non-canonical file, a missing family,
an unknown identity reference or a non-contiguous decision sequence refuses before
destination mutation. The manifest inventory covers the file like every other payload.

Restore validates the family before any inherited write, installs the inherited Portable 17
state, then inserts items, evidence and decisions into the empty follow-up tables and
records the `followups_restored` checkpoint. Visibility is not restored: each session's
view is recomputed on read from the current tier, space, lifecycle and egress rules, so a
restored item is hidden from a client exactly when any of its evidence is unreadable to
that client. Portable 17 and older inputs restore with empty follow-up tables; a current
Brain that already holds follow-ups refuses an older archive with
`older archive restored follow-ups drift`.

Schema 23 adds `followup_items`, immutable `followup_evidence` and append-only
`followup_decisions`. Migration from schema 22 creates the three tables empty. Items are
created and decided through `open-brain followup` and the `brain_followup_*` tools under
the `followup-read`, `followup-propose` and `followup-decide` grants; see
[follow-ups](followups.md).

The exact catalog is in
`packages/engine/src/open_brain_engine/portable/v18_catalog.py`.
