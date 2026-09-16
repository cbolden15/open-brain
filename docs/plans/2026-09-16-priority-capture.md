# Complete the five priority capture sources

Status: active implementation plan for the owner's current priority. All other unfinished
integrations wait. Outlook email/Calendar and the previously excluded Microsoft sources remain
excluded. This plan does not claim that existing fixture adapters are live integrations.

## Outcome and acceptance

An owner can connect Gmail, Google Drive, and Slack; select bounded resources; preview and import
them; retrieve their content through a fresh client; and explicitly enable recurring collection.
The same configuration and source operations serve the headless CLI and desktop. Selected Claude
Code and Codex projects automatically produce captures after turns, with summaries and transcripts
separate opt-ins. Existing explicit saves continue to work.

Each of the five sources has a separate acceptance row. Completion requires actual provider/client
evidence, updates without duplicate active results, retry after capture failure, restart/pause/resume,
lost access/revocation or opt-out, preserved provenance/history in export, and passed project checks.
Synthetic tests establish implementation correctness, not live-source acceptance. Public OAuth app
registration, provider approval, sign-in and native prompts remain visible owner actions if needed.
The owner permits choosing any test resources; choose small selections and do not enable broad
mailbox, channel, or session-history capture by default. No outbound messages are authorized.

## Existing source and boundaries

Base: `6b22f22ea1f4920511e4b6fea0a1df56639f1166`. Existing connector modules include
`mail_drive.py`, `slack.py`, `agent_session.py`, bounded Calendar OAuth/HTTP, source provenance,
and a public engine capture sink. The collector currently dispatches GitHub and local fixtures.
Its desktop bridge directly edits collector state and supports only GitHub. This must not become
a second, divergent configuration implementation for the new sources.

Keep network access, provider authentication, credentials and scheduling in the optional connector
and collector packages. The foreground core gains no provider dependency, listener or service.
Preserve Portable Brain v1, stable source identity and existing GitHub/Calendar behavior. The
desktop host communicates with the optional collector over its private local control boundary;
the renderer never receives credentials or chooses executable commands.

## Architecture decisions

The chosen approach is one shared capture service in the optional collector, backed by reusable
provider modules. A smaller CLI-only implementation would leave desktop and unattended acceptance
incomplete; a parallel desktop-only implementation would create inconsistent source state. Both
are rejected as the final architecture. Headless functionality is implemented first and exposed
through the same service to the desktop.

The collector owns source configuration, schedules, lease, control operations and acknowledged
checkpoints. Provider modules return bounded intakes and proposed continuation state; fetching
cannot commit a checkpoint. Persist a preview batch privately before capture, bind its identifier
to the source configuration and Brain identity, and advance only after acknowledgements. Automatic
runs use the same apply path under the source/collector lease. A pending batch survives a crash;
changed selections invalidate stale previews and require an explicit reset/resync. Do not maintain
independent manual and scheduled cursors for the same source.

OS-backed storage holds provider tokens and refresh material. Configuration and receipts contain
opaque account references only. On macOS use Keychain; Linux uses an available Secret Service and
otherwise refuses unattended cloud capture. Missing/locked storage produces a recoverable status;
there is no plaintext fallback for unattended credentials. Secrets do not appear in argv, logs,
exceptions, source documents, exports or renderer responses. Reuse the existing bounded helper
pattern rather than introducing an account-token API in the core.

All HTTP requests have fixed provider origins, denied redirects, body/page limits and a total
deadline. OAuth uses state, PKCE where the provider supports it, a loopback callback bound to the
initiating flow, verified account identity and narrowly disclosed read scopes. Refresh is serialized
per account. Disconnect prevents future reads and clears the owned credential while preserving
already captured Brain history. No hosted relay or embedded confidential-client secret is added.

Google grants are separate: Gmail requests `openid` plus
`https://www.googleapis.com/auth/gmail.readonly`; Drive requests `openid` plus
`https://www.googleapis.com/auth/drive.readonly`. Gmail labels and Drive file selections constrain
Open Brain's capture operations, not the credential's provider permissions. The connection screen
must disclose account-wide read grants; selected capture does not turn them into per-resource tokens.
Slack uses user scopes `channels:read`, `channels:history`, `groups:read`, `groups:history`. Its token
can read channels the authorizing user may access; channel selection is enforced by Open Brain.
All clients reject unselected IDs even when an authorized API response contains them.

Imported provider/session content stays third-party and unverified through capture, search and export.
Render resource names, titles and previews as inert text, never raw HTML. Provider content cannot
invoke collector operations or become executable commands. Native URL opening allows only validated
HTTPS provider links or the initiating OAuth URL; arbitrary schemes/navigation are refused. Test
malicious HTML, unsafe links and instruction-like content through the real capture/search boundary.

## Implementation units and ownership

### 1. Provider modules

Three independent writers own new modules and their focused tests; the coordinator owns shared
interfaces, CLI, collector, engine-boundary integration and all shared files. Workers do not commit,
push, alter account/OS settings, scan personal data, or launch nested workers.

Google writer: new `google_sources.py`, `google_sources_auth.py`, their tests and provider setup
documentation. Implement separate Gmail/Drive grants, explicit account metadata, Gmail label listing,
bounded selected-label/date-range message reads, MIME body extraction without attachments, history
continuation and bounded full recovery when Gmail invalidates history. Google Drive initially reads
individually selected text-like files and exports supported Workspace documents; no recursive folder
mirroring. Preserve original links, changes, removal/access-loss status and body limits. Unsupported
binary formats and incomplete pagination fail visibly rather than claiming full capture.

