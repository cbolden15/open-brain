# Add confidentiality-scoped access and offline delivery to one canonical Brain

Status: planned

Date: 2026-09-19

Scope: open-source product behavior only. This plan must not contain real hostnames, account
identifiers, provider credentials, private source selections, private paths, or live acceptance
receipts.

## Fixed decisions

- One Brain is the only canonical store and commit authority.
- One Brain has exactly one active logical sequencer. Multiple foreground sessions may read
  concurrently. Any operation capable of producing a canonical commit must acquire the Brain-scoped
  writer fence for the current epoch; otherwise it fails with a retryable result.
- Version 1 uses a placement-independent durable Brain ID and carries the issuer epoch on grants,
  commits, delivery envelopes, and receipts. This cutover stays in one stationary epoch; moving the
  active Brain requires separate cold-transfer work.
- Upgrade binds pre-cutover commits to a designated legacy epoch through append-only migration
  evidence. It does not rewrite historical commit payloads or digests.
- Search is online-only against that Brain. Version 1 has no searchable replica or offline read
  cache.
- Capture may work offline through a bounded destination-bound outbox. An outbox is not a Brain and
  cannot answer searches.
- An outbox never discards an unaccepted body automatically. An item that exhausts its age or
  attempt limit stops retrying and remains quarantined until the owner explicitly retries or
  removes it.
- Enqueue reports success only after the complete item is durably stored. A full outbox returns a
  stable `outbox_full` result, never evicts or overwrites an existing item, and never reports
  `queued` for a body that was not persisted.
- Destination Brain ID and expected issuer epoch are immutable for an outbox item. Retargeting is an
  explicit owner conversion that creates a new envelope and delivery ID with provenance linking the
  original; it is never an in-place edit or automatic rollback behavior.
- Live SQLite files and managed Markdown are never synchronized between hosts.
- The foreground core remains an unprivileged local application. It gains no listener, daemon,
  service manager, or network authentication stack.
- Remote deployment authentication and the mapping from real devices or model providers to generic
  principals stay outside this repository.
- This repository owns the transport-neutral launcher-policy contract and synthetic conformance
  suite. Every private launcher and identity mapping must pass that suite before receiving real
  Brain access.
- Scoped sessions assume an owner-controlled launcher and operating-system account. They do not
  protect against a compromised launcher, a hostile process running as the same user, or mutually
  untrusted operating-system users.
- Non-owner scoped sessions may use only explicitly scoped retrieval and capture operations. Inbox,
  space, source-route, review, workspace, graph, export, and consent administration remain
  owner-only in version 1.
- Spaces remain organizational. Privacy tiers and egress consent are separate authorization inputs.
- Provider consent is absent by default and managed only through owner-local actions. Grant,
  replacement, and revocation are durable generation-changing decisions; scoped principals cannot
  administer consent.
- Export remains owner-only in version 1. Scoped principals cannot invoke it.
- Existing records without valid retained privacy evidence migrate individually to `unknown`. They
  remain owner-accessible for repair and unavailable to every scoped principal; one invalid record
  does not abort migration of unrelated records.
- Derived records inherit the most restrictive complete privacy decision from every source: tier and
  stored egress authority. Provider consent may narrow access but cannot turn a stored denial into an
  allowance.
- Canonical capture admission is bounded independently of client outboxes. Implementations expose
  explicit envelope/body/batch sizes, request-rate budgets, concurrent-admission and writer-queue
  bounds, and storage watermarks. A rejected or backpressured request cannot produce an accepted
  receipt or partial canonical record.
- A deployment may require independent protection before a receipt can authorize client body
  removal. The public capture boundary exposes a transport-neutral receipt-protection finalizer. A
  local commit awaiting that finalizer returns the stable retryable result `recovery_pending`, emits
  no accepted or duplicate body-removal receipt, and remains idempotent when the delivery is replayed.

These decisions preserve the single-sequencer and epoch-binding rules in
[ADR 0007](../architecture/decisions/0007-single-sequencer-fencing.md) and the foreground-only
boundary in the [product contract](../product-family.md). The first cutover does not implement or
claim signed cold transfer. They intentionally replace the product contract's current whole-Brain
MCP search grant with startup-scoped retrieval.

