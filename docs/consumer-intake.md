# Engine conversation intake

The engine has an internal conversation intake seam for ChatGPT and Claude
identities at schema20/runtime15/Portable15. Both production parser profiles
remain inactive. This does not add an archive parser, an `import-chat` command,
a provider connection, or a model resource. Synthetic tests inject reviewed
profile records and call the real engine tasks.

The app exposes these tasks to owner-authorized shell agents through
`open-brain consumer-import`; see `docs/consumer-intake-agents.md`. This adds
neither an external archive parser nor delegated MCP authority.

The tasks are available through `engine.consumer_imports` and
`engine.tasks.consumer_imports`:

| Task | Behavior |
|---|---|
| `preview_consumer_import(plan, authority=owner)` | Bind the complete input, proposed or existing account scope, privacy choice, replacement choice, current groups and source preimages. |
| `consumer_import_preview_body(preview_id, body_page, authority=owner)` | Read one complete inert transcript segment from an unexpired preview. |
| `commit_consumer_import(preview_id, operation_id, rebuilt_plan, authority=owner)` | Reconcile an accepted exact operation first, otherwise validate the preview and retain immutable intent before capture. |
| `consumer_import_status(operation_id, authority=owner)` | Read body-free durable outcomes without recovery. |
| `recover_consumer_import(operation_id, authority=owner)` | Explicitly resume retained intent with its original identities. |
| `halt_consumer_import(operation_id, authority=owner)` | Seal unfinished work while preserving earlier complete conversations and custody. |

The engine-facing `ConsumerImportPlan` contains provider/profile/operation/archive
identities, normalized selected messages and omission counts, exactly one existing
scope or new scope label, and owner privacy/replacement choices. It cannot supply
a namespace, source identity, capture reservation, owner role, session, route,
publication or grant. Exact provider identifiers are protected UTF-8 byte evidence;
conversation text is normalized separately. Empty conversations retain `no_text`
evidence without creating captures.

The engine issues a capture-only non-owner context. Every transcript speaker,
including a speaker named “user,” remains third-party and unverified. The default
privacy is personal with cloud and external egress disabled. Choosing another
tier does not enable egress. Input identity and normalized text occupy separate
bounded objects, so maximum accepted identifiers do not consume the text budget.

Previews last one hour, with at most16 live previews per owner. Repeating the same
preview reuses its reservations and token. Summary/status output contains no
message text, title, provider identifier, archive path or source digest. The body
page is the explicit content-output exception. Accepted exact replay works after
preview expiry, profile deactivation, later replacement or retraction; it returns
the retained outcome and never reactivates content.

Each conversation activates atomically after all its segments materialize. Its
scope/archive registration, stable source heads, complete membership, session
membership and receipt commit together. Previous complete content remains current
during replacement. Missing conversations have no deletion meaning. Shorter
replacements retain old tail identities as noncurrent history. Unchanged imports
create no capture or new session membership. Their new operation-bound outcome
receipt retains the original activation receipt reference as checked evidence. A different privacy choice or a
previously retained noncurrent revision reports conflict rather than promising a
replacement that the lifecycle cannot admit.

Retraction and withdrawal cannot be reversed through import. One unavailable or
withdrawn required source suppresses the complete current conversation. Current
reads also require authority for every member's route and effective privacy.
Owner historical reads retain the predecessor rules, but never admit unfinished
staging. Ordinary source intake cannot claim the consumer namespace, and accepted
consumer delivery identities are reserved before the first segment is custodied.

Startup does not resume consumer capture reservations. Status is read-only;
recovery and halt are explicit owner mutations. A new issuer may inspect or halt
old unfinished work but cannot resume its authorization. Portable15 retains
accepted intent and terminal custody while refusing nonterminal operations.

Implementation:
`$REPO/.worktrees/competitive-gap-reconciliation/packages/engine/src/open_brain_engine/engine/consumer_intake.py`.
The complete engine contract is in
`$REPO/.worktrees/competitive-gap-reconciliation/docs/plans/2026-10-15-competitive-gaps-g1/CONSUMER-INTAKE.md`.
