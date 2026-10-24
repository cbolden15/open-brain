# Daily work brief

Open Brain can give you a short list of what needs attention today: follow-ups that are due or
have new evidence, open follow-ups you own, and capture families that have gone quiet or hold
captures that can no longer be read. You ask for it; nothing runs in the background. A brief
holds at most 5 items, each one saying why it is there and pointing at the records behind it.
When nothing needs attention, the brief says so instead of filling the list.

The brief is composed by rules, with no model call, no network path and no subprocess. A client
may reword it for you. Open Brain never writes a note, sends a message or changes a follow-up
when you read a brief. The only file a brief writes is its own log, described below.

Follow-up items come from the store described in [commitments and open questions](followups.md).

## What a brief holds

| Field | Meaning |
|---|---|
| `generated_at` | When this brief was composed, from the engine clock |
| `scope` | `space_ids` (the spaces you selected, or `all`) and `tiers` (the tiers this session may read) |
| `items` | Zero to 5 items, ranked |
| `nothing_needs_attention` | True when `items` is empty |
| `since` | `generated_at` of the previous brief for the same visibility, or null the first time |
| `coverage` | Counts and provider status, below |
| `model` | Always `none` |
| `saved` | False when the brief could not be written to the log |

Each item carries:

| Field | Meaning |
|---|---|
| `kind` | One of the kinds below |
| `ref` | `followup:<item_id>` or `family:<family_id>` |
| `reasons` | One or more reason codes |
| `reason_text` | One plain sentence saying why the item appears |
| `evidence` | Follow-up evidence IDs; empty for a capture family |
| `changed` | `new`, `still_open` or `changed` against the previous brief for the same visibility |
| `rank` | 1 to 5 |

A brief never carries quote text, summaries or titles. To read what a follow-up says, use
`followup show` (or `brain_followup_show`), where scope is checked again.

`coverage` holds `families_checked`, `families_stale`, `captures_unavailable`,
`followups_scanned`, `followups_truncated`, `followups` (`read`, `not_granted` or `failed`),
`freshness` (`read` or `failed`), `collector` (always `not_read` in version 1), `hidden_dropped`
(how many candidates could not be re-read at assembly time, never which) and `log` with
`entries`, `bytes` and `unavailable` for this visibility's brief log.

## Kinds and when they appear

| Kind | Appears when | Reason codes |
|---|---|---|
| `followup_due` | A suggested or confirmed follow-up has a due date today or in the past | `overdue`, `due_today` |
| `followup_new_evidence` | A dismissed or resolved follow-up gained evidence after the decision | `new_evidence` |
| `followup_open` | A suggested or confirmed follow-up you own has no due date today or past | `open_owned` |
| `source_unavailable` | A capture family holds captures whose availability is missing or inaccessible | `captures_unavailable`, plus `no_capture_since` when the family is also stale |
| `source_stale` | A capture family's newest capture is older than the stale window | `no_capture_since` |

A capture family is the unit you mean by "a source": one agent session source (a consumer
namespace, identified as `consumer:<connector_name>/<connection_id>:<space_id>`) or the Markdown
import of one space (`markdown:<space_id>`). A capture that is in no space reports the space as
`unassigned`. Manual captures form no family. Retired captures are not counted. The stale window
is `--stale-days`, 7 days by default, at least 1 and at most 365.

A snoozed follow-up is not open until its date. When the date passes it returns in its earlier
state and is judged by that state.

## Ranking

The order is fixed, so you can tell why an item is where it is:

1. `followup_due`, by due date, then by creation time
2. `source_unavailable`, most unavailable captures first
3. `followup_new_evidence`, newest evidence first
4. `followup_open`, oldest first
5. `source_stale`, longest silence first

Within one level, items that are new since the previous brief rank above items that were already
there. At most 2 family items appear in one brief, so source health cannot push out your work.

The follow-up scan reads at most 500 open items: items due today or earlier first, then the rest
by creation time. When more exist, `followups_truncated` is true. Dismissed and resolved items are
scanned separately, and only those with new evidence since their decision are read, so old
decisions never use up that budget.

## What changed since the last brief

Each brief for a given visibility is kept in a small log, so the next one can say what is `new`,
what is `still_open` and what `changed` (the same ref with a different kind, reason codes or
evidence IDs), and can fill
`since`. Losing the log resets `since` and the acknowledgements below and nothing else. The log is
a rebuildable view, like graph state; it is not part of a Portable Brain export.

## Acknowledging a capture family

A follow-up item is handled where follow-ups live: `followup decide` (or `brain_followup_decide`)
resolves, dismisses or snoozes it with the usual grants and history. The brief adds no second way
to dismiss a follow-up.

A capture family is acknowledged:

```sh
open-brain brief ack family:FAMILY_ID
```

That records the family's current state (newest capture time and unavailable count) for your
visibility. The family stays out of your briefs until either value changes. Acknowledgements are
kept per visibility and per family, independent of the `--space` selection you used when you saw
the item. A family ref is `family:` followed by `consumer:` or `markdown:` and 1 to 2200
characters with no control characters. The owner CLI refuses a malformed ref as a usage failure
or `invalid_request`, and MCP refuses it as invalid tool arguments. Acknowledging a
family you cannot read, or a well-formed ref the Brain does not hold, is `not_found`. An
acknowledgement that races another write is `version_conflict`; retry it.

