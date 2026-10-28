# Saved item capture design

Revision 5 (plan-authoring amendments): the folder source runs on the live-source runtime
(`LiveSourceManager`, `LiveCaptureService`, `PriorityRuntime`) with `agent_session` as the
template, not on the older `saved_markdown` controller; one run is one preview page of at most 25
intakes under a byte budget, continued while `has_more`; bounds follow the plain capture path and
custody quota; titles cap at 200 characters; text sections are masked with `redact_text` instead
of refusing the save, and only the URL query and fragment can refuse it.
Revision 4 (hardening cycle 2): the reason is always a `Stated reason` text section, because every
collector delivery uses the public-job path, which cannot assert owner authorship
(`CaptureSubmission.for_public_job`, `contracts.py`); the first save of a URL wins and there are no
revisions in v1, because the collector revision sink holds at most 256 retained items with no
eviction (`lifecycle.py`); identity uses the adapter's own URL normalization, since
`canonicalize_source_url` drops fragments; the folder index is keyed by fixed-size hashes and stat
fingerprints; entries are read through the folder descriptor and dataless cloud files are skipped;
bounds are measured in the encoding the collector enforces; tier travels as a selection option.
Revision 3 (hardening cycle 1): fragments and content-selecting parameters kept in identity;
dedupe scoped to one folder; regular files only; non-blank `why`; `platform_id` and
`preview-records` dropped.
Revision 2: generalized from social posts to any saved URL before any record exists.
Revision 1: social posts only.

Adds a `saved_item` connector source and a collector folder source for it. A saved item is a URL
the owner chose to keep, with the reason they kept it: a social post, an article, a video, an event
page, a product, a place. Producers write one bounded JSON record per item into a folder the owner
selects, which may be local or cloud-synced. The collector reads the folder and delivers each new
item through custody as a capture whose text includes the stated reason.

Records may be rich (a producer already fetched and extracted the content) or thin (only the URL,
the reason and whatever the share sheet passed along). Enrichment, revisions and Open Brain-owned
summarization are later specs; this one fixes the contract and the intake path.

## Goal

Anything the owner saves from a phone, a browser or a script becomes a searchable Open Brain record
that includes the reason it was saved, with no engine, schema or Portable Brain change and no
listener or new dependency.

## Product layers

| Layer | Owner | In this spec |
|---|---|---|
| Capture surface (share sheet, bookmarklet, script, import) | Producer | Example iOS Shortcut documented |
| Inbox transport (a folder, optionally cloud-synced) | Owner's filesystem | Yes |
| Enrichment (fetch, extract, transcribe) | Producer today; optional collector enricher later | Field contract only |
| Distillation (summary) | Producer today; Open Brain later | Field contract with origin tag |

## Decisions taken

- **Connector source; no engine change.** Records enter through the optional collector and
  custody on the existing plain public-job capture path. No engine, schema, runtime or Portable
  Brain change; coordinates stay at 23/16/18. The only core-package edit is one entry in the app
  catalog of optional sources.
- **One contract for every kind of saved URL.** `source_type` and `content_kind` reuse the engine's
  `SourceType` and `ContentKind` values. A social post is `source_type: social`,
  `content_kind: post`.
- **The reason is stated, not owner-authored.** The collector's public-job path always records
  `owner_context=AUTOMATION_ABSENT` and carries no `capture_why`, and Portable source validation
  refuses a reason on source captures. So `why` is stored as a `Stated reason` section of the
  capture text, which also makes it searchable. `SourceRecordIntake`, custody and the live capture
  serializers are unchanged. An owner-authored reason for trusted producers would be a separate
  engine spec.
- **Thin records are first-class.** `fetch_status: not_attempted` is valid.
- **The first save of a URL wins.** v1 has no revisions. The first record for an identity in a
  folder is captured. A later record with the same identity and identical admitted content is
  skipped; one with different content is refused locally as `already_captured` and counted in the
  scan report. Revisions arrive with enrichment, together with revision-sink capacity work.
- **Identity comes from the URL, within one folder.** Two selected folders are two sources; the
  same URL in both becomes two captures, each with its own tier.
- **The tier is chosen per selected folder.** The documented default is personal.
- **Only text is captured.** Media files never enter Open Brain.
- **A failed fetch still captures.** A record with `fetch_status: failed` keeps the URL, reason and
  shared text.
- **Select only folders you control.** Anyone who can write into the folder can add saves, and
  the first writer of a URL wins. The integration doc says so.
- **`web_clip` is left alone.** Folding it into `saved_item` is a separate decision.

