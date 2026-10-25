# Configure priority source capture

These contributor commands use the same source service as the desktop. The optional
collector must be installed separately from the core. In a source checkout, run
`uv sync --frozen --all-packages --group dev` and use `.venv/bin/open-brain-collector` as the executable.
The desktop discovers the separately installed collector at its supported Homebrew locations;
contributors can set `OPEN_BRAIN_COLLECTOR` to that absolute executable path before launching it.

## Choose a Brain and connect

Use the existing Brain path reported by `open-brain status --json`. Keep client configuration
files outside Git. The examples use placeholders that must be replaced with absolute paths.

```sh
CAPTURE_BRAIN="/absolute/path/to/brain"
CAPTURE_STATE="$CAPTURE_BRAIN/collector/state.json"

open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground status

open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground connect --provider gmail --client-config /absolute/path/to/google-desktop.json
```

Connect Drive separately with `--provider google_drive`. A shared Google development client can
identify the app for both flows, but Gmail and Drive grants remain separate. Each sign-in opens
the system browser. Read [Google setup](google-sources.md) before creating the development client.
Use [Slack setup](slack.md) for its public-client JSON and registered loopback redirect.

The client JSON identifies the application. It is distinct from the user's login and access
tokens. Development clients are for testing; public onboarding requires the production app
identity and provider approval. Credentials are stored in the operating-system store with a
separate namespace for each local account-state root. Disconnecting a test Brain cannot delete
another Brain's connection to the same account.

## Select, preview and import

List accounts with `accounts --provider gmail`, then discover resources:

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground resources --provider gmail --connection-id account:REPLACE_FROM_ACCOUNTS
```

Save one selection in a local arguments file. Use the exact connection and resource IDs returned
by discovery. Gmail's date floor is a calendar date. Slack's date floor is RFC 3339 with a timezone.
A `drive_file` selection takes an empty options object. Drive folder and account sets take
`date_floor` and `title_suffixes`; see the Meet-notes walkthrough below.

```json
{
  "selection": {
    "connector_name": "gmail",
    "connection_id": "account:REPLACE_FROM_ACCOUNTS",
    "resource_id": "mail_label:REPLACE_FROM_RESOURCES",
    "resource_type": "mail_label"
  },
  "options": {"date_floor": "2026-09-01"}
}
```

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground configure --arguments-file /absolute/path/to/selection.json
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground preview --source-id REPLACE_FROM_CONFIGURE
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground import --source-id REPLACE_FROM_CONFIGURE --preview-id REPLACE_FROM_PREVIEW
```

Import consumes the saved preview and advances its checkpoint after every item is durably captured
or transferred to quarantine. Provider acknowledgement follows that local commit. See
[collector recovery](collector-recovery.md) for inspect, status, retry, and backpressure. An
uncertain response can be retried with the same preview ID. Changed selections require an explicit
`"reset": true` in the configure arguments. Preview and import do not enable recurring collection.
The duplicate cache holds at most 2,048 deliveries and protects every delivery in a pending batch.
After cache eviction, the engine still enforces stable delivery identity.

## Capture Gemini Meet notes from Drive

Gemini writes a notes Doc and a transcript Doc into Drive after a Google Meet call. Notes for
meetings other people organized reach the account as shared files and never sit in the owner's
`Meet Recordings` folder, so the account shape with the two Meet title suffixes captures both the
owner's and the shared Docs. To capture one folder instead, list folders first and use its id:

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground resources --provider google_drive --connection-id account:REPLACE_FROM_ACCOUNTS \
  --resource-type drive_folder
