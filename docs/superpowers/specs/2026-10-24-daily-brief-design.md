# Daily work brief design

Item 5 of `docs/audits/2026-10-14-work-laptop-product-evaluation.md`. Builds on follow-ups
(`docs/superpowers/specs/2026-10-23-commitments-open-questions-design.md`, PR #119).

Revision 2, after an adversarial review: visibility key, no `withheld` items for clients,
freshness per capture family, collector receipts deferred, per-key log files, package order.

## Goal

On request, give the user up to five items that need attention today: follow-ups that are due or
have new evidence, open follow-ups they own, and capture families that have gone stale or hold
unavailable captures. Each item says why it appears and points at its evidence. When nothing needs
attention, the brief says so instead of filling a quota.

## Decisions taken

- **The engine composes the brief with no model.** Ranking is rule-based and deterministic. A client
  may reword the result; Open Brain adds no model call, no network path and no background work.
- **Brief items are pointers, not state.** Acting on a follow-up item uses the existing
  `followup decide` operation with its grants and history. A capture family is acknowledged, and
  stays out until its state changes. There is no brief-only dismiss list.
- **Freshness comes from engine facts, per capture family.** A family is one consumer namespace
  (an agent session source, from `source_namespaces`) or the Markdown import of one space. Manual
  captures form no family. One `logical_sources` row is one capture, so per-source freshness would
  flag the oldest unchanged notes forever; the family is the unit a user means by "source".
- **Collector receipts are deferred to version 1.1.** The core can read collector health for the
  journal-selected source only, and has no headless list operation. Version 1 reports
  `coverage.collector: not_read`. A collector list operation is its own later feature.
- **Each generated brief is stored** in a capped, rebuildable per-key log outside Portable, so the
  next brief for the same visibility can report what changed, and the owner keeps a dated brief.
- **Scope is selected by space.** Spaces are the project selector; they already gate launcher
  policies, so the brief's scope is enforced, not just filtered. The free-text follow-up `project`
  field is not a filter in version 1.
- **Architecture A:** an app-layer composer (`services/brief.py`), one read-only engine task, and
  JSON log files under `.open-brain/state/briefs/`. Coordinates stay 23/16/18.

## Surfaces

### Owner CLI

```
open-brain brief [--space NAME ...] [--stale-days N] [--json]
open-brain brief ack FAMILY_REF [--json]
open-brain brief last [--space NAME ...] [--json]
```

- Default scope is every space, every tier. `--space` is repeatable and validated against
  `space list`; an unknown name is `invalid_request`, never an empty brief.
- `--stale-days` defaults to 7, minimum 1, maximum 365. There is no stored configuration.
- `brief last` shows the most recent saved brief for the owner's visibility key, re-read with the
  owner's authority so a follow-up whose evidence was retracted shows as `withheld`.

### MCP

- `brain_brief` (optional `space_ids`, optional `stale_days`) and `brain_brief_ack` (`family_ref`).
- Two new grants, off by default and independent: `brief-read` and `brief-ack`. They are wired
  through `open-brain mcp --allow-brief-read/--allow-brief-ack` and `agent setup` with the same
  flags, and forwarded by the plugin bridge like the follow-up grants. Scoped Work evidence setup
  refuses them, as it refuses the follow-up grants.
- Follow-up items appear only when the session also holds `followup-read`. Without it, the brief
  carries family items only and `coverage.followups` is `not_granted`.
- Every MCP session is Work-only by default, including an unscoped `open-brain mcp`. Personal or
  Public appears only when the launcher policy allows that tier.
- There is no MCP `brief last`. A client never receives a brief served from the log.
- External-provider sessions get no brief tools, following the follow-up rule.

### Session authority

One function, `brief_session_authority`, produces the authority used for every provider read and
for the visibility key. It applies the follow-up clamp (`followup_session_authority`) and additionally
records which brief and follow-up grants the session holds. The engine task `source_freshness`
requires `owner` or the `brief-read` capability. No provider ever runs under owner authority for a
non-owner session.

## Brief shape

A brief holds at most five items plus brief-level fields. Text fields never carry quote text,
summaries or titles. A client fetches those through `followup show`, where scope is checked again.

### Item

| Field | Meaning |
|---|---|
| `kind` | One of the registered kinds below |
| `ref` | `followup:<item_id>` or `family:<family_id>` |
| `reasons` | One or more reason codes plus one plain sentence |
| `evidence` | Follow-up evidence IDs; empty for family items |
| `changed` | `new`, `still_open` or `changed` against the last brief for this visibility key |
| `rank` | 1 to 5 |

Kinds in version 1:

| Kind | Condition |
|---|---|
| `followup_due` | state suggested or confirmed, due date today or past |
| `followup_new_evidence` | state dismissed or resolved, `new_evidence_since_decision` set |
| `followup_open` | state suggested or confirmed, owner `self`, no due date today or past |
| `source_unavailable` | one or more captures in the family have availability `missing` or `inaccessible` |
| `source_stale` | newest capture in the family is older than `stale_days` |

"Open" means suggested or confirmed. A snoozed item is not open until its date; when the date
passes it returns in its earlier state and is judged by that state. The kind set is an open registry:
later features add kinds with their own provider and no shape change (`decision_changed` for item 2,
`feedback_recurring` for item 4).

### Brief-level fields

| Field | Meaning |
|---|---|
| `generated_at` | Generation time from the engine clock |
| `scope` | Selected space IDs (or `all`) and the tiers the authority may read |
| `items` | Zero to five items |
| `nothing_needs_attention` | True when `items` is empty |
| `since` | `generated_at` of the previous brief for this key, or null |
| `coverage` | Aggregate counts and provider status, below |
| `model` | Always `none` |
| `saved` | False when the log could not be written |

`coverage` holds: `families_checked`, `families_stale`, `captures_unavailable`,
`followups_scanned`, `followups_truncated` (bool), `followups` (`read`, `not_granted`, `failed`),
`freshness` (`read`, `failed`), `collector` (`not_read`), `hidden_dropped` (count of candidates the
composer could not re-read; never their identity), and `log` (`entries`, `bytes`, `unavailable`).

## Candidate providers and ranking

Each provider returns candidates with reason codes. The composer merges, sorts and keeps five.

1. **Follow-ups.** Read through the follow-up service with the session authority, so visibility,
   tier and retraction rules apply unchanged. The provider scans at most 500 open items ordered by
   due date then creation time, and sets `followups_truncated` when more exist. For `--space`, an
   item's space is the set of spaces of its evidence head captures; the owner path filters by
   selected spaces explicitly, since owner authority carries no space restriction.
2. **Capture-family freshness.** A new read-only engine task
   `source_freshness(authority, space_ids, stale_days)` returns, for every family the authority can
   read: `family_id`, `kind` (`consumer_namespace` or `markdown_import`), `space_id`, newest
   `recorded_at`, capture count, and count of captures with availability `missing` or
   `inaccessible`. Retired captures are excluded. It reads `source_namespaces`, `logical_sources`,
   `source_revisions` and, to classify Markdown-import captures, `markdown_import_revisions`, all
   read-only; no schema change. A capture with no space reports `space_id` as `unassigned`, which a
   space selection never matches.

Ranking is fixed and explainable:

1. `followup_due`, by due date then creation time
2. `source_unavailable`, by unavailable count
3. `followup_new_evidence`, by `recorded_at` of the newest evidence row added after the decision
4. `followup_open`, oldest first
5. `source_stale`, longest silence first

Within a level, items new since the last brief rank above items still open. At most two family
items appear in one brief, so health warnings cannot crowd out work.

## Acknowledging a family

`brief ack family:<id>` records a fingerprint of the family's current state (newest `recorded_at`,
unavailable count) in the log for the caller's visibility key. The family stays out of that key's
briefs until either component changes. Acks are keyed by visibility key and family ref, independent
of the `--space` selection used to generate the brief. Acking a family the authority cannot read, or
an unknown ref, is `not_found`. The ack is written with compare-and-swap; a conflict is
`version_conflict` and the caller retries.

