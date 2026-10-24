# Commitments and open questions

Open Brain can keep a short, reviewable list of two kinds of follow-up item drawn from your
evidence: commitments (something someone said they would do) and open questions (something nobody
has answered yet). Each item points at exact quotes in captures you already hold, names a proposed
owner, carries a due date only when a quote states one, and has a state you control.

Open Brain does not detect these items itself. Your MCP client reads captures it is allowed to
read, decides what looks like a commitment or a question, and proposes an item with the quotes that
support it. The engine checks the quotes against the stored revision, keeps the item and its
history, and lets you confirm, dismiss, snooze, correct, link, resolve, reopen or supersede it.
Nothing is sent anywhere. See the [feature matrix](core-v01-features.md) for the release
coordinates and [Portable 18](portable-brain-v18.md) for the export format.

In code, commands, tools, grants and tables this feature is called `followup`, because
`commitment` already means a hash commitment inside the engine.

## The two kinds

| Kind | Meaning | Example quote |
|---|---|---|
| `commitment` | A statement that someone will do something | "I will send the Q3 forecast to Dana by Friday" |
| `question` | A question nobody has answered in the evidence | "who owns the vendor renewal?" |

A request, a hypothetical, or a ticket assignee is not a commitment by the person it names. Items
start as suggestions. `owner_confirmed` becomes true only after you decide so.

## What an item holds

- `summary`: one line written by the proposing client, at most 280 characters. It is always shown
  beside the quotes, never on its own.
- `project`: free text up to 128 characters, or empty.
- `owner`: `self`, `other` with an `owner_name` of up to 128 characters, or `unknown`. The CLI
  shows `unknown` as "no owner identified". That is a valid result, not an error.
- `due`: a date only when a quote states one. Otherwise the CLI shows "no date stated". Proposing a
  due date without pointing at the quote that states it is refused.
- `ticket_key` and `ticket_url`: an optional key (up to 200 characters) and URL (up to 2048
  characters) you record with `link`. Open Brain never writes to the ticket system.
- `privacy_tier`: taken from the origin quote and fixed for the life of the item.
- `version`: starts at 1 and increases on every change. Decisions must send the version they saw.
- `new_evidence_since_decision`: set when evidence arrives on an item you already dismissed or
  resolved.
- `evidence_retracted`: set when a cited capture has been retracted; see "Retraction".

Evidence rows are immutable. Each one names a capture, its revision, the start and end of the
quote and the SHA-256 of the quoted text. The quote text itself is not stored; it is rebuilt from
the revision every time you read the item. Roles are `origin`, `supporting`, `correction` and
`resolution`.

## States

| State | Meaning | In the default list |
|---|---|---|
| `suggested` | Proposed by a client, not yet reviewed | yes |
| `confirmed` | You agreed it is real | yes |
| `snoozed` | Hidden until `snoozed_until`, then back to its earlier state | no, until the date |
| `dismissed` | Not a real commitment or question | no |
| `resolved` | Done or answered, with resolution evidence or your statement | no |
| `superseded` | Replaced by another item | no |

A dismissed or resolved item comes back into the default list only when new evidence is added to
it after the decision; the item then shows `new_evidence_since_decision`. `--include-closed` lists
everything. Snooze expiry is computed when you read the list; nothing runs in the background.

## Grants

Three grants, all off by default and independent of each other and of every other grant:

| Grant | Allows | MCP tools |
|---|---|---|
| `followup-read` | list and show | `brain_followup_list`, `brain_followup_show` |
| `followup-propose` | propose an item, add evidence | `brain_followup_propose`, `brain_followup_add_evidence` |
| `followup-decide` | record a decision | `brain_followup_decide` |

Configure them with `agent setup`:

```sh
open-brain agent setup --client claude-code --scope project \
  --project-dir /absolute/path/to/project \
  --allow-followup-read --allow-followup-propose --allow-followup-decide --json
```

