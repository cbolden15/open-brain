# Open Brain Capture Implementation Plan

Build the reusable capture infrastructure in the public Open Brain repository, and keep workspace selection, business rules, credentials, and runtime data private on the device. Extend the existing engine, connectors, collector, and review workflow.

The first release should reliably ingest selected Slack sources, preserve provenance, prevent duplicate captures, and present a manageable review queue. DM coverage is a separately enabled connector capability. Automatic publication and automatic taxonomy creation remain out of scope.

This plan prepares implementation; it does not activate schedules, change the repository, or grant source access.

# Ownership and architecture

| Layer | Public implementation | Private deployment |
| --- | --- | --- |
| Engine | Capture validation, privacy enforcement, record identities, evidence relationships, idempotency, taxonomy mutations, publication checks | Brain content, taxonomy values, approval history |
| Connectors | Slack channel and optional mention/DM adapters, normalized source identities, thread and revision handling | Account references, source selections, credentials in OS storage |
| Collector | Durable staging, processing, deduplication, retries, candidate discovery, proposal generation, run health | Checkpoints, queues, source mappings, relevance rules, model selection and budgets |
| Clients | Review list, evidence inspection, diffs, defer and dismiss controls | Review priorities and decisions |
| Device operations | Example service/timer definitions and configuration validation | Installed schedule, alert destination, paths, monitoring settings |

Keep core foreground-only. Scheduling belongs to the optional collector and device supervisor. Collector code may propose changes but must not receive publication-decision or taxonomy-creation authority.

Spaces and facets organize content. Privacy classification remains a separate engine control. The owner’s six facet dimensions and their values are private configuration, not product defaults.

# Existing foundations to reuse

The inspected repository already contains the following foundations. Phase 0 must verify their availability in the actual installed executable and target checkout before extending them.

| Foundation | Starting point | Planned extension |
| --- | --- | --- |
| Slack private policy | `packages/collector/src/open_brain_collector/slack_policy.py` | Coverage, extraction and review settings |
| Slack identities and checkpoints | `packages/connectors/src/open_brain_connectors/runtime/slack.py` | Shared discovery identity and mention/DM integration |
| Collector recovery | Existing custody and retry workflow | Distillation and write recovery using the same durable receipt principles |
| Review publication | Existing list, show, approval and patch proposals | Attention queue, deferral, resurfacing and prioritization |
| Facets | Existing engine-owned dimensions, values, assignments and history | Candidate resolution into owner-approved values |
| Privacy | Existing capture decision and owner CLI tier support | Reproduce and resolve the reported interface failure |

Do not duplicate these systems in private scripts. Current source-only integrations are not automatically supported features of an installed release.

# Delivery sequence

| Work package | Dependency | Main deliverable |
| --- | --- | --- |
| 0 Baseline and contracts | None | Compatibility inventory and synthetic failing cases |
| 1 Privacy and authority | 0 | Verified safe admission and restricted automation capabilities |
| 2 Durable shared ingestion | 1 | One recoverable source registry and processing queue |
| 3 Distillation and review | 2 | Source-linked developments and a bounded review workflow |
| 4 Entity candidates | 2 and the review queue from 3 | Owner-controlled candidate resolution and backfill |
| 5 Optional DM and mention discovery | 2 | Explicitly enabled additional discovery surface |
| 6 Scheduling and private pilot | 3 and 4; include 5 when enabled | Monitored daily operation and acceptance evidence |

Use one focused PR per work package where practical; split schema changes from UI work when that improves review. Start the selected-channel pilot without waiting for DM permissions.

# Phase 0 Baseline and contracts

**Deliverables**

- Record the target commit, installed version, catalog output, available CLI/MCP operations, and state/export schema compatibility.

- Reproduce the privacy failure separately through the installed interface and the target checkout. Determine whether it is a product bug, unsupported MCP argument, stale client schema, or release mismatch.

- Inventory existing source identities, capture retry behavior, collector custody limits, review operations, and facet grants.

- Define versioned contracts for source observations, derived developments, run receipts, and entity candidates before selecting storage changes.

- Create synthetic conversations covering a decision, commitment, correction, cross-post, unknown entity, older-thread reply, and unsupported source.

**Acceptance**

Every proposed feature is marked existing, extension, or new. The privacy problem has an exact reproducible request and expected behavior. Existing data and exports have a documented compatibility path.

**Boundary**

The owner CLI and capture-only MCP are intentionally different authorities. Do not treat a rejected owner-only argument as justification to widen MCP permissions.

# Phase 1 Privacy and authority

**Public changes**

- Fix the failing supported interface, or provide the correct supported invocation and explicit error guidance if the request was unsupported.

- Resolve and validate privacy before a capture becomes available to ordinary retrieval.

