# Resume Slack capture with noteworthy-channel selection and recurring-note updates

Status: not started for noteworthy-channel selection, recurring-note routing, and patch proposals.
The selected-channel Slack integration deferred by
[priority-capture](2026-09-16-priority-capture.md) now exists on current `main`: `slack_auth.py`,
`slack_live.py`, collector dispatch, and focused contract tests are implemented. Extend those
surfaces instead of recreating them. This plan adds automatic candidate-channel selection, explicit
recurring-topic routing, and first-class review patches for safe canonical-note updates.
Base confirmed on 2026-09-18: `26ba75ef7530cee295fac633b1914077b5e86759`.

## Motivation

The owner wants a recurring job that pulls Slack conversations they're involved in, or from a set of
noteworthy channels, and keeps Open Brain notes current from that activity — without watching Slack
manually. Two things are missing to make that real:

1. Today's deferred design requires the owner to explicitly select channels. There's no notion of
   "noteworthy" — a channel that suddenly gets busy, or matches topics the owner cares about, won't
   surface on its own.
2. Raw capture alone produces inbox entries without deciding whether a new Slack thread
   is a continuation of an existing canonical note (should update it) or a new topic (should become a
   new one). Left unhandled, recurring discussions in the same channel fragment into many small notes
   instead of one maintained page.
3. The current review update operation accepts complete replacement Markdown. An unattended job
   cannot safely use a raw Slack thread as that draft because approval would replace the entire
   canonical page. Recurring updates need a bounded patch tied to the inspected page revision.

The first two changes extend the collector. Patch proposals deliberately extend the review engine and
Portable Brain review schema rather than disguising partial content as a full-page replacement.

## Outcome and acceptance

An owner can enable a Slack source through the existing collector source workflow, select an explicit
channel set, and additionally opt into heuristic candidate discovery
(activity threshold, keyword list) that surfaces other channels for one-time or standing approval —
heuristics widen the candidate set, they never auto-enable a channel without the owner accepting it.
On each scheduled run, new/updated threads in enabled channels are captured, and threads that match an
owner-curated channel-to-page mapping produce a page patch proposal bound to the target page's exact
revision. Review shows the proposed diff. Approval revalidates and applies only the patch; a changed
target fails with `review_conflict`. Nothing publishes without the owner's explicit review decision.
Acceptance mirrors the other sources: restart/pause/resume, lost access/revocation, retry after
capture failure, and preserved patch/provenance history in export.

## Existing implementation to extend (do not duplicate)

- Auth/scopes: user-scope PKCE flow, `channels:read channels:history groups:read groups:history`,
  no bot/posting/DM/admin scopes. See priority-capture's "Google grants are separate" section for the
  exact scope list and rationale.
- Capture shape: `slack_live.py` already implements bounded channel discovery, history and thread
  replies, edits, observed deletions, pagination, `Retry-After` handling, and resumable state. Extend
  it only where scoring needs bounded activity metadata. `slack_auth.py` already owns the public PKCE
  flow and narrow scopes; do not replace it.
- Existing fixture: `slack.py` in the connector package is a fixture-level adapter only, not a live
  integration. Keep it separate from the live provider module.
- State/scheduling: the collector owns source config, schedule, lease, and checkpoints
  (`packages/collector/src/open_brain_collector/live_manager.py`); provider modules return bounded
  intakes and proposed continuation state, same as every other source. No new scheduler.
- Store credentials in OS-backed storage (Keychain on macOS), retain only opaque account references,
  and provide no plaintext fallback for unattended capture — identical to the Gmail/Drive pattern.

## New work

### 1. Candidate-channel heuristics

A scoring pass over channels the authenticated user can see (via `channels:read`/`groups:read`),
run before a scheduled fetch when the daily discovery interval is due:
`score = message_count(lookback_window) * w_activity +
keyword_hits(channel_name_and_topic) * w_keyword`. Channels above threshold, or in the owner's
explicit allowlist, become fetch candidates. Config needed from the owner: keyword list, lookback
window, weights/threshold. Heuristic-only channels above threshold but not yet allowlisted should
surface as a pending suggestion (status/CLI output), not silently start capturing — capturing a
channel the owner never explicitly approved is a scope expansion the existing design's consent model
doesn't allow. Store the resulting allowlist/threshold config next to other source config in the
collector's private state, not in the engine or the git repo.

### 2. Recurring-topic-to-page mapping and patch proposals

An owner-curated mapping (channel, or channel+keyword, → canonical `page_id`) is consulted after each
captured thread lands in the inbox. If a match exists, the collector creates a typed append patch
proposal against that page instead of a create-new proposal. The patch contains a bounded Slack
update block with the captured thread's provenance and link. The review engine stores the expected
target revision and structured text edits, derives a human-readable diff for inspection, and applies
the edits only after approval revalidates the target. Patch operations contain no filesystem paths or
shell commands and cannot address content outside the selected canonical page.

Full-page review proposals remain backward compatible. Add a distinct patch draft and portable
record shape rather than overloading `markdown` with partial content. `review show` exposes both the
bounded patch and its derived diff. Approval materializes the patched page through the existing
conflict-preserving publication path. Rejection changes no page. Edit-and-approve must either accept
a complete replacement body explicitly or gain a typed edited-patch input; do not reinterpret an
ambiguous string as one or the other.

Do not implement fuzzy/semantic matching of "is this thread about the same topic as an existing
page" — that's a judgment call an unattended job shouldn't make silently, and the existing review
flow already requires owner inspection before anything publishes. The mapping is explicit and owner
maintained, the same way `inbox route` targets a space explicitly today.