Slack writer: new `slack_live.py`, `slack_auth.py`, focused tests and setup documentation. Use the
current public-client PKCE flow with read-only user scopes for selected public/private channels;
do not request bot, posting, DM, or administration scopes. Support bounded channel discovery,
history and thread replies, edits, observed deletions, retention/access changes, pagination and
Retry-After. A rate-limited partial run resumes its durable state instead of recapturing an entire
channel. App setup must be explicit; enabling PKCE on an existing app is an owner action.

Session writer: new `agent_session_live.py`, `agent_session_hooks.py`, focused tests and docs.
Verify actual installed client capabilities against official docs and synthetic sessions. Support
Claude Code and Codex independently using documented lifecycle hooks when present and versioned,
tested transcript adapters. Add only owned hook entries, preserving unrelated settings. Config
changes require a preview/preimage check and a reversible uninstall. Do not depend on a global
history sweep. Select exact projects and bind event cwd/session/path to that selection. Record
bounded durable events, tolerate partial JSONL lines and rotation, and fail closed on unsupported
formats. Both summary and transcript capture start disabled. Require an explicit choice for each, per project.
Never ingest structured tool results, hidden reasoning, credential fields or Brain-returned content.
Before persisting extracted text, scan supported secret patterns (tokens, private keys and credential
URLs); reject matched events with metadata-only quarantine notices and a queue-discard operation.
Scanning is best-effort and cannot detect every secret in arbitrary prose; disclose this limitation.
Test representative secrets, false positives and feedback-loop prevention.
Do not invent model-generated summaries. An extractive summary must be labeled and contain only
permitted user/assistant text. Background hooks must not fail or delay the host client if capture is
unavailable; queued work is bounded and later drainable.

### 2. Shared state and headless commands

Coordinator: add strict, versioned shared selection/batch contracts, private source configuration,
OS credential access and bounded provider HTTP as needed. Preserve existing stable adapter identities.
Expose account connect/status/disconnect, resource discovery, source selection, preview/import,
status, explicit automatic enable/pause/resume/disable and schedule commands. CLI output is metadata
and actionable error codes. Owner setup references client configuration by path, never token values
in command arguments. Normal public onboarding must ultimately use registered application identity;
a developer-client setup is not labeled public onboarding complete.

Validate source/Brain binding on every operation and stage state with owner-only permissions, safe
paths, atomic replacement and no symlink traversal. Pin original source references for revisions.
Test that changed content replaces the active search result while export retains history. Where
provider updates alter metadata only, the revision still changes and the capture sink must see it.

### 3. Collector and desktop integration

Extend the existing optional collector, preserving existing v1 state or providing a tested explicit
migration. Reuse its process lease, opt-in launchd management and public capture sink. The real
implementation preserves legacy v1/GitHub state and stores priority sources in adjacent private
`live/` state, under the same collector process lease and scheduler. Both CLI and desktop call
the same `LiveCaptureService`; neither keeps a second source cursor. The real
private Unix-socket protocol has bounded typed messages, owner-only directory/socket, one service
owner, honest connection status and durable pause acknowledgement. All control writers route through
the same state implementation; concurrent disable/pause cannot be overwritten by a completed fetch.

Desktop offers provider/account selection, resource selection, preview, import, automatic capture
opt-in, schedule and status, with the same CLI configuration. Agent capture offers project selection
and independent summary/transcript controls. OAuth opens the system browser through the native host.
The desktop never claims connected/complete from a synthetic payload or a queued sync request.
Closing the window preserves an independently enabled collector; quit stops only desktop-owned
children. Account setup and OS prompts remain user-visible.

### 4. Verification and real-source acceptance

Use focused tests during implementation, then run `make verify`, `git diff --check` and actionlint.
Native/packaging changes also run `make native-audit`, `make homebrew-smoke` and exact desktop/native
proof. Independent read-only reviews cover authentication, source/Brain binding and lifecycle.
Resolve P0-P2 findings before integration. The coordinator commits verified units and serially
integrates with the preserved Mac mini branch; never relax the worker sandbox to make Git writes work.

The source matrix names exact commit, command, result and redacted receipt for all five sources.
Live checks use a disposable Brain and small chosen resources. Do not publish real message bodies,
transcripts, tokens or account identities in evidence. Real Claude/Codex tests use a fresh designated
test project with synthetic conversations; prove automatic capture, search, restart, opt-out and no
Brain-result feedback. Remaining unavailable sign-in/app registration/native prompts are queued with
specific actions and do not become passing checks. Continue independent implementation while waiting.

## Primary provider references

- [Google desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Gmail sync](https://developers.google.com/workspace/gmail/api/guides/sync)
- [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Drive download/export](https://developers.google.com/workspace/drive/api/guides/manage-downloads)
- [Slack public-client PKCE](https://docs.slack.dev/changelog/2026/03/30/pkce/)
- [Slack OAuth](https://docs.slack.dev/authentication/installing-with-oauth/)
- [Claude Code hooks](https://code.claude.com/docs/en/hooks)
- [Codex configuration](https://developers.openai.com/codex/config-reference/)