## Record format, version 1

One UTF-8 JSON object per `*.json` file, at most 131,072 bytes on disk. The filename carries no
meaning, but a producer should write each item once, atomically, to a stable name.

| Field | Type | Rule |
|---|---|---|
| `schema_version` | int | `1` |
| `url` | string | `https` only |
| `why` | string | Required; after trimming, 1 to 280 characters, one line, no control characters |
| `received_at` | string | RFC 3339 UTC time the producer received the save |
| `fetch_status` | string | `not_attempted`, `complete`, `partial` or `failed` |
| `source_type` | string or null | Engine `SourceType` value; derived from the URL host when null |
| `content_kind` | string or null | Engine `ContentKind` value; `other` when null |
| `platform` | string or null | Lowercase site or app name, at most 40 characters |
| `title` | string or null | At most 300 characters |
| `author` | string or null | At most 200 characters |
| `body` | string | Extracted text or caption; may be empty |
| `media_text` | string | Transcript or image description; may be empty |
| `shared_text` | string | Text the capture surface passed along; may be empty |
| `summary` | string or null | Producer summary |
| `summary_origin` | string or null | Required when `summary` is set, `producer:<name>` |

Only `schema_version`, `url`, `why`, `received_at` and `fetch_status` are required. Optional fields
may be absent. Unknown fields are refused, so a later version cannot be silently half-read.

A minimal thin record:

```json
{"schema_version": 1, "url": "https://example.com/post/123",
 "why": "Recipe to try this weekend", "received_at": "2026-10-26T14:00:00Z",
 "fetch_status": "not_attempted"}
```

### Identity

The adapter computes its own identity URL from the raw record URL with `urlsplit`. It applies
the host, default-port and percent-escape rules of `canonicalize_source_url` (`core/ids.py`),
removes tracking parameters, and keeps the remaining query and the fragment.
`item_key` is the first 32 hex characters of the sha256 of the identity URL.

The fragment is only hash input, because single-page apps route content through it
(`https://app.example/#/note/123` and `#/note/456` are different items). It is never stored: the
intake URL is the identity URL without its fragment, which `ReferencePayload` then canonicalizes.
So the stored and searchable URL never carries a sharer's tracking identifier or a fragment.

Removal rules are a versioned constant table in the adapter, matched against the host and its
subdomains, with tests for each row:

| Hosts (and subdomains) | Removed parameters |
|---|---|
| Any | `utm_*`, `fbclid`, `gclid` |
| `instagram.com`, `threads.net`, `threads.com` | `igsh`, `igshid` |
| `youtube.com`, `youtu.be`, `spotify.com` | `si` |

Generic names such as `ref` and `si` are never removed on other hosts. Records dedupe only when
their identity URLs are identical; host aliases (`youtu.be/<id>` and `youtube.com/watch?v=<id>`)
and short links are not expanded in v1. A table change is a new rule version noted in the
integration doc and affects new saves only.

## Components

### Connector adapter

New `packages/connectors/src/open_brain_connectors/runtime/saved_item.py`:

- `SavedItemSourceAdapter.record_from_bytes(bytes) -> SavedItemRecord` validates the format above.
- `SavedItemSourceAdapter.intake(record) -> SourceRecordIntake` maps it:
  - `key`: `connector_name="saved_item"`, `connection_id` and `resource_id` derived from a hash of
    the selected folder's identity (resolved path plus `st_dev`/`st_ino`, following
    `saved_markdown`), `external_id=<item_key>`, `revision_id="1"`. The readable path lives only
    in the selection options.
  - `url`: the identity URL without its fragment.
  - `title`: the record title; else `<author> on <platform>`; else the stored URL. Truncated to
    200 characters, the `SourceRecordIntake` title bound.
  - `text`: sections `Stated reason`, `Summary (<origin>)`, `Body`, `Media text`, `Shared text`,
    `Kind` (`source_type`/`content_kind`/`platform`), `Fetch status`, omitting empty sections.
  - `privacy`: `local_source_privacy()`. The selected tier travels as the `privacy_tier` selection
    option and is applied by `capture_policy.admitted_intake`, as for other live sources.
- Bounds follow the plain capture path. That path prefixes an 83-character `Source revision:` line,
  so the adapter keeps the rendered text at most 60,000 characters and its
  `bounded_json(ReferencePayload(url, text).to_dict())` encoding at most 65,536 bytes. Both are
  measured the way the collector encodes them, which escapes non-ASCII. Past either bound it
  truncates `Media text`, then `Body`, then `Shared text`, each with a visible `[truncated]`
  marker, until the payload fits. A record that still does not fit with those sections empty is
  refused as `payload_too_large`. A save is never refused for long content alone.