## Outcome

Open Brain can serve one canonical store to multiple foreground clients without disclosing records
outside each client's trusted startup policy. A client can be limited by privacy tier, model egress
class, provider consent, ordinary capability grants, and optional space filters. Those limits are
applied before search matching and again before complete record or history materialization.

An optional client-side component can retain captures while the canonical Brain is unavailable. It
retries with a stable delivery identity, accepts only a receipt bound to the intended Brain and
request, and removes the queued body only after that receipt is verified or the owner explicitly
discards a quarantined item. When the deployment requires independent protection, verification also
requires the terminal receipt to attest that the configured protection finalizer completed.

Completion requires all of the following:

1. A general model principal can read `public` and `work`, but cannot discover or read `personal`,
   `secret`, or `unknown` records.
2. A personal-capable local principal can read `public`, `work`, and `personal`, but cannot discover
   or read `secret` or `unknown` records.
3. A cloud-provider principal receives only tiers covered by both its read policy and active owner
   consent. Revocation invalidates new searches, record reads, history reads, and existing cursors.
4. Derived canonical records inherit the most restrictive effective privacy of every contributing
   source, including stored egress authority and consent eligibility. Search, full reads, history,
   managed Markdown, and owner-only export preserve that effective privacy. Export remains
   unavailable to scoped principals. Records without valid retained privacy evidence become
   `unknown`, remain owner-visible for repair, and stay hidden from scoped principals.
5. Offline capture survives restart and repeated delivery without duplicate commits. Queue contents
   are not indexed, searchable, or available to a model. Exhausted items remain in quarantine
   without automatic body deletion.

## Architecture choice

### First cutover: trusted foreground sessions

The first deployment uses the existing inherited-stdio boundary. A trusted launcher constructs a
session policy before `open-brain mcp` begins. The wire client cannot supply or widen its principal,
allowed read tiers, allowed capture tiers, egress mode, provider identity, consent reference,
capabilities, destination Brain ID, or expected issuer epoch. An individual capture request may name
one requested tier only within the launcher's allowed capture tiers.

In a remote deployment, an operator may bind a fixed launcher to an SSH key or another private
transport. That transport is deployment configuration, not part of the Open Brain trust claim.
Open Brain still sees one foreground process owned by the local operating-system user.

```text
capture client -> bounded outbox -> foreground stdio session -> canonical Brain
model client --------------------> foreground stdio session -> authorized retrieval
```

The selected implementation adds a generic session-policy input and transport-neutral outbox. It
does not add SSH configuration or real provider routes to this repository.

#### Launcher conformance contract

The public contract treats the launcher as a security-critical adapter without prescribing its
transport. A conforming launcher must authenticate its transport peer outside the core, select one
preconfigured generic principal, construct the complete immutable session policy before process
startup, and bind the session to the intended Brain and current authorization generation. Policy
fields received over the client wire are rejected rather than merged.

The public repository provides a synthetic conformance harness that proves all of the following:

1. Synthetic transport identities receive only their configured principal and policy.
2. A client cannot supply, replace, or widen identity, allowed tier sets, egress mode, provider,
   consent, capabilities, spaces, destination Brain ID, issuer epoch, or authorization generation.
   A requested capture tier is accepted only when it is inside the immutable allowed capture set.
3. Missing, malformed, stale, or destination-mismatched launcher policy fails closed before any
   search result, record body, history, or capture acceptance is returned.
4. Consent revocation is observed by an already-running session before its next protected operation,
   and old cursors fail under the new authorization generation.
5. Revoking a static launcher identity or grant terminates its active process and prevents a new
   session from starting with the revoked mapping.

Private deployment tests supply real transport authentication and mapping fixtures without adding
their identities, credentials, hostnames, or acceptance receipts to this repository.

### Proper architecture option