## Scope and the visibility key

A brief is composed with the authority of the session that asks for it. Follow-ups keep their
own scope rules (tier, space and lifecycle of every cited capture); families are read with the
same tier and space rules as any record read. The owner CLI sees everything. An MCP session is
Work-only unless its launcher policy allows another tier.

With `--space`, the owner brief keeps a follow-up when any of its evidence captures lies in a
selected space, and keeps the families of the selected spaces. An `unassigned` family never
matches a selection.

Two sessions share log history and acknowledgements only when they would see the same things:
same owner flag, same readable tiers, same spaces, same egress mode and provider, same brief and
follow-up grants, same `--space` selection. Those values are hashed into a visibility key, and
each key has its own log file under `.open-brain/state/briefs/`. An owner brief is never read back
by a client, and a session with `followup-read` never shares history with one without it.

A selected space must exist and be visible to the session. Any other selection is
`invalid_request`, refused before the visibility key is computed, so a client cannot create log
files at will.

## Grants

Two grants, off by default and independent of each other and of every other grant:

| Grant | Allows | MCP tool |
|---|---|---|
| `brief-read` | compose and read a brief | `brain_brief` |
| `brief-ack` | acknowledge a capture family | `brain_brief_ack` |

The flags are switched separately, but acknowledging needs both grants: an ack is stored under,
and reads, the session's brief data. `brain_brief_ack` in a session without `brief-read` is
`unsupported_capability`.

Follow-up items appear in a client's brief only when the session also holds `followup-read`.
Without it the brief carries family items only and `coverage.followups` is `not_granted`.

Configure them with `agent setup`:

```sh
open-brain agent setup --client claude-code --scope project \
  --project-dir /absolute/path/to/project \
  --allow-brief-read --allow-brief-ack --allow-followup-read --json
```

Repeat the same options with `--apply --preview-id ID` to apply the previewed fragment, as in
[agent setup](agent-setup.md). Scoped Work evidence setup (`agent setup` with `--session-policy`)
refuses the brief flags, as it refuses the follow-up flags. External-provider sessions get no brief
tools. The owner CLI needs no grants.

## Owner CLI

```sh
open-brain brief                              # every space, every tier, 7-day stale window
open-brain brief generate                     # the same as open-brain brief
open-brain brief --space "Q3 forecast" --space Vendors --stale-days 14
open-brain brief --json
open-brain brief ack family:FAMILY_ID         # acknowledge a capture family's current state
open-brain brief last                         # the most recent saved brief for your visibility
open-brain brief last --space "Q3 forecast" --json
```

`--space` is repeatable and matches a space's name, slug or ID from `space list`. An unknown
value, including `unassigned`, is `invalid_request`, never an empty brief. `--stale-days` takes 1
to 365, is not stored, and is accepted only by `brief` and `brief generate`. A value outside that
range is a usage failure.

The text brief prints a header with `generated_at` and `since`, then one line per item with its
rank, `changed`, kind, ref, reason codes, `reason_text` and evidence count, then one coverage
line. It prints `Nothing needs attention.` when the list is empty.

`brief last` reads the owner's most recent saved brief for the same selection, re-reading each
follow-up with owner authority. A follow-up that still reads shows as `current`; one whose
evidence was retracted since, or that no longer reads, shows as `withheld`. Family items show as
`logged`. The log keeps reason codes, not sentences, so `brief last` prints reason codes only. It
also reports the log's current `entries` and `bytes`. With no saved brief it says so, and `--json`
returns `"data": null`. `brief last` never returns `operation_pending`. Only the owner CLI has
`last`; a client never receives a brief served from the log.

With `--json`, a successful command prints `{"outcome": "ok", "data": {...}, "code": null}` where
`data` is the brief, the acknowledgement receipt (`family_ref` and its `fingerprint`) or the saved
brief. A refusal prints `{"status": "failed", "error": {"code": ..., "message": ...}}`.

| Exit | Meaning |
|---|---|
| 0 | Success |
| 1 | `not_found`, `version_conflict` or `unsupported_capability` |
| 2 | A usage failure or `invalid_request` |
| 75 | `operation_pending` |

## MCP

| Tool | Grant | Arguments |
|---|---|---|
| `brain_brief` | `brief-read` | optional `space_ids` (1 to 32 unique space IDs), optional `stale_days` (1 to 365) |
| `brain_brief_ack` | `brief-ack`, and the session must also hold `brief-read` | `family_ref` |

Results are `{"outcome": "ok", "data": {...}, "code": null}` or
`{"outcome": "refused", "data": null, "code": "..."}`. A response is at most 32 KB. One MCP
session allows 200 brief reads, 200 acknowledgements and 4 MiB of encoded brief output. Every MCP
session is Work-only by default, including an unscoped `open-brain mcp --allow-brief-read`; a
session started with a launcher policy (`--session-policy`) reads the tiers that policy lists.
There is no MCP `brief last`.

