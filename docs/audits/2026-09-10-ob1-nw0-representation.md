# Native workspace representation decision

Decision scope: NW0 representation and first-release write policy only. Full NW0 remains incomplete.

## Decision

Use a sibling projection. Accepted canonical records and their audit history remain in the private store. The managed Markdown vault is an editable presentation beside that store, with an explicit mapping from each stable note ID to its current workspace-relative path, accepted revision and materialized body digest. Opening the entire private store as a vault is rejected.

An opaque note ID is durable identity. A path is mutable presentation metadata and may change on rename without changing identity. Every accepted edit creates an append-only typed revision bound to the note ID, its parent revision, the exact accepted body digest, capture provenance, privacy policy and the observed source base. Acceptance must revalidate the current source observation and the frozen parser version and resource digest. An unavailable or mismatched parser blocks acceptance; it is not interpreted as an empty result.

Links are directed from the selected source note to the selected target note. The current structural link belongs to the accepted portable source revision, not to generated Markdown. The acceptance record remains in audit history if a later accepted edit or merge removes the current link. Self-links, missing endpoints, stale observations, duplicate bindings, unknown record families or versions, and empty evidence are rejected.

File absence is not, by itself, a canonical deletion. Deletion is an explicit deactivation recorded against the stable ID and survives restart. A deactivated note cannot be used for inference. Restore is explicit, retains the same stable ID and revision history, and does not reactivate prior inference consent.

Conflicting canonical and workspace changes are preserved as a typed conflict. Resolution requires an owner preview followed by an explicit choice of the accepted body, the candidate body or an explicit merged body. Resolution is idempotent and auditable.

The feasibility proof did not implement or prove a production filesystem writer or automatic conflict-safe promotion. It proved the narrower restriction that portable link, conflict, deactivation and restore state changes left already materialized Markdown bodies unchanged.

For the first shipped release, automatic canonical-to-Markdown overwrite of an existing managed body is disabled. This policy does not ban the materializer. Setup may create the initial managed projection. An explicit owner-invoked materialize or refresh operation may write after rechecking owner authority, the accepted revision and the target's current digest against its last materialization. After a crash, NW1 may resume an already-authorized pending materialization from its durable operation journal, bound to the original request and subject to the same current-state checks. Inference, link acceptance, conflict state, deactivation, restore, or an engine revision alone never silently rewrites an existing Markdown body. Conflict resolution may change accepted canonical state; changing the existing Markdown presentation still requires a separate explicit owner materialization.

Privacy is immutable from each source capture and is enforced before selection. Excluded stable IDs, mixed-source decisions and canaries are checked before dispatch. Consent is provider- and operation-specific, must be active at the point of evidence generation and acceptance, and is restored inactive after export/import. Denied or revoked work makes no provider or credential call. Capacity is reserved before handoff; pre-handoff failure releases it, while a failure after durable handoff is recorded as uncertain usage rather than silently refunded. Restart must not reset shared limits.

Freeze an explicit, versioned typed Portable contract for origin, note revision, structural-link history, conflict and conflict-resolution state. Preserve existing version-one record bytes where their meaning is unchanged. Readers must fail closed on unknown families, versions, missing references, duplicate identities and downgrade attempts. Every typed record must map to the shared generic kernel with provenance intact.

That generic mapping is the compatibility seam. It does not mean an installed Secure Node reader understands or enforces these typed records. The existing experimental version-two fixture remains a negative control and is not the shipping schema.

## Alternatives rejected

The dedicated canonical subtree used directly as the vault is rejected for this boundary. It avoids duplicate files, but it couples flexible Markdown frontmatter, editor rename/delete behavior and generated artifacts to the Portable namespace and would require production-grade filesystem authority and migrations that have not been demonstrated. Treating Markdown as an unconditional overwrite source, opening the entire private store as a vault, or using a symlink between the two stores is also rejected.

Automatic conflict-safe promotion is not a first-release feature. The selected first-release policy is the bounded no-automatic-overwrite restriction above, with explicit owner materialization and authorized pending-operation recovery still supported. A future release may add automatic promotion only after a separate gate proves authenticated authority, current-digest compare-and-swap behavior, cross-store crash recovery and conflict preservation against real external edits. This is a future option, not a promised deferred feature.

## Implementation ownership and acceptance

NW1 owns the installed schema and migrations, reader, materializer, production writer, durable operation journal and owner enforcement. Its acceptance suite must reconstruct a genuine exported base, fail closed on every required typed-family error, preserve unchanged version-one bytes and prove restart-safe reconciliation against a real managed vault.

NW1 must also test the first-release write policy directly. Initial setup creation, explicit owner-invoked materialization and recovery of an already-authorized pending operation must succeed only after current owner, revision and digest checks. State-only inference, link, conflict, deactivation and restore operations, an engine-only revision, stale targets, unauthorized callers and unbound recovery attempts must not overwrite an existing Markdown body. Crash tests at each journal and filesystem boundary must produce one authorized result without losing either version or duplicating a revision.

NW3 owns installation, client discovery, credential custody, provider selection and onboarding. It does not own the workspace writer, materializer, operation journal or owner-enforcement contract.

## Evidence boundary

The independently reviewed proof covers real Engine capture, export and base validation followed by a synthetic local workspace lifecycle: edit, rename, link, conflict, restart, resolution, deactivation, restore, export validation, byte-exact import and retrieval. Separate reviewed proofs cover source-derived evidence, privacy, consent, revocation, reservation and uncertain accounting, cooperative serialization, current-link reconciliation and forged-evidence rejection.

This decision does not establish a production schema, installed Secure Node semantics, a real-vault filesystem writer, operating-system identity, cross-store atomicity, provider behavior or GUI behavior. It closes the representation and first-release write-policy decision for the feasibility milestone only. It does not close the full native-workspace milestone. The full A–E decision record still requires independent review before NW1 starts.

See the [implementation plan](../plans/2026-09-09-ob1-native-workspace.md#filesystem-and-authority) and [current evidence and remaining gates](2026-09-10-ob1-nw0-continuation.md).