The larger long-term option is a separate `open-brain-hub` distribution with authenticated client
identities, capability issuance, revocation, network lifecycle, observability, and its own support
and conformance boundary. It would keep the local core unchanged and call the same engine tasks.

This option is pending a separate decision on authentication, service lifecycle, protocol version,
rate limits, upgrade policy, and whether a remotely hosted Brain remains a single-user product. It
is not required for the first cutover because forced-command transport can supply a fixed identity
without adding a listener to the core.

### Rejected for this cutover

- Synchronizing a live Brain with Syncthing, Git, Dropbox, NFS, or SMB.
- Running separate personal and general Brains.
- Letting a model receive all results and filter them after retrieval.
- Treating a space ID or Markdown path as a confidentiality boundary.
- Adding an encrypted read replica before real offline-search demand exists.

## Authorization model

### Stored privacy and session policy

`PrivacyDecision` remains immutable evidence attached to each accepted capture. Its tier and egress
authority are not replaced by a session grant.

A trusted retrieval session adds a separate effective policy with these fields:

- principal ID and session ID;
- ordinary capabilities such as `search`, `content-read`, and `history-read`;
- optional allowed space IDs;
- allowed read tiers and a separate allowed capture tier set;
- egress mode: owner-local computation or a named external provider;
- durable destination Brain ID and expected issuer epoch;
- active consent binding and authorization generation when provider egress is possible.

The effective allowed set is the intersection of stored privacy, session tiers, provider consent,
capability grants, and optional space filters. The owner authority may inspect all tiers locally.
No non-owner model principal can read `secret` or `unknown` in this version.

Provider consent is evaluated at read time. It may create a narrower effective decision for a named
provider without mutating or broadening the capture's original privacy record. Provider-and-tier
consent is necessary but not sufficient for external access: every contributing stored privacy
decision must also permit the requested egress. Consent removal must fail closed immediately and
invalidate cursors bound to the older authorization generation.

### Owner consent lifecycle

Consent records contain no provider credential or content. Administration is available only through
an owner-local CLI or owner-controlled desktop action, never through MCP or a scoped launcher
session.

1. **Grant:** The owner selects one named provider, egress mode, and exact allowed privacy tiers. A
   real state change atomically creates an active consent binding and advances the authorization
   generation once. An idempotent duplicate returns the existing receipt without advancing it.
2. **Inspect:** The owner can list the provider, egress mode, allowed tiers, opaque consent ID,
   active or revoked status, current generation, and decision timestamps without exposing a
   credential or record content.
3. **Replace:** Changing the provider, egress mode, or tier set atomically revokes the prior binding,
   creates a new binding, and advances the generation once. It never reactivates an old consent ID.
4. **Revoke:** Revocation atomically marks the binding inactive and advances the generation once.
   Already-running sessions recheck before their next protected operation, and older cursors fail.
5. **Fail closed:** Missing, malformed, stale, or multiply active consent state authorizes no provider
   retrieval. Later revocation cannot recall content already returned to a provider.

### Derived records

Effective privacy is calculated from all source revisions used to produce a canonical record:

- any `secret` input produces `secret`;
- otherwise any `unknown` input produces `unknown`;
- otherwise any `personal` input produces `personal`;
- otherwise any `work` input produces `work`;
- otherwise the result is `public`.

The tier result is only one part of the derived decision. The engine also computes `cloud` and
`external_egress` by logical intersection across every source authority. Source decision digests and
confirmation references are retained as a set of lineage evidence; they are never collapsed into one
new consent or treated as current authorization. Any explicit local-only source therefore keeps the
derived record local-only.

The calculation is deterministic and engine-owned. Proposal input, client metadata, frontmatter,
space selection, or title text cannot lower it. Historical revisions keep their original complete
effective privacy even if a later revision uses different sources.

Unauthorized objects return the same bounded `not_found` result as absent objects. Counts, titles,
snippets, cursor behavior, timing-oriented error variants, and relationship edges must not reveal
that a hidden record exists. Adding, removing, or changing an unauthorized record must not alter an
authorized client's visible result order, scores, pagination, cursor validity, page boundaries, or
continuation results. Opaque cursor token bytes are random and are not required to match.