- Hold unresolved items in protected local staging. Apply source processing restrictions before sending any text to a model.

- Validate private source mappings against existing privacy tiers and spaces without treating space membership as access control.

- Verify that collection and proposal generation cannot approve publication or create, rename, or archive taxonomy values. Add a narrower capability only if the current interface cannot enforce that separation.

**Private setup**

Define permitted sources, their privacy defaults, allowed processing providers, and uncertainty handling. Keep credentials in the OS credential store.

**Acceptance**

A synthetic sensitive item cannot become visible through an unauthorized retrieval session. Invalid classification cannot silently fall back to a broader default. Automated assignment to an existing facet succeeds while automated vocabulary creation and publication approval are refused. Misclassification recovery uses the supported owner workflow.

# Phase 2 Durable shared ingestion

**Public changes**

- Normalize observations using workspace ID, conversation ID, message timestamp, source revision, observed time, and source link. Multiple discovery methods must identify the same message.

- Reuse durable collector staging and receipts; separate ingestion checkpoints from processing completion.

- Deduplicate identical source revisions before model calls. Changed text creates a new observation rather than reusing an incompatible capture retry key.

- Cache relevant conversation context and process only changed material. Persist unresolved work so a restart resumes it.

- Distinguish no activity, policy exclusion, access failure, rate limiting, partial scan, and unsupported coverage.

- Preserve capture immutability. Subsequent evidence creates supported revisions or explicit relationships; do not mutate raw capture text in place.

- Reuse existing edit/deletion reconciliation. Absence in an incomplete scan must never prove deletion.

**Private state**

Source registry, checkpoints, staged payloads, processing receipts and run history stay outside the public checkout. Separate editable policy from machine-maintained state.

**Acceptance**

Both discovery surfaces return the same message and produce one processed observation. Crashes before and after capture acknowledgement cause no silent loss or duplicate accepted write. A failed channel does not advance its checkpoint. Queue capacity stops admission visibly rather than evicting unresolved evidence. Changed and deleted synthetic messages follow the documented lifecycle.

# Phase 3 Distillation and review

**Public changes**

- Define a generic development schema: claim, actor, subject, relevant environment, event time, supporting observation IDs, existing facet IDs, uncertainty, and relationships to earlier developments.

- Store one independently useful development per derived capture. Use a thread as context; build digests as views.

- Preserve “requested,” “proposed,” “committed,” and “approved” as distinct meanings.

- Link supporting evidence and superseding developments. Only merge cross-posts when their scope, actors, timing and substance agree.

- Keep evidence storage separate from the attention queue. Add deferral dates and resurfacing around existing proposals without changing approved/rejected terminal semantics.

- Generate bounded updates against an explicit canonical page and revision using the existing proposal and inspection contracts.

- Revalidate evidence and page state at approval. A source correction flags affected captures/proposals; it does not silently rewrite a canonical page.

**Private defaults**

Run extraction daily. Start with a 72-hour overlapping discovery window, a maximum of ten weekly review proposals, and a 20-minute review session. Remove routine evidence from the active attention queue after 30 days while preserving its unverified status and searchability. Keep unresolved commitments and important contradictions visible.

**Acceptance**

A conversation containing one decision and one commitment yields two supported developments. A request is not upgraded to an approval. A repeated update creates no redundant development. Deferred proposals resurface. A changed target page or bound evidence causes an approval conflict. No scheduled job publishes a page.

Older-thread coverage must be explicit: refresh discovered changed threads and tracked unresolved threads, and report any connector limits on discovering replies to previously untracked old threads.

# Phase 4 Entity candidates

**Public changes**

- Add a persistent collector candidate queue with observed names, evidence references, proposed dimension, possible existing matches, priority and disposition.

- Keep candidates outside the authoritative facet vocabulary and out of exact facet filters.

- Provide owner actions to map to an existing value, explicitly create a value through the engine, reject, or defer.

- Record the owner decision before backfill. Backfill approved assignments through existing engine operations with retry receipts and an audit trail.

- Start ranking with active commitments, repeated retrieval needs and distinct identity; use mention volume as a secondary signal.

**Private setup**

Your dimensions, values, aliases, candidate evidence and business relevance rules remain private. Start with five candidate decisions per weekly review session.

**Acceptance**

Unknown names remain searchable in text without creating facets. Repeated discoveries consolidate conservatively. An owner-approved resolution can resume interrupted backfill without duplicate mutations. Rejected/deferred candidates retain their disposition and do not regenerate immediately as new proposals.

# Phase 5 Optional DM and mention discovery

**Public changes**

- Add an opt-in discovery adapter for owner-authored messages and mentions, feeding the same source registry.

- Retain useful discoveries outside selected channels while deduplicating overlap inside them.

