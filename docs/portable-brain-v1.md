# Portable Brain v1

Portable Brain v1 is the versioned interchange contract for one single-user Brain root. The
engine implements validation, export, clean-root import, and disposable index rebuild over this
layout. Portable operations preserve identities, exact source/canonical/history bytes, review
chains, and append-only routing. They exclude operational state and do not provide daemon,
backup/restore, upgrade, uninstall, provider, or hosted-runtime orchestration.

## Product-family role

Portable Brain v1 is the shared minimum record and export format for Open Brain and Secure Node.
The default product can create a complete export without Secure Node installed. Secure Node must
accept that export as its upgrade input and preserve stable Brain, actor, space, capture, source,
proposal, decision, publication, action, and provenance identities plus every exact portable byte.

Secure Node grants, compartments, key envelopes, nonces, sequencer state, service state, and
projection checkpoints are not silently invented as historical facts. A Secure Node import applies
new custody and authorization policy in a separate receipt-bound operation and records that the
protection starts at import. It never claims that the source Open Brain data had application-level
encryption.

BrainPack v2 is a Secure Node envelope and advanced semantic profile. Before Secure Node persistence
advances, a conformance mapping must prove that its shared semantic records round-trip through this
Portable Brain v1 profile without identity, provenance, or byte drift. Default-to-Secure-Node
upgrade uses this export/import boundary, not a copy or reinterpretation of live SQLite files.

The shared mapping distinguishes semantic IDs from Secure Node envelope IDs. Portable IDs remain
the stable semantic identities and are preserved exactly inside protected imported records. Secure
Node's role-specific IDs identify its protocol envelopes. Re-encoding or deriving an envelope ID
does not rename the Portable record. The complete mapping is specified by ADR 0014 and the
`CORE-W0` plan.

## Layout

```text
<brain-root>/
  brain.toml
  content/spaces/<space-slug>/_space.md
  content/spaces/<space-slug>/... Markdown pages
  sources/captures/YYYY/MM/<capture-id>.json
  sources/batches/YYYY/MM/<batch-id>.jsonl
  sources/blobs/sha256/<first-two-hex>/<sha256>
  history/{proposals,decisions,publications,actions,routes}/YYYY/MM/<record-id>.json
  portable-manifest.json
  .open-brain/{state,indexes,run,credentials}/  # operational; never exported
```

Date partitions use immutable UTC acceptance or recording timestamps persisted with the record,
not the local wall-clock month. Source occurrence-time
corrections append a new event or measurement batch row with `supersedes`; they do not move
or rewrite the original record.

## Serialization

Portable JSON records are UTF-8 canonical JSON with NFC strings and keys, sorted keys, no
insignificant whitespace, and no final newline. `portable_canonical_json_bytes` rejects floats,
non-string object keys, and NFC-normalized key collisions. `canonical_json_bytes` remains the
legacy serializer and continues to support existing float/vector callers. JSONL uses strict
Portable record bytes plus one LF for every row; a complete file ends in LF. Structured JSON and
Markdown frontmatter use snake_case field names.

Portable IDs are lowercase UUIDv4 values with a type prefix. They are generated once and do not
depend on a name, slug, path, timestamp, or payload. `space_id` in Markdown frontmatter remains
unchanged when the mutable space name or directory slug changes.

Measurement `value` is a fixed-point decimal string with at most 128 decimal digits. Values such
as `0.5` and `-0.5` are valid; JSON numeric values, exponents, leading zeros, trailing fractional
zeros, and negative zero are invalid. Role claims are immutable snapshots containing their stable
claim and role IDs plus a sorted, unique capability list. Their tenant and actor IDs bind to the
enclosing record. Receipt IDs bind to one subject, and each receipt digest covers its strict
canonical `payload` object. Reusing a receipt ID with different bytes is invalid. Routing an
existing capture appends a route record. Later routes link to the route they supersede, so export
and import preserve current space membership without rewriting the immutable source record.

## Schemas and fixture evidence