## Visibility key and storage

The visibility key is `sha256` over the canonical JSON of: `owner`, sorted `allowed_read_tiers`,
sorted `space_ids` or `all`, `egress_mode`, `provider_id`, sorted brief and follow-up grants held,
and the sorted selected space IDs. Two sessions share log entries only when every one of those is
equal, so an owner brief is never read by a client and a session with `followup-read` never shares
with one without it.

Each key has its own file, `.open-brain/state/briefs/<key>.json`, written with `atomic_replace` and
`expected_existing_sha256`, the `GraphProjectionStore` pattern.

- A file holds the last 30 briefs and the acks for that key, and is capped at 64 KB; the oldest
  brief is dropped first. At most 16 key files exist; the least recently written is removed when a
  seventeenth is needed.
- Entries hold `generated_at`, items with refs, reasons and evidence IDs, the coverage snapshot,
  and ack fingerprints. No quote text, summaries or titles.
- Not Portable content. The log is a rebuildable view like graph state; losing it resets `since`
  and the acks and nothing else.
- A corrupt or unreadable file is renamed to `<key>.json.aside` (replacing any earlier aside copy),
  never silently deleted. The brief generates with `since: null` and `coverage.log.unavailable: true`.
  Only that key's history is affected.
- Two concurrent writers on one key: the loser retries once, then returns the brief with
  `saved: false`.

