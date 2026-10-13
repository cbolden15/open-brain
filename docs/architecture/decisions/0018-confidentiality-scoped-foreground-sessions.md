# ADR 0018: Confidentiality-scoped foreground sessions

Status: Accepted

Date: 2026-09-19

## Context

Open Brain is one unprivileged, single-user foreground application. Its inherited-stdio sessions
currently grant broad access selected when the process starts. The product needs narrower model and
capture sessions without adding a listener, daemon, multi-user service, or network authentication
stack to the core.

The new contract must preserve the Brain and record identity rules in
[ADR 0002](0002-brain-and-record-identity.md), capability narrowing and pre-ranking authorization in
[ADR 0004](0004-capability-authorization.md), information-flow propagation in
[ADR 0005](0005-information-flow-labels.md), and the single-sequencer rule in
[ADR 0007](0007-single-sequencer-fencing.md). It also must reuse the identifier encoding already
defined by [ADR 0014](0014-shared-record-import-envelope.md) without activating archived Secure Node
code or crossing the foreground package boundary in
[ADR 0016](0016-foreground-runtime-package-boundary.md).

This decision narrows the current [product contract](../../product-family.md) and
[privacy model](../../privacy-model.md) for future scoped sessions. Until the later implementation
phases land, those documents continue to describe shipping behavior.

## Decision

### Identity and generation vocabulary

The existing canonical Portable identity remains `tenant_<uuid>`. The durable Brain ID used by
session, delivery, and receipt contracts is derived deterministically as
`reencode(tenant_id, "tenant", "brn")`, using ADR 0014 exactly: parse the lowercase canonical UUID,
encode its 16 network-order bytes with RFC 4648 Base32, remove `=` padding, lowercase the result,
and prepend `brn_`. It is independent of path, inode, host, process, transport, and current
placement. The derivation is one-to-one; implementations must reject a malformed tenant identity
rather than invent a Brain ID.

Four values have separate meanings:

- `issuer_epoch` is a positive integer identifying the Brain's active commit issuer. This cutover
  provisions one stationary value and does not change it during ordinary startup or restart. A
  fresh post-cutover Brain persists its initial value once. An upgraded Brain persists one
  designated positive legacy value and one strictly greater stationary current value.
- `authorization_generation` is a nonnegative integer advanced by an effective grant, consent, or
  launcher-policy revocation or replacement. It invalidates older protected operations and cursors.
- `control_epoch` retains its existing source-control and intake coordination meaning.
- `fencing_epoch` retains its existing local writer-fence and migration meaning.

New schemas, canonical values, cursor bindings, and receipts use those full field names. No new
serialized field may be named only `epoch`. The stationary `issuer_epoch` is carried by launcher
policy, grants, commits, delivery envelopes, and receipts. It is not inferred from
`authorization_generation`, `control_epoch`, or `fencing_epoch`.

### Trust boundary and operation classes

A scoped session is single-user startup policy. It assumes an owner-controlled launcher and the
same trusted operating-system account as the Brain. It does not provide multi-user network
authorization and does not protect against a compromised launcher, a hostile process running as the
same user, or mutually untrusted operating-system users.

The launcher authenticates any transport peer outside Open Brain, selects one preconfigured generic
principal, and supplies one complete immutable policy before the foreground child begins serving
requests. Open Brain adds no transport authentication, listener, daemon, service lifecycle, or real
identity mapping. The MCP child reloads its trusted policy and any external-provider consent before
tool discovery and every tool call. A changed mapping or consent generation terminates that child.
Revocation of transport identity outside those files still requires the launcher to terminate the
active child and prevent a replacement child from starting with that mapping.

Owner-local operations are constructed from the local owner profile and do not accept owner status
from launcher policy or the client wire. They include initialization, owner capture and import,
unscoped owner retrieval, inbox and space administration, source routing, review, workspace and
graph administration, export, status, doctor, and consent administration.

A non-owner scoped session may expose only the retrieval and capture operations explicitly present
in its immutable startup capabilities. Inbox, space, source-route, review, workspace, graph, export,
status, doctor, and consent administration remain unavailable to it. Spaces remain organization,
not confidentiality authority.

### Effective authority and complete privacy

An immutable effective authority contains the principal ID, session ID, ordinary capabilities,
optional allowed space IDs, allowed read tiers, allowed capture tiers, egress mode, optional named
provider, optional active consent ID, `authorization_generation`, destination `brain_id`, expected
`issuer_epoch`, and whether the authority is the locally constructed owner authority. A caller may
only narrow this value. A wire request cannot add, replace, or widen any field. An individual
capture request may select one requested tier only from the startup policy's allowed capture tiers.

Every stored `PrivacyDecision` remains immutable evidence. Before use, the engine validates the
complete decision, including tier, reason, policy version, confirmation reference, `cloud`, and
`external_egress`. Missing, malformed, or internally inconsistent evidence is `unknown` and
local-only for scoped access; it remains owner-visible for later repair.

