# Daily work brief design

Item 5 of `docs/audits/2026-10-14-work-laptop-product-evaluation.md`. Builds on follow-ups
(`docs/superpowers/specs/2026-10-23-commitments-open-questions-design.md`, PR #119).

## Goal

On request, give the user up to five items that need attention today: follow-ups that are due or
have new evidence, open follow-ups they own, and sources that have gone stale or unavailable. Each
item says why it appears and points at its evidence. When nothing needs attention, the brief says
so instead of filling a quota.

## Decisions taken

- **The engine composes the brief with no model.** Ranking is rule-based and deterministic. A client
  may reword the result; Open Brain adds no model call, no network path and no background work.
- **Brief items are pointers, not state.** Acting on a follow-up item uses the existing
  `followup decide` operation with its grants and history. A source item is acknowledged, and stays
  out until that source's state changes. There is no brief-only dismiss list.
- **Freshness comes from engine facts, plus optional collector receipts.** The engine reports last
  capture time and availability for every readable active source. The owner CLI may add collector
  run receipts when the collector is installed. The core never requires the collector, and MCP
  sessions see engine facts only.
- **Each generated brief is stored** in a capped, rebuildable log outside Portable, so the next brief
  can report what changed, and a dated brief survives when inputs are unavailable.
- **Scope is selected by space.** Spaces already gate launcher policies, so the brief's scope is
  enforced, not just filtered.
- **Architecture A:** an app-layer composer (`services/brief.py`), one read-only engine freshness
  task, and a JSON brief log under `.open-brain/state`. Coordinates stay 23/16/18.

## Surfaces

### Owner CLI

```
open-brain brief [--space NAME ...] [--stale-days N] [--json]
                 [--collector-state PATH --collector-executable PATH]
open-brain brief ack SOURCE_ID [--json]
open-brain brief last [--space NAME ...] [--json]
```

- Default scope is every space the owner can read, every tier. `--space` is repeatable and
  validated against `space list`; an unknown name is `invalid_request`, never an empty brief.
- `--stale-days` defaults to 7. There is no stored configuration.
- The collector flags are optional and owner-only. They read HealthV1 receipts through the same
  foreground subprocess path as `source guide status`. When absent or unreadable, `coverage` says so
  and the brief still generates.
- `brief last` shows the most recent saved brief for the scope, re-scoped at read time.

### MCP

- `brain_brief` (scope, optional `stale_days`) and `brain_brief_ack` (`source_id`).
- Two new grants, off by default and independent: `brief-read` and `brief-ack`. They are wired
  through `open-brain mcp --allow-brief-read/--allow-brief-ack` and `agent setup` with the same
  flags, and forwarded by the plugin bridge like the follow-up grants.
- Follow-up items appear only when the session also holds `followup-read`. Without it, the brief
  carries source items only and `coverage` reports `followups: not_granted`.
- Every MCP session is Work-only by default, including an unscoped `open-brain mcp`. Personal or
  Public appears only when the launcher policy allows that tier. The collector read is not available
  to MCP sessions.
- External-provider sessions get no brief tools, following the follow-up rule.

## Brief shape

A brief holds at most five items plus brief-level fields.

### Item

| Field | Meaning |
|---|---|
| `kind` | One of the registered kinds below |
| `ref` | A follow-up `item_id` or a `source_id` |
| `reasons` | One or more reason codes plus one plain sentence |
| `evidence` | Evidence IDs only; never text |
| `changed` | `new`, `still_open` or `changed` against the last brief for this scope |
| `rank` | 1 to 5 |

Kinds in version 1:

| Kind | Source | Condition |
|---|---|---|
| `followup_due` | follow-ups | open, due date today or past |
| `followup_new_evidence` | follow-ups | dismissed or resolved, `new_evidence_since_decision` set |
| `followup_open` | follow-ups | suggested or confirmed, owner `self`, not snoozed |
| `source_unavailable` | engine, collector | availability `missing` or `inaccessible`, or a failed collector run |
| `source_stale` | engine, collector | no capture in more than `stale_days`, or a partial or overdue collector run |

The kind set is an open registry. Later features add kinds with their own candidate provider and
no change to the brief shape: `decision_changed` (item 2) and `feedback_recurring` (item 4).

### Brief-level fields

| Field | Meaning |
|---|---|
| `generated_at` | Generation time |
| `scope` | Sorted space IDs and the tiers actually read |
| `items` | Zero to five items |
| `nothing_needs_attention` | True when `items` is empty |
| `since` | `generated_at` of the previous brief for this scope, or null |
| `coverage` | Sources checked, sources stale or unknown, providers that failed or were not granted, collector status |
| `saved` | False when the log could not be written |
| `stale_brief` | True only on the fallback path |

Text fields never carry quote text, summaries or titles. A client fetches those through
`followup show`, where scope is checked again at read time.

## Candidate providers and ranking

Each provider returns candidates with reason codes. The composer merges, sorts and keeps five.

1. **Follow-ups.** Read through the follow-up service with the caller's authority, so visibility,
   tier and retraction rules apply unchanged. Snoozed items stay out until their date.
2. **Source freshness.** A new read-only engine task `source_freshness(authority, space_ids)`
   returns, for every active source the caller can read: `source_id`, space, availability, last
   capture time. Retired sources are excluded.
3. **Collector health** (owner CLI only). Receipts with outcome `failed` become `source_unavailable`;
   `partial` or overdue become `source_stale`. The collector's own reason code is carried in
   `reasons`.

Ranking is fixed and explainable:

1. `followup_due`, by due date then age
2. `source_unavailable`
3. `followup_new_evidence`, by evidence time
4. `followup_open`, oldest first
5. `source_stale`, longest silence first

Within a level, items new since the last brief rank above items still open. At most two source
items appear in one brief, so health warnings cannot crowd out work.

## Acknowledging a source

`brief ack SOURCE_ID` records a fingerprint of the source's current state (last capture time,
availability, last collector outcome when present) in the brief log for the caller's scope. The
source stays out of briefs until the fingerprint changes. Acking an unknown or hidden source is
`not_found`. The ack is written with compare-and-swap; a conflict is `version_conflict` and the
caller retries.

