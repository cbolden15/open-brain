# Commitments and open questions: design

Status: approved design, 2026-10-23. Not yet planned or implemented.
Source: `docs/audits/2026-10-14-work-laptop-product-evaluation.md`, section "3. Commitments and open questions".
Baseline: `main` at `9f50bd0`, schema/runtime/Portable 22/16/17.

## Goal

Give the owner a small, reviewable list of possible commitments and unresolved questions from their Work evidence. Each item carries a project, quoted evidence, a proposed owner, a due date only when one is stated, and a state. The owner confirms, dismisses, snoozes, corrects, links or resolves items, and the history of every change is kept.

## Decisions taken

| Question | Decision |
|---|---|
| Who detects candidates | The owner's existing MCP client, using its own model. Open Brain adds no extractor and no new network path. |
| Which evidence an item may cite | Any capture revision the proposing session may read: approved Slack copies, Gmail, Drive, Claude Code and Codex sessions, Markdown notes. |
| Who records decisions | A client holding a separate, off-by-default grant, or the owner through the CLI. |
| Where items live | A new engine store next to the canonical evidence, included in Portable Brain. |

Rejected: a managed-model extractor (needs a new egress task contract; revisit for #5 unattended runs), local pattern rules only (too many misses and false positives), extending the existing Slack developments (Slack-bound, immutable, no question kind, adds search noise), and an app-local tracker (outside Portable, no engine scope checks, no history).

## Naming

`commitment` already means a hash commitment in the engine (`commitment_sha256`, `development_commitments`, `policy_commitment`). To avoid confusion, the code, CLI, MCP tools, grants and tables use **follow-up**: `open-brain followup`, `brain_followup_*`, `followup-read`, `followup-propose`, `followup-decide`, `followup_*` tables. User-facing text still says "commitment" and "open question", which are the two item kinds.

## Data model

### Item

One row per item, updated only by compare-and-swap on `version`.

| Field | Meaning |
|---|---|
| `item_id` | Stable identifier |
| `kind` | `commitment` or `question` |
| `project` | Free text, or null |
| `summary` | One line written by the proposing client, at most 280 characters, always shown beside the quotes |
| `owner` | `self`, `other` with a name, or `unknown` |
| `owner_confirmed` | True only after an owner decision. A request or a ticket assignee is never treated as the owner's promise without it. |
| `due` | A date only when a quote states it; otherwise null, shown as "no date stated" |
| `state` | `suggested`, `confirmed`, `snoozed` (with `snoozed_until` and the state to return to), `dismissed`, `resolved`, `superseded` |
| `ticket` | Optional opaque key and URL. Recorded only; nothing is sent to a ticket system. |
| `privacy_tier` | Taken from the origin evidence; fixed |
| `version`, `created_at`, `updated_at` | Compare-and-swap version and timestamps |
| `new_evidence_since_decision` | Set when evidence is added to a dismissed or resolved item |

### Evidence

Immutable. Every item has at least one `origin` row.

| Field | Meaning |
|---|---|
| `evidence_id`, `item_id` | Identifiers |
| `capture_id`, `revision_id` | The cited revision |
| `quote_start`, `quote_end`, `quote_sha256` | Position and hash of the quote within the revision text. The quote text itself is not stored. |
| `role` | `origin`, `supporting`, `correction`, `resolution` |

At write time the engine checks that the position lies inside that revision, that the text there hashes to `quote_sha256`, that the revision is current, and that the proposing session can read it.

### Decision log

Append-only, one row per proposal, evidence addition or decision: `operation_id`, `request_sha256`, principal, action, prior and new state, prior and new version, affected evidence IDs, and time. A repeated `operation_id` with the same request returns the original result; a different request under that ID is refused. History is never rewritten. A correction is a new row, not an edit.

### Duplicates

A client that finds an existing item adds evidence to it instead of proposing a new one. The engine also refuses a new item whose origin evidence matches an existing item's origin revision and quote position, and returns the existing `item_id`.

## Scope and visibility

- An item's tier comes from its origin evidence. Added evidence must have the same tier, the same rule development binding uses today. A Work item never acquires Personal evidence.
- An item is visible to a session only if the session can read every evidence revision under the current rules: tier, space, and for external-provider sessions, egress eligibility. This check runs on every list and show, not only at write time.
- If any evidence revision is unreadable to a session, the whole item is hidden from it. The summary could otherwise reveal what that evidence says.
- When a cited source later gains a new revision (an edited message or note), the item stays visible. Visibility is judged against the source's current head: the session must be able to read the head under the rules above, and the head must keep the item's tier. If the quoted text still appears unchanged in the head, the quote is shown from the head with its new position. Otherwise the evidence is marked `source_changed` and shown without any text, so the client can re-check and add new evidence. The engine compares against the older revision internally but never returns older revision text to a client session.
- Quotes are rebuilt from the revision on every read. If owner retraction removes an evidence revision, its quote disappears with it. The item is then marked `evidence_retracted` and its summary is withheld. Client sessions no longer see it under the rule above; the owner CLI shows it as a state row the owner can dismiss.
- Follow-up tools are Work-only by default in every MCP session. A session reads or writes Personal or Public items only when its launcher policy explicitly allows that tier. This is stricter than review and search, where an unscoped `open-brain mcp` session runs with owner authority over every tier. Personal items exist only if a session allowed to read Personal proposes them. The owner CLI sees everything and filters by tier and project.

## Operations

One shared service, `services/followups.py`, backs both the CLI and MCP, following `services/review_publication.py`.

| Operation | Purpose | Grant |
|---|---|---|
| `list` | Open items by default; filters for kind, state, owner, project, due before | `followup-read` |
| `show <item_id>` | Item, rebuilt quotes and full decision history | `followup-read` |
| `propose` | New item with kind, summary, project, owner, due and one or more evidence quotes | `followup-propose` |
| `add-evidence <item_id>` | Attach supporting, correction or resolution evidence | `followup-propose` |
| `decide <item_id>` | One of confirm, dismiss, snooze `--until`, correct (owner, due, project, summary), link `--ticket`, resolve, reopen, supersede `--by <item_id>`; takes `--expected-version` | `followup-decide` |

All three grants are off by default. `agent setup` gains `--allow-followup-read`, `--allow-followup-propose` and `--allow-followup-decide`, matching the review grants. The owner CLI can always perform every operation. Writes take an `operation_id`.

Behavior rules:

- A snoozed item leaves the default list until `snoozed_until`, then returns in its earlier state.
- Adding evidence to a dismissed or resolved item sets `new_evidence_since_decision`. Closed items otherwise stay out of the default list.
- Resolve either cites resolution evidence or is recorded as "resolved by owner statement".
- Open Brain never writes to a ticket system and never sends a message. Draft ticket updates and follow-up messages stay in the client conversation until the owner acts.

## Errors

Every refusal returns a closed code and writes nothing.

| Condition | Code |
|---|---|
| Quote position or hash does not match, or the revision is not current | `evidence_mismatch` |
| The session cannot read a cited capture or item | `not_found` (does not reveal existence) |
| Evidence tier differs from the item's tier | `tier_mismatch` |
| `expected_version` is stale | `version_conflict`, with the current version |
| Same `operation_id`, different request | `operation_conflict` |
| Origin evidence already belongs to an item | `duplicate_item`, with that `item_id` |
| Snooze date in the past, summary too long, due date with no stating quote, unknown action | `invalid_request` |
| More than 10,000 items in one Brain or 50 evidence rows on one item | `capacity_reached` |

## Compatibility

New tables make this state schema 23 and Portable 18. The plan includes the migration from 22, exclusive runtime admission during migration, Portable export and clean-restore round trips, a new `docs/portable-brain-v18.md`, and refusal of older clients, following the pattern of #96. Runtime session stays at 16, as #96 kept it; the plan's first task confirms that no runtime-session contract changes. Portable exports carry evidence positions and hashes, never quote text, so a retracted revision leaves no follow-up quote behind; the export marks such items `evidence_retracted` and omits their summary.

## Testing

All tests use synthetic fixtures.

- Engine: every state transition and its log row, replay safety, `version_conflict`, quote position and hash checks, tier matching, capacity limits, and retraction withholding quotes and summary.
- Scope: a Work session never sees a Personal item; an item is hidden once any of its evidence becomes unreadable; an external-provider session sees only items whose evidence is egress-eligible.
- Migration and Portable: 22 to 23 upgrade, export and clean-restore round trip, older-client refusal.
- CLI and MCP: each grant enforced independently; no grant on by default.
- Evaluation acceptance: duplicate mentions become one item with several sources; a correction or completion updates the item without erasing history; "no owner identified" and "no date stated" are valid; a resolved item stays out of the default list until new evidence arrives.

## Not in v1

- Detection inside Open Brain by a managed model or local rules.
- Feeding the existing Slack regex developments in as automatic candidates.
- Any write to a ticket system or any outgoing message.
- The daily brief (#5), which will read this store.
- A reusable client workflow or prompt for detection. The plan may add a short documented example for an MCP client, but no new core task.
