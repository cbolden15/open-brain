# Saved items

> Optional collector source. It is not part of the core install; it needs the
> `open-brain-collector` distribution and runs only after you configure it.

A saved item is a URL you chose to keep and the reason you kept it: a social post, an article, a
video, an event page, a product or a place. A producer, such as an iOS Shortcut, a bookmarklet or a
script, writes one small JSON file per item into a folder you select. The collector reads the folder
and captures each new item as a searchable record whose text includes your reason.

The collector never opens the URL. Text the producer already extracted, such as a caption, a
transcript or a summary, is captured as text. Media files never enter Open Brain.

## Record format

Each item is one UTF-8 JSON object in its own `*.json` file of at most 131,072 bytes. The file name
carries no meaning. Write each item once, atomically, under a name that does not change.

| Field | Type | Rule |
|---|---|---|
| `schema_version` | int | `1` |
| `url` | string | `https` only |
| `why` | string | Required; after trimming, 1 to 280 characters, one line, no control characters |
| `received_at` | string | RFC 3339 UTC time the producer received the save, ending in `Z` |
| `fetch_status` | string | `not_attempted`, `complete`, `partial` or `failed` |
| `source_type` | string or null | `youtube`, `social`, `web` or `text`; derived from the URL host when null |
| `content_kind` | string or null | `event`, `article`, `product`, `place`, `post`, `video` or `other`; `other` when null |
| `platform` | string or null | Lowercase site or app name, at most 40 characters |
| `title` | string or null | At most 300 characters |
| `author` | string or null | At most 200 characters |
| `body` | string | Extracted text or caption; may be empty |
| `media_text` | string | Transcript or image description; may be empty |
| `shared_text` | string | Text the capture surface passed along; may be empty |
| `summary` | string or null | Producer summary |
| `summary_origin` | string or null | Required when `summary` is set, `producer:<name>` |

Only `schema_version`, `url`, `why`, `received_at` and `fetch_status` are required; optional fields
may be left out. Unknown fields are refused, so a later format version is never half-read. A
minimal record:

```json
{"schema_version": 1, "url": "https://example.com/post/123", "why": "Recipe to try this weekend", "received_at": "2026-10-26T14:00:00Z", "fetch_status": "not_attempted"}
```

A record whose fetch failed, or was never attempted, is still captured with its URL, reason and
shared text.

## What a capture contains

The capture text is a list of sections in this order, and empty sections are left out:
`Stated reason`, `Summary (<origin>)`, `Body`, `Media text`, `Shared text`, `Kind` and
`Fetch status`. The title is the record title. Without one it is `<author> on <platform>`, and
without those it is the stored URL. Titles are cut to 200 characters.

Before anything is stored, the title and every section are masked with the capture redaction
policy: email addresses, phone numbers, `token=` values and any run of 32 or more letters, digits,
`_` or `-` are replaced. The save still lands.

Long records are cut to fit the collector's plain capture path: at most 60,000 characters of text
and 65,536 bytes of encoded payload, where one non-Latin character can cost six bytes. `Media text`
is cut first, then `Body`, then `Shared text`, each ending in a visible `[truncated]` marker. A
record is refused as `payload_too_large` only if it still does not fit after that.

## The stated reason

Your reason is stored as the `Stated reason` section of the capture text, so search finds it. It
is not an owner-authored capture reason. Every collector delivery goes through the public-job
capture path, which cannot claim that the owner wrote the text.

## Identity and tracking parameters

An item's identity comes from its URL, within one selected folder. The collector lowercases the
host, drops the default port, uppercases percent escapes and removes tracking parameters. The rest
of the query and the fragment stay in the identity, because some apps put the item id in the
fragment: `https://app.example/#/note/123` and `https://app.example/#/note/456` are different
items. The fragment is never stored. The stored and searchable URL is the identity without it.

Tracking rule version 1 removes these parameters:

| Hosts (and subdomains) | Removed parameters |
|---|---|
| Any | `utm_*`, `fbclid`, `gclid` |
| `instagram.com`, `threads.net`, `threads.com` | `igsh`, `igshid` |
| `youtube.com`, `youtu.be`, `spotify.com` | `si` |

Generic names such as `ref` and `si` are kept on every other host. Host aliases and short links are
not expanded, so `youtu.be/<id>` and `youtube.com/watch?v=<id>` are two items. A change to this
table is a new rule version and affects new saves only.

If the query that remains after removal, or the fragment, looks like a secret (a `token=` value, an
email address, or a run of 32 or more letters, digits, `_` or `-`), the record is refused as
`url_secret`. The URL path is never checked, because long ids are normal there.

