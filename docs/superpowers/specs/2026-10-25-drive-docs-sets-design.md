# Drive Docs sets design (Gemini Meet notes through Drive)

Item I1 of `docs/audits/2026-10-25-integrations-assessment.md`. Extends the Google Drive source in
`packages/connectors/src/open_brain_connectors/runtime/google_sources.py` and the collector's live
capture path. The audit's inventory of the current Drive path is summarized under "What exists".

Revision 1.

## Goal

Capture the Google Docs that Gemini writes after a Google Meet call, the notes Doc and the
transcript Doc, as live Work sources, so meetings feed search, follow-ups and the daily brief. The
owner selects a folder of Docs, or Docs across the whole account by title suffix, with a backfill
date. Long Docs are split into parts instead of failing. An edited Doc becomes a new revision of the
same record instead of a quarantined conflict.

## Decisions taken

- **Notes and transcripts are both captured.** Transcripts are long verbatim speech, so splitting
  over the 60,000-character export bound is required in version 1.
- **Backfill is bounded by a date.** The owner passes `date_floor` on configure. Nothing older is
  listed. Later runs are incremental.
- **Two selection shapes ship.** A folder shape for any Docs folder, including the owner's
  `Meet Recordings`, and an account shape for notes of meetings other people organized, which reach
  the owner as shared files and never sit in the owner's folder.
- **Edits are revision-aware, like Slack.** The collector's revision sink is generic in the engine;
  only its collector gate is Slack-specific. The gate becomes a table keyed by connector name.
- **`drive_file` is unchanged.** Records captured through the single-file shape carry a plain
  delivery id with a `Source revision:` text prefix. Routing them through the revision sink would
  turn their first edit into a conflict against the existing capture. The documentation is
  corrected to say an edited single file is quarantined (gotcha below).
- **No new provider.** Meet notes are Docs in Drive. A `google_meet_notes` provider would duplicate
  Drive auth and client code and break on a Google rename. A Meet or Zoom API client stays out, as
  the audit decided.
- **Coordinates stay 23/16/18.** No schema, runtime-session or Portable change.

## What exists

Facts from the code at `df6b5df`, with the file lines a plan author needs:

- Drive capture is one file by id: selection `drive_file:<fileId>`, options must be `{}`
  (`google_sources.py` `_drive_selection`, `fetch` option check). The checkpoint shape is exactly
  `{file_id, revision, status}` (`_drive_state`).
- `resources` lists the whole account with `q="trashed = false"` and no folder, type or time filter,
  returning only id, name and type. `_DRIVE_FIELDS` omits `parents` and `createdTime`.
- Docs export as `text/plain`. A body over 60,000 characters (`_MAX_TEXT`) raises
  `drive_content_too_large` from `fetch`, so the source is stuck. The intake cap is 65,536
  characters (`source_intake.py`), and the non-revision submit path prepends a
  `Source revision: <64 hex>` line.
- The provider revision is a SHA-256 over `modifiedTime`, `version`, `md5Checksum`, `mimeType`,
  `name` and `webViewLink` (`_drive_revision`). The delivery id excludes the revision.
- Gmail takes `options={"date_floor": "YYYY-MM-DD"}` (`_date_floor`) and keeps a stateful
  checkpoint. `capture_policy.validate_options` strips only `privacy_tier` and hands every other
  key to the provider.
- Only Slack goes through `collector_revision_sink`: `capture_policy.observe_slack` attests the
  intake and `live_capture.py` `_submit` routes on `connector_name == "slack"` to a
  `slack-revisions` directory. Any other connector with a changed revision reaches the engine as a
  `DeliveryConflict` and is recorded `quarantined` with `source_revision_conflict`
  (`test_live_capture.py::test_preview_then_unordered_revision_refusal_preserves_source_and_checkpoint`).
- `LiveBatch` holds at most 25 intakes and 25 notices. `_response` caps an export body at 1 MiB.
- The collector maps one resource type per provider (`live_capture.py` `_PROVIDERS`), and the CLI
  `resources` command has no resource-type argument (`sources_cli.py`).

## Selection

Two resource types are added to the `google_drive` descriptor. `drive_file` stays.

