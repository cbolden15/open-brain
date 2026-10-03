# Portable Brain v8

The historical-authority candidate uses local schema 13, runtime session 8 and
Portable 8. This is an unreleased contract. Product version `0.1.0` does not prove
compatibility. Schema 12/runtime 7 used Portable 7; those format interpretations
remain unchanged.

## Required history

Portable 8 requires `history/historical-authority/reconciliation-v1.json` in
addition to every required v7 sidecar. Even an empty registry is explicit. A
missing sidecar, omitted operation or disagreement with retained evidence refuses
validation. A v7 reader rejects the v8 manifest.

The closed sidecar contains the final historical registry and an ordered operation
chain. Each operation retains its typed request, exact receipt and previous,
proposed and transition digests. Registry snapshots reconstruct from those typed
operations and must match the original digests. This avoids repeatedly storing
every earlier membership while preserving the exact immutable transitions.
The sidecar has a 64 MiB input/output limit. Exceeding it refuses; it never drops
records or truncates a body.

Validation joins the chain to retained capture bytes, immutable privacy, request
aliases, source membership, baseline observation and namespace, both relation
sources and provider evidence. It proves historical correspondence. It does not
prove that an upstream file still exists, a selected binding is currently
installed, an old approval document is authentic, or independent custody exists.
The private reconciliation caller owns those proofs before original admission.

## Successors and current eligibility

A retained baseline can have a null ordinary revision key. Its separately proven
observed key can be the predecessor of the first actual successor. V8 checks that
reference without rewriting the old revision or source-admission sidecar. The v6
validator retains its strict ordinary-intake interpretation and refuses that
historical chain.

Export and restore preserve withdrawn, advanced and revoked history. They do not
require stale historical CAS witnesses to equal present source state. Current
read and publication eligibility still checks the independently settled registry,
exact SQL projection and present source witnesses. Within restored history that
includes withdrawal, advancement or revocation, replaying an old link receipt
cannot restore eligibility. An older archive cannot prove that it includes later
events. Disaster recovery must reconcile every accepted post-snapshot record
before activation; a snapshot alone is not a freshness or zero-loss proof.

## Clean restore

V8 also requires `history/capture-metadata/original-v1.json`. It preserves the
original settled capture row, including delivery/request identity, supplied title,
admission provenance, stored privacy, canonical allocation and file bytes. Its
closed, canonical representation is bounded to 64 MiB and sorted by capture ID.
Validation requires complete capture coverage and correspondence to archived
payloads, accepted receipts, immutable privacy evidence and record paths. Hidden
materialization and fresh semantic audits use this evidence, not inferred owner
metadata or import aliases. The unreleased v8 catalog digest commits to this
required sidecar; Portable1–7 interpretations remain unchanged.

This metadata is not a replay grant or a fresh audit of a live upstream source.
Pending ingestion still refuses ordinary Portable export. Complete pending-custody
recovery and independently authenticated post-baseline records remain necessary
for zero-loss deployment; settled capture metadata alone does not prove them.

The foreground `open-brain restore` command uses the same clean importer without
opening a primary Brain or generating a caller identity. It admits only trusted
owner-local authority and an owner-only absolute archive directory. Portable
5–8 retain explicit issuer evidence; this standalone entry point refuses earlier
formats rather than creating replacement issuer state. Existing engine task
imports keep their frozen format behavior. Both paths share hidden staging,
identity-preserving materialization, audits, no-replace promotion and exact retry
validation.

Managed delivery v1/v2 envelopes retain supplied capture titles even though the
frozen capture record has no title field. V8 materialization restores those titles
from validated terminal envelope evidence and refuses conflicting titles for one
capture. Portable1–7 materialization stays unchanged. Archive re-export equality
alone does not prove that restored runtime views preserve supplied metadata.

Import uses a new hidden stage, never an existing live database. Base records and
inherited authority restore first. Historical operations then use the forward
intent, pending fence, registry and SQL projection protocol. A fence becomes
complete only after its SQL projection commits. Interrupted stages are not
promoted; the original archive remains available for a clean retry.

Promotion requires fresh content, privacy, source, sharing and historical audits,
an index check and a schema-3 local ready record committing to restored semantic
state. A lost response after promotion returns a verified duplicate rather than
installing the history again. Retry audits precede ordinary engine recovery, so
missing or inconsistent authority cannot be silently repaired as part of retry.

Import installs no model-provider consent. An archived receipt is historical
truth, not a new approval, current eligibility grant or independently protected
custody acknowledgement. Existing consent and authorization must be supplied by
the current application boundary before external use. The core does not add
encryption or a background service; deployment must provide the required storage
protection separately.