Repeat the same options with `--apply --preview-id ID` to apply the previewed fragment, as in
[agent setup](agent-setup.md). A client that should only suggest gets `--allow-followup-read
--allow-followup-propose` and leaves decisions to you. Scoped Work evidence setup (`agent setup`
with `--session-policy`) refuses the follow-up flags. The owner CLI needs no grants and has full
authority over every item.

A session is not offered the tools for grants it lacks. Calling one anyway fails with "unknown
tool".

## Which sessions see which items

Every MCP session is Work-only for follow-ups by default. This includes an unscoped
`open-brain mcp --allow-followup-read` session, which with other grants may run search and review
with owner authority but reads and writes only Work follow-ups.

A session started with a launcher policy (`--session-policy`) reads the tiers that policy lists in
`allowed_read_tiers`, so Personal or Public items are visible only when the policy allows that
tier. Such a session gets follow-up tools only for capabilities the policy grants: its follow-up
capabilities are the policy's `capabilities` intersected with the `--allow-followup-*` flags. A
flag the policy does not grant offers no tool, and a policy grant without its flag offers none
either.

External-provider sessions get no follow-up tools. Follow-up tools are offered only to sessions
whose egress mode is owner-local, so a launcher policy with an external provider cannot list,
show, propose or decide follow-ups, whatever it grants. The engine still applies the
egress-eligibility rule to every cited revision, but no MCP session exercises it today.

Personal items therefore exist only if you propose them in the CLI, or a launcher-policy session
allowed to read Personal proposes them.

## Owner CLI

```sh
open-brain followup list                      # open items: suggested, confirmed, due snoozes
open-brain followup list --include-closed     # everything
open-brain followup list --kind question --project "Q3 forecast" --due-before 2026-11-01
open-brain followup show ITEM_ID              # item, rebuilt quotes, full decision history
open-brain followup propose --kind commitment --summary "Send the Q3 forecast to Dana" \
  --owner self --project "Q3 forecast" --due 2026-10-31 --due-evidence-index 0 \
  --evidence '{"capture_id": "capture_...", "revision_id": "capture_...", "quote_start": 15, "quote_end": 71, "quote_sha256": "...", "role": "origin"}' \
  --operation-id OP
open-brain followup add-evidence ITEM_ID \
  --evidence '{"capture_id": "capture_...", "revision_id": "capture_...", "quote_start": 0, "quote_end": 40, "quote_sha256": "...", "role": "supporting"}' \
  --operation-id OP
open-brain followup decide ITEM_ID --action confirm --expected-version 1 --operation-id OP
open-brain followup decide ITEM_ID --action snooze --until 2026-11-03 --expected-version 2 --operation-id OP
open-brain followup decide ITEM_ID --action correct --set owner=other --set owner_name=Dana --expected-version 3 --operation-id OP
open-brain followup decide ITEM_ID --action link --ticket-key SYN-123 --ticket-url https://tickets.example.com/browse/SYN-123 --expected-version 4 --operation-id OP
open-brain followup decide ITEM_ID --action resolve --resolution owner_statement --expected-version 5 --operation-id OP
open-brain followup decide ITEM_ID --action reopen --expected-version 6 --operation-id OP
open-brain followup decide ITEM_ID --action supersede --by OTHER_ITEM_ID --expected-version 7 --operation-id OP
```

Each `--evidence` is one JSON object with `capture_id`, `revision_id`, `quote_start`, `quote_end`,
`quote_sha256` and `role`; repeat the flag on `propose` for several quotes. `--set FIELD=VALUE`
corrects `owner`, `owner_name`, `project`, `summary` or `due`. A `due` correction also needs
`--set due_evidence_id=EVIDENCE_ID`, the evidence row whose quote states the date. An empty value
clears a field. `list` returns 50 rows by default and accepts `--limit` up to 100 and `--cursor`.

`show` prints each evidence row with its role and status. A row whose quote cannot be shown prints
"(quote withheld)". Decision rows list the action, the prior and new state and version, the cited
evidence and the time. They carry no actor or principal field.

Add `--json` for structured output. A successful command prints the wire object
`{"outcome": "ok", "data": {...}, "code": null}`; a refusal prints
`{"status": "failed", "error": {"code": ..., "message": ...}}` and exits 1. `--expected-version`
is the `version` you last saw; if the item changed first you get `version_conflict` with the
current version and nothing is written.