## Public implementation plan

### Phase 1: freeze contracts and privacy semantics

- Add an ADR for confidentiality-scoped foreground sessions. State explicitly that this is
  single-user startup policy, not a multi-user network authorization claim.
- Extend `EffectiveAuthority` with immutable privacy and egress policy. Include every new field and
  the authorization generation in cursor bindings.
- Add engine helpers that validate stored `PrivacyDecision` values and combine the complete source
  privacy decision, including authority booleans and lineage evidence. Do not duplicate this logic
  in the app, collector, or transport.
- Define generic owner-consent records and owner-only grant, inspect, replace, and revoke operations
  for provider-and-tier retrieval. Consent identifiers are opaque, state-changing operations are
  idempotent, and provider names in fixtures are synthetic.
- Define the versioned launcher-policy schema and synthetic launcher conformance harness. The policy
  enters through a launcher-owned startup boundary, never through the client request stream.
- Keep the frozen `t03.v1` request and response shapes unchanged for scoped retrieval; session policy
  remains startup-internal. Define separate versioned launcher-policy and `outbox.v1` delivery
  contracts. Any future retrieval wire-field change requires `t03.v2`, explicit negotiation, and no
  silent downgrade.
- Define a durable Brain ID independent of path, inode, or host placement. Bind the stationary issuer
  epoch into launcher policy, grants, commits, delivery envelopes, and receipts. Signed epoch
  transfer remains outside this cutover and the base distribution.

Gate `OSS-G1`: contract tests prove narrowing, consent revocation, complete derived privacy, cursor
binding, unchanged `t03.v1` compatibility, launcher-policy immutability, durable destination binding,
stationary epoch binding, and fail-closed startup before storage or CLI work begins.

Execution status on 2026-09-19: `OSS-G1` passes in the working tree. `make verify` completed with
2,655 Python tests passed and five platform-dependent filesystem tests skipped, 550 JavaScript tests
passed, one desktop Python test passed, 26 Rust tests passed, and all static checks and package builds
green. Persistence, CLI wiring, and runtime enforcement remain later phases.

### Phase 2: persist and project effective privacy

- Add the next local schema migration for effective privacy on search documents and any missing
  canonical or history projection metadata. Keep capture privacy as the source of truth.
- Backfill existing rows by validating their retained capture records. Missing, malformed, or
  inconsistent evidence produces an `unknown` effective tier plus a durable invalid-evidence marker
  and reason for that record. Never infer privacy from a path, space, title, or content.
- Keep invalid-evidence records owner-readable and owner-exportable for repair while denying them to
  every scoped principal. Repair appends an immutable record keyed to the target record or revision
  and invalid-evidence digest, with owner actor, replacement decision, operation ID, timestamp, and
  optional superseded repair. The latest valid non-superseded repair in canonical ledger order wins;
  retained invalid evidence is never rewritten. Continue migrating unrelated records.
- Update canonical publication and patch application so every revision records the deterministic
  combined privacy of its source set.
- Add the next Portable Brain schema version for invalid-evidence markers, privacy repairs, complete
  derived authority, and their lineage. Preserve original evidence and deterministic repair
  precedence across export, import, restore, and rebuild.
- Add an append-only legacy-epoch binding manifest keyed by historical commit identity and digest.
  Assign every pre-cutover commit to the designated legacy epoch without rewriting it; new commits
  carry the stationary current epoch. Portable Brain preserves the durable Brain ID, current epoch,
  legacy binding manifest, and migration marker.
- Make rebuild, reconcile, import, restore, and projection-health checks detect privacy drift.

Gate `OSS-G2`: migration fixtures cover valid, missing, malformed, and inconsistent retained
privacy evidence. Invalid-evidence records round-trip and rebuild as `unknown` with the same marker,
remain owner-accessible, stay hidden from every scoped principal, and do not block unrelated rows.
Repaired records round-trip with immutable original evidence and identical supersession results. The
pre-migration ledger, legacy commit-to-epoch bindings, export/import round trips, and projection
rebuilds produce identical complete effective policy and epoch evidence for every record and
historical revision.

