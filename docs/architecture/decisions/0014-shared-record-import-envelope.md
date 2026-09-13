# ADR 0014: Shared record import envelope

Status: accepted after independent review remediation

Date: 2026-09-07

## Context

Portable Brain v1 and Brain Protocol v1 were accepted before Open Brain and Secure Node became
separate product profiles. They agree on immutable records, provenance, and portable history, but
their wire vocabularies are not interchangeable.

Portable Brain uses UUIDv4 identities such as `tenant_...`, `capture_...`, and `page_...`. Secure
Node uses role-specific Base32 envelope identities such as `brn_...`, `rec_...`, and `pri_...`.
Portable history also contains `edited` decisions, publication records, routing records, readable
Markdown, JSONL source rows, and nested receipts that do not have one equivalent Secure Node state
transition. Treating those values as native Secure Node proposals, decisions, revisions, or effect
receipts would change their meaning. Importing the whole export as one opaque blob would preserve
bytes but lose record-level identity and provenance.

`CORE-W0` runs before Secure Node persistence. It therefore cannot prove a durable encrypted import
into a running node. It can freeze the common model, produce a pure import plan accepted by the
semantic kernel, and prove exact reconstruction from that plan. `UP1-W0` owns the later durable
fresh-node proof.

## Decision

The engine has a product-neutral shared record model between Portable Brain and Secure Node. A
shared record contains:

- the exact Portable semantic identity and family;
- its Portable schema identity, source path, and JSONL ordinal when applicable;
- its exact canonical source bytes and SHA-256 digest;
- its actor, time, space, and record-level provenance identities when present; and
- no grant, key, compartment, nonce, service, host, index, or projection state.

The model also carries exact attachments. `SharedBlob` represents content-addressed source blobs;
`SharedAttachment` represents other manifest-declared Portable files that have no stable semantic
record ID, currently owner Markdown outside canonical `page_*.md` pages. Neither kind receives an
invented record identity. `SharedImportEvidence` retains the exact manifest bytes and digest,
export ID, and ordered declared inventory. The validated Portable manifest binds the source
snapshot but is import evidence, not a semantic Brain record.

### Identity layers

The exact Portable identifier remains the shared semantic identity. Secure Node envelope IDs have a
separate role and never replace it.

- `reencode(source_id, expected_prefix, target_prefix)` parses the lowercase canonical UUID text
  after `expected_prefix + "_"`, encodes the UUID's 16 network-order bytes with RFC 4648 Base32,
  removes `=` padding, lowercases the result, and prepends `target_prefix + "_"`.
- The Secure Node Brain ID is `reencode(tenant_id, "tenant", "brn")`.
- A producer principal ID is `reencode(actor_id, "actor", "pri")`.
- For an imported record, encode the exact Portable `tenant_id`, semantic family, and semantic ID
  as UTF-8. Hash this byte sequence exactly:

  ```text
  b"open-brain:portable-to-secure-node:v1\x00"
  + tenant_id_utf8 + b"\x00"
  + family_utf8 + b"\x00"
  + semantic_id_utf8 + b"\x00"
  ```

  Encode the first 16 SHA-256 digest bytes as lowercase unpadded RFC 4648 Base32 and prepend
  `rec_`. Portable identifiers and family names cannot contain NUL, so the framing is unambiguous.
- The importer rejects any derived-ID collision before constructing a commit batch.
- The exact Portable ID appears in both the Secure Node record `origin_id` and its protected body.

This derivation hashes random semantic identifiers, not content. Content digests do not become
public record IDs. Different Portable families remain distinct even if their UUID portions happen
to match.

### Record mapping

Every Portable semantic item becomes an immutable Secure Node `record` whose type is
`portable_brain_v1.<family>`. Portable proposals, decisions, publications, actions, and routes are
historical shared records. They are not admitted into active Secure Node state machines.

The protected body follows one versioned shared-envelope schema. It carries the exact source bytes
as Base64, their digest, the semantic ID and family, the Portable schema URI, path, optional JSONL
ordinal, source Brain ID, actor ID, space ID, and exact Portable provenance IDs. Secure Node
provenance separately links the corresponding imported record envelopes so query, purge, and
inspection can follow the same directed history.

`brain.toml`, space Markdown, canonical pages, captures, event and measurement rows, proposals,
decisions, publications, actions, and routes each produce one shared record. A JSONL row keeps its
original zero-based ordinal; reconstruction restores rows in order and appends one LF per row.
Content-addressed blobs stay exact import attachments referenced by digest. Other Portable-valid
owner Markdown without canonical page frontmatter stays an exact path-and-digest attachment because
Portable v1 assigns it no stable semantic ID or provenance. Attachments do not create public record
IDs. The source manifest is retained only as import evidence.

Protocol provenance uses only these normative edges. Every other nested identity remains exact in
the protected bytes but is not a Secure Node lineage edge.

| Shared family | Secure Node source-record edges |
|---|---|
| `brain`, `space` | none |
| `event`, `measurement` | the same-family row named by `supersedes`, when present |
| `capture` | the event or measurement row named by a `batch` payload binding, when present |
| `page` | every capture ID in the page frontmatter `provenance` list |
| `proposal` | every ID in `capture_ids`; `sibling_context` is not lineage |
| `decision` | its proposal |
| `publication` | its decision and page |
| `action` | its proposal and decision |
| `route` | its capture and the prior route named by `supersedes`, when present |