`--operation-id` makes a retried command safe. The same ID with the same request returns the
original receipt, even after a cited source was edited; the same ID with a different request is
refused with `operation_conflict`. If you omit it, the CLI generates one. Decision rows restored
from a Portable export have no stored receipt, so replaying one of those old operations returns
the item's current state.

`resolve` either cites `resolution` evidence already added to the item
(`--resolution evidence`) or is recorded as resolved by owner statement
(`--resolution owner_statement`). Both keep every earlier quote and decision.

## MCP

The five tools take and return JSON. Every write carries an `operation_id`. Results are
`{"outcome": "ok", "data": {...}, "code": null}` or `{"outcome": "refused", "data": ..., "code": "..."}`.
A refusal writes nothing, and `data` then carries only `current_version` (for `version_conflict`)
or `existing_item_id` (for `duplicate_item`), otherwise null. Arguments that do not match the
tool schema fail with "invalid tool arguments" before anything runs.

| Tool | Grant | Arguments |
|---|---|---|
| `brain_followup_list` | `followup-read` | `include_closed`, `kinds`, `states`, `owner`, `project`, `due_before`, `limit`, `cursor` |
| `brain_followup_show` | `followup-read` | `item_id` |
| `brain_followup_propose` | `followup-propose` | `operation_id`, `kind`, `summary`, `owner`, `evidence[]`, optionally `project`, `owner_name`, `due`, `due_evidence_index` |
| `brain_followup_add_evidence` | `followup-propose` | `operation_id`, `item_id`, `evidence` |
| `brain_followup_decide` | `followup-decide` | `operation_id`, `item_id`, `expected_version`, `action`, then `snoozed_until`, `corrections`, `ticket_key`, `ticket_url`, `superseded_by` or `resolution` as the action needs |

Leave optional arguments out rather than sending null. `brain_followup_list` returns `items`,
`next_cursor` and `truncated`, with at most 100 items per page. A response is capped at 250 KB:
when rows are dropped to fit, `truncated` is true and `next_cursor` resumes right after the last
returned row, so no row is skipped.

An evidence object is `{"capture_id", "revision_id", "quote_start", "quote_end", "quote_sha256",
"role"}`. Positions are character offsets into the revision text as `brain_read` returns it, and
the hash is SHA-256 of the UTF-8 bytes of exactly that slice. `brain_followup_show` returns the
item, each evidence row with `quote`, `status`, `head_revision_id`, `head_quote_start` and
`head_quote_end`, and the decision rows. Status and head positions are computed on every read; see
"Edited and retracted sources".

### How a client should propose an item

1. Read the capture with `brain_read` and keep the exact `revision_id` and text it returns.
2. Pick the shortest span that states the commitment or question. Do not paraphrase; the engine
   rejects a quote whose bytes do not match the revision.
3. Compute `quote_start`, `quote_end` and `quote_sha256` from that text.
4. Call `brain_followup_list` first. If an item already covers the same promise, call
   `brain_followup_add_evidence` with role `supporting` instead of proposing again. The engine also
   refuses a second item whose origin quote matches an existing one and returns `duplicate_item`
   with that item's ID.
5. Propose with a one-line summary, the owner you can justify from the quotes, and a `due` only
   if a quote states a date; point `due_evidence_index` at that quote.

A worked proposal, given a Work capture whose text is
`Standup recap. I will send the Q3 forecast to Dana by Friday 2026-10-31.`, so the quote starts
at character 15:

```json
{
  "operation_id": "6f1d9f4e-6a1e-4a0c-9d60-8f0c6a1d2e31",
  "kind": "commitment",
  "summary": "Send the Q3 forecast to Dana",
  "project": "Q3 forecast",
  "owner": "self",
  "due": "2026-10-31",
  "due_evidence_index": 0,
  "evidence": [
    {
      "capture_id": "capture_3e6e8e2c-e638-47c6-8195-4bd6f306d67b",
      "revision_id": "capture_3e6e8e2c-e638-47c6-8195-4bd6f306d67b",
      "quote_start": 15,
      "quote_end": 71,
      "quote_sha256": "<sha256 of the 56 quoted characters as UTF-8>",
      "role": "origin"
    }
  ]
}
```