### Phase 3: enforce authorization before retrieval

- Filter the materialized authorized row set by effective privacy before the FTS match in paged and
  legacy search.
- Compute ranking from the authorized corpus or from a corpus-independent deterministic score. BM25
  or other statistics over hidden rows cannot influence visible scores, ordering, pagination, or
  cursors.
- Recheck policy in `RecordProjector` before reading source bytes, blobs, canonical Markdown,
  retained history, relationships, or provenance.
- Bind cursors to the full effective policy and consent generation. A changed grant or revoked
  consent returns `cursor_stale` or a single documented authorization-safe error.
- Replace global retrieval-generation freshness for scoped cursors with an authorization-visible
  generation or digest. A write that changes only hidden records must not stale an existing scoped
  cursor or change its continuation; owner cursors may continue using whole-Brain freshness.
- Recheck the current consent binding and generation before every provider-scoped search, record
  read, history read, and cursor continuation. Keep all consent administration unavailable through
  MCP and scoped launcher sessions.
- Apply the same rules to legacy `brain_search`, negotiated search and read operations, workspace
  projections, and any agent-facing retrieval adapter. A compatibility surface cannot remain a
  whole-Brain bypass.
- Inventory every MCP and plugin operation. In version 1, scoped non-owner sessions deny inbox,
  space, source-route, review, workspace, graph, export, and consent operations before lookup or
  materialization; their counts, names, status, and mutation errors cannot disclose hidden state.
- Replace the exclusive read path with a Brain-scoped reader/writer lease. Retrieval holds a shared
  lease across its SQLite snapshot and all source, blob, and canonical Markdown materialization;
  commit-capable work holds the exclusive current-epoch writer lease. Cursor allocation and mutation
  use a separate short cursor-state lock.
- Keep export owner-only and unavailable through scoped sessions. Owner exports must preserve the
  original and effective privacy evidence for every record and historical revision.
- Keep owner CLI behavior unchanged unless an explicit scoped mode is selected.

Negative tests must place a matching query term only in an unauthorized record and prove that no
result, count, title, revision ID, cursor, relationship, or differentiated error escapes. Paired
datasets that differ only in unauthorized records must return identical visible scores, ordering,
pagination, cursor validity, page boundaries, and continuation results. Tests compare cursor
semantics, not random opaque token bytes, and include a live cursor across a hidden-only mutation.

Gate `OSS-G3`: the complete principal-by-tier matrix passes for search, fetch, full read, history,
relationships, stale cursors, mixed-source authority, and every MCP/plugin operation. Two readers can
overlap under shared leases, a reader and writer preserve the combined SQLite-plus-filesystem
snapshot and exclusive fence invariants, and concurrent cursor allocation remains unique. Every
scoped principal is denied export, and owner exports preserve original and effective privacy
evidence. Consent tests cover absent state, grant, inspection, idempotent duplicate, atomic
replacement, revocation, restart, multiple-active corruption, active sessions, and stale cursors.

### Phase 4: add explicit capture classification and remote admission

- Keep current no-flag local capture and Markdown import behavior compatible. Existing callers must
  not become less private after an upgrade.
- Add an owner-only explicit privacy selection for manual capture and Markdown-root import. A single
  import invocation has one fixed tier unless a validated owner manifest supplies per-root policy.
- Add a bounded capture-submission operation for destination-bound clients. The trusted startup
  policy fixes allowed capture tiers. A request may select one tier inside that set; a missing tier
  becomes `unknown`, and an out-of-set tier is rejected before acceptance.
- Rescan admitted content at the canonical boundary. Secret detection or missing/invalid
  classification may narrow a submission to `secret` or `unknown`; it may never broaden one.
- Include the requested tier in the immutable request digest. Bind the final admitted tier separately
  in the terminal receipt. Preserve source identity, provenance, delivery ID, destination Brain ID,
  issuer epoch, and exact receipt binding.
