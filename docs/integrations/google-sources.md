# Gmail and Google Drive sources

> Core v0.1 scope: this is optional **source-only implementation and historical evidence**, not
> a supported public onboarding path. It is not installed by the core artifact. Connected-source
> continuity, production provider readiness and real service setup remain deferred or separately
> gated. Historical acceptance below does not certify the current candidate. Use
> [core sources](core-sources.md) for the supported local capture/Markdown path.

Gmail and Google Drive are separate connections. They request different read scopes and
Open Brain stores their refresh material only through the configured operating-system
credential store. A resource selection limits what Open Brain imports. It does not narrow
the read grant shown by Google during consent.

## Owner setup

1. Create a separate development Google Cloud project and an OAuth client of type
   **Desktop app**. Your personal Google account can administer the project.
2. Enable the Gmail API for Gmail capture and the Google Drive API for Drive capture.
3. Configure the consent screen for testing and add the Google account used for acceptance
   as a test user. A development client is enough for these sign-in checks.
4. Download the client configuration JSON to an owner-controlled local path.
5. Connect one provider at a time using that path. The path is read locally; client secrets,
   authorization codes, and tokens are never accepted in CLI arguments or written to source
   configuration.

The OAuth client identifies the application, not the user's mailbox. Each person signs in
with their own Google account and receives separate local authorization tokens. A public
release should use a separate production Open Brain project/client and consent screen,
with Google's required verification for the requested scopes. Do not reuse an unrelated
personal integration's identity as the public Open Brain app. Development consent and tokens
are not evidence that production onboarding or long-running authorization has been approved.

The native connection flow opens the system browser and receives the result at a temporary
`http://127.0.0.1:<port>/oauth2/callback` listener. It uses an authorization code flow with
PKCE S256 and validates the callback state. Google desktop clients cannot keep a confidential
client secret, so a downloaded configuration is developer setup rather than proof of public
onboarding.

When the downloaded Desktop configuration includes `client_secret`, Open Brain sends it in
the token exchange and refresh request bodies. Google can require this field even with PKCE.
It is retained with the refresh material in the OS credential store, never in authorization
URLs, account metadata or logs. Refresh therefore does not require rereading the downloaded file.

## Gmail

Gmail requests exactly:

```
openid https://www.googleapis.com/auth/gmail.readonly
```

Choose one Gmail label from resource discovery, then provide a UTC calendar-date floor such
as `2026-01-01`. Fetch reads at most five messages at a time. It only accepts the selected label
and date floor; it does not fetch attachments or call any write endpoint.

Message bodies come from bounded `text/plain` MIME parts. If no plain-text part is available,
the result carries a stable notice instead of treating HTML or an attachment as capture text.
The source link is the Gmail message URL and the source identity is stable across edits.

A continuation checkpoint may contain Gmail history and page state, but fetching only returns
the proposed checkpoint. The collector advances it after acknowledged capture. Gmail history
includes label changes, so a message that is removed from the selected label is reported as a
removal rather than silently remaining active. If Gmail rejects an expired history ID with HTTP
404, Open Brain returns a bounded full-resync proposal. Recovery retains prior selected IDs,
tracks seen IDs through every successful page, and emits unavailable records only after the
terminal snapshot succeeds. A busy history record drains through proposed five-message pending
pages rather than being rejected. It never persists that recovery state from provider fetch.

## Google Drive

Drive requests exactly:

```
openid https://www.googleapis.com/auth/drive.readonly
```

Resource discovery lists files the account can read from My Drive and shared drives, or folders
when the collector's `resources --resource-type drive_folder` is used. A Drive selection has one
of three shapes: one file (`drive_file`), one folder of Google Docs (`drive_folder`), or Google
Docs across the account chosen by title suffix (`drive_account`). The single-file shape supports
UTF-8 `text/*` blobs and Google Workspace Docs, Sheets, and Slides exported as plain text or CSV
where applicable; the folder and account shapes capture Google Docs only. No shape narrows the
`drive.readonly` grant. A shape narrows what Open Brain imports.

Unsupported binary formats, unavailable exports, missing download permission, and incomplete
pagination return stable notices or error codes. They are visible to the caller and are never
reported as a successful full capture. Captured records retain the Drive `webViewLink`; modified
time, version, checksum, and trashed/access state participate in their revision identity. A
missing, trashed, or inaccessible selected file produces a removal/access-status proposal so the
collector can update the active result while preserving already captured history.