## Storage

The brief log is `.open-brain/state/brief-log.json`, written with `atomic_replace` and
`expected_existing_sha256`, the `GraphProjectionStore` pattern.

- Holds the last 30 briefs per scope key, where the key is the sorted space IDs plus tiers read.
  Each entry holds `generated_at`, items with refs, reasons and evidence IDs, the coverage snapshot,
  and the source-ack fingerprints.
- Stores no quote text, summaries or titles.
- Is not Portable content. It is a rebuildable view like graph state; losing it resets `since` and
  the acks and nothing else.
- Owner and MCP briefs are separate entries because their scope keys differ. One never reveals the
  other's items.
- A corrupt or unreadable file is renamed aside with a timestamp, never silently deleted; the brief
  generates with `since: null` and `coverage.log: unavailable`.
- Two concurrent writers: the loser retries once, then returns the brief with `saved: false`.

## Failure behavior

- A provider that fails partway is listed in `coverage` with a reason code; the others still appear.
- When nothing is readable, return the last saved brief for the scope with `stale_brief: true` and
  its original `generated_at`, re-scoped at read time so withheld items show as `withheld`.
- Error codes: `invalid_request`, `unsupported_capability`, `operation_pending`,
  `version_conflict`, `not_found`.

## Bounds

- Response at most 32 KB; five items; fifty coverage entries.
- No background work, no model calls, no network. The collector read is a local foreground
  subprocess that exits with the command.
- Scheduled generation is out of version 1. The evaluation asks for on-request generation first,
  and the core has no unattended model execution path. The log format already supports a later
  collector-run brief.

## Testing

Synthetic fixtures only.

1. **Engine `source_freshness`:** respects tier and space; skips retired sources; reports
   `missing` and `inaccessible`; stale cutoff under a frozen clock.
2. **Brief service:** ranking order, tie-breaks and the two-source cap; `changed` against the last
   brief; acks hide a source until its fingerprint changes; `nothing_needs_attention`; CAS conflict
   gives `saved: false`; corrupt log gives `since: null` and a renamed file; partial provider failure
   appears in coverage; stale-brief fallback is re-scoped.
3. **Scope:** a Work-only MCP session never sees Personal follow-ups or sources; a session without
   `followup-read` gets no follow-up items; owner and MCP log entries never mix; retracted evidence
   shows as `withheld` in a past brief.
4. **CLI and MCP:** `brief-read` and `brief-ack` off by default and independent, through `mcp` and
   `agent setup`; collector flags owner-only; absent collector reported in coverage.
5. **Acceptance** (evaluation section 5): every item can be inspected and acted on through existing
   operations; a resolved item does not reappear without new evidence; unavailable inputs still
   return the last dated brief; reading a brief publishes no note and sends nothing.

## Documentation

- New `docs/daily-brief.md`, with one short example of a client turning `brain_brief` into a
  morning summary.
- Grant lists in the reference docs, both `CLAUDE.md` files and the documentation gate.

## Compatibility

Coordinates stay 23/16/18. No schema, Portable or runtime-session change. The native smoke module
list gains the new app module only if the audit requires it.

## Delivery

Depends on PR #119. Work starts from `feat/followups` and rebases onto main once #119 merges.
Roughly 12 to 14 plan tasks in three packages: engine freshness read and brief log; composer, CLI
and MCP; acceptance, docs and gate. Same execution pattern as item 3: one implementer and one
independent reviewer per package.

## Not in version 1

Scheduled or collector-run generation, model-written summaries, decision and feedback items, a
brief-only mute list, per-user ranking weights, and storing briefs in Portable.
