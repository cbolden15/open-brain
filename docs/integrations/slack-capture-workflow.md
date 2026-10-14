# Slack development capture

The optional collector retains selected Slack observations, extracts separate developments, and prepares owner-reviewed page updates. Raw and derived captures stay unverified. Publication and facet vocabulary changes require an owner decision.

This is a contributor implementation. The installed `0.1.0` executable may lack these interfaces. Check `open-brain catalog --json` on the candidate: state schema 16, runtime session 11 and Portable Brain 11. Read [compatibility and migration](../portable-brain-v11.md) before upgrading an existing Brain.

## Configure selected sources

Reuse [Slack authorization and policy setup](slack.md). Credentials stay in the OS credential store. Core installation does not install or enable the collector. Set private absolute paths:

```sh
CAPTURE_STATE=/absolute/private/collector/state.json
CAPTURE_BRAIN=/absolute/private/brain
```

A contributor checkout can use `uv run --package open-brain-collector open-brain-collector` in place of `open-brain-collector`. Create a private source arguments file:

```json
{
  "source_id": "slack.selected",
  "selection": {
    "connector_name": "slack",
    "connection_id": "account:REPLACE",
    "resource_id": "channel:REPLACE",
    "resource_type": "channel"
  },
  "options": {
    "date_floor": "2026-10-11T00:00:00Z",
    "privacy_tier": "work"
  }
}
```

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground configure --arguments-file /absolute/private/source.json
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground preview --source-id slack.selected
```

Inspect the preview before `import --source-id slack.selected --preview-id REPLACE`. Configuration does not enable scheduling. Use `control --source-id slack.selected --action enable` only after approving recurring collection. One-shot and resident collectors must use the same explicit lease path.

Source tiers are `public`, `work`, `personal`, `secret` and `unknown`. Owner-configured privacy stays local-only and binds the retained preview and selection generation. Unknown classification holds the preview without capture or ordinary retrieval. Invalid tiers refuse before fetch. A changed policy requires explicit selection reset, fences the old preview and does not reclassify accepted evidence. Accepted evidence uses the supported owner privacy repair workflow.

## Process developments

Create a private processing configuration:

```json
{
  "enabled": true,
  "ai_exclusions_verified": true,
  "excluded_conversations": [],
  "max_calls": 25,
  "max_input_bytes": 262144,
  "max_developments": 25
}
```

`ai_exclusions_verified` is an owner attestation that the private exclusion policy was checked. It does not query or override Slack workspace AI restrictions. Leave it false until that policy is established. Unverified policy holds processing. Exclusions are checked before source fetch, retained-preview writes, extraction and postprocessing.

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground capture-setup --arguments-file /absolute/private/processing.json
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground capture-process
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground capture-status
```

The default extractor is local and conservative. Explicit lines such as `Decision: Use the agreed rollout` and `Committed: Deliver the draft on Friday` create separate developments. Requested, proposed, approved and correction statements retain separate meanings. Missing actor and scope remain uncertain. Unlabeled conversation remains searchable raw evidence; this extractor does not promise general conversational recall.

The public processing API supports an injected extractor with an explicit provider policy and budgets. External processing requires permitted privacy egress and a supplied adapter. The CLI has no automatic cloud provider, ambient credentials or fallback. Provider/model selection and an approved private adapter are deployment decisions.

Observations deduplicate across discovery methods by account/workspace identity, conversation, message timestamp and revision. Managed revisions remain immutable. Historical overlap replays the old receipt without promoting it as a newer head. Processing reserves development operations before capture, binds canonical supporting evidence before completion and retains failed work for exact retry. Capacity stops admission visibly; unresolved evidence is never evicted.

## Review and classify

Reuse explicit Slack canonical-page mappings and proposal opt-in. Updates freeze the target page revision. The automation authority can propose and assign existing facets; it cannot approve publication or create vocabulary. A maximum of ten weekly proposal slots is reserved before writing. Capacity failures leave work pending.

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground capture-review
open-brain review show REPLACE_PROPOSAL_ID --data-dir "$CAPTURE_BRAIN" --json
```

Use ordinary review approval/rejection after inspection. Approval revalidates the target page, current raw evidence and transitive development support. Source corrections or deletions conflict with stale approval. Lost-acknowledgement retries return the original frozen proposal, including after Portable restoration.

`capture-review-defer --proposal-id ID --until ISO_TIMESTAMP` changes attention only; items resurface on that date. `capture-review-dismiss --proposal-id ID` removes attention without changing a decision. Routine attention expires after 30 days; commitments and contradictions remain visible. Approved and rejected engine states remain terminal.

Add private `candidate_dimensions` mappings for actor, subject or environment to existing facet dimension IDs. There are no product-default dimensions or values. Candidates remain outside authoritative facet filters.

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground capture-candidates
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground capture-candidate-decide --candidate-id ID --action map --value-id EXISTING_VALUE_ID
```

Owner actions are map, create, reject and defer. Creation uses the engine's existing facet operation. Decisions precede backfill; exact revision, version and operation IDs persist before assignment. `capture-candidate-retry --candidate-id ID` replays unfinished operations. Conflicts need explicit owner `--replan`. New evidence after resolution appears as `backfill_pending`; rejected and deferred candidates retain their disposition.

## Optional discovery and old threads

Channel-only authorization retains its original scopes. Optional `connect --capability owner_messages`, `mentions`, `dm` or `group_dm` requests only the extra scopes selected by the owner. Authorization alone does not grant admission.

Add `owner_id`, enabled `capabilities` and exact `permitted_conversations` to processing configuration. Run `capture-discover --connection-id ACCOUNT`. Full message/thread hints and cursors persist before canonical resolution. Permitted hints create protected `slack-extra.*` selections with unknown privacy. They do not enable sources. Inspect returned source IDs, explicitly configure a chosen tier with `reset: true`, then enable after review. Current configuration is checked before lookup, fetch and writes; changed permissions or policy generation fence older optional selections.

For an unresolved selected old thread, use `capture-track-thread --connection-id ACCOUNT --channel-id CHANNEL --thread-ts TIMESTAMP --privacy-tier TIER`, then enable its returned source ID. Thread refresh shares channel identities, reports partial coverage and cannot prove channel-wide absence. Missing canonical thread identity is reported unsupported. Previously untracked old roots and exhaustive search remain connector limits.

## Health, recovery and rollout

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground health
```

Bounded run receipts distinguish empty, completed, partial and failed work without storing message bodies or raw provider errors. Health reports heartbeat age, coverage, backlog count and known retries/usage. Unknown measurements stay unknown. The default heartbeat deadline is 24 hours plus a two-hour grace period; monitor it independently of the collector process.

Backlog age uses the oldest pending observation time. Checkpoint lag measures time since the oldest acknowledged, caught-up run among enabled sources. It remains unknown if an enabled source has no complete acknowledged run. These are local operation times, not an assertion of exhaustive Slack coverage.

The [systemd service](examples/slack-capture.service), [timer](examples/slack-capture.timer) and [launchd example](examples/slack-capture.plist) are templates. Nothing installs or activates them here.

Canonical evidence and append reservations survive Portable Brain 11. Private policy, discovery hints, processing/candidate/attention queues, source checkpoints and custody require the private collector recovery backup alongside the Brain. Verify matching restore in a disposable copy before rollout. On failure, pause admission and scheduling while retaining pending work and receipts. Do not delete queues to force progress or run an older binary against a migrated store.

The seven-day private pilot, channel selection, quality sampling and schedule activation are owner-run deployment work. Synthetic tests do not certify live Slack precision or recall.