- Enforce configured maximum envelope, body, and batch bytes before materialization; per-principal
  request-rate and concurrent-admission limits before writer acquisition; bounded writer waiters;
  and high/critical storage watermarks before commit. Return stable retryable or terminal admission
  results with no partial record and no accepted receipt. Recovery below a threshold must not require
  process restart.
- Add a transport-neutral receipt-protection finalizer that deployments may require after the local
  canonical commit and before terminal receipt release. Bind its acknowledgement to the durable
  Brain ID, issuer epoch, delivery ID, request digest, final admitted tier, and record/revision
  identity. A finalizer timeout or failure returns `recovery_pending`; replay retries protection for
  the existing commit and cannot create a second record.
- Keep real source-to-tier mappings out of defaults, examples, tests, logs, and documentation.

Gate `OSS-G4`: public, work, personal, secret, and unknown submissions produce the expected durable
privacy; replay is idempotent; a reused delivery ID with different bytes conflicts; and a remote
client cannot choose a tier outside its startup policy. Missing and out-of-set tier cases follow the
documented `unknown` and rejection behavior. Boundary tests cover every size, rate, concurrency,
writer-queue, and storage-watermark limit and prove that no rejected request leaves partial state or
an accepted receipt. Finalizer tests cover success, timeout, failure, crash after local commit,
replay after restart, mismatched acknowledgement, and recovery without duplicate canonical state.

Execution status on 2026-09-21: `OSS-G4` passes on the working branch. Admission limits, stable
admission results, owner explicit tiers, the import manifest, narrow-only boundary classification,
and the destination-bound submission bound to the trusted startup policy are implemented with
synthetic tests; `make verify`, the native audit, and the Homebrew smoke pass. Batch bounds move to
the outbox phase, and the receipt-protection finalizer remains an optional deployment requirement
that no shipped path configures.

### Phase 5: implement the bounded offline outbox

Place the reusable outbox in an optional package, not the base core dependency graph. It stores one
versioned delivery envelope containing durable destination Brain ID, expected issuer epoch, request
digest, stable delivery ID, requested tier, fixed policy reference, payload, attempts, and terminal
receipt metadata.

Required behavior:

- owner-only directory and file permissions where the platform supports them;
- atomic enqueue and state transitions, including file and parent-directory sync where the platform
  exposes those primitives; enqueue success means the complete item survives process restart;
- strict item and byte limits with a visible `outbox_full` failure; the full path writes no partial
  item, reports no queued success, and never evicts or overwrites an existing active or quarantined
  body; reserved headroom is included so terminal metadata can still be persisted safely;
- one drain lease per outbox and deterministic retry order;
- receipt verification against durable destination Brain ID, issuer epoch, delivery ID, request
  digest, final admitted tier, and required protection-finalizer acknowledgement; stale-epoch or
  unprotected receipts never authorize body removal;
- body removal only after a verified accepted or duplicate receipt, or an explicit owner-confirmed
  discard that leaves a metadata-only terminal record; `recovery_pending` retains the body and
  remains in the bounded retry schedule;
- quarantine on age or attempt exhaustion, delivery conflict, malformed receipt, wrong Brain, or
  policy mismatch; quarantined bodies stop automatic retries and continue counting against item and
  byte capacity until resolved;
- an explicit owner retry starts a new bounded age-and-attempt window while preserving the original
  enqueue time, delivery ID, destination, request digest, and prior-attempt metadata;
- no automatic destination rewrite or retry against a different Brain. An explicit owner conversion
  creates a new immutable envelope and delivery ID, records lineage to the original item, and leaves
  the original quarantined or retained until reconciliation accepts the conversion;
- no FTS index, query API, model access, curation, canonical history, or background listener;
- metadata-only status and logs report the quarantine reason, consumed capacity, and available owner
  actions without exposing payload content.

Expose a transport protocol and a foreground stdio process adapter. The transport receives an
already constructed immutable delivery and returns bounded receipt bytes. Network setup,
credentials, hostnames, and launcher commands remain private deployment inputs.