For a derived record, the engine combines every source revision used by that revision. Tier order
is `secret`, then `unknown`, then `personal`, then `work`, then `public`: the first tier present in
that order is the result. `cloud` and `external_egress` are each the logical intersection across all
source decisions. The result retains a deterministic set of every source decision digest and every
source confirmation reference as lineage evidence. It does not collapse those references into a
new consent. Proposal input, client metadata, frontmatter, space, title, or later consent cannot
lower the result.

Access is the intersection of the stored or derived decision, session tier sets, ordinary
capabilities, optional spaces, egress mode, current provider consent, destination Brain, and current
generations. Consent may narrow access but cannot turn a stored egress denial into permission. The
owner may inspect every tier through owner-local computation. No non-owner scoped principal may read
`secret` or `unknown` in this contract version.

### Owner consent lifecycle

Provider consent is absent by default. A consent record contains one opaque consent ID, named
provider, egress mode, exact allowed privacy tier set, active or revoked state, the
`authorization_generation` at which it was granted or revoked, and decision timestamps. It contains
no credential and no record content.

Only an owner-local CLI or owner-controlled desktop action may administer consent:

1. Grant creates one active binding and advances `authorization_generation` once. An idempotent
   duplicate returns the existing receipt and does not advance it.
2. Inspect returns only the consent metadata defined above and the current generation.
3. Replace atomically revokes the prior binding, creates a new consent ID, and advances the
   generation once. It never reactivates an old ID.
4. Revoke atomically marks the binding inactive and advances the generation once. An idempotent
   duplicate does not advance it.

Missing, malformed, stale, or multiply active consent authorizes no provider access. A running
provider-scoped session rechecks consent and generation before every protected operation. Older
cursors fail after a generation change. Revocation cannot recall bytes already returned.

The durable consent projection is deployment authority outside the Brain database. Its exact
canonical file is bound to the durable Brain ID and issuer epoch, lives in an owner-only directory,
and is replaced atomically. It persists records and operation-ID replay receipts across restarts.
It contains no provider credential or Brain content. The owner-local `consent` CLI is its only
administration surface; scoped MCP sessions cannot invoke it.

### `launcher-policy.v1`

`launcher-policy.v1` is a closed, versioned startup contract containing exactly the scoped
authority fields defined above, except that it cannot assert owner authority. Its egress mode is
either owner-local computation or one named external provider; the provider and active consent
binding are required only for the external-provider form. The policy binds one destination
`brain_id`, one expected `issuer_epoch`, and one `authorization_generation`.

The policy enters through a launcher-owned startup boundary, never through `t03.v1`, MCP tool
arguments, capture bodies, or another client request stream. Missing, malformed, stale,
destination-mismatched, or issuer-mismatched policy fails before any result, body, history, or
capture acceptance is returned. Client-supplied policy fields are rejected rather than merged.

The installed MCP boundary accepts the policy only through `--session-policy`. An external-provider
policy also requires `--consent-state`. The immutable effective authority used by every adapter is
the intersection of policy capabilities and launch-selected grants. Discovery and dispatch then
intersect that authority with injected implementations and the scoped-safe operation matrix.
`--capture-policy` is retained as a capture-submit compatibility alias and cannot coexist with
`--session-policy`. The alias normalizes the legacy capture-tier grant into the single
`capture-submit` capability before intersection. A whole-session policy receives no such implicit
grant: it must name `capture-submit` in `capabilities` and at least one allowed capture tier before
the operation is discoverable or callable.

The public conformance suite uses synthetic identities and providers to prove immutable mapping,
narrowing, requested-capture-tier bounds, destination and issuer binding, generation freshness,
consent revocation, client-field rejection, and fail-closed startup. Real transport identities and
their mappings remain deployment-private.

### Frozen retrieval and delivery contracts

The existing `t03.v1` request and response shapes remain byte-for-byte unchanged. Session policy is
startup-internal and does not become a retrieval wire field. Any later retrieval wire change
requires `t03.v2`, explicit negotiation, and no silent downgrade.

`outbox.v1` is a separate closed delivery contract. An immutable delivery envelope contains its
contract version, destination `brain_id`, expected `issuer_epoch`, stable delivery ID, request
digest, requested tier, fixed policy reference, and payload. The request digest covers the exact
canonical request value, including the contract version, destination, issuer epoch, delivery ID,
requested tier, policy reference, and payload. A terminal acceptance receipt binds status
(`accepted` or `duplicate`), destination `brain_id`, `issuer_epoch`, delivery ID, request digest,
and final admitted tier.

