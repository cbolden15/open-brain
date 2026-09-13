# ADR 0009: BrainPack v2 portability

Status: Accepted

## Context

BrainPack is the implementation-independent portability boundary for a Brain. It must preserve
semantic history and erasure state without carrying a Node's credentials, authority, placement, or
rebuildable operational state. Import must prove origin, require destination authorization, and
avoid exposing tombstoned data.

## Decision

BrainPack v2 exports semantic Brain data: Brain identity; protocol and schema versions;
compartments and spaces; record envelopes; available encrypted payloads and blobs; typed links;
purge tombstones; artifact revision chains; proposals; decisions; effect receipts; and commit
ordering. Each referenced schema is bundled or identified by an immutable URI and verified hash.

Its manifest is RFC 8785 canonical JSON, signed by an export key whose certificate chains to the
Brain owner-authority key. The pack includes the signed public-key history needed to validate that
chain. Packs are recipient-encrypted by default. A deliberate plaintext export is explicitly
marked.

An importer trusts a signature chain only after it matches a previously pinned owner-authority
fingerprint or the owner explicitly approves that fingerprint on first import. The pack confers no
destination authority: import requires a destination grant, remaps compartments in the destination,
and imports no grants. A repeated record ID with the same digest is idempotent; the same ID with a
different digest is a blocking conflict. Tombstones are applied before any payload is indexed or
made available to a query surface.

## Contract invariants

- A v2 pack contains semantic records and history, not a Node-specific datastore image. Semantic
  inclusions are Brain identity, versioning, compartments, spaces, record envelopes, available
  encrypted payloads/blobs, links, tombstones, artifact histories, proposals, decisions, effect
  receipts, commit ordering, and verifiable schema references.
- A v2 pack excludes credentials, capability grants, secret handles, jobs, leases, checkpoints,
  indexes, host paths, process state, and other runtime operational state. Markdown may accompany a
  pack for convenience but remains rebuildable.
- The manifest uses RFC 8785 canonical JSON and is signed by a certified export key. The certified
  public-key history is included so an importer can validate the owner-authority chain.
- Recipient encryption is the default export mode. Plaintext is allowed only as an explicitly
  marked, deliberate export.
- Signature validity alone does not establish trust. Import requires an existing explicit trust pin
  or explicit owner approval of the owner-authority fingerprint on first import.
- Import never imports or creates grants and never bypasses destination authorization. It remaps
  source compartments under the destination's policy.
- Same record ID plus same digest is idempotent. Same record ID plus a different digest stops the
  import as a conflict; it does not overwrite or quarantine the existing semantic record silently.
- Imported tombstones suppress their payloads before projection indexing or query exposure.

## Consequences

Brain data can move between compliant implementations without product lock-in while retaining
provenance, semantic ordering, and erasure intent. Import is intentionally not a shortcut around
destination policy: operators must choose and authorize the destination, establish trust, and
resolve identity conflicts. A recipient that needs Markdown, search, or vectors rebuilds them from
the imported semantic history.

## Rejected or deferred alternatives

- Raw datastore, filesystem, or runtime-image export is rejected because it couples portability to
  one Node implementation and risks exporting operational secrets or placement.
- Implicit trust on first sight is rejected; first import requires explicit owner approval when no
  trust pin exists.
- Importing source grants or treating a source signature as destination authority is rejected.
- Plaintext-by-default export is rejected.
- Backup and restore behavior is deferred; BrainPack portability does not define those operations.