- Extend native Slack authorization only for explicitly enabled capabilities. Existing selected-channel installations must retain their current permission scope.

- Treat discovery and admission as separate steps: finding a conversation does not authorize capturing it.

- Preserve workspace AI exclusions. An alternate connector is not permission to bypass them.

**Private setup**

Configure the owner identity, permitted DM/group-DM scope, exclusions, and privacy/processing policy. Enable only after the relevant access and policy checks succeed.

**Acceptance**

Public-channel overlap is processed once. A permitted DM development is captured with provenance. An excluded conversation remains excluded. Revoked access produces a visible coverage failure. An installation without DM opt-in makes no DM requests.

# Phase 6 Scheduling and private pilot

**Public changes**

- Provide a headless collector command, validated configuration, single-run locking, bounded retries, catch-up behavior, structured run receipts and a health command.

- Reuse the collector lifecycle. Choose one scheduling owner so a timer and a resident collector cannot independently process the same source.

- Include optional systemd and launchd examples; installing core must not enable either.

- Report source coverage, backlog age, partial failures, pending retries, captures, suppressed duplicates, token/cost use and checkpoint lag.

**Private rollout**

1. Snapshot the Brain and collector state and verify the supported restore path in a disposable copy.

2. Configure five to ten representative permitted channels; use synthetic or explicitly approved test DMs if phase 5 is enabled.

3. Run discovery and proposed outputs in preview mode before accepting writes.

4. Enable daily collection on that subset and an independent missed-heartbeat check. Use a daily deadline plus a two-hour grace period as the initial alert threshold.

5. Review daily results for seven days and complete one weekly publication/taxonomy review.

6. Expand in batches after passing the gates below. Trim low-value sources using measured results.

**Pilot gates**

- Zero observed duplicate accepted developments from retries or dual discovery in the test set.

- No unauthorized taxonomy creation, publication, or privacy widening.

- All injected failures are visible and recoverable; silent and partial failures are distinguishable.

- Every sampled development has resolvable evidence and accurate scope.

- Review takes no more than 20 minutes and important unresolved items remain visible.

- Record sampled recall against manually labeled source developments, precision, coverage, processing cost and backlog. Aim initially for at least 90% supported useful outputs and 90% recall of labeled substantive developments; treat a small sample as a pilot result, not a general guarantee.

If quality fails, narrow sources or repair extraction before increasing volume.

# Compatibility and rollback

For any durable schema change, include a migration, retry behavior, export/import treatment, and explicit handling by older readers. Preserve canonical evidence in the supported portable format. Keep collector checkpoints and operational queues in the documented private recovery backup.

If a rollout fails, disable new scheduling and pause admission, retain pending work and receipts, and inspect the failure. Do not delete queue files to force progress. Do not run an older binary against an incompatible migrated store. A restore must use the verified matching backup and supported runtime; accepted captures cannot be assumed selectively deletable.

Public tests and PR evidence use synthetic data only. Real channel names, customer taxonomy, message bodies, tokens, prompts containing private examples, and raw operational logs remain private.

# Completion checklist

- [ ] Installed and source feature boundaries are documented.

- [ ] Privacy behavior is verified through the actual capture path.

- [ ] Shared source identity, replay and checkpoint recovery pass.

- [ ] Developments retain provenance and semantic distinctions.

- [ ] Review proposals remain visible and require owner decisions.

- [ ] Entity candidates cannot become taxonomy without owner action.

- [ ] Optional DM coverage preserves existing permission boundaries.

- [ ] Daily operation, catch-up and independent health monitoring pass.

- [ ] The private pilot meets the agreed quality and review-time gates.

- [ ] Configuration examples, operator instructions and compatibility notes ship with the public changes.

Defer real-time capture, automatic canonical publication, automatic taxonomy creation, full query-language expansion, and broad historical backfill until this baseline is reliable.

# Repository references

- [Core source and collector boundary](https://github.com/cbolden15/open-brain/blob/main/docs/integrations/core-sources.md)

- [Slack integration and private policy](https://github.com/cbolden15/open-brain/blob/main/docs/integrations/slack.md)

- [Privacy model](https://github.com/cbolden15/open-brain/blob/main/docs/privacy-model.md)

- [Review and publication](https://github.com/cbolden15/open-brain/blob/main/docs/review-publication.md)

- [Collector recovery](https://github.com/cbolden15/open-brain/blob/main/docs/integrations/collector-recovery.md)

- [Owner-defined facets](https://github.com/cbolden15/open-brain/blob/main/docs/architecture/decisions/0020-independent-owner-defined-facets.md)

Repository behavior above reflects the inspection on October 14, 2026. Phase 0 pins the implementation baseline before work begins.