An edited `drive_file` record is quarantined, not re-imported. The single-file shape submits each
record under a stable delivery id with a `Source revision:` text prefix and no revision
observation, so a changed body reaches the engine as a revision conflict; the collector records
the receipt as `quarantined` with `source_revision_conflict` and the first capture stays
searchable. Only the folder and account shapes described below go through the revision-aware
path. The 2026-09-16 live body-edit check ran on an older schema and does not describe the current
engine (gotcha DOCS-005).

Expired or revoked account credentials return an authentication error. Quota and rate responses
return a retryable rate-limit error. Only documented selected-file permission failures produce an
access-loss record; unknown permission errors do not advance the checkpoint.

Drive downloads use a bounded response size. Google Workspace exports are also bounded below
Google's 10 MB export limit. Open Brain does not fetch attachments, images, PDFs, videos, or
other binary content as text.

## Drive Docs sets

The folder and account shapes capture the Google Docs that Gemini writes after a Google Meet call,
the notes Doc and the transcript Doc, and any other folder of Docs. Both shapes list Docs, export
them as plain text, split long Docs into parts and store an edited Doc as a new revision of the
same record.

### Shapes and options

| Shape | `resource_type` | `resource_id` | Options |
|---|---|---|---|
| Folder | `drive_folder` | `drive_folder:<folderId>` | `date_floor` required, `title_suffixes` optional |
| Account | `drive_account` | `drive_account:all` | `date_floor` required, `title_suffixes` required |

- `date_floor` is a UTC calendar date, `YYYY-MM-DD`, in the same form Gmail takes. It bounds the
  first run: nothing modified before it is listed. It has no effect once the checkpoint is
  incremental.
- `title_suffixes` is a JSON list of 1 to 8 strings, each 1 to 64 characters after stripping, with
  no duplicates and no NUL. A Doc is selected when its name ends with one of them. The Meet values
  are `["Notes by Gemini", "Transcript"]`.
- Any other option key, a missing required option or a malformed value is `drive_invalid_options`.
  A resource id that does not match its type is `drive_invalid_selection`.
- `drive_account:all` is a fixed id and is not listed by discovery. Folder ids come from
  `resources --resource-type drive_folder`.

### Listing query

Each page is one `files.list` call:

```
mimeType = 'application/vnd.google-apps.document'
and trashed = false
and modifiedTime > '<bound>'
[and '<folderId>' in parents]
[and (name contains '<s1>' or name contains '<s2>' ...)]
```

The folder clause applies to the folder shape. The name clause applies when `title_suffixes` is
set; it is a server-side prefilter, and the exact suffix check runs on the returned name. Suffix
values are escaped for the Drive query grammar (backslash and single quote). The request uses
`orderBy=modifiedTime`, `pageSize=100`, `includeItemsFromAllDrives=true` and
`supportsAllDrives=true`. Recordings, PDFs and other types never match the MIME clause, so a Docs
set raises no `drive_unsupported_type` notices.

### Checkpoint

The checkpoint is metadata only and is validated on every fetch:

```json
{"mode": "backfill",
 "page_token": null,
 "watermark": "2026-10-01T00:00:00Z",
 "known": {"<fileId>": {"revision": "<revision>", "modified": "<RFC3339 modifiedTime>"}}}
```

- The first run is `backfill` with `watermark` set to `<date_floor>T00:00:00Z`, and the query bound
  is the watermark for the whole listing, because a Drive page token is bound to its query.
  `page_token` advances when a page is fully drained. When the last page drains without a
  `nextPageToken`, the watermark moves to the newest `modified` time recorded in `known` and `mode`
  becomes `incremental`.
- An incremental run queries from `watermark` minus 24 hours. The overlap covers late indexing and
  clock skew. Files in `known` with an unchanged revision are skipped without a fetch.
- `known` holds at most 500 entries. When it is full, the entries with the oldest `modified`
  value are dropped. A dropped entry risks one re-submit, which the sink answers as `duplicate`.
- Any other shape, an unknown key or more than 500 entries is `drive_invalid_checkpoint`. A
  `"reset": true` on configure clears it.

### Fetch and per-file notices

One fetch lists one page, then, for each file whose revision is new or changed, reads the
metadata, exports the body and splits it. A batch holds at most 25 intakes and 25 notices. Files
not reached stay on the same page for the next call, and `has_more` is true while files on the
page remain or a `nextPageToken` is present.

A single file never stalls a set. A file that returns 404, a lost-access 403, `trashed`,
`canDownload != true`, a non-UTF-8 body, a body of more than 18 parts or an empty body produces the notice
`drive_file_skipped:<reason>:<fileId>` with `reason` in
`missing|access_lost|trashed|not_downloadable|not_text|too_large|empty`; the file is recorded in
`known` and the run continues. Account-level errors still raise and leave the checkpoint
unchanged: 401 is `google_auth_required`, a rate-limit 403 is `google_rate_limited`, and any other
403 on the list call is `drive_forbidden`. When the folder itself returns 404 or lost access, the
fetch returns no intakes with the notice `drive_folder_missing` and the checkpoint unchanged; no
placeholder record is written.