The 15 Draft 2020-12 schemas are under `schemas/portable-brain/v1/`. They have immutable v1
URN identifiers and only local URN references. The conformance suite registers every schema with
`jsonschema.Draft202012Validator` and format checking.

The repository and both default Python artifacts include a synthetic populated root under
`tests/fixtures/portable-brain/v1/brain-root`. It covers all four capture payload families,
blob-backed originals, batch-backed event and measurement payloads, readable page frontmatter,
page and action proposal/decision chains, routed space membership, publication and action results,
and a SHA-256 manifest.
The test factory reproduces this fixture byte for byte. Operational state is created only in a
temporary test root and is excluded from exact-byte export; no `.open-brain` file is checked in.

The manifest has an export ID, layout and schema versions, contract version and compatibility
range, and a schema catalog digest. It lists every portable file except itself in sorted unique
normalized-relative path order. Each listed digest is the SHA-256 hash of the exact stored bytes.
A v1 schema catalog digest is SHA-256 over strict canonical JSON mapping each schema filename to
the SHA-256 digest of that schema's exact bytes. The runtime compares the manifest value with the
frozen v1 catalog digest.
A conforming import/export must reject a missing file, changed checksum, traversal or absolute
path, operational path, symlink, incompatible version, or non-canonical bytes. Runtime validation
also fails closed on mismatched original, normalized, blob, batch, role-claim, receipt, evidence,
proposal-state, decision, publication-content, action-request, or action-result bindings.

For each decision, `expected_state_digest` covers the exact canonical proposal bytes and
`expected_receipt` repeats the proposal-creation receipt. `terminal_digest` covers the decision ID,
proposal ID, outcome, expected state digest, and optional edited-content digest. A publication's
bytes must equal the decision's effective page content and the exact file at `published_path`. An
action request must equal the effective approved action proposal; its approval receipt binds the
action ID, decision ID, and request digest, while the result has its own canonical digest.

For upgrade comparison, “exact bytes” means every file listed by the validated source manifest.
The source manifest itself is export evidence, not a canonical Brain record; a later export creates
a new export ID and timestamp. JSONL rows retain their original order and terminating line feeds.
Blob bytes remain digest-verified payload attachments rather than content-derived public IDs.
Owner Markdown that lacks canonical page frontmatter has no Portable semantic ID; an upgrade keeps
its exact path, digest, and bytes as an attachment instead of inventing a path-based record ID.

## Engine operations

`PortabilityTask.validate(source)` validates one existing Portable root and returns a bounded
receipt. `export(destination, export_id=...)` snapshots the live portable files, validates that
immutable snapshot, stages a new destination, and promotes it only after exact-byte verification.
`import_clean(source, destination, import_id=...)` validates one source snapshot, materializes a
new clean root, restores the non-secret identity in `brain.toml`, rebuilds the disposable index,
records retry evidence, and promotes the same validated snapshot. A conflicting or non-empty
destination fails closed. Repeating an already completed operation returns a duplicate receipt.
`rebuild_index()` updates only disposable index state and returns its generation in a bounded
receipt.

The export/import boundary excludes `.open-brain` credentials, SQLite databases, leases, run
state, and indexes. Operational receipts may record counts and index generation, but never expose
host paths or secret values. Validation, materialization, and promotion all use the same immutable
snapshot; a later pathname read cannot substitute different bytes.

## Conformance boundary

`tests/contract/test_portable_brain_v1.py` verifies schemas, lossless capture envelopes, strict
canonical bytes, JSONL corrections, same-family supersession, cross-record receipt and digest
chains, canonical-page privacy, root confinement, symlink rejection, operational exclusion, the
complete checked-in fixture, semantic tamper rejection, and an exact-byte clean-root round trip.
These helpers cover the engine contract. Phase 3 composes backup/restore, upgrade, and uninstall as
separate app-owned source-checkout lifecycles without changing Portable Brain semantics. Hosted
runtime and native-artifact evidence remain deferred.