- Sensitive data is masked, not refused. `open_brain_engine.capture.redaction.redact_text` runs over
  the title and every text section, including `Stated reason`, before the bounds are measured. The
  engine's check is broad: emails, phone numbers, `token=` strings and any run of 32 or more
  letters, digits, `_` or `-`. Refusing on it would drop many ordinary saves. The URL path is never
  checked or masked, because long ids are normal there. The raw URL's query and fragment, where
  tokens live, are checked with `has_redaction_finding`, and a hit refuses the record as
  `url_secret`.
- Refusals stay local and never become an intake: invalid JSON, unknown or missing field, invalid
  enum or time value, non-https URL, blank `why`, `payload_too_large` and `url_secret`.
- Registration: a `saved_item_source_descriptor()` in `source_registry.py` and an entry in
  `app/services/catalog.py` `OPTIONAL_SOURCE_DESCRIPTORS`, with the catalog pin tests updated.

### Collector folder source

**Runtime**

- The source runs on the live-source runtime that `sources configure/preview/import/control` and
  `run-once` drive: `LiveSourceManager`, then `LiveCaptureService`, then
  `PriorityRuntime.fetch` and `acknowledge`. `agent_session`, the existing local accountless
  source, is the template.
- Edit points:
  - the `PriorityRuntime.fetch` and `acknowledge` dispatch in `live_manager.py`
  - the `_PROVIDERS` table in `live_capture.py`
  - the descriptor in `source_registry.py`
  - the `SourceCatalog` tuple in the connectors `source_cli.py`
- Selecting a folder derives the selection from the path: `connection_id` and `resource_id` are
  hashes of the resolved path plus `st_dev` and `st_ino`. `configure` receives them already
  computed. The resolved path is stored in the selection options and copied into the checkpoint,
  because `acknowledge` receives the checkpoint but not the options, as with `agent_session`.

**Reading the folder**

- The folder is opened once with `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`. Owner, mode and type checks
  run with `fstat` on that descriptor: owned by the current user, not writable by group or others.