```

`--resource-type` accepts `drive_file` (the default) and `drive_folder`, for `google_drive` only.
Each listed folder is `drive_folder:<id>`; use it as the `resource_id` with
`"resource_type": "drive_folder"`. `drive_account:all` is a fixed id and is not listed.

Save the account selection with the two suffixes and a date floor. Nothing modified before the
date floor is listed:

```json
{
  "selection": {
    "connector_name": "google_drive",
    "connection_id": "account:REPLACE_FROM_ACCOUNTS",
    "resource_id": "drive_account:all",
    "resource_type": "drive_account"
  },
  "options": {
    "date_floor": "2026-10-01",
    "title_suffixes": ["Notes by Gemini", "Transcript"]
  }
}
```

Then configure, preview, import and enable with the same commands as any other source:

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground configure --arguments-file /absolute/path/to/meet-notes.json
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground preview --source-id REPLACE_FROM_CONFIGURE
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground import --source-id REPLACE_FROM_CONFIGURE --preview-id REPLACE_FROM_PREVIEW
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground control --source-id REPLACE_FROM_CONFIGURE --action enable --interval-seconds 900
```

The first preview backfills from the date floor; later runs are incremental. A transcript over
60,000 characters previews and imports as parts titled `<name> (part <n> of <N>)`. An edited Doc
becomes a new revision of the same record. A `drive_file` selection does not get this behavior:
an edited single file is quarantined with `source_revision_conflict`. Options, the checkpoint,
splitting and the per-file notices are described in [Google setup](google-sources.md).

## Recurring collection

Enable a saved source separately from the service:

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground control --source-id REPLACE_FROM_CONFIGURE --action enable --interval-seconds 900
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  background-enable
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" status
```

macOS background setup installs one owned user launchd service. The desktop provides the same
opt-in. Closing the desktop preserves that independent service. `background-disable` waits for
the owned service to stop and preserves Brain content. Failed startup removes the attempted
registration; an unconfirmed cleanup is reported explicitly. Linux automatic service setup remains
a separate operator step.

The `control` actions are `pause`, `resume`, `disable`, `schedule`, and `sync_now` in addition to
`enable`. `sync_now` queues an enabled source for the scheduler; it is not a completed import.
Use status to inspect the result. A rate limit keeps the checkpoint and schedules the next attempt
after the provider's requested delay. Manual and recurring imports share the same state.

For Slack, configure discovery and recurring-note routing separately from source enablement. The
policy is private to the collector state and defaults to proposal opt-out:

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground slack-policy-setup --connection-id account:REPLACE \
  --keyword roadmap

open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground slack-mapping-list --connection-id account:REPLACE
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground slack-suggestions --connection-id account:REPLACE
```

Use either one or more `--keyword` options or `--continue-without-keywords`; the latter is an
explicit choice to use activity-only discovery. Approve suggestions with
`slack-suggestion-approve`, then enable the selected channel source with the normal `control
--action enable --interval-seconds 14400` flow. Add a page mapping with `slack-mapping-add` only
after confirming the canonical `page_id`. A scheduled run may queue a patch, but only the owner can
approve or reject it in the review workflow.

## Selected agent projects

Create a local arguments file with the exact project path and separate content choices:

```json
{
  "client": "claude_code",
  "project_path": "/absolute/path/to/project",
  "capture_summary": true,
  "capture_transcript": false,
  "action": "configure"
}
```

Use `session-preview --arguments-file /absolute/path/to/session.json`, review its configuration
paths, then `session-apply --preview-id REPLACE_FROM_PREVIEW`. Enable the returned source ID with
`control` only when recurring capture is wanted. Use `"client": "codex"` for Codex, or
`"action": "remove"` to preview and remove owned hooks. Existing unrelated hooks are preserved.
Read [session behavior and limits](agent-session-capture.md), including supported client formats,
secret quarantine, and queue discard commands.

## Acceptance limits

Fixture checks are not live-source acceptance. Google and Slack need registered developer apps,
actual consent, and bounded imports from the selected account. Public app verification remains a
separate release gate. Native Claude and Codex checks require fresh synthetic conversations in
selected projects; do not substitute a manually written transcript for a native-client receipt.
Keep actual message bodies, transcripts, credentials, private paths and receipts out of Git.