## Failure behavior

- A provider that fails is reported in `coverage` (`failed`); the others still appear.
- A candidate the composer cannot re-read at assembly time is dropped for non-owner sessions and
  counted in `coverage.hidden_dropped`. Only the owner CLI (`brief last`) shows `withheld` entries.
- When the engine cannot be read, every surface returns `operation_pending`. Nothing is served from
  the log on the `brief` or `brain_brief` path.
- Error codes: `invalid_request`, `unsupported_capability`, `operation_pending`,
  `version_conflict`, `not_found`.

## Bounds

- Response at most 32 KB; five items; the follow-up scan stops at 500 items.
- No background work, no model calls, no network, no subprocess.
- Scheduled generation is out of version 1. The evaluation asks for on-request generation first,
  and the core has no unattended model execution path. The log format already supports a later
  collector-run brief.

## Testing

Synthetic fixtures only; the engine clock is injected.

1. **Engine `source_freshness`:** one family per consumer namespace and per Markdown space;
   manual captures excluded; respects tier and space; skips retired captures; counts `missing` and
   `inaccessible`; stale cutoff under a frozen clock; refuses an authority without `brief-read`.
2. **Brief service:** ranking order, tie-breaks and the two-family cap; `changed` against the last
   brief; acks hide a family until a fingerprint component changes and ignore `--space`;
   `nothing_needs_attention`; follow-up scan truncation; CAS conflict gives `saved: false`; corrupt
   file gives `since: null`, an aside copy and no effect on other keys; file and key caps; partial
   provider failure appears in coverage; engine unreadable gives `operation_pending`.
3. **Scope:** two sessions differing only in grants, tiers, egress or owner flag get different
   keys and never read each other's entries; a Work-only MCP session never sees Personal follow-ups
   or families; a session without `followup-read` gets no follow-up items; a non-owner never
   receives a `withheld` entry or a hidden ref; `source_freshness` runs under the clamped authority.
4. **CLI and MCP:** `brief-read` and `brief-ack` off by default and independent, through `mcp` and
   `agent setup`; scoped setup refuses them; plugin bridge forwards them; external-provider sessions
   get no brief tools; `services/brief.py` is allow-listed in the architecture boundary test.
5. **Acceptance** (evaluation section 5): every item can be inspected and acted on through existing
   operations; a resolved item does not reappear without new evidence; the owner's last dated brief
   stays readable when follow-up reads fail; reading a brief publishes no note and sends nothing;
   `model` is `none`; storage growth is visible in `coverage.log`.

## Documentation

- New `docs/daily-brief.md`, with one short example of a client turning `brain_brief` into a
  morning summary, and a plain statement that collector health and scheduling are not in version 1.
- Grant lists in the reference docs, both `CLAUDE.md` files and the documentation gate.

## Compatibility

Coordinates stay 23/16/18. No schema, Portable or runtime-session change. The native smoke module
list gains the new app module only if the audit requires it.

## Delivery

Depends on PR #119. Work starts from `feat/followups` and merges main once #119 lands. Three
packages, each with one implementer and one independent reviewer:

1. Contracts (kinds, reason codes, coverage, visibility key), `brief_session_authority`, and the
   engine `source_freshness` task with its tests.
2. Log store, composer, owner CLI, MCP tools and both grants across `mcp`, `agent setup` and the
   plugin bridge.
3. Acceptance module, `docs/daily-brief.md`, reference docs and the gate.

## Not in version 1

Collector receipts and the owner-CLI collector flags, scheduled or collector-run generation,
model-written summaries, decision and feedback items, a brief-only mute list, a `--project` filter,
per-user ranking weights, an MCP `brief last`, and storing briefs in Portable.