Gate `OSS-G5`: crash tests cover every boundary from enqueue through body cleanup. Restart, timeout,
duplicate reply, mismatched Brain, partial write, concurrent drain, queue exhaustion, age exhaustion,
attempt exhaustion, stale epoch, owner retry, explicit destination conversion, and owner-confirmed
discard preserve exactly-once canonical acceptance, a recoverable quarantined item, or an explicit
metadata-only terminal record. Full-capacity tests prove no eviction, overwrite, partial item, or
false queued result, including across restart at every atomic-write boundary. When receipt protection
is required, crash tests also prove that a locally committed but unprotected delivery retains its
client body and becomes removable only after replay obtains a protected terminal receipt.

### Phase 6: documentation and release verification

- Update `docs/product-family.md`, `docs/privacy-model.md`, `docs/capture-contract.md`,
  `docs/import.md`, `docs/records-and-history.md`, agent setup documentation, and the feature matrix.
- Document that model access is an intersection of stored privacy, trusted session policy, and
  provider consent. Do not describe the feature as hostile same-user isolation.
- Document canonical admission limits, stable backpressure results, durable enqueue semantics,
  `outbox_full`, `recovery_pending`, the optional receipt-protection finalizer, and explicit
  destination conversion. Examples must not imply that queueing succeeded before durable storage,
  that a configured independent-protection requirement can be bypassed, or that rollback can
  retarget an existing item.
- Add synthetic examples only. Use generic principal and provider names.
- Record a disposable-Brain acceptance matrix for general, personal-capable local, consented cloud,
  revoked cloud, and owner sessions, including consent lifecycle receipts and generations,
  owner-only export, and scoped-session export denial.
- Run focused tests during each phase, then `make verify`, `git diff --check`, and
  `actionlint .github/workflows/ci.yml`.
- Because CLI and packaged optional components change, run `make native-audit` and
  `make homebrew-smoke`. Run the repository's exact desktop/package checks if those surfaces change.

Gate `OSS-G6`: all checks pass on the exact candidate commit, built artifacts contain only the
declared packages, the synthetic launcher conformance suite passes, and the public evidence contains
no private deployment data.

## Likely implementation surfaces

- `packages/engine/src/open_brain_engine/core/models.py`
- `packages/engine/src/open_brain_engine/engine/t03_contracts.py` or a successor contract
- `packages/engine/src/open_brain_engine/engine/paging.py`
- `packages/engine/src/open_brain_engine/engine/records.py`
- `packages/engine/src/open_brain_engine/engine/search_projection.py`
- `packages/engine/src/open_brain_engine/engine/source_schema.py`
- `packages/engine/src/open_brain_engine/engine/spaces.py`
- `packages/engine/src/open_brain_engine/engine/cursors.py`
- `packages/engine/src/open_brain_engine/storage/locks.py`
- `packages/engine/src/open_brain_engine/engine/local_schema_catalog.py`
- `packages/engine/src/open_brain_engine/engine/managed_policy.py` or a successor generic consent task
- `packages/engine/src/open_brain_engine/engine/markdown_import.py`
- `packages/app/src/open_brain/services/t03_adapters.py`
- `packages/app/src/open_brain/services/local_mcp.py`
- `packages/app/src/open_brain/services/local_entrypoints.py`
- `packages/connectors` or another optional package for outbox contracts and foreground draining

Exact ownership should be confirmed against the candidate base before implementation. Do not copy
archived Secure Node authorization or service code into the active core.

## Private cutover dependency

The private deployment may prepare inventories and synthetic transport while this plan runs, but it
must not redirect real capture or model retrieval until `OSS-G6` identifies an exact verified
artifact. The private cutover records that artifact digest and treats any later build as a new
candidate requiring re-verification. Before redirecting a real client, the private cutover must also
record that its launcher and identity mapping passed the public conformance suite, including active
revocation, without copying private fixtures or receipts into this repository.

Estimated implementation: 10 to 15 working days for the required schema, Portable Brain, ranking,
operation-surface, and lock changes; 15 to 20 working days if authorized-corpus ranking needs a new
index design or verification exposes a required `t03.v2` contract.