| Shape | `resource_type` | `resource_id` | Options |
|---|---|---|---|
| Folder | `drive_folder` | `drive_folder:<folderId>` | `date_floor` required, `title_suffixes` optional |
| Account | `drive_account` | `drive_account:all` | `date_floor` required, `title_suffixes` required |

- `date_floor` is `YYYY-MM-DD`, parsed by the helper Gmail uses. It bounds the first run. It has no
  effect once the checkpoint is incremental.
- `title_suffixes` is a JSON list of 1 to 8 strings, each 1 to 64 characters after stripping, no
  duplicates, no NUL. A Doc is selected when its name ends with one of them. The Meet values are
  `["Notes by Gemini", "Transcript"]`.
- Any other option key, a missing required option, or a malformed value is `drive_invalid_options`.
  A resource id that does not match its type is `drive_invalid_selection`.
- The grant stays account-wide `drive.readonly`. The selection narrows what is imported, not the
  grant, as `docs/integrations/google-sources.md` already states.

### Listing query

One `files.list` call per page, for both shapes:

```
mimeType = 'application/vnd.google-apps.document'
and trashed = false
and modifiedTime > '<bound>'
[and '<folderId>' in parents]                      # folder shape
[and (name contains '<s1>' or name contains '<s2>' ...)]  # when title_suffixes is set
```

- `orderBy=modifiedTime`, `pageSize=100`, `includeItemsFromAllDrives=true`,
  `supportsAllDrives=true`, `fields=nextPageToken,files(<_DRIVE_FIELDS>,parents,createdTime)`.
- `name contains` is a server-side prefilter. The exact suffix check runs on the returned name.
- Suffix values are escaped for the Drive query grammar (backslash and single quote).
- Recordings, PDFs and other types never match the MIME clause, so no `drive_unsupported_type`
  notices arise from a Docs set.

### Finding a folder

`resources` gains `--resource-type {drive_file,drive_folder}` with default `drive_file`, valid for
`google_drive` only. `drive_folder` lists folders (`mimeType = 'application/vnd.google-apps.folder'
and trashed = false`) as `LiveResource("drive_folder:<id>", name, "drive_folder")`, paged as today.
`drive_account:all` is a fixed id documented in the operator guide and is not listed.

## Checkpoint

Per source, metadata only, validated on every fetch like `_drive_state` today:

```json
{"mode": "backfill",
 "page_token": null,
 "watermark": "2026-10-01T00:00:00Z",
 "known": {"<fileId>": {"revision": "<revision>", "modified": "<RFC3339 modifiedTime>"}}}
```

- `mode` is `backfill` or `incremental`. `page_token` is a string or null. `watermark` is an
  RFC 3339 UTC timestamp. `known` maps file id to the provider revision last submitted or skipped
  and the `modifiedTime` seen with it.
- First run: `mode=backfill`, `watermark=<date_floor>T00:00:00Z`. The query bound is the watermark.
  `page_token` and `watermark` advance only when a page is fully drained (every file on it submitted
  or skipped); `watermark` then becomes the newest `modifiedTime` on that page. When
  `nextPageToken` is absent after the last page drains, `mode` becomes `incremental`.
- Incremental run: the query bound is `watermark` minus 24 hours. The overlap covers late indexing
  and clock skew. Files in `known` with an unchanged revision are skipped without a fetch.
- `known` holds at most 2,000 entries. When full, the entries with the oldest `modified` value are
  dropped. A dropped entry only risks one re-submit, which the sink answers as `duplicate`, so
  eviction is safe.
- Any other shape, an unknown key, or more than 2,000 entries on read is `drive_invalid_checkpoint`.
  A `reset` on configure clears it, as for every source.

## Fetch

One `fetch` call:

1. Validate selection, options and checkpoint.
2. List one page at `page_token` (or the first page for the current bound).
3. For each file in order whose revision is new or changed: GET metadata, export, split (below).
   Stop adding files when the next file's parts would push the batch past 25 intakes. Files not
   reached stay unlisted until the next call, which repeats the same page with the same
   `page_token`; `known` makes the repeat cheap.
4. Return `LiveBatch(intakes, checkpoint, notices, has_more)`. `has_more` is true while files on
   this page remain or `nextPageToken` is present.

