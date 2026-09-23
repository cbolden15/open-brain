# Portable Brain v5

Schema-9 Brains export Portable Brain v5. A clean import restores the Brain's
identity, retained privacy values, privacy repair history, logical-source
history, and optional relationship and managed-workspace evidence. V1–v3
imports still use their existing restore path. Standalone v4 import remains
refused with `Portable v4 import is not supported`.

The manifest has the same nine keys as v4, with schema and layout version `5`,
contract version `"5"`, and compatibility versions `"1"` through `"5"`. Payload
hashes cover exact bytes. Canonical JSON has no trailing newline. V5 has one
catalog digest, whether relationship evidence is absent or present.

## Evidence

V5 adds three required sidecars to the unchanged v4 payload inventory:

- `history/privacy/effective-privacy-v1.json` preserves type-tagged retained
  values, immutable base projections, every invalid-evidence marker, exact
  repair receipts, and resolved revision/search assertions.
- `history/issuer/legacy-bindings-v1.json` preserves legacy artifact commitments.
- `history/issuer/migration-v1.json` preserves Brain identity, issuer epochs,
  identity time, and the optional migration marker with exact historical
  manifest bytes.

Schema 9 supports lossless NULL, TEXT, and BLOB privacy values. Its TEXT-affinity
column cannot preserve INTEGER or REAL storage classes, so full v5 archives
reject those tags. The pure value codec still defines them for conformance.
Repair sequence gaps are valid. Import preserves them, and the next owner repair
allocates the greatest existing sequence plus one. Replaying an imported
operation returns its exact receipt without advancing retrieval generation.

Historical migration hashes are commitments. They do not recover old mutable
files or prove unavailable JSONL row bytes. Current files may differ from the
historical manifest. An absent relationship sidecar remains absent; a present
empty sidecar remains present.

## Export and clean restore

Export holds the canonical writer fence and uses one explicit SQLite read
transaction for source, relationship, privacy, issuer, and managed evidence.
It regenerates exactly the three v5 sidecars from durable state and validates
the complete staged archive before promotion.

Portable export refuses while the schema-10 ingress journal has any pending or quarantined payload.
Run `open-brain journal status --json`, resolve retryable or quarantined items, and confirm the
summary reports zero retained ingress payloads before starting an export. The journal itself is
operational custody state and is not a Portable sidecar; exporting it would expose incomplete
captures and would not provide a canonical replay boundary.

Import first validates an immutable source snapshot. The hidden sibling stage
gets an empty schema-9 database seeded with the imported identity. Restore
consumes snapshot bytes, restores durable evidence, derives current search
state, and compares the resulting evidence with the archive. The audit binds
normalized capture content to the captured source, route, and publication
records, then derives expected search content from those verified captures and
captured canonical Markdown. Both the complete normalized capture projection
and authoritative search projection must match. The index is disposable and
rebuilt after authoritative state is restored.

Historical owner claims may contain a sorted unique subset of the current
owner capabilities. Tenant, actor, role and claim identifiers must still match.
Those artifact capabilities never grant current authority. Early publications
with `owner` trust metadata can retain it when their historical claim is a
strict subset, no modern review head exists, and current durable state derives
`reviewed`. Current search trust remains `reviewed`.

## Promotion and retries

V5 writes a local ready record with schema version `2`. It binds the import ID,
source manifest digest and identity, file materialization counts, exact
authoritative table counts, semantic-state digest, and index generation and
document count. It is local recovery evidence, not an exported sidecar.

The semantic digest covers issuer commitments, source and relationship
projections, retained privacy, immutable bases, every marker and repair, and
resolved revision/search privacy. It also covers normalized capture payloads,
search text, titles, provenance, source/owner/routing/publication fields, and
the complete authoritative search projection, including title, body, trust,
provenance, routing, timestamps, and resolved privacy. It excludes local paths,
SQLite/FTS row IDs, index and retrieval generations, generated relationship
replay columns, and the outer export ID/time.

The stage is reopened and checked before promotion, then checked again at the
rename boundary. Same-ID import retries repeat the semantic, source, managed,
row-count, and index checks before returning a duplicate receipt. The v5 archive
audit runs read-only before recovery-capable engine opening. Capture drift and
matching corruption in authoritative search and its index are rejected without
silently repairing the database, index, ready record, or archived files. Index
rows must also exactly match the authoritative search rows. A missing index fails retry
until rebuilt; deleting and rebuilding it from an unchanged imported Brain
restores generation 1. A later generation does not match the original ready
record. A changed import ID or source manifest conflicts.

Every failure before promotion removes the hidden stage and leaves no
destination. A failure after promotion leaves the complete destination, which
can be verified through a same-ID retry. Export/import/reopen/rederive/re-export
preserves every manifest-declared payload byte; only export ID and creation
time may change.
