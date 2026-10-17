# Portable Brain16

The development candidate pairs Portable16 with local schema21 and runtime
session16. It inherits every Portable15 file and its bytes unchanged, and adds one
evidence family for the owner-local embedding policy. Portable1–15 validators and
migrations1–20 keep their original bytes and meanings. Portable15 and older readers
refuse a Portable16 snapshot before any destination write; they do not parse its
inherited subset. Clients upgrade together under exclusive runtime admission.

| Family | Contents |
|---|---|
| `history/embedding-policy/embedding-policy.jsonl` (`ob.embedding-policy.v1`) | One canonical JSON row per owner policy transition: `action` (`enable` or `disable`), `changed_at`, `component_id` (64 lowercase hex for `enable`, `null` for `disable`), `contract` and `version`. |

The file is required and may be empty. Every line must be canonical JSON ending in a
newline, rows have exactly those keys, and versions increase strictly. Anything else
refuses before destination mutation. The manifest inventory covers the file like
every other payload.

Only transition history travels. The live policy row, its policy generation,
embedding caches, vectors, query data and helper staging are host-local and never
enter an archive; the `.open-brain/cache/embedding-v1/` tree is outside the exact
Portable inventory.

Restore installs the validated history and always leaves the destination policy
disabled: `enabled` is 0, `component_id` is null, and its consent version is one
greater than the highest restored version. A later owner `enable` continues from
there. Restore never reactivates local inference, and it does not reuse the managed
provider-consent restore path. Portable15 and older inputs restore with empty
embedding history and a disabled policy at consent version0.

Schema21 adds the `embedding_policy` row (one per Brain, bound to the stationary
Brain ID and issuer epoch) and the append-only `embedding_policy_history` table.
Migration from schema20 creates both with the policy disabled. Only the owner-local
CLI changes the policy:

```sh
open-brain embedding status --json
open-brain embedding enable --component-id <64 lowercase hex> --json
open-brain embedding disable --json
```

Each command prints one object with exactly `schema`
(`ob.embedding-policy-status.v1`), `enabled`, `component_id` and `consent_version`;
`status` also adds `embedding_health`. Every enable or disable bumps the consent
version and policy generation and appends history in one transaction. No MCP tool,
plugin operation or desktop surface reads or changes this policy.

See [offline semantic search](semantic-search.md) for the cache, modes and health states.

The exact catalog is in
`packages/engine/src/open_brain_engine/portable/v16_catalog.py`.