Per-file metadata and export are the same two requests the single-file path makes, with the same
`fields` and `supportsAllDrives=true`.

### Per-file failures are notices

A single file must not stall a set. For a file that returns 404, a lost-access 403, `trashed`,
`canDownload != true`, a non-UTF-8 body, a body over 1 MiB, or an empty body, the fetch appends the
notice `drive_file_skipped:<reason>:<fileId>` with `reason` in
`missing|access_lost|trashed|not_downloadable|not_text|too_large|empty`, records the file in `known`
with the revision seen, and continues. Notices are capped at 25 per batch; the 26th and later
skipped files are left out of `known` so the next call reports them.

Account-level errors still raise and leave the checkpoint unchanged: 401 is
`google_auth_required`, rate-limit 403 is `google_rate_limited`, other 403 is `drive_forbidden`.
When the folder itself returns 404 or lost access, the fetch returns no intakes with the notice
`drive_folder_missing` and the checkpoint unchanged; no placeholder record is written.

### Splitting

When the exported text is over 60,000 characters:

- Parts are at most 60,000 characters. The cut is the last blank line (`\n\n`) within the limit,
  else the last newline, else a hard cut. Leading newlines of the next part are stripped.
- At most 18 parts, which the 1 MiB export cap already guarantees; a 19th is `too_large`.
- Each part is one intake: `external_id` is `file:<fileId>#part:<n>` (1-based), title is
  `<name> (part <n> of <N>)` cut to 200 characters with the suffix preserved, `url` is the Doc link,
  `revision_id` is `revision:<drive revision>` for every part.
- A Doc under the limit is one intake with `external_id` `file:<fileId>`, as today.
- When a later revision has fewer parts, the surplus parts keep their last revision. This is
  documented, not repaired.

Each intake keeps the Drive key layout: `connector_name="google_drive"`,
`connection_id="account:<hash>"`, `resource_id` of the set, `external_id` as above.

## Revision-aware intake

The engine stores Slack revisions through `collector_revision_sink` with a
`SourceRevisionObservation`. Two collector gates make it Slack-only. They become one table:

```python
_REVISION_CONNECTORS = {
    "slack": ("slack-source-v1", "slack-revisions"),
    "google_drive": ("drive-source-v1", "drive-revisions"),
}
```

- `capture_policy.observe_slack` becomes `observe_revision(intake)`. It attests any intake whose
  connector is in the table and whose `observation` is None, with that connector's normalization
  version. Slack bytes do not change; the Slack suites are the regression net. `observe_slack` is
  removed and its callers call `observe_revision`.
- `live_capture.py` `_submit` routes on the table and uses the connector's revisions directory
  under the collector state root. Slack keeps `slack-revisions`.
- Privacy narrowing (`admitted_intake`) runs before observation, as for Slack, because it refuses an
  intake that already carries an observation.
- The gate applies to `drive_folder` and `drive_account` intakes only. The provider sets
  `observation=None` and the collector attests; `drive_file` intakes are excluded by resource type
  in the same function, so they keep the `Source revision:` prefix path.

An edited Doc: its revision hash changes, the file is re-exported and re-split, each part is
submitted as a new revision of the same source record. The previous text stays in history and the
new text is searchable. Follow-up evidence quoted from an earlier revision stays bound to that
revision, as it does for Slack.

Plan-time check: confirm the engine revision path has no Slack-specific constant (normalization
version, connector name, or directory). If one exists, generalizing it is a plan task.

## Surfaces

- CLI: `resources --resource-type` as above. `configure --arguments-file` already carries the
  selection and options JSON. `preview`, `import`, `control`, `status`, `custody-*` are unchanged.
  Preview output stays `title`, `source_reference`, `trust`, so a split Doc previews as its parts.
- Collector: the provider-to-resource-type map becomes a set per provider; `_provider` and the CLI
  `--provider` choices are unchanged.
- Descriptor: `google_drive_source_descriptor().resource_types` becomes
  `("drive_file", "drive_folder", "drive_account")`; `content_types` stays `("drive_file",)`.
- App, engine, catalog, grants and MCP: unchanged.

## Failure behavior

