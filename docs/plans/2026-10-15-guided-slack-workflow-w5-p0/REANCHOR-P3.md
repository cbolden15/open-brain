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
