# Portable Brain v2

Portable Brain v2 extends the unchanged Portable Brain v1 file set with managed-workspace history.
A Brain without managed-workspace state still exports v1 bytes and a v1 manifest. A Brain with one
managed workspace exports a v2 manifest and exactly one additional record:

```text
history/managed-workspace/workspace_<uuid>.json
```

The v2 manifest reports layout and schema version 2, contract version `2`, compatibility from `1`
through `2`, and the v2 schema-catalog digest. Every v1 content, source, history, blob, profile, and
Markdown byte is validated by the existing v1 reader. The v2 reader then validates the managed
record and its bindings to the canonical page IDs in that unchanged file set.

The managed record contains stable workspace and note IDs, complete accepted revision chains,
accepted links and evidence provenance, resolved conflict candidates, consent audit history,
restriction-preserving note sets, and charged provider budgets. It excludes workspace paths, relative
path mappings, filesystem identities, credentials, prompts, generated caches, pending suggestions,
and live or reserved inference requests.

Export fails before staging if an active note is missing or differs from its last materialized digest,
a conflict remains open, an inference request is reserved or dispatching, a budget reservation remains,
or the managed page set does not match the canonical snapshot. This prevents an export from silently
choosing between an owner edit and accepted engine state.

Clean import restores the managed record into the new Brain as detached state. It preserves stable
IDs, accepted bytes, links, resolved conflicts, exclusions, and used or uncertain budget charges.
Every imported consent is inactive, including consent that was active at export. No source host path
is reconstructed.

The owner attaches imported state by running `open-brain workspace setup` against the new Brain. The
engine regenerates note paths from the imported Brain's canonical page layout and writes accepted
bytes only for active notes. Inactive notes remain absent until the owner explicitly restores and
materializes them.

Portable Brain v1 remains the shared minimum contract. A v1-only reader may reject a v2 manifest;
it must not reinterpret or partially import the managed record. Use a v2-capable Open Brain release
to retain managed-workspace state.
