# Google Calendar capture

The optional connector can sign in to Google, list calendars, preview a selected date
range, and import that preview into an existing Brain. It runs in the foreground.
It does not schedule collection or add a listener to the core runtime. The desktop
configuration screen remains separate work.

Install the optional connector package, or use `uv run --frozen open-brain-source` from
a contributor checkout. The commands below assume `open-brain-source` is on your PATH.
Initialize your Brain with the normal Open Brain command before importing.

## Connect an account

This contributor flow needs a Google Cloud project with the Calendar API enabled and
an OAuth **Desktop app** client. Download its JSON configuration to an absolute local
path. Configure the consent screen and permitted test users as required by Google.
This is not evidence of a publicly verified, one-click desktop sign-in release.

```sh
open-brain-source google-calendar connect \
  --client-config "$HOME/Downloads/google-desktop-client.json" \
  --credential-dir "$HOME/Library/Application Support/open-brain-calendar/credentials"
```

The command opens your system browser and waits up to 180 seconds. Its temporary
callback listener binds only to `127.0.0.1` on an ephemeral port and closes when the
flow ends. Authorization uses PKCE and verifies the callback state. Account identity
comes from Google's authenticated OpenID userinfo endpoint.

The requested scopes are `openid` and
`https://www.googleapis.com/auth/calendar.readonly`. Calendar consent does not grant
email or transcript access. Google grants the account's calendar scope; Open Brain's
explicit calendar and range selection limits what this connector captures.

Credential files are local plaintext protected by owner-only directory and file modes
(`0700` and `0600`). They contain access and refresh credentials. Keep this directory
outside your Brain, source control, and synced folders. The connector never discovers
ambient Google credentials. Sign-in receipts contain only an opaque account ID,
expiry, and granted scopes.

```sh
open-brain-source google-calendar accounts \
  --credential-dir "$HOME/Library/Application Support/open-brain-calendar/credentials"

open-brain-source google-calendar calendars \
  --credential-dir "$HOME/Library/Application Support/open-brain-calendar/credentials" \
  --connection-id 'account:REPLACE_WITH_RETURNED_ID'
```

The calendar chooser returns calendar IDs, titles, timezones, and access roles. Use
the actual returned calendar ID and timezone. The mutable alias `primary` is rejected.

## Preview and import

Set these shell variables to your selected account and calendar. The example Brain
path is the macOS default. Every directory argument must be an absolute path.

```sh
calendar_account='account:REPLACE_WITH_RETURNED_ID'
calendar_id='REPLACE_WITH_CALENDAR_ID'
calendar_zone='America/Chicago'
calendar_brain="$HOME/Library/Application Support/open-brain/brain"
calendar_state="$HOME/Library/Application Support/open-brain-calendar/sync"
calendar_credentials="$HOME/Library/Application Support/open-brain-calendar/credentials"

open-brain-source google-calendar preview \
  --connection-id "$calendar_account" --calendar-id "$calendar_id" \
  --range-start '2026-09-15T00:00:00-05:00' --range-end '2026-09-22T00:00:00-05:00' \
  --timezone "$calendar_zone" --brain-root "$calendar_brain" \
  --state-dir "$calendar_state" --credential-dir "$calendar_credentials"
```

Choose your own dates. Both timestamps need UTC offsets; the range may span at most
366 days. Preview returns event titles, stable delivery IDs, source references, and a
`preview_id`. Event descriptions, access tokens, and sync cursors are not printed.
The exact pending capture bodies are saved privately in the sync directory.

Review that selection before importing. Copy the returned preview ID:

```sh
open-brain-source google-calendar import \
  --connection-id "$calendar_account" --calendar-id "$calendar_id" \
  --range-start '2026-09-15T00:00:00-05:00' --range-end '2026-09-22T00:00:00-05:00' \
  --timezone "$calendar_zone" --brain-root "$calendar_brain" \
  --state-dir "$calendar_state" --preview-id 'REPLACE_WITH_PREVIEW_ID'
```

Import reads that saved preview without contacting Google. It rejects a different
Brain or a stale preview. A failed capture leaves the checkpoint unchanged; retry
the same import before preparing another preview. A later preview replaces the
pending batch for that selection.

The last completed import also has a durable receipt. If the command is interrupted
after committing the checkpoint, retrying its preview ID acknowledges that completion
without another fetch or capture.

Run `status` with the same selection, Brain, and state arguments to inspect the local
checkpoint. It needs neither credentials nor network access. To pause this foreground
flow, stop invoking preview/import. Revoking the app in your Google account prevents
future provider reads; it does not erase already imported captures or a saved preview.

## Captured content and limits

Events include title, description, start/end, timezone, attendee display names, and
the video meeting link when supplied. Email-only attendees are represented as an
unnamed attendee count. The connector keeps the original Google event link when
available and a generated opaque source reference otherwise. Every record is
third-party, personal, and local-only.

Google expands recurring events into occurrences, including exceptions. All-day dates
use the selected calendar's timezone, including daylight-saving changes. Known
cancellations, events moved outside the range, and events missing from a complete
resync replace the searchable copy with a status-only record. Original capture history
remains in Portable Brain exports. Revisions keep the first accepted source link so
the active record can be replaced reliably.

Each preview is bounded to 20 pages and 500 provider events. Event descriptions are
limited to 60,000 characters; at most 50 attendee entries are accepted. The serialized
private batch is limited to 4 MiB. An API response
that says attendees were omitted fails rather than silently presenting a partial
capture. Malformed data, incomplete pagination, redaction findings, and exhausted
limits leave the previous checkpoint intact. Narrow the selected calendar/date range
when a full snapshot exceeds the limits.

Provider HTTP requests run in a short-lived child with a hard 15-second deadline and
bounded response size. The parent terminates and reaps a stalled child. Authorization
requests share the remaining sign-in deadline. Credentials travel through the child's
private input pipe, never command-line arguments.

Incremental requests use Google's saved sync token and apply the selected date range
locally because Google disallows time-range filters with a sync token. An expired token
triggers one bounded full resync. No Brain data is deleted during recovery. A changed
calendar timezone is not silently reinterpreted. This version binds one date range
and timezone per account/calendar/Brain in the selected state directory. A later
change returns `google_calendar_selection_changed`; an explicit selection-migration
command is not implemented yet. Choose a suitably bounded range before the first
preview, and do not run competing state directories for the same calendar and Brain.

## Owner acceptance still required

Synthetic tests do not establish real-account support. Before marking this adapter
accepted, use a designated test calendar and Brain to verify:

1. Sign in with the chosen Desktop OAuth client and confirm the read-only grant.
2. Select a small range containing an all-day event and a recurring-event exception.
3. Preview, import, and retrieve the event through a fresh agent session.
4. Edit and cancel events, then repeat preview/import and confirm no active duplicates.
5. Revoke access, confirm a closed authentication/access error, and inspect the Portable
   export for provenance and retained history.

Public OAuth rollout, recurring collector wiring, desktop UI, and live-source evidence
remain separate acceptance gates. Outlook Calendar and Outlook/Microsoft 365 email
were removed from scope at the owner's request on 2026-09-15.

References: [Google desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app),
[Calendar sync](https://developers.google.com/workspace/calendar/api/guides/sync), and
[event listing](https://developers.google.com/workspace/calendar/api/v3/reference/events/list).