One exception to the per-file rule: an export body over 1 MiB is refused by the transport with
`source_transport_unavailable` before the connector sees it. That error raises like an
account-level error, so the checkpoint stays unchanged and the set retries the same Doc on every
run until the Doc shrinks or the selection changes.

### Splitting

An exported text over 60,000 characters is split into parts of at most 60,000 characters. The cut
is the last blank line within the limit, else the last newline, else a hard cut; leading newlines
of the next part are stripped. At most 18 parts are produced. A Doc that would need a 19th is
skipped with `drive_file_skipped:too_large:<fileId>`. Each part is one record. Its `external_id`
is `file:<fileId>#part:<n>` (1-based). Its title is `<name> (part <n> of <N>)`, cut to
200 characters with the suffix preserved. Its link is the Doc link, and every part carries the
same `revision:<drive revision>`. A Doc under the limit is one record with `external_id`
`file:<fileId>`, as for the single-file shape.

### Revision behavior

Folder and account records go through the collector's revision-aware path, as Slack records do.
The collector attests each intake and stores it under `drive-revisions` in its state root. When a
Doc is edited, its revision hash changes, the file is re-exported and re-split, and each part is
submitted as a new revision of the same source record: the earlier text stays in history and the
new text is searchable. Follow-up evidence quoted from an earlier revision stays bound to that
revision. When a later revision has fewer parts, the surplus parts keep their last revision; this
is documented, not repaired.

## Live acceptance and limits

Bounded live acceptance passed on 2026-09-16 using a development OAuth client and a disposable
Brain. The final edge checks ran at `9f22849`: Gmail selected-label removal/restoration, Drive
body edit/trash/restoration, replacement of active search results, and a verified export retaining
all seven captured transitions. Revoking the Google project grant made manual, fresh-process and
scheduled reads fail with `google_auth_required`, without advancing checkpoints or adding captures.
Both owner connections were subsequently restored with the same read-only scopes. Fresh-process
resource reads and real refresh requests passed; disposable Keychain entries were removed.

Earlier live checks covered bounded import, retry/replay and the actual background collector's
pause/restart/resume lifecycle. The [integration audit](../audits/2026-09-16-priority-capture-audit.md)
maps each check to its candidate and private receipt. Per-file Drive permission loss has automated
coverage; the live unavailable-state checks used trash/restoration and account-grant revocation.
Public OAuth verification and long-duration authorization remain separate release checks.

Google project-grant revocation affects other connections using that project, including another
local Brain holding a copy of the grant. Removing one Open Brain connection only clears its local
credential reference. Plan equivalent reconnection before testing upstream revocation.

### Live acceptance for Drive Docs sets

The Docs-set live gate is owner-authorized and separate from the automated suite. It needs a
development OAuth client, a consenting test account and a disposable Brain, and its receipts stay
outside Git. The gate passes when three checks are recorded here: one real Gemini note captured
through `drive_account:all` with the suffixes above, one real transcript over 60,000 characters
captured as parts, and one edit to the note stored as a new revision with the first text still in
history. The same run answers the open question on Gemini's title format: if real titles differ
from `Notes by Gemini` and `Transcript`, only the `title_suffixes` values change. Until that run is
recorded, the Docs-set shapes have synthetic acceptance only, through the collector integration
tests.

## References

- [Google OAuth 2.0 for installed applications](https://developers.google.com/identity/protocols/oauth2/native-app): system-browser loopback redirects, state, and PKCE.
- [Google token revocation](https://developers.google.com/identity/protocols/oauth2/web-server#tokenrevoke): revocation removes the project's granted scopes and invalidates its affected tokens.
- [Gmail incremental synchronization](https://developers.google.com/workspace/gmail/api/guides/sync): `history.list` uses `startHistoryId`; an unavailable history range returns HTTP 404 and requires a full sync.
- [Gmail `users.messages.get`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get): `gmail.readonly` authorizes message reads.
- [Drive download and export](https://developers.google.com/workspace/drive/api/guides/manage-downloads): blobs use `files.get?alt=media`; Workspace documents use `files.export`; exports are limited to 10 MB.
- [Drive shared-drive support](https://developers.google.com/workspace/drive/api/guides/enable-shareddrives): discovery uses `includeItemsFromAllDrives=true`; file operations use `supportsAllDrives=true`.