An outbox is not a Brain, cannot answer queries, and has no model or curation access. It removes a
body only after verifying the complete terminal receipt binding or after an explicit owner-confirmed
discard that retains a metadata-only terminal record. A malformed, wrong-Brain, wrong-issuer,
wrong-request, policy-mismatched, conflicted, expired, or attempt-exhausted item is quarantined and
is never automatically discarded. Queue persistence, limits, retry scheduling, crash recovery, and
the foreground transport adapter are later implementation work in an optional package.

### Portable Brain v5 and legacy issuer evidence

Portable Brain versions 1 through 4 remain immutable compatibility inputs. Portable Brain v5 will
preserve the durable `brain_id`, current stationary `issuer_epoch`, complete effective privacy and
lineage evidence, invalid-evidence markers, privacy repairs, a migration marker, and an append-only
legacy issuer binding manifest.

The legacy manifest assigns every pre-cutover manifest-declared Portable artifact to one designated
positive legacy `issuer_epoch`, strictly lower than the stationary current `issuer_epoch`, without
rewriting the artifact, its containing file, or any existing digest. Each binding key is the
artifact's normalized manifest path plus an optional zero-based JSONL ordinal. The ordinal is
absent for a non-JSONL artifact and required for each JSONL row. Its payload digest is SHA-256 over
the exact artifact bytes: the whole declared file for a non-JSONL artifact, or the exact row bytes
excluding the single structural LF separator for a JSONL row. Bindings are unique by
`(path, ordinal)`, sorted by path and then ordinal, and include the exact payload digest and
designated legacy `issuer_epoch`.

The migration marker binds the source Portable manifest digest, durable `brain_id`, designated
legacy `issuer_epoch`, current stationary `issuer_epoch`, and legacy binding-manifest digest. New
commits carry the current stationary issuer epoch directly. Import, export, restore, rebuild, and
repair must later preserve identical identity, epoch, privacy, and migration evidence.

This artifact binding is migration evidence for the pre-cutover Portable history. It does not
reinterpret every Portable artifact as a native protocol commit or alter its semantic family.

## Contract invariants

1. Brain identity is deterministic from the canonical tenant identity and never depends on
   placement.
2. `issuer_epoch`, `authorization_generation`, `control_epoch`, and `fencing_epoch` are distinct;
   new serialized contracts use no generic `epoch` field.
3. One stationary issuer accepts canonical commits. Every commit-capable operation later acquires
   the Brain-scoped writer fence for that issuer or returns a retryable result.
4. Launcher policy and owner consent can only narrow stored privacy and ordinary capabilities.
5. Scoped authorization is complete before candidate selection, ranking, counts, snippets, full
   reads, history materialization, or capture acceptance.
6. `t03.v1`, launcher policy, and outbox delivery are three separate versioned contracts.
7. Legacy epoch migration is append-only and preserves every pre-cutover payload and digest.
8. Active implementation remains in the foreground product packages. Archived Secure Node code is
   historical evidence only and cannot become an active dependency.

## Delivery boundary

This ADR freezes names, value semantics, version boundaries, and conformance obligations. It does
not add persistence, migrate existing state, change retrieval behavior, implement an outbox, or
claim runtime enforcement. Those changes occur in the later phases of the accepted cutover plan.

The cutover remains in one stationary issuer epoch. Owner-signed epoch certificates, signed cold
transfer, automatic failover, concurrent canonical writers, network authentication, and a
multi-user service are out of scope. A future cold-transfer implementation must satisfy ADR 0007
through a separate decision and cannot overload any generation defined here.

## Consequences

The foreground product gains a precise contract for later confidentiality-scoped sessions and
destination-bound offline capture without changing its transport or service posture. Authorization
and privacy logic have one engine-owned meaning, while launchers and optional outboxes receive
closed conformance contracts.

Implementations must carry more explicit identity, privacy, consent, and generation evidence.
Portable Brain v5 is required before that evidence can round-trip. Existing data requires
append-only legacy bindings rather than rewritten history.

Implementation status, 2026-09-21: foreground MCP and plugin sessions require one immutable
effective authority for their lifetime. Discovery and dispatch consume the same derived operation
registry. Owner entrypoints construct owner authority explicitly, while scoped capture can reach
only an injected bounded sink.

## Rejected alternatives

- Using the Brain root, inode, host, process, or transport address as Brain identity is rejected.
- Reusing `tenant_<uuid>` directly on contracts that require the role-distinct Brain identifier is
  rejected; the deterministic ADR 0014 `brn_` encoding is canonical.
- Naming every changing value `epoch` is rejected because issuer authority, authorization
  revocation, source control, and local fencing have different lifecycles.
- Accepting policy from the client wire or filtering unauthorized results after ranking is rejected.
- Treating consent as permission to broaden stored egress authority is rejected.
- Rewriting old payloads to add issuer fields is rejected; append-only migration evidence is the
  compatibility mechanism.
- Copying archived protocol, ledger, custody, service, or authorization code into the active product
  is rejected by ADR 0016.
