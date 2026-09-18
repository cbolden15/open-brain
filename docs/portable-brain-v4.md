# Portable Brain v4

The M2 source build exports Portable Brain v4. It preserves retained capture, proposal, review,
publication and managed-workspace evidence in their established byte formats. Current storage uses
schema 7 and runtime session 2; those versions are separate from the Portable format version.

`sources/logical-sources.json` supplies versioned source identity, immutable revision membership,
proven predecessors, current heads and ordered canonical revision membership. Capture IDs and
retained source bytes remain unchanged. Historical captures without proven identity links stay
independent and carry diagnostics; equal content, URLs or timestamps do not establish lineage.

Once relationship decisions exist, `history/relationships/decisions-v1.json` records the exact
revision endpoints, relationship state and every accepted, rejected or removed decision. This
sidecar has its own version 1 and an explicitly supported Portable v4 catalog digest. Source-only
v4 exports retain their original catalog digest and remain valid. A reader without the relationship
catalog refuses the newer export rather than silently omitting its evidence. Validation checks
endpoint membership, decision sequence, state/version consistency and accepted supersession cycles.

Create and validate an export in a new destination:

```sh
open-brain export /absolute/path/to/new-export --verify --json
```

The manifest inventories exported paths and SHA-256 digests. Export contains no operational cursor
signing material or disposable continuation state. Historical Portable v1–v3 imports remain supported.
New Portable v4 restore/import is outside M2 and is refused; export validation is not a restore
receipt. See [records and history](records-and-history.md) for reading retained revisions and
[Portable v3](portable-brain-v3.md) for the preserved review-binding format.