When the client later sees a message that completes or corrects the item, it adds evidence with
role `resolution` or `correction`, and leaves the decision to a session that holds
`followup-decide`, or to you in the CLI.

## Scope rules

- An item's tier comes from its origin quote. Added evidence must have the same tier. A Work item
  never gains Personal evidence (`tier_mismatch`).
- A session sees an item only if it can read every cited source under the current rules: tier,
  space and lifecycle. The check runs on every list and show, not only at write time.
- If a session cannot read any one cited source, the whole item is hidden from it. Citing a
  capture the session cannot read is refused as `not_found`, which does not reveal whether the
  capture exists.

## Edited and retracted sources

When a quoted message or note is edited later, the item stays. Visibility is judged against the
source's current version: the session must be able to read it and it must keep the item's tier.
Each quote then carries a `status`:

- `current`: the cited revision is still the current one and the quote matches.
- `moved`: the same words appear exactly once in the new version; `head_revision_id`,
  `head_quote_start` and `head_quote_end` give their new position.
- `source_changed`: the words are gone or appear more than once. No quote text is returned to any
  session, the owner CLI included, which prints "(quote withheld)". Re-check the source and add
  new evidence.
- `retracted`: the cited capture was retracted; no quote text is returned.

Text from an older revision is never returned to anyone.

If you retract a capture (see [owner retraction](owner-retraction.md)), its quotes disappear with
it. Items citing it are hidden from every client session. The owner CLI still lists them with
`evidence_retracted` set and the summary withheld, so you can dismiss them. Portable exports carry
positions and hashes, never quote text, so a retracted revision leaves no quote behind in an export
either.

## Export and restore

Portable 18 adds `history/followups/followups-v1.json` with items, evidence positions and hashes,
and the decision log; see [Portable 18](portable-brain-v18.md). Restoring Portable 17 or earlier
input gives a Brain with no follow-ups.

## Errors

| Code | When |
|---|---|
| `evidence_mismatch` | The position or hash does not match the revision, or the revision is not current |
| `not_found` | The session cannot read the cited capture or item |
| `tier_mismatch` | Evidence tier differs from the item's tier |
| `version_conflict` | `expected_version` is stale; the current version is returned |
| `operation_conflict` | The same `operation_id` arrived with a different request |
| `duplicate_item` | The origin quote already belongs to an item; that item's ID is returned |
| `invalid_request` | Snooze date not after today, due date with no stating quote, or an action not allowed in the item's state |
| `capacity_reached` | The Brain already holds 10,000 items, or the item has 50 evidence rows |
| `unsupported_capability` | The session's launcher policy or capability set does not allow the operation (a tool for a missing grant is not offered at all) |
| `operation_pending` | An unfinished Brain maintenance operation, such as a privacy migration, blocks the request; finish it and retry |

No other refusal code is returned.

## Limits

- Summary: 280 characters.
- `project` and `owner_name`: 128 characters each; `ticket_key`: 200; `ticket_url`: 2048.
- Items per Brain: 10,000 items.
- Evidence per item: 50 evidence rows.
- List page: 100 items at most, and 250 KB per MCP response.
- Per MCP session, follow-up reads, writes and response bytes have separate counters like the
  review tools. Exceeding one fails that call with `session_followup_read_limit`,
  `session_followup_write_limit` or `session_followup_response_limit`, and the process keeps
  running.

## What is not in version 1

- Detection inside Open Brain, by a managed model or by local rules. Your client detects.
- Using the existing Slack pattern matches as automatic candidates.
- Any write to a ticket system or any outgoing message. Draft ticket updates and follow-up
  messages stay in your client conversation until you act on them yourself.
- Follow-up tools for external-provider MCP sessions.
- The daily brief, which will read this store later.
- A packaged client workflow or prompt for detection. The example above is the whole guidance.