Self references and sibling relationships are not lineage. Missing targets, cross-Brain targets,
or a cycle reject the mapping. Deterministic topological sorting breaks ties by source path, then
JSONL ordinal with non-JSONL records first, then family, then semantic ID.

### Import policy

The caller supplies an immutable `ImportEnvelopeContext` containing one canonical protocol
`observed_at`, a non-empty target compartment set, target policy digest, issuer epoch, sequencer
epoch, and exactly one caller-generated delivery ID per planned batch. Portable privacy metadata is
preserved exactly inside the shared body but is not silently translated into Secure Node
authorization history. Spaces remain organization, not authority.

All envelope `observed_at` values equal the context observation time. A Portable source timestamp
is copied to envelope `captured_at` only when its exact spelling conforms to the protocol timestamp
profile; otherwise `captured_at` is `null`. Every original timestamp, including arbitrary valid
Portable fractional precision, remains unchanged inside the protected body. The adapter never
truncates or invents a historical timestamp.

The pure adapter emits `pending` records and one or more bounded commit batches. The batches are
topologically ordered, contain at most the frozen protocol limit, and use import provenance. A
batch digest is RFC 8785 SHA-256 over the exact `commit-batch` wire object without an added digest
field. Reordering items therefore changes the digest.

For exports larger than one batch, conformance uses a private transient staging state. After the
kernel accepts one batch, the validator materializes its records in memory as
`unknown_historical`, advances the pure capacity snapshot, and validates the next batch. This state
is discarded, never returned as persisted evidence, and makes no encryption claim. `SN1-W2`
instead supplies verified encrypted accepted records. A conformance fixture must cross the
128-record boundary with a provenance edge.

The adapter creates no grant, receipt, key, nonce, job, service state, or encryption claim. Secure
Node persistence later encrypts accepted storage and records the receipt-bound import operation.

`SharedBlob` and `SharedAttachment` store exact path, declared digest, and bytes, including payloads
not referenced by a record. A payload of any Portable-valid size remains in the plan; later
persistence streams it in chunks no larger than the frozen staging limit. The canonical
full-import-plan digest covers the manifest evidence, ordered semantic record inventory and
envelope IDs, every attachment path, kind, digest and size, and every ordered batch delivery ID and
digest. Commit digests bind only protocol items. `UP1-W0` must define an import-control receipt that
binds the full-plan digest before it writes durable state.

The source Open Brain and source export stay unchanged. The test-only inverse reconstructs the
manifest-declared Portable file set from a trusted in-memory import plan. It is conformance proof,
not a user-facing Secure Node plaintext export capability.

## Required conformance

`CORE-W0` closes only when the checked-in full Portable Brain fixture proves all of these facts:

1. Every manifest-declared non-blob semantic item has exactly one shared record.
2. Every shared semantic ID and nested identity is preserved exactly.
3. Every record body validates against the shared-envelope schema and every emitted item validates
   against Brain Protocol v1.
4. The Secure Node semantic kernel accepts the ordered import batches from an empty Brain.
5. Reconstructing the Portable file set returns every manifest-declared byte exactly, including
   JSONL order and final line feeds; blob bytes match their declared digests.
6. Operational paths and state, source manifest metadata, grants, keys, credentials, and encryption
   claims do not enter the shared records.
7. Tampered bytes, digest mismatches, missing provenance, duplicate semantic identities, derived-ID
   collisions, and unsafe paths fail closed.
8. Valid high-precision Portable timestamps remain exact in protected bytes, and missing source
   timestamps never become invented history.
9. A fixture larger than 128 records validates sequentially with a provenance edge crossing the
   batch boundary.
10. The full-plan digest changes for omitted, extra, reordered, or changed records or blobs, and for
    changed batch context.
11. A Portable-valid owner Markdown file without a stable page ID remains byte-exact as an
    attachment and never receives a fabricated semantic or protocol record identity.

The durable fresh-node import, encrypted persistence, signed receipt, authorized comparison, abort
behavior, idempotent retry, and user-facing cleanup guidance remain `UP1-W0` and `UP1-W1` work.

## Alternatives

### Product-neutral shared model with Secure Node envelopes

Selected. This is the correct architecture because neither product's storage or authorization
model becomes the other's canonical record format. It preserves exact bytes and record-level
history while keeping Secure Node controls additive.

### Direct conversion into native Secure Node proposal and effect types

Rejected. The two contracts have different state vocabularies and required links. Coercion would
invent missing revisions, collapse `edited` outcomes, or make historical actions executable.

### One opaque export record

Rejected. It preserves an archive but hides record identity, provenance, compartment application,
query evidence, and purge closure from the Secure Node kernel.

### Change all protocol identifiers to Portable UUID strings

Rejected for v1. It would reopen the accepted role-distinct wire contract and still would not solve
the semantic mismatch between the history families. The shared identity remains exact inside the
envelope without weakening protocol ID validation.

## Consequences

Secure Node receives an honest imported-history representation and can add custody metadata without
claiming the source was encrypted. Open Brain keeps a small export contract and does not depend on
Secure Node code. Later import persistence must store both the protected envelope and exact payload
attachments, and later export must enforce Secure Node authorization and purge state before any
plaintext leaves the encrypted boundary.

## Review disposition

The first independent plan review returned `REWORK` with four P1 and one P2 finding. This revision
resolves them by defining envelope observation time, transient multi-batch validation, the complete
lineage table and tie-break, byte-exact ID derivation and batch context, and manifest/blob/full-plan
evidence. No implementation began before these corrections.