| Condition | Result |
|---|---|
| Unknown option key, bad `date_floor`, bad `title_suffixes` | `drive_invalid_options` |
| Resource id does not match its type | `drive_invalid_selection` |
| Checkpoint shape wrong | `drive_invalid_checkpoint` |
| 401 | `google_auth_required`, checkpoint unchanged |
| Rate-limit 403 | `google_rate_limited`, checkpoint unchanged |
| Other 403 on list | `drive_forbidden`, checkpoint unchanged |
| Folder 404 or access lost | notice `drive_folder_missing`, no intakes, checkpoint unchanged |
| Per-file loss or bad body | notice `drive_file_skipped:<reason>:<fileId>`, run continues |
| Over 18 parts | notice `drive_file_skipped:too_large:<fileId>` |
| Edited Doc | new revision of the same record |

## Bounds

- 100 files listed per page; 25 intakes and 25 notices per batch; 60,000 characters per part;
  18 parts per Doc; 1 MiB per export body; 8 suffixes of 64 characters; 2,000 `known` entries.
- No new network destination: the Drive v3 `files` endpoints already used.
- No background work in the core. Scheduling stays in the optional collector.

## Testing

Synthetic fixtures only, no network, `tmp_path` roots.

1. Connectors contract tests beside `test_google_sources.py`, using its `_Http` and `_auth`
   fakes: query string for each shape, with and without suffixes, with escaping; suffix check on
   returned names; two-page backfill moving the watermark and switching to incremental; incremental
   bound 24 hours before the watermark; `known` skip and eviction at 2,000; 25-intake bound with
   `has_more` and page repeat; every `drive_file_skipped` reason; `drive_folder_missing`; account
   errors leave the checkpoint unchanged; split at blank line, newline and hard cut; part titles and
   external ids; 18-part cap; a Doc under the limit unchanged. Fixtures: a Doc shaped like a Gemini
   note (Summary, Details, Suggested next steps) and a 150,000-character transcript.
2. Collector integration tests beside `test_live_capture.py`: a folder-set intake whose Doc is
   edited becomes a new searchable revision with the first text in history; a `drive_file` intake
   edited is still quarantined with `source_revision_conflict`; a Slack intake's attested bytes are
   unchanged after the gate refactor; the revisions directory is `drive-revisions`.
3. `test_source_registry.py` descriptor resource types; `sources_cli` test for `--resource-type`,
   its default and its rejection for other providers; docs tests for the new sections.

## Documentation

- `docs/integrations/google-sources.md`: the two shapes, options, listing query, checkpoint,
  splitting, revision behavior, and a corrected statement that an edited `drive_file` is
  quarantined.
- `docs/integrations/priority-capture-operator.md`: a Meet-notes walkthrough: list folders,
  configure `drive_account:all` with the two suffixes and a date floor, preview, import, enable.
- Repo `CLAUDE.md` product boundary: Drive capture selects a file, a folder of Docs, or account
  Docs by title suffix.
- `docs/engineering/gotchas/README.md`: one entry for the `drive_file` edit contradiction (docs
  said body edits re-import; the current engine quarantines them).

## Acceptance

Synthetic acceptance is the collector integration test above. The live gate is owner-authorized and
separate, recorded in `docs/integrations/google-sources.md` "Live acceptance" with receipts outside
Git: one real Gemini note, one real transcript over 60,000 characters, one edit to the note. It also
answers the audit's open question on Gemini's title format; if real titles differ, only the
`title_suffixes` values change.

## Delivery

Three packages, each PR-ready on its own:

1. Connector: selection shapes, listing, checkpoint, fetch loop, per-file notices, splitting,
   descriptor, contract tests.
2. Collector: revision gate table, `_submit` routing, provider map as a set, integration tests.
3. CLI flag, documentation, gotcha, acceptance walkthrough, docs tests.

## Not in version 1

- Carrying `createdTime`, `modifiedTime`, folder or owner as record metadata.
- Migrating `drive_file` records to the revision sink.
- Repairing surplus parts when a revision shrinks.
- Structured export of Docs (headings, tabs) beyond `text/plain`.
- Calendar linkage of a note to its event, and the meeting-transcript adapter's live client.