### Turning a brief into a morning summary

A client holding `brief-read` and `followup-read` can produce a short morning note in three steps,
with no Open Brain change and no message sent:

1. Call `brain_brief` with `{}` (or `{"space_ids": ["..."]}` for one project).
2. For each item whose `ref` starts with `followup:`, call `brain_followup_show` with the
   `item_id` to read the summary and quotes. Family items need no second call; their `reason_text`
   is the whole message.
3. Write one line per item in rank order, then one line for coverage.

Given this result:

```json
{
  "generated_at": "2026-10-24T08:00:00Z",
  "scope": {"space_ids": "all", "tiers": ["work"]},
  "since": "2026-10-23T08:05:00Z",
  "items": [
    {"kind": "followup_due", "ref": "followup:followup_2b9c...", "reasons": ["overdue"],
     "reason_text": "Due 2026-10-22; 2 days overdue.", "evidence": ["evidence_7f1a..."],
     "changed": "still_open", "rank": 1},
    {"kind": "source_stale", "ref": "family:consumer:...:space_9d3e...",
     "reasons": ["no_capture_since"], "reason_text": "No capture since 2026-10-10T17:42:00Z.",
     "evidence": [], "changed": "new", "rank": 2}
  ],
  "nothing_needs_attention": false,
  "coverage": {"families_checked": 3, "families_stale": 1, "captures_unavailable": 0,
               "followups_scanned": 12, "followups_truncated": false, "followups": "read",
               "freshness": "read", "collector": "not_read", "hidden_dropped": 0,
               "log": {"entries": 9, "bytes": 14210, "unavailable": false}},
  "model": "none",
  "saved": true
}
```

a client might write:

> Saturday brief (since yesterday 08:05). 1. Still open and overdue: send the Q3 forecast to Dana,
> due 2026-10-22. 2. New: the coding-agent session source has had no capture since 2026-10-10.
> Checked 3 sources and 12 follow-ups; nothing was hidden.

The wording is the client's. Open Brain supplied the pointers, the reasons and the counts.

## Errors

| Code | When |
|---|---|
| `invalid_request` | A space selection that does not exist or that the session cannot see, or an owner CLI ref that is not a well-formed `family:` ref |
| `unsupported_capability` | The session lacks `brief-read`, or acknowledges without holding both `brief-read` and `brief-ack` |
| `operation_pending` | The engine could not be read (for example an unfinished maintenance operation); nothing is served from the log on the `brief` or `brain_brief` path |
| `version_conflict` | An acknowledgement raced another write to the same log file; retry |
| `not_found` | Acknowledging a family the session cannot read or that does not exist |

A provider that fails does not fail the brief: `coverage.followups` or `coverage.freshness` reads
`failed` and the other provider's items still appear, and the brief is still saved. A candidate
that cannot be re-read at assembly time is dropped and counted in `hidden_dropped`; only the owner
CLI (`brief last`) shows a `withheld` entry.

Over MCP, arguments outside a tool's schema (an unknown key, a `stale_days` outside 1 to 365, a
`space_ids` entry that is not a space ID, a malformed `family_ref`) fail the call with
`invalid tool arguments` before any read. A session past its quota gets
`session_brief_read_limit`, `session_brief_ack_limit` or `session_brief_response_limit`.

## Limits

- 5 items per brief, at most 2 family items.
- The follow-up scan stops at 500 open items; `followups_truncated` says when it did.
- A response is at most 32 KB.
- Each visibility key's log keeps the last 30 briefs and its acknowledgements in one file of at most
  64 KB; the oldest brief is dropped first.
- At most 16 key files written by client sessions exist; the least recently written is removed
  when another is needed. A file written by the owner CLI is marked `owner`, does not count toward
  the 16 key files and is never evicted, so no client selection pattern can remove the owner's
  acknowledgements.
- A corrupt or unreadable log file is renamed to `<key>.json.aside` (replacing an earlier aside
  copy), never deleted. That brief has `since: null` and `coverage.log.unavailable: true`; other
  keys are unaffected.
- When two writers race on one key, the loser retries once and then returns its brief with
  `saved: false`.

## Collector health and scheduling

Version 1 does not read collector health and does not schedule anything. `coverage.collector` is
always `not_read`. The core can read collector receipts only for the one source selected in the
source guide and has no headless list of sources, so the brief reports no collector state rather
than a partial one. Scheduled generation needs an unattended runner the core does not have; the
evaluation asks for on-request generation first, and that is what version 1 does. A brief says
when it was generated; ask again for a fresh one.

## What is not in version 1

- Collector receipts and any owner-CLI collector flags.
- Scheduled or collector-run generation; model-written summaries.
- Decision-change and recurring-feedback items (the kind registry is open for them).
- A brief-only mute list for follow-ups; use `followup decide`.
- A `--project` filter on the free-text follow-up project field; select by space instead.
- Per-user ranking weights.
- An MCP `brief last`.
- Storing briefs in a Portable Brain export.