- Entries are listed with `os.scandir` on the descriptor. Each `*.json` entry is opened relative
  to it (`dir_fd`) with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK` and must be a regular file by `fstat`.
  Symlinks, pipes, sockets, devices and directories named `*.json` are refused without blocking.
- Names starting with `.` or ending in `.icloud`, `.tmp` or `.partial` are skipped. On macOS, an
  entry whose `st_flags` include `SF_DATALESS` is a cloud placeholder and is skipped without being
  read, so a scan never forces a download. Any skipped entry is retried on a later scan.
- A file is read through its descriptor up to 131,072 bytes plus one. It is accepted only if
  device, inode, size and `mtime_ns` match before and after the read.

**The known-file index**

- The index lives in its own `PrivateJsonStore` file, not in the checkpoint. It is keyed by
  sha256 of the entry name, truncated to 32 hex characters. Each value is a stat fingerprint
  (device, inode, size, `mtime_ns`) plus a terminal state: `delivered`, `skipped` or a refusal
  code.
- A second map in the same file records delivered `item_key`s, for first-save-wins.
- Entries are fixed-size. With at most 10,000 record files and 10,000 delivered items, the
  measured worst case must stay under half of the 4 MiB bound, and a test asserts it. A folder
  with more than 10,000 `*.json` entries is refused as `folder_over_limit`, with nothing
  delivered.
- An entry whose fingerprint matches the index is skipped without being opened. Entries whose
  files disappear are pruned on each complete scan.
- An entry gets a terminal state only after its outcome is final: a terminal custody receipt
  (captured or duplicate), a local refusal, or a first-save-wins skip. The index write follows the
  checkpoint commit. A crash between delivery and the index write rescans the file, and custody
  dedupes the identical delivery.
- If the index is lost, every file is rescanned. Identical deliveries dedupe in custody. A
  different record for an already-captured item then becomes a custody conflict, handled by the
  existing collector recovery.

**Delivering**

- One run is one `LiveBatch`, which holds at most 25 intakes, with a page budget of 1 MiB of
  encoded intakes. The adapter caps each intake at 64 KiB, so a page always fits under the
  2,097,152-byte custody quota. Entries past either limit set `has_more`, and the runtime
  reschedules the next page after one second. Scan progress lives in the index store, not in the
  131,072-byte checkpoint.
- Within a page, at most one intake is emitted per `item_key`. The earliest `received_at` wins,
  then the lowest entry-name hash. Losers are handled by first-save-wins on the next page.
- The source never deletes or moves files in the folder. The producer side owns retention.

**Reporting**

- A scan reports counts per outcome and refusal code. A single entry is identified only by a
  12-character prefix of its name hash, never by its raw name, because other writers control
  names.

**CLI**

- The collector CLI gains one step that turns a folder path into a selection with a tier. After
  that the existing `sources configure`, `control` and `run-once` commands drive the source. The
  exact command is fixed in the implementation plan. `saved_item` is not added to any revision
  allow-list.

## Error handling

| Case | Behavior |
|---|---|
| Malformed or refused record | Refused locally with a named code; not retried until the file changes |
| Long content | Truncated with a visible marker; refused only if the reason and URL alone do not fit |
| Placeholder, dataless or partial file | Skipped without reading; retried on a later scan |
| Same identity, identical content | Skipped |
| Same identity, different content | Refused locally as `already_captured`; counted in the report |
| Same identity twice in one page | One intake; the earliest `received_at` wins |
| Entry is a symlink, pipe, socket, device or directory | Refused by code; the scan continues |
| More than 10,000 record files | Scan refuses as `folder_over_limit`; nothing is delivered |
| More new items than one page holds | Up to 25 per page within 1 MiB; `has_more` continues |
| Sensitive data in text | Masked with `redact_text`; the save lands |
| Sensitive data in the URL query or fragment | Refused as `url_secret` |
| Folder missing, wrong owner or mode, or a symlink | Scan refuses; nothing is delivered |
| Index lost | Full rescan; custody dedupes; a changed item becomes a recoverable conflict |
| Custody or engine failure | Existing collector recovery (`docs/integrations/collector-recovery.md`) |

## Testing

- Adapter contract tests:
  - Field mapping for thin and rich records.
  - Every refusal code.
  - Each row of the tracking table, with subdomains, plus preservation of the fragment and of
    generic `ref`/`si`.
  - Two URLs that differ only by fragment get different keys, and neither stored URL contains a
    fragment.
  - A `why` that is empty, spaces-only, tab-only or contains a newline.
  - Title fallbacks and text layout, including `Stated reason`.
  - Bounds: a 30,000-character non-Latin record is truncated to fit 65,536 encoded bytes, a
    title over 200 characters is truncated, and a record whose reason and URL alone do not fit is
    refused.
  - An email in the body and a token in the stated reason are masked and the record is admitted;
    a 44-character id in the URL path is admitted; `token=` in the query or fragment is refused as
    `url_secret`.
- Collector integration tests:
  - Folder to scan, custody and a temporary Brain, after which search finds the item by body text
    and by stated reason.
  - First-save-wins: identical content skipped, different content refused as `already_captured`,
    same URL twice in one page yields one intake.
  - Index ordering: the delivery fails, and the file is redelivered on the next scan.
  - A named pipe and an entry symlink named `*.json` are refused without blocking.
  - Mocked `SF_DATALESS` and `.icloud` entries are skipped without being opened.
  - 501 unchanged files reach a second scan with no file opened and no delivery.
  - 10,001 files are refused as `folder_over_limit`.
  - 30 new files are delivered across two pages, the first with 25 and `has_more`.
  - 20 large non-Latin records split across pages under the 1 MiB page budget.
  - The index with 10,000 maximum-size entries stays under half of the 4 MiB bound.
  - A refusal report for an entry whose name contains a control character or newline shows only
    the name-hash prefix.
- Release doc test for the new integration page, matching the Drive Docs pattern.
- Gates: `make verify`, `git diff --check`, `actionlint .github/workflows/ci.yml`. No packaging
  change is expected, so the native audit runs only if one appears.

## Documentation

- New `docs/integrations/saved-items.md`:
  - The record format.
  - The identity rule and its parameter table.
  - First-save-wins.
  - Selecting a folder you control.
  - That the reason is stored as stated text.
  - An example iOS Shortcut that asks "Why are you saving this?" and writes a thin record into a
    cloud-synced folder.
- `CLAUDE.md` gains one line naming the source.

## Out of scope

- An owner-authored `capture_why` for trusted producers (needs an engine public-job change).
- Revisions and revision-sink capacity.
- Open Brain-owned summarization.
- Fetching or enriching content. Producers own network access and platform logins.
- Polling a platform's own saved list, browser extensions and read-later imports.
- Host alias and short-link expansion.
- Folding `web_clip` into `saved_item`.
- Publishing records as managed-vault pages.
