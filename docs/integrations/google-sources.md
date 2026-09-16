# Gmail and Google Drive sources

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

Resource discovery lists files the account can read from My Drive and shared drives. Select
exactly one file. Fetch gets that file only; it does not recursively mirror folders. The source supports UTF-8 `text/*` blobs and
Google Workspace Docs, Sheets, and Slides exported as plain text or CSV where applicable.

Unsupported binary formats, unavailable exports, missing download permission, and incomplete
pagination return stable notices or error codes. They are visible to the caller and are never
reported as a successful full capture. Captured records retain the Drive `webViewLink`; modified
time, version, checksum, and trashed/access state participate in their revision identity. A
missing, trashed, or inaccessible selected file produces a removal/access-status proposal so the
collector can update the active result while preserving already captured history.

Expired or revoked account credentials return an authentication error. Quota and rate responses
return a retryable rate-limit error. Only documented selected-file permission failures produce an
access-loss record; unknown permission errors do not advance the checkpoint.

Drive downloads use a bounded response size. Google Workspace exports are also bounded below
Google's 10 MB export limit. Open Brain does not fetch attachments, images, PDFs, videos, or
other binary content as text.

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

## References

- [Google OAuth 2.0 for installed applications](https://developers.google.com/identity/protocols/oauth2/native-app): system-browser loopback redirects, state, and PKCE.
- [Google token revocation](https://developers.google.com/identity/protocols/oauth2/web-server#tokenrevoke): revocation removes the project's granted scopes and invalidates its affected tokens.
- [Gmail incremental synchronization](https://developers.google.com/workspace/gmail/api/guides/sync): `history.list` uses `startHistoryId`; an unavailable history range returns HTTP 404 and requires a full sync.
- [Gmail `users.messages.get`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/get): `gmail.readonly` authorizes message reads.
- [Drive download and export](https://developers.google.com/workspace/drive/api/guides/manage-downloads): blobs use `files.get?alt=media`; Workspace documents use `files.export`; exports are limited to 10 MB.
- [Drive shared-drive support](https://developers.google.com/workspace/drive/api/guides/enable-shareddrives): discovery uses `includeItemsFromAllDrives=true`; file operations use `supportsAllDrives=true`.