The scheduled collector is an owner-configured local service, not an MCP client. Its default-off
Slack proposal setting and explicit mapping are the authorization to create patch proposals. Do not
change agent grants as a side effect. Agent clients still require `--allow-review-propose` when they
invoke review tools. The collector never receives review-decision authority: proposals queue in
`review list --status pending` for manual inspection and approval. Auto-approval is out of scope.

## Decisions

- The channel-to-page mapping uses an owner-facing CLI backed by typed, versioned config in the
  collector's private state. The CLI provides add, list, and remove operations and validates channel
  and page identifiers before writing. Direct config-file editing is not the normal supported
  workflow.
- Candidate discovery defaults to a 24-hour lookback, `w_activity = 1`, `w_keyword = 20`, and a
  threshold of `20`, with qualification at `score >= threshold`. This makes either 20 messages in
  the lookback window or one configured keyword match sufficient to surface a suggestion. The
  portable default keyword list is empty; setup prompts the owner to add workspace-specific terms or
  explicitly continue without them. Discovery runs at most once per 24 hours even when capture runs
  more often, checkpoints partial scans, and honors Slack's `Retry-After` response so history calls
  remain bounded.
- Enabled Slack channels are captured every four hours, including outside business hours. If the
  collector restarts after missing one or more runs, it performs one immediate checkpoint-based
  catch-up run rather than replaying every missed interval. Candidate discovery keeps its separate
  once-per-24-hours limit.
- Heuristic-surfaced channels appear in a dedicated
  `open-brain-collector sources slack-suggestions` listing rather than in `open-brain space`.
  General Slack source status may show the pending-suggestion count and point to that command, but
  approval remains an explicit Slack-source action.
- Canonical-note updates use first-class structured patch proposals. Each proposal is bound to the
  target page ID and revision hash, stores bounded text edits and source provenance, and exposes a
  derived diff. Approval fails on target drift and applies only the validated patch.
- Slack mapping and suggestion commands live in the optional collector CLI, not the foreground core.
  Use the `open-brain-collector sources` command family so the core gains no collector dependency.

## Execution phases

<!-- model: sonnet -->
### Phase 1: review patch foundation

- [ ] Add typed patch-draft, patch-operation, binding, history, and Portable Brain contracts with
  strict byte/count limits and backward compatibility for existing full-page proposals.
- [ ] Extend review propose/show/approve/reject/edit behavior and owner CLI output so a patch is
  revision-bound, displayed as a diff, and applied only through the confined canonical-page writer.
- [ ] Add engine and service tests for valid patches, malformed or overlapping edits, stale targets,
  idempotent retries, edited decisions, exports, imports, and unchanged full-page behavior.

<!-- model: sonnet -->
### Phase 2: Slack policy, mappings, and suggestions

- [ ] Add typed, versioned private Slack policy state with the chosen heuristic defaults, daily scan
  checkpoint, allowlist, pending suggestions, proposal opt-in, and channel/channel+keyword mappings.
- [ ] Add collector CLI operations for policy setup, mapping add/list/remove, suggestion list/approve/
  dismiss, and status counts. Validate channel IDs and canonical page IDs before writing.
- [ ] Extend bounded Slack discovery to score activity and channel-name/topic keywords without
  capturing unapproved channels, while preserving `Retry-After` and resumable partial scans.

<!-- model: sonnet -->
### Phase 3: scheduled patch creation

- [ ] Group new Slack captures by thread, apply the explicit mapping, construct the bounded Slack
  update fragment, and create an idempotent patch proposal through the review engine boundary.
- [ ] Keep proposal creation default-off, never auto-approve, and make retries/restarts produce one
  pending proposal for the same capture set, target revision, and mapping generation.
- [ ] Cover four-hour capture, daily discovery, catch-up after downtime, mapping changes, revoked
  access, target drift, capture failure, and preserved provenance with focused integration tests.

<!-- model: haiku -->
### Phase 4: documentation and verification

- [ ] Update operator, Slack setup, review/publication, CLI, portability, and privacy documentation
  with synthetic examples and the explicit approval boundary.
- [ ] Run focused tests, `make verify`, `git diff --check`, and
  `actionlint .github/workflows/ci.yml`; run native/package checks only if those surfaces changed.
- [ ] Run disposable-Brain acceptance with synthetic Slack transport. Record real-source acceptance
  as owner-run evidence without committing message bodies, account data, or tokens.

## Verification

Same bar as the other four sources: focused tests per module, then `make verify`, `git diff --check`,
actionlint; native/packaging changes also need `make native-audit` and `make homebrew-smoke`. Real-source
acceptance uses a disposable Brain and a small number of real test channels — do not publish real
message bodies or tokens in evidence, matching the existing source matrix convention.

## References

- [Priority capture plan](2026-09-16-priority-capture.md) — deferred Slack scopes, module names, and
  architecture this plan builds on
- [Review and publish captured material](../review-publication.md) — proposal/target-page-id/approval
  mechanics used for recurring-note updates
- [Reading records and retained history](../records-and-history.md) — record/page identity model
- [Slack public-client PKCE](https://docs.slack.dev/changelog/2026/03/30/pkce/)
- [Slack OAuth](https://docs.slack.dev/authentication/installing-with-oauth/)
- [Slack Web API rate limits](https://docs.slack.dev/apis/web-api/rate-limits/)