## First save wins

The first record for an identity in a folder is captured. A later record with the same identity
and the same admitted text is skipped. One with different text is refused as `already_captured`
and counted in the scan report; it does not replace the first capture. When two records for the
same URL arrive in one scan, the earlier `received_at` wins. Revisions of a saved item are not
supported yet.

## Select a folder you control

Anyone who can write into the folder can add saves, and the first writer of a URL wins. Select a
folder that only you can write to. The collector refuses a folder that is a symlink, is not owned
by you, or is writable by group or others.

A cloud-synced folder works. Names starting with `.` or ending in `.icloud`, `.tmp` or `.partial`
are skipped, and on macOS a file that is still a cloud placeholder is skipped without being read,
so a scan never forces a download. Skipped files are retried on a later scan.

The collector never deletes or moves files; clean the folder up on the producer side. Two selected
folders are two sources: the same URL in both becomes two captures, each with its own tier.

## Set up the source

Turn a folder into a selection. The tier defaults to `personal`; pass `--privacy-tier` with
`public`, `work`, `personal` or `secret` to choose another.

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground saved-item-select --folder-path /absolute/path/to/saved-items \
  --privacy-tier personal > saved-items-selection.json
```

The output is the arguments file for `configure`. From there the usual live-source commands drive
the source:

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground configure --arguments-file "$PWD/saved-items-selection.json"
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground preview --source-id REPLACE_FROM_CONFIGURE
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground import --source-id REPLACE_FROM_CONFIGURE --preview-id REPLACE_FROM_PREVIEW
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground control --source-id REPLACE_FROM_CONFIGURE --action enable --interval-seconds 900
```

One preview holds at most 25 items and at most 1 MiB of encoded text. When more items wait, the
preview reports `has_more`; repeat preview and import, or let a scheduled run continue one second
later. `saved_item` uses the plain capture path and is not on any revision allow-list. See
[priority-capture-operator.md](priority-capture-operator.md) for state paths and
[collector-recovery.md](collector-recovery.md) for custody failures.

## Refusals and reports

| Code | Meaning |
|---|---|
| `invalid_json` | Not UTF-8 JSON, not an object, a duplicate key or a non-finite number |
| `unknown_field` | A field outside the format |
| `missing_field` | A required field is absent |
| `invalid_field` | Wrong type, length, enum value, time format or summary origin |
| `invalid_url` | Not an `https` URL, or not a valid one |
| `invalid_why` | Blank after trimming, over 280 characters, or more than one line |
| `url_secret` | The remaining query or the fragment looks like a secret |
| `payload_too_large` | Still too large after truncation |
| `file_too_large` | The file is over 131,072 bytes |
| `not_regular_file` | A symlink, pipe, socket, device or directory named `*.json` |
| `already_captured` | Same identity as a captured item, different text |

A refused file is not retried until it changes. Three scan-level refusals deliver nothing:
`folder_missing`, `folder_unsafe` (a symlink, another owner, or a group- or world-writable mode)
and `folder_over_limit` (more than 10,000 `*.json` entries).

A scan reports counts per outcome as `saved_item_count:<outcome>:<n>` notices and names a refused
file only as `saved_item_refused:<code>:<prefix>`, where the prefix is the first 12 characters of
the SHA-256 of its name. File names are never reported, because other writers control them.

The collector keeps a private index of the files it has finished with, so an unchanged file is not
opened again. If that index is lost, every file is read again; identical items deduplicate in
custody, and a changed item becomes a conflict that the usual collector recovery handles.

## Example iOS Shortcut

This Shortcut saves a thin record from the Share Sheet into a folder in iCloud Drive. It uses only
standard Shortcuts actions.

1. Create a Shortcut that shows in the Share Sheet and receives URLs.
2. Add Ask for Input with the prompt "Why are you saving this?".
3. Add Adjust Date to shift Current Date to UTC (subtract your offset from UTC), then Format Date
   with the custom format `yyyy-MM-dd'T'HH:mm:ss'Z'`.
4. Add a Dictionary with `schema_version` (Number 1), `url` (Shortcut Input), `why` (Provided
   Input), `received_at` (Formatted Date) and `fetch_status` (Text `not_attempted`).
5. Add Save File with the Dictionary, Ask Where to Save turned off, and a subpath of
   `saved-items/` followed by the date formatted as `yyyyMMdd-HHmmss` and `.json`.

On the Mac that runs the collector, select the `saved-items` folder inside the Shortcuts folder of
iCloud Drive. A wrong UTC offset only changes which of two saves of the same URL in one scan wins;
it never drops a save.
