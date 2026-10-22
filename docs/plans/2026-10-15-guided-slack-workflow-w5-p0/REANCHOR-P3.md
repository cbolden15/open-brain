# W5 P3 re-anchor record

Date: 2026-10-22. Branch `feat/w5-guided-slack-workflow`. Coordinates stay schema/runtime/Portable
21/16/16. No persisted format, receipt or schema changes.

## Demonstrated gap: owner enumeration of a space's records

The guide must prove the release destination is dedicated: every record in it is one this
workflow routed or released. No existing engine or owner read lists a space's records. Inbox
listing returns only visible quick captures, search excludes hidden rows, and space listing
returns names only.

P3 adds one narrow owner-only read, `InboxSpaceTasks.space_capture_ids(space_id, *, limit)`
(`engine/spaces.py`, with the matching `InboxSpaceTask` protocol entry). It returns at most
`limit` (1–10,000) capture IDs assigned to the space, at any stage or visibility, plus a
`truncated` flag. A non-owner authority gets `unsupported_capability`; an unknown space gets
`not_found`. It reads existing columns only. The guide calls it with `limit=1000` and refuses
when the result is truncated or the read fails.

## Destination choice in v1

v1 refuses every existing space (`destination` returns `authority_refused` with
`create-destination` as the next action). The only destination is a space created by this
workflow's `create-destination`. Accepting existing spaces would need a choice-time proof of
dedication plus an ownership rule for records that predate the workflow. Neither is worth the
complexity for v1.

Dedication is checked again at policy-preview, policy-apply and setup-apply. Allowed records
are this workflow's acknowledged revisions, routed heads, release-preview heads and approved
copies. Anything else, such as a separate approval or capture placed into the space, makes the
step refuse with `authority_refused` and record a refused operation in the journal. No
authority write happens. A newly imported revision of a routed message also refuses until it
is inspected.

## Other P3 decisions

- A halted journal accepts an explicit journal-only `cancel` (RECOVERY.md cancel row). It
  keeps the unresolved operation and every receipt and makes no authority call; every other
  action still refuses with `receipt_unresolved`.
- A grant-time consent store failure is an unknown outcome, because the store can fail after
  its atomic replace and a same-ID replay is idempotent. A file CAS conflict or foreign Brain
  binding halts.
- An existing policy file is reused only if it carries the generated guide principal and
  session IDs, is 0600 under a 0700 parent, and a fresh parse proves the exact scope.
- A release preview is refused for a message that this journal already approved and captured
  at its current head; the result shows the summary instead.

## TDD deviation

The unit tests in `packages/app/tests/unit/test_scoped_policy_authoring.py` were written just
after `scoped_policy_authoring.py`, not before. All later P3 behavior and every review fix had
a failing test first.

## P4 decision: when a journal becomes completed

This replaces P3 deviation 6. A journal becomes `completed` when all of these hold: a
`setup-apply` has completed, no operation is unresolved, and every member of the selection has a
current captured approved copy (the result `count` equals the member count). The check runs after
`setup-apply`, after `approve` and after a reconciled `approve`. Until then the journal stays
`active`, so a partial release or an edited member can still be repaired in the same journal.

`completed` is terminal. RECOVERY.md only requires a completed journal to be kept until the owner
removes it; refusing every `advance`, including `cancel`, with `invalid_request` is a P4 choice. If
the completion marker cannot be written after a succeeded `setup-apply` or `approve`, that step
still reports its real outcome, the journal stays `active`, and the next non-cancel action records
the marker and refuses. `status` still reads the selected source. The interactive guide reports the
completion and asks nothing. The journal is kept until the owner removes it. A message edited after
completion is repaired with the existing owner `open-brain sharing preview` and `approve`
commands, not by reopening the journal.

The cross-journal race test (`test_source_guide_release_recovery.py`) runs two journals against one
Brain, collector state and Slack source. The second journal reroutes the shared source to its own
destination. The first journal's `select`, `release-preview` and `policy-preview` then refuse with
`inspection_required`. Exactly one release preview and one approval exist, and the approved copy
is in the second journal's space.
