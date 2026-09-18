# Slack selected-channel capture

Open Brain reads only channels the owner selects after connecting a Slack user account. It requests these user scopes:

- `channels:read` and `channels:history` for public channels.
- `groups:read` and `groups:history` for private channels the authorizing user can access.

It never requests bot, direct-message, posting, administration, or file-write scopes. A Slack user token can access channels available to that user, so selection limits what Open Brain captures; it does not narrow Slack's token permission.

## App setup

Create or use a Slack app with PKCE enabled in **OAuth & Permissions**. This is an owner action: Slack treats enabling PKCE as a one-way public-client setting. Register one fixed loopback redirect URL, for example `http://localhost:8765/oauth2/callback`. Do not add a client secret to Open Brain.

Give Open Brain a JSON client configuration file containing only the public client ID and that exact registered redirect URL:

```json
{
  "client_id": "123456789.123456789",
  "redirect_uri": "http://localhost:8765/oauth2/callback"
}
```

`SlackAuth.connect()` opens the system browser through the shared loopback flow, binds `localhost` on that fixed port, and uses PKCE with `S256`. It sends `client_id`, authorization code, verifier, and redirect URI to `oauth.v2.access`; it never sends a client secret. Slack refreshes also use no client secret for this public desktop client.

## Capture options and behavior

`SlackSourceClient.resources(connection_id)` lists at most 100 non-archived public or private channels per cursor page. `fetch(selection, {"date_floor": "2026-01-01T00:00:00Z"}, checkpoint)` accepts exactly one selected `channel:<id>` and an RFC 3339 date floor with timezone.

Each batch contains at most 25 messages and makes at most two provider requests. The proposed checkpoint carries selected-channel identity, Slack pagination cursors, a frozen full-scan cutoff, and compact message/reply identities with revisions. It does not contain tokens or message text. Slack REST history omits deleted content, so after a complete successful frozen scan Open Brain reconciles previously known messages and replies that were absent from that scan. It emits same-identity unavailable revisions only then. A failed, rate-limited, or partial history/reply scan never proves deletion.

The checkpoint holds at most 500 known and 500 seen identities. This keeps the durable state within its 128 KiB contract. A selected channel exceeding that bounded scan state stops with `scan_state_too_large` instead of silently losing deletion detection. Slack's restrictive history rates can make a complete scan take many scheduled batches; Open Brain never sleeps through `Retry-After`. A rate limit produces `LiveSourceError("rate_limited")` with Slack's `Retry-After` value; the caller retains the prior checkpoint and resumes the same frozen history or reply page. The next scheduled cycle begins a new frozen full scan, so new channel traffic cannot starve older cursor pages. Edits create a new revision for the same message identity.

Captured Slack content uses Open Brain's local-only default privacy decision. It remains third-party, unverified content. Tests use synthetic data only; they do not prove Slack app registration, sign-in, or live capture.

## Candidate discovery and recurring notes

Discovery is opt-in policy, not automatic channel consent. Configure it for the connected account
with the optional collector CLI:

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground slack-policy-setup --connection-id account:REPLACE \
  --keyword roadmap --keyword launch --lookback-hours 24 \
  --activity-weight 1 --keyword-weight 20 --threshold 20 \
  --proposal-opt-in
```

The defaults are a 24-hour lookback, activity weight `1`, keyword weight `20`, and threshold `20`.
Discovery runs at most once per 24 hours, checkpoints partial scans, and qualifies scores at or
above the threshold. A qualifying channel that is not already approved is only a pending suggestion:

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground slack-suggestions --connection-id account:REPLACE
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground slack-suggestion-approve --connection-id account:REPLACE --channel-id C123
```

Approval adds the channel to the explicit allowlist. Dismissal leaves it uncaptured. Heuristics never
enable a channel or create a proposal by themselves.

Recurring-note routing is also explicit and validates the canonical page before writing private
collector policy:

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground slack-mapping-add --connection-id account:REPLACE \
  --channel-id C123 --page-id page_11111111-1111-4111-8111-111111111111 \
  --keyword roadmap
```

With proposal opt-in enabled, a matching captured thread creates a pending append-patch proposal.
It includes bounded quoted Slack text, the source link, capture provenance, the target page ID, and
the target page revision hash. The collector can create proposals but cannot approve them; inspect
and decide them through `review show`, `review approve`, or `review reject`. A changed page fails
approval with `review_conflict`, and rejection leaves the page unchanged.

Synthetic acceptance uses a disposable Brain and an injected Slack transport to cover discovery,
four-hour capture, restart catch-up, mapping, patch inspection, target drift, and retry behavior.
Real-source acceptance is owner-run only: use a small test channel, retain pass/fail and safe
identifiers, and do not commit message bodies, account data, tokens, or raw receipts.

Official references: [Slack PKCE](https://docs.slack.dev/authentication/using-pkce/), [conversation history](https://docs.slack.dev/reference/methods/conversations.history/), and [thread replies](https://docs.slack.dev/reference/methods/conversations.replies/).
