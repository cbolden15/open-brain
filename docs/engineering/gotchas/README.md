# Gotcha registry

Non-obvious behaviors, sharp edges, and lessons learned belong here.

## Registry

### PRIVACY-001: A redaction receipt does not authorize a sink

Symptom: Redacted content appears safe but still carries a `secret`, `unknown`, classification-failure, explicit-local-only, or unconfirmed `personal` privacy decision.

Cause: Redaction and authorization answer different questions. A receipt records a transformation; it does not widen the original privacy authority.

Fix: Have every work-tier event or Markdown adapter enforce the immutable privacy decision before any I/O.

Discovered: 2026-08-13.

### REVIEW-001: Approval evidence must be bound to one review

Symptom: A valid approval event can be paired with another review or approved-intent record during deserialization.

Cause: The event shape was validated without checking its review ID and deterministic approved record against the enclosing review.

Fix: Reject mismatched review IDs and approved records at the model boundary.

Discovered: 2026-08-13.

### STORAGE-001: SQLite paths are root capabilities

Symptom: A database path under the configured root escapes through a symlink, or SQLite sidecar files receive permissive default modes.

Cause: Lexical path checks and default SQLite file creation do not enforce filesystem containment or private permissions.

Fix: Traverse parents without following symlinks and maintain database, WAL, and SHM files at `0600`.

Discovered: 2026-08-13.

### CAPTURE-001: Private raw storage preserves canonical bytes

Symptom: Persisting a capture changes its deterministic capture ID.

Cause: Redaction was applied to identity-bearing raw fields before private persistence.

Fix: Store canonical raw captures unchanged. Apply redaction only when producing typed work-tier records.

Discovered: 2026-08-13.

### INDEX-001: Excluded symlinks must not block canonical indexing

Symptom: An index rebuild fails even though a retained source symlink is explicitly outside
the indexed content policy.

Cause: Traversal rejected the presence of every symlink instead of excluding links without
following them.

Fix: Ignore source symlinks before file classification, never follow file or directory targets,
and regression-test that target content is absent from the index.

Discovered: 2026-08-19.

### RELEASE-001: Assemble detector canaries at runtime

Symptom: The release audit flags a test fixture that resembles a real credential assignment.

Cause: Secret scanners correctly inspect committed test text without knowing whether a value is synthetic.

Fix: Build detector canaries from harmless fragments at runtime instead of committing assignment-shaped literals.

Discovered: 2026-08-13.

### CAPTURE-002: Resume from a durable event after a crash

Symptom: A retry after event persistence fetches mutable source content again and creates a second event with different bytes.

Cause: Recovery restarted the full extraction pipeline instead of checking the event stream first.

Fix: If one matching durable extraction event exists, rebuild only the deterministic distillation item and continue from that boundary.

Discovered: 2026-08-13.

### CAPTURE-003: Extracted metadata does not always repeat the source URL

Symptom: A provenance-bound YouTube capture retries forever during saved-content
publication because its normalized extraction has a platform and video ID but no canonical
URL.

Cause: Publication required `metadata.canonical_url` even though some approved extractors
represent source identity through typed platform fields.

Fix: Require the immutable envelope URL to match its provenance source reference, prefer an
extracted canonical URL when present, and otherwise use the provenance-bound envelope URL.

Discovered: 2026-08-26.

### SECURITY-001: A closed gate must be unreachable

Symptom: Documentation says an executor or redactor is unavailable, but callers can inject any implementation through a public constructor.

Cause: Policy checks were mistaken for runtime confinement or approved-policy selection.

Fix: Reject arbitrary injection until a concrete adapter passes its gate; use one policy-version-locked redactor for work-tier events.

Discovered: 2026-08-13.

### MEDIA-001: Resource limits must be enforceable

Symptom: A command object lists timeout, memory, or process limits that the runtime never applies.

Cause: Descriptive metadata was treated as execution enforcement.

Fix: Apply operating-system limits, process-group cleanup, output bounds, and staging checks. Return `tool_unavailable` on unsupported platforms.

Discovered: 2026-08-13.

### PROVIDER-001: Cloud authority does not prove prompt safety

Symptom: A cloud-authorized privacy decision allows secret-shaped prompt text to reach adapter construction or credential resolution.

Cause: Routing authority and content redaction were treated as the same gate.

Fix: Scan the final cloud prompt before factory construction. A finding returns a closed code with zero factory, credential, import, or provider calls.

Discovered: 2026-08-13.

### LEDGER-001: Frozen dataclasses are not trusted constructors

Symptom: A caller directly constructs a sanitized leaf or trusted citation and injects Markdown structure or traversal into output.

Cause: Merge code checked the Python type but not the value's invariants.

Fix: Validate during construction and again at every merge, render, apply, and synthesis boundary.

Discovered: 2026-08-13.

### LEDGER-002: Two sink writes need one visibility decision

Symptom: A crash after the first Markdown write exposes half of a ledger publication.

Cause: Individual atomic files were mistaken for an atomic document set.

Fix: Readers use only the durable applied manifest that binds every intended document and sink digest. Reconciliation completes partial physical writes before publishing the manifest.

Discovered: 2026-08-13.

### LEDGER-003: Slim authorization comes from durable state

Symptom: Caller-provided booleans or citation tuples authorize archive/slim without a persisted applied ledger row.

Cause: Evidence was passed as mutable orchestration input instead of loaded from the store.

Fix: Use a store-issued row identity, verify persisted citations, and atomically record archive digest, successor digest, and `slimmed` after both writes verify.

Discovered: 2026-08-13.

### LEDGER-004: A writer cannot certify its own persistence

Symptom: A no-op or memory-only writer returns plausible IDs, digests, and cached read-back bytes, causing durable state to advance although no artifact exists.

Cause: Apply trusted caller-provided receipt fields or allowed the same adapter to attest both the write and its persistence.

Fix: Validate receipt type, disposition, ID, and deterministic rendered-byte digest, then read exact bytes through a separate approved root-confined reader before the manifest or slim state advances.

Discovered: 2026-08-13.

### LEDGER-005: A model lock guard cannot be optional

Symptom: Constructing synthesis with no lock probes invokes a model without proving transaction and writer locks are clear.

Cause: A safety invariant was represented as an optional callback tuple.

Fix: Reject construction without an authoritative probe and fail closed when a probe errors or reports a held lock.

Discovered: 2026-08-13.

### LEDGER-006: A purge exception belongs to one closure

Symptom: A record retained or activated as a replacement under one purge remains visible even
though it also descends from a separately tombstoned source.

Cause: Loaded-state validation combined every retain and replacement into one global exception set
before checking each tombstone closure.

Fix: Scope visibility exceptions to the purge that owns the tombstone. Derive suppression and
purge-pending sets exactly from stored tombstones and purge resolutions, then reject orphan flags.

Discovered: 2026-09-07.

### REVIEW-002: Review audit text is not owner output

Symptom: Third-party instructions in a source reference or proposal reason survive approval and appear in an owner-authored record.

Cause: Delivery copied audit fields instead of deriving an output-safe reference.

Fix: Render owner text plus a deterministic opaque capture reference only, then verify the sink receipt before marking the outbox delivered.

Discovered: 2026-08-13.

### REVIEW-003: Review creation receipts bind the initial aggregate

Symptom: Intent routing reports an open review after a no-op boundary returns the right review ID with unrelated bytes.

Cause: Routing checked identity and disposition but not the canonical aggregate digest.

Fix: Hash `ReviewAggregate.create(proposal)` canonically and require the creation receipt to match before returning `review_open`.

Discovered: 2026-08-13.

### SYNTHESIS-001: Citation authority includes destination and durable proof

Symptom: A persisted citation ID is paired with a forged destination, or a memory-only persistence port returns synthesis success.

Cause: Authority compared IDs only and accepted an unconstrained persistence result.

Fix: Recompute the deterministic destination from the published capture document and require the approved SQLite store to return and confirm an exact typed durable record.

Discovered: 2026-08-13.

### TAXONOMY-001: Printable labels only

Symptom: NEL or Unicode line/paragraph separators create extra structure inside a rendered topic heading.

Cause: Label validation rejected ASCII controls but not all non-printable Unicode characters.

Fix: Reject non-printable characters and Markdown structural punctuation before route construction.

Discovered: 2026-08-13.

### CLI-001: Dependency injection is not output authority

Symptom: A typed adapter returns success while echoing argv through a command value or dynamic JSON key, asserting reserved readiness fields, or pairing a failure status with exit zero.

Cause: The composition root trusted the adapter's result type without independently validating its public schema and process semantics.

Fix: Use closed command and schema-key allow-lists, scan converged encoded keys and values against argv and secret/path residuals, reserve live/parity/cutover fields for a separate evidence boundary, and enforce status/exit coherence.

Discovered: 2026-08-14.

### AUDIT-001: Unclassifiable runtime references are unsafe

Symptom: An old-source path passes audit because the deny-root set is empty or a path-bearing argument uses an unsupported representation.

Cause: No extracted path was treated as proof that no path existed.

Fix: Require a non-empty deny policy and reject executable, working-directory, referenced-file, or argument values that cannot be completely classified into the canonical path representation.

Discovered: 2026-08-14.

### HOOK-001: Path strings do not grant hook-install authority

Symptom: A manual hook installer can target a replaced, symlinked, or different repository after planning.

Cause: Installation authority was represented by caller-provided paths instead of a bound filesystem identity.

Fix: Bind an explicit capability to repository, Git, and hooks-directory identities; revalidate before descriptor-relative writes; keep dry-run as default; and make post-commit delivery bounded and fail-zero.

Discovered: 2026-08-14.

### MIGRATE-001: Atomic files do not make an atomic state generation

Symptom: A crash after the first migrated state file leaves readers with a partial seven-family target that cannot safely resume.

Cause: Per-file atomic replacement was mistaken for one visible migration commit.

Fix: Stage and verify a complete generation off-path, hold an identity-bound exclusive lease, publish one atomic `CURRENT` pointer as the reader-visible commit, and use a durable journal to recover pre- and post-pointer crashes.

Discovered: 2026-08-14.

### PORTABLE-001: UTC partitions come from persisted recording time

Symptom: A record near a month boundary is written under different partitions on hosts with
different local time zones or after a retry.

Cause: Partitioning used the process wall-clock month instead of the immutable acceptance or
recording timestamp stored with the record.

Fix: Normalize the persisted timestamp to UTC before deriving `YYYY/MM`. Occurrence-time
corrections append a superseding row; they do not move the original record.

Discovered: 2026-08-31.

### ARCHITECTURE-002: Package entry points are runtime dependency edges

Symptom: Static imports report zero architecture debt while the installed command imports a
legacy composition module before selecting the current app path.

Cause: Ownership checks classified Python files but did not verify the targets in package script
metadata.

Fix: Point default scripts directly at app-owned entrypoints and test their imports in a fresh
process. Keep predecessor and scheduled behavior behind an explicit legacy facade.

Discovered: 2026-08-31.

### PRIVACY-002: Encoded residue must be compared after bounded decoding

Symptom: A protected source reference is removed in raw form but survives in percent-encoded or
HTML-entity form, or a query term is echoed by a result explanation.

Cause: Projection compared only the stored literal and renderers treated encoded output as
unrelated text.

Fix: Apply bounded repeated percent/HTML decoding when checking public tokens, fail closed when
decoding does not converge, protect exact digests, and keep query explanations generic.

Discovered: 2026-08-31.

### CLI-002: Prefix flags must survive family dispatch

Symptom: `--dry-run` before a command family is discarded, or the process creates a Brain root
and operational state before the adapter rejects the request.

Cause: The process shell opened application composition before handling the global non-mutating
flag, then passed only arguments after the family name to the adapter.

Fix: Reject unsupported global dry-run requests before opening the application. Direct adapter
dispatch still receives non-representation prefix flags, and the process regression must prove
the requested root was never created.

Discovered: 2026-08-31.

### PRIVACY-003: A hash of private input is not an opaque ID

Symptom: A metadata-only result omits the query text but returns a deterministic digest or digest
prefix derived from it.

Cause: A content hash was treated as an identifier even though callers can verify guesses against
private input.

Fix: Generate retrieval IDs independently with a cryptographic random source, return them only as
opaque correlation values, and scan public results for full and truncated query digests.

Discovered: 2026-08-31.

### CLI-003: Root-free operations normalize representation flags first

Symptom: `--version` works without configuration, but adding `--json` before or after it opens
the application and fails for a missing Brain root.

Cause: Root-free detection compared the raw argument tuple instead of removing the
representation-owned output flag.

Fix: Validate duplicate flags, remove `--json` before recognizing help/version, and test each
supported ordering without runtime configuration.

Discovered: 2026-08-31.

### PRIVACY-004: Case-insensitive values have many reversible digests

Symptom: A protected URL is removed regardless of case, but the SHA-256 of a case-varied spelling
still appears in a public result.

Cause: The projection knew the canonical value's digest but could not enumerate every
case-equivalent preimage digest.

Fix: Treat standalone SHA-256-shaped output tokens as private residue. Use prefixed opaque record
IDs for public correlation instead of publishing bare content hashes.

Discovered: 2026-08-31.

### PORTABLE-002: Promote the exact validated snapshot

Symptom: Validation succeeds, but the bytes materialized or promoted during import/export differ
from the bytes that were checked.

Cause: The operation validated a pathname and read it again after another process or filesystem
transition changed the content.

Fix: Retain one immutable validated snapshot, materialize and verify that snapshot, then perform
the identity-bound atomic promotion. Retry evidence must bind to the same manifest and snapshot.

Discovered: 2026-08-31.

### CONNECTOR-001: Host evidence owns connector checkpoints

Symptom: A connector reports successful fetches or checkpoint advancement that do not match the
captures durably accepted by the Brain.

Cause: Connector-mutated counters or receipt claims were treated as authoritative at the host
boundary.

Fix: Meter discovery, fetch, extraction, and submission at host-owned capabilities. Record the
exact sink receipt with its delivery ID and source reference, and advance the checkpoint only when
that evidence matches the host receipt.

Discovered: 2026-08-31.

### ARCHITECTURE-001: Line-numbered debt is a live coordinate

Symptom: An import-debt report points to the wrong source line after a nearby edit, making a
violation appear fixed or assigning it to the wrong code.

Cause: Debt entries stored line numbers from an earlier source snapshot.

Fix: Regenerate line-numbered debt from the current source tree after every source edit and verify
the reported edge before accepting the classification or marking the debt resolved.

Discovered: 2026-08-31.

### INGRESS-001: Rejected pages must not starve later eligible rows

Symptom: A rejected first row keeps the cursor pinned forever, so later eligible input is never
processed; advancing the whole page can instead lose retry evidence when an eligible sink write
fails.

Cause: Cursor advancement treated a mixed page as all-or-nothing and did not distinguish policy
rejection from durable sink failure.

Fix: Advance past policy-rejected rows, but retain the cursor at the eligible row when its sink
submission fails. Keep bounded retry evidence for both outcomes.

Discovered: 2026-08-31.

### MCP-001: Scope retrieval before capability injection

Symptom: MCP tool calls enforce a space allow-list, but the adapter's stored retrieval attribute
still exposes unrestricted search or fetch to other callers.

Cause: The app injected the full retrieval task and asked the representation to derive a scope for
each tool call.

Fix: Derive the scoped retrieval capability at composition, inject only that capability, and reject
objects that still expose the unrestricted `scoped` factory.

Discovered: 2026-09-01.

### INTEGRATION-001: Installed optional modules are not necessarily preloaded

Symptom: An explicitly enabled optional integration reports `optional_dependency` even though its
module is installed and importable.

Cause: Availability checked `sys.modules`, which describes prior import state rather than installed
module availability.

Fix: At the reviewed app extension host, import the composition-declared module only after its
capability is enabled. Keep disabled/default profiles import-free.

Discovered: 2026-09-01.

### BACKUP-001: Replay-identical backups cannot include mutable scheduler state

Symptom: Replaying one backup request produces different app-state bytes even though the
canonical Brain content did not change.

Cause: The backup included scheduler cursors and claims that can advance while the request is
being verified or replayed.

Fix: Back up only schema-validated immutable run receipts. Recreate scheduler runtime state after
restore instead of treating it as recoverable instance data.

Discovered: 2026-09-01.

### BACKUP-002: An allow-listed backup path does not validate its contents

Symptom: A known app-state filename enters a backup with arbitrary JSON or credential-shaped
content.

Cause: Inventory validation treated the relative path as authority for every byte stored there.

Fix: Give each allowed app-state artifact an exact bounded schema, reject extra fields, and scan
validated values for forbidden residue before publishing the manifest.

Discovered: 2026-09-01.

### LIFECYCLE-001: A pending journal does not prove that its owner crashed

Symptom: A concurrent lifecycle request sees a pending upgrade and rolls back work that another
process is still performing.

Cause: Durable intent records survive crashes, but they do not carry live process authority.

Fix: Hold a distinct root-scoped kernel lease for the full lifecycle attempt. Recover a pending
record only after acquiring that lease, and bind each staged effect and terminal receipt to the
same request fingerprint.

Discovered: 2026-09-01.

### LIFECYCLE-002: A daemon cannot safely coordinate its own replacement

Symptom: Upgrade or uninstall restarts or removes the daemon unit before the coordinator can
record success, failure, or rollback evidence.

Cause: The lifecycle coordinator runs inside the process and supervisor unit that it is replacing.

Fix: Run lifecycle orchestration from a short-lived owner command outside the daemon, inject the
artifact and supervisor ports, and keep the default source-checkout composition fail closed.

Discovered: 2026-09-01.

### AUDIT-002: Cleaning the current tree does not clean reachable history

Symptom: The source tree is clean, but the public-history audit still reports an older planning
path or synthetic private-network fixture.

Cause: Replacing the current file leaves every earlier blob reachable, while broad path or rule
exceptions would hide unrelated future residue.

Fix: Sanitize the current tree and record only reviewed historical false positives by exact blob
SHA-256, normalized repository path, and allow-listable rule. Never permit credential or private
denylist findings through that policy.

Discovered: 2026-09-01.

### CI-001: Short Unix-socket test roots must exist on every supported host

Symptom: Daemon, UI, recovery, and subprocess tests pass on macOS but fail before setup on every
Linux CI version.

Cause: Tests shortened Unix-socket paths by creating temporary roots below macOS-specific
`/private/tmp`, which is absent on Linux.

Fix: Resolve `/tmp` before creating deliberately short temporary roots. It becomes canonical
`/private/tmp` on macOS and remains `/tmp` on Linux, preserving both path identity and host
portability. Treat the full multi-version Linux jobs as required evidence.

Discovered: 2026-09-01.

### CI-002: Syntax-valid action pins can still fail before checkout

Symptom: A newly copied CI job fails during job setup on every matrix version, before checkout or
any project command runs, while `actionlint` passes locally.

Cause: A one-character commit-SHA drift still matches workflow syntax but does not identify the
same valid pinned action revision used by sibling jobs.

Fix: Keep repeated action pins identical and assert that invariant in a repository test. Treat
`actionlint` as syntax validation, not remote action-revision validation.

Discovered: 2026-09-02.

### CONTROL-001: Default Unix-socket backlog behavior varies by host

Symptom: A partial client occupies the serial daemon reader and the next owner request gets
`EAGAIN` on Linux, while the same stalled-client regression passes on macOS.

Cause: Calling `listen()` without an explicit backlog left queue capacity to platform defaults.
The accepted-client timeout bounded the first read but did not guarantee that the next connection
could queue.

Fix: Set an explicit bounded backlog larger than one and keep the accepted-client timeout. Assert
the backlog in the stalled-client regression instead of adding sleeps or client-side retries.

Discovered: 2026-09-01.

### PACKAGING-001: A moved regular subpackage hides unmoved workspace modules

Symptom: Root tests cannot import legacy or connector modules even though both source roots are on
`PYTHONPATH`.

Cause: A moved `open_brain.cli`, `open_brain.integrations`, or `open_brain.services` initializer
creates a regular package whose search path excludes the still-classified directory under
`src/open_brain`.

Fix: Extend those search paths only in the root test harness while the phased move is incomplete.
Never add workspace path extension to the shipping app. The wheel-only harness must remain green,
and P4-W4 removes the test overlay with the old monolith tree.

Discovered: 2026-09-01.

### TOOLING-001: Ruff cache can hide import reclassification after package moves

Symptom: Local Ruff verification passes after moving modules between source roots, but a clean CI
checkout reports many `I001` import-order failures.

Cause: Cached lint results predate the relocation even though isort's first-party classification
depends on the module's source root and distribution boundary.

Fix: Configure `open_brain` as first-party and `open_brain_engine` as its third-party dependency.
Run Ruff once with `--no-cache` after package moves and treat that result as the migration gate.

Discovered: 2026-09-01.

### PACKAGING-002: Module depth does not prove a source checkout

Symptom: An installed supervisor manifest contains a nonexistent `PYTHONPATH` and working
directory below the interpreter's library directory.

Cause: The factory counted parents above `__file__`. Source and installed modules have similar
depth, so a `site-packages` path was mistaken for a checkout.

Fix: Enable source mode only when the module resolves to the exact declared source layout. Exercise
the production factory from the installed wheel, not only constructors with `checkout_root=None`.

Discovered: 2026-09-01.

### PACKAGING-003: An installed CLI does not imply an installed daemon launcher

Symptom: The app and engine wheels install and `open-brain init` succeeds, but a user cannot start
the required daemon without discovering uv's private environment interpreter and invoking an
internal module path.

Cause: Wheel acceptance exercised daemon behavior in-process and checked console scripts, but the
documented daemon command existed only for a source checkout.

Fix: Expose the foreground daemon through `open-brain daemon`. Verify that command through the CLI
contract, isolated wheel help, and a clean installed-wheel journey.

Discovered: 2026-09-04.

### TESTING-001: An inner fixed interpreter can falsify a CI version matrix

Symptom: Python 3.13 and 3.14 jobs pass even though their wheel-isolation subprocesses run on
Python 3.12.

Cause: The outer matrix selected an interpreter, but the acceptance harness hard-coded another
version when creating product and test environments.

Fix: Derive isolation environments from the active matrix interpreter. Run the installed journey
independently on every declared Python version.

Discovered: 2026-09-01.

### AUDIT-003: ImportFrom.module alone misses private child and lazy dependencies

Symptom: An artifact scan accepts a private engine child imported through a public parent, or an
undeclared dependency loaded only on a lazy path.

Cause: The scanner recorded only the parent in `from package import child` and inspected only
engine-prefixed static imports.

Fix: Resolve aliases against the complete manifest module map, compare external roots with wheel
metadata, reject forbidden workspace roots, and allow-list each variable dynamic import by exact
artifact path and signature.

Discovered: 2026-09-01.

### AUDIT-004: Callable spelling does not establish dynamic-import provenance

Symptom: An artifact scan misses imports reached through `builtins`, assignment aliases, or
reflective lookup, while rejecting an unrelated local object whose name happens to be `importlib`.

Cause: The scanner trusts global names instead of following bindings from actual importer
capabilities. An allow-listed call signature also leaves untracked capability escapes in the same
file.

Fix: Track importer bindings and calls through lexical provenance. Treat every capability use as a
review event, and bind the sole exception to its exact artifact path and function signature.

Discovered: 2026-09-02.

### AUDIT-005: Importer provenance also flows through runtime namespaces

Symptom: Direct and aliased dynamic-import checks pass, but an artifact can still reach the same
importer through `sys.modules["builtins"]`, namespace helpers, or dynamic evaluation.

Cause: Python exposes built-in objects through module registries and namespace dictionaries. A
scanner that follows only import statements and local aliases loses that provenance.

Fix: Treat `sys.modules`, `globals`, `locals`, `vars`, `eval`, and `exec` as reviewed artifact
capabilities. Reject them by default and keep the one authorized dynamic import bound to its exact
file and function.

Discovered: 2026-09-02.

### AUDIT-006: AST walk order is not control-flow provenance

Symptom: A dead-branch assignment erases a real importer path, while a loop, context-manager, or
exception target is mistaken for the standard-library module it shadows.

Cause: One mutable binding map follows AST visitation order. It neither joins alternate outcomes
nor applies Python's lexical and compound-target binding rules.

Fix: Track sets of possible provenance, join control-flow outcomes, predeclare function-local
names, and model loop, `with`, exception, match, and comprehension scopes before inspecting uses.

Discovered: 2026-09-02.

### AUDIT-007: Import aliases and comprehension walrus targets share existing authorities

Symptom: Attribute-based reflection is rejected, but importing that same member with
`from ... import ...` passes; or a walrus target disappears when a comprehension scope exits.

Cause: The analyzer maintains separate syntax-specific member rules and treats every name inside a
comprehension as comprehension-local. Python resolves imported members through the module object,
while PEP 572 binds assignment-expression targets in the enclosing scope.

Fix: Use one module-member provenance function for attribute and `ImportFrom` syntax. Keep
iteration targets local to the comprehension, but propagate walrus targets to the nearest enclosing
scope and predeclare them as function locals.

Discovered: 2026-09-02.

### AUDIT-008: A reviewed name does not prove a reviewed dynamic-import value

Symptom: An artifact passes because a dynamic loader and argument retain approved spellings, even
though an equivalent module alias reaches the loader or the argument was reassigned first.

Cause: The analyzer keys exceptions to syntax instead of semantic authority and value provenance.
Python also exposes import state through package `__init__` modules, `pkgutil`, frames, and type
reflection.

Fix: Normalize equivalent authorities, distinguish pristine parameters from unknown or reassigned
values through control-flow joins, and reject internal package roots again at the runtime optional
loader boundary.

Discovered: 2026-09-02.

### AUDIT-009: Architecture gates should close authority, not simulate a sandbox

Symptom: Each review finds another Python spelling that reaches the same generic loader, and the
acceptance analyzer grows without producing a finite security boundary.

Cause: P4H009 is treated as malicious-code containment even though its contract is to catch app
architecture regressions. The generic string loader keeps the unwanted authority open.

Fix: Replace arbitrary module strings with a closed typed provider registry, reject internal
identifiers at runtime, and review the gate against a named finite adversarial corpus.

Discovered: 2026-09-02.

### AUDIT-010: Source projections must not erase stale review evidence

Symptom: A removed exception remains in the canonical review inventory, but the normal
architecture gate passes after the reviewed file moves to another source root.

Cause: A helper filters both source records and their review entries before stale-review
validation. The evidence disappears from the test instead of becoming stale.

Fix: Validate the canonical review inventory against current source locations, including moved
records. Keep legacy source projections limited to the code or debt they were created to select.

Discovered: 2026-09-02.

### CONNECTOR-002: A `python -m` protocol module creates a second type identity

Symptom: A valid worker request reaches the child, but the connector rejects it because exact type
checks see a different `ConnectorWorkerRequest` class.

Cause: Running the protocol module with `python -m` defines its classes under `__main__`. The
connector imports the canonical module name and receives a second set of class objects.

Fix: Execute a small child bootstrap module that imports and invokes the canonical protocol module.
Keep all request, receipt, and error types defined only under the canonical module name.

Discovered: 2026-09-02.

### CONNECTOR-003: A valid child receipt does not prove host-budget compliance

Symptom: A connector worker returns schema-valid metadata with counts above the parent-issued
budget, or claims that replay created another capture.

Cause: Receipt validation checked field types and local count relationships without binding both
runs to the request limits or the replay contract.

Fix: Revalidate each run against the exact parent budget. Require replay to submit no captures and
bind the reported capture count to the created receipts before accepting worker output.

Discovered: 2026-09-02.

### CONNECTOR-004: Metadata discovery must not retain installed execution authority

Symptom: The app discovers an installed connector without importing it, but a later call through
the same registry loads and executes that connector in the parent process.

Cause: Metadata-only discovery and in-process compatibility shared one loadable entry-point group
and registry implementation, so the bounded worker was optional rather than exclusive.

Fix: Reserve the installed public group for metadata and child execution only. Use a distinct
group for explicitly injected compatibility sources, reject installed registry resolution, and
prove from wheels that every parent resolver leaves the connector module unloaded.

Discovered: 2026-09-02.

### RELEASE-002: Artifact coordinates and manifest labels must use the same number

Symptom: Connector wheels and sdists build, but artifact policy reports a stale rewrite or empty
canonical membership.

Cause: The policy coordinate `connectors` generated `connectors-wheel` and `connectors-sdist`, while
the canonical manifest uses singular `connector-wheel` and `connector-sdist` dispositions.

Fix: Name the policy coordinate `connector`. Keep the distribution directory and Python package
names independently plural where their published identities require it.

Discovered: 2026-09-02.

### LEGACY-001: Importing every wheel module does not exercise lazy dependency edges

Symptom: A private legacy wheel installs beside engine and imports every packaged module, but an
enabled compatibility path later imports an undeclared third-party SDK from the host environment.

Cause: Import smoke tests execute module bodies, not lazy loaders. A copied compatibility snapshot
can therefore retain ambient package authority while its metadata still claims an engine-only edge.

Fix: Inspect every compatibility source in the built wheel for static and dynamic import roots.
Require optional providers through an injected callable, and exercise an enabled injected path in
the engine-plus-legacy-only environment.

Discovered: 2026-09-02.

### READINESS-001: CLI-style probe exits bypass ordinary fail-closed handling

Symptom: One readiness probe terminates the aggregate preflight and exposes raw exit text even
though ordinary probe exceptions become opaque unavailable receipts.

Cause: `SystemExit` inherits directly from `BaseException`, so `except Exception` does not catch a
CLI helper that exits.

Fix: Catch `SystemExit` explicitly beside `Exception` at the probe boundary, preserve
`KeyboardInterrupt`, and test both paths with sensitive canaries.

Discovered: 2026-09-02.

### RELEASE-003: Build tools may add control files beside requested outputs

Symptom: Every requested wheel and sdist builds, but exact output inventory validation reports an
extra `.gitignore` containing one `*` byte.

Cause: `uv build --out-dir` creates its own ignore marker in the destination directory.

Fix: Keep exact output inventory validation. Remove only the tool's exact known marker before
validation, and reject the same filename when its bytes differ.

Discovered: 2026-09-03.

### RELEASE-004: A zero-issue notarization log may encode issues as null

Symptom: Apple accepts a submission, but the bounded parser rejects its log before stapling because
`issues` is `null` instead of an empty array.

Cause: Notary service output has two observed zero-issue representations.

Fix: Accept only `null` or an empty list when status is accepted. Reject nonempty lists and every
other shape, then require stapling and validation independently.

Discovered: 2026-09-03.

### RELEASE-005: Homebrew rejects local formula files outside a tap

Symptom: `brew install --formula /absolute/path/open-brain.rb` fails even though the formula and
archive are valid.

Cause: Current Homebrew requires formulae to belong to a tap. Invoking a developer command such as
`brew tap-new`, even for help, can also enable Homebrew developer mode in user configuration.

Fix: For local smoke tests, create a temporary Git-backed tap, install the fully qualified formula,
then uninstall and untap it. Avoid `tap-new`; if a developer command was probed, run
`brew developer off` and verify cleanup.

Discovered: 2026-09-08.

### RELEASE-006: A repository transfer can disable security scanning

Symptom: A public repository keeps its branches, workflows, and branch protection after an
organization-to-personal transfer, but secret scanning and push protection change from enabled to
disabled.

Cause: GitHub reevaluates account-scoped security settings when repository ownership changes.
Preserved repository identity does not guarantee that every feature setting remains enabled.

Fix: Snapshot `security_and_analysis`, branch protection, workflows, access, and repository ID
before transfer. Query the new repository directly afterward, restore any changed security control,
verify the old URL redirect, and update local remotes to the new URL.

Discovered: 2026-09-08.

### LIFECYCLE-003: Copying a symlink path is not portable target-copy behavior

Symptom: A lifecycle copy produces a candidate directory on macOS but a preserved `current`
symlink on GNU/Linux.

Cause: `ditto` and `cp -RPp` do not treat a symlink supplied as the source path the same way.

Fix: Validate the managed activation link, resolve it to the enrolled candidate identifier, and
copy that explicit candidate directory. Never rely on platform-specific source-link handling.

Discovered: 2026-09-03.

### LIFECYCLE-004: Metadata-preserving fixture copies can retain a foreign UID

Symptom: Artifact installation succeeds in a Linux container, then initialization rejects existing
owner-only state even though modes and bytes are correct.

Cause: Root runs `cp -p` against a bind-mounted fixture owned by the host runner, preserving UID
1001 inside the container. Docker Desktop ownership remapping can hide the defect locally.

Fix: Copy private fixture contents into a newly created destination without preserving ownership.
Preserve bytes, modes, and symlinks, then exercise a synthetic foreign-owner fixture regression.

Discovered: 2026-09-03.

### TOOLING-002: Make prints path-bearing recipes unless they are silent

Symptom: A command returns bounded JSON but Make prints the expanded recipe first, including local
absolute artifact, fixture, or evidence paths.

Cause: Output safety covered the child process but not Make's default command echo.

Fix: Prefix path-bearing recipes with `@` and test the Makefile text as part of the public-output
contract.

Discovered: 2026-09-03.

### AUDIT-011: A later shell command can mask an earlier audit failure

Symptom: An audit prints findings, but the overall shell invocation exits zero after a following
history check succeeds.

Cause: Sequential commands ran without fail-fast shell behavior. The final command supplied the
reported exit status.

Fix: Run release gates with `set -eu` or as separate checked commands. Keep binary-native media in
its dedicated validator instead of over-scoping a text scanner with a fixed content-size limit.

Discovered: 2026-09-03.

### TOOLING-003: Parallel uv commands can race on one project environment

Symptom: Independent verification commands fail to spawn Python or fail while removing `.venv/bin`
even though their inputs are valid.

Cause: Concurrent `uv run` processes select different Python versions and replace the same project
`.venv` at the same time.

Fix: Run shared-environment `uv` gates sequentially, or give each parallel command an isolated
environment. Restore a raced environment with one bounded `uv sync` before retrying.

Discovered: 2026-09-03.

### SIGNING-001: A read-only sandbox can falsify macOS signature verification

Symptom: `codesign --verify --strict` reports unchanged signed DMG bytes as modified only inside a
macOS read-only agent sandbox, while the same relative, absolute, and deep checks pass elsewhere.

Cause: The sandbox can restrict macOS security-service or metadata access while `codesign` reports
the result as a signature failure instead of a permission failure.

Fix: Hash the subject before and after, reproduce the exact command outside that sandbox, and use
a bounded sandbox that permits the required verification services while retaining a no-write task
contract. Never rebuild a frozen candidate from one unreproduced sandboxed signature error.

Discovered: 2026-09-03.

### SEARCH-001: Project private text before deriving snippets

Symptom: A protected source reference disappears from stored search text but a partial prefix or
suffix still appears in a result snippet.

Cause: Snippet generation or length clamping ran before the complete field passed through the
public-safe projection boundary. The shortened fragment no longer matched the protected value.

Fix: Apply the public-safe projection to complete title and body fields before indexing,
highlighting, snippet generation, or truncation. Reapply the projection at the output boundary.

Discovered: 2026-09-08.

### TOOLING-004: MyPy recognizes static platform guards

Symptom: A platform-only import passes MyPy on its supported host but fails with `import-not-found`
and `unused-ignore` on another CI operating system.

Cause: The runtime branch used `platform.system()`, which MyPy does not use to prune unreachable
platform code.

Fix: Guard platform-only imports with `sys.platform` and run focused MyPy checks with each supported
`--platform` value.

Discovered: 2026-09-08.

### CONTROL-002: Process exit can race pipe observation

Symptom: A worker sends a valid receipt and exits, but the supervisor reports `probe-failure` because
the first nonblocking pipe poll found nothing just before process exit became visible.

Cause: The supervisor treated a dead process as proof that no unread IPC result remained.

Fix: Join a completed worker, drain its receive pipe once more, and classify failure only when no
valid receipt remains.

Discovered: 2026-09-08.

### CONTROL-003: Short readiness retries can poison a socket backlog

Symptom: A daemon process stays alive and owns its Unix socket, but a readiness loop never receives a
status response on a slower host.

Cause: Each short-lived probe connects, times out, and closes before the single-threaded server begins
accepting. Those abandoned connections remain queued, so the server drains stale requests while the
probe loop adds more. Starting a fresh interpreter for every attempt adds avoidable startup variance.

Fix: Use one isolated probe process. Retry only while the socket is unavailable; after connecting,
keep that request open for the remaining bounded startup budget. Exclude unrelated listeners from a
control-only integration test.

Discovered: 2026-09-08.


### SQLITE-001: Read-only validation cannot recover a hot rollback journal

Symptom: After an interrupted migration with dirty-page spill, read-only SQLite inspection reports
that recovery needs a writable database. Treating that result as malformed state prevents retry.

Cause: SQLite must restore committed pages from its rollback journal before it can read the schema.
A read-only connection cannot perform that recovery. Separately, committing a validation transaction
before the caller's query leaves a race between validation and use.

Fix: Classify a private, confined hot-journal candidate as `recovery_required` and allow SQLite to
recover before locked schema validation. Keep normal readers in their validated read transaction;
revalidate application writes inside `BEGIN IMMEDIATE`. Test process exit after dirty-page spill,
read-only refusal, recovery retry, and schema changes between opening and application work.

Discovered: 2026-09-09, OB1-W5 migration implementation and read-only review.


### MCP-001: Default MCP cannot import the retained work adapter

Symptom: Adding a stdio command imports a module excluded by the default native dependency audit.

Cause: The retained transport imported concrete work-scoped adapters from the integrations package.
Those adapters belong to a separate composition boundary.

Fix: Keep protocol types and bounded framing in the neutral `services/mcp_protocol.py` module.
Default MCP injects a non-owner capture sink and a separate whole-Brain read operation. The retained
`mcp_stdio.py` wrapper keeps its scope check and remains excluded from the default artifact.

Discovered: 2026-09-09.


### TOOLING-005: Homebrew name and version inventory need separate commands

Symptom: A preservation check fails before its smoke starts with `Options --full-name and
--versions are mutually exclusive`.

Cause: `brew list` cannot combine fully qualified formula names with version output.

Fix: Read identities with `brew list --formula --full-name`, then read the selected product's
versions with `brew list --formula --versions <full-name>`. Treat any inventory failure as an error,
not as evidence that the product is absent. Test the actual smoke shell with a command-recording
fake Homebrew, including an installed product and interrupted-run recovery.

Discovered: 2026-09-09, OB1-W7 contributor path.


### RELEASE-007: Source audit success does not establish native artifact safety

Symptom: Both platform CI jobs and source/history owner audits pass, but the required artifact-aware
owner audit rejects both actual release archives with `content-scan-limit-exceeded`.

Cause: The archive scanner feeds the bundled executable into a source-text rule capped at 2 MiB.
The observed native executables are about 10 MB and 13.7 MB. The native dependency inventory verifies
module names and format/signature, but does not scan packaged contents with the owner denylist.

Fix: `artifact_audit.py` retains source limits and adds bounded inspection of native containers,
compressed CArchive/PYZ members, base-library ZIPs, and marshaled code data. Synthetic large and
hostile fixtures cover the parser; both actual platform archives complete inspection. They still
fail content policy on build paths and standard-library address examples. No exemption was added.

Parser trap: standard-library archive readers can allocate metadata before yielding a member.
Preflight ZIP directories and tar extension records before those readers, and reject uncovered ZIP
payload gaps or nonzero tar padding. PYZ keys are module identities; map them to module paths before
applying file-suffix rules so a module ending in `.sqlite` is not mistaken for a database file.

Discovered: 2026-09-09, public release readiness audit at goal commit `d81bb64`.

### RELEASE-008: Frozen metadata must describe the runtime, not its builder

Symptom: A native bundle contains installer paths in distribution metadata and generated sysconfig
values, even though its Python code filenames were normalized by PyInstaller.

Fix: Positively select runtime and legal dist-info files. Retain the full sysconfig scalar mapping,
but compile references to its own installation prefixes as frozen runtime-prefix expressions.
Do not derive those prefixes from the builder's virtual environment, delete unknown variables,
or remove `sysconfig` from the module graph. Preserve all other PyInstaller cached code objects.
Test the actual frozen metadata paths and pointer ABI as part of the existing self-check.

Related policy trap: `ipaddress` contains three runtime private-network constants; they are not
disposable examples. `urllib.request` also contains a documentation example. Changing those strings
to hide them from a scanner is not remediation. Any exception needs a separate exact-module,
payload-hash, and rule-bound decision while owner terms and all other findings remain enforced.
The [approved payload policy](../../audits/2026-09-09-ob1-stdlib-content-policy-proposal.md)
now records that decision. Python patch changes can alter marshal hashes even for similar source;
never copy an older CI hash into the policy merely to make its artifact pass.

Discovered: 2026-09-09, native metadata remediation after `46bf308`.

### INTEGRATION-002: A narrow Graphify callable does not imply a narrow runtime

Symptom: Planning treats `graphify.extractors.markdown.extract_markdown` as a standalone
Markdown-only dependency and assumes it resolves links throughout an Obsidian vault.

Cause: At upstream commit `3f82bf7f837a07fb0f7668fbdbd5662801906942`, the extractor package
initializer eagerly imports other extractors. Vault-global wiki-link resolution also depends on
root context supplied by the larger extraction facade, which checks tree-sitter before dispatch.

Fix: Measure the complete import closure and use root-aware extraction as the correctness baseline.
Test cross-folder links and duplicate basenames, keep the cache outside the source snapshot, and
verify any proposed narrow API against that baseline. Process isolation alone does not reduce the
packaged module inventory. Runtime and native compatibility remain unverified until the spike.

Discovered: 2026-09-09, native workspace planning and independent upstream source verification.
See [the integration plan](../../plans/2026-09-09-ob1-native-workspace.md) for pinned source anchors.

### INTEGRATION-003: Graphify's scan root and raw link IDs need an adapter boundary

Symptom: Root-aware Markdown extraction resolves a link to an unselected note, chooses one of two
same-basename notes, or emits an unresolved target ID containing an encoded absolute source path.

Cause: At the pinned `graphifyy==0.9.57` source, vault-wide lookup walks the scan root independently
of the selected input list. Duplicate-name fallback uses a deterministic tie-break, while unresolved
links retain path-derived IDs. The NW0-B1 synthetic probe reproduced all three behaviors and an
unresolved frontmatter alias; relocation changed dangling IDs while selected-page mappings remained stable.

Fix: Stage only eligible notes under the scan root. Keep alias/ambiguity resolution, stable Brain-ID
mapping, and path-free unresolved diagnostics at the engine boundary before graph publication or
permanent-link acceptance. Keep Graphify cache output private and outside the source snapshot.
Do not treat a selected input list as the full exclusion boundary or export raw upstream IDs.

Discovered: 2026-09-09, bounded macOS arm64 NW0-B1 closure/import probe. See the
[decision record](../../plans/2026-09-09-ob1-native-workspace-nw0.md) for results and remaining gates.

### INTEGRATION-004: Official-client flags do not establish a complete isolation boundary

Symptom: A client accepts empty-tool, safe-mode, or ephemeral flags, but the adapter treats that as
proof of tool-free inference with no ambient effects or retained prompts.

Cause: Codex `0.153.4` can register tools from model metadata independently of shell suppression.
Claude Code `2.1.265` offline diagnostics added 687 system-tool tokens when `--json-schema` was
present despite empty/denied tools; the category disappeared without schema mode. Claude safe mode
also retains managed hooks. Session-persistence controls do not mean no configuration/cache writes.

Fix: Verify the effective tool catalog and policy before sending notes. Keep plain-text JSON with
engine validation as the strict tool-free candidate; measure its quality. Reject incompatible
managed policy, enforce runtime egress/time/output bounds, count internal attempts, and test prompt
retention and active cancellation separately. An idle offline startup does not pass a subscription
gate, and a staged-executor interface does not supply OS confinement.

Discovered: 2026-09-09, NW0-C1 source/control inspection and synthetic macOS startup probes.
See the [control audit](../../audits/2026-09-09-ob1-nw0-c1-client-isolation.md).

### INTEGRATION-005: Project instruction limits do not suppress Codex home instructions

Symptom: A Codex thread with no environments, no runtime roots, and zero project-document bytes
still reports the client-state `AGENTS.md` as an instruction source.

Cause: The `0.153.4` home-instruction provider loads global instructions independently of the
project-document byte setting. C2 also observed a denied host-skills discovery attempt with the
proposed skill-suppression flags. Empty hook/MCP inventories do not establish empty model context.

Fix: Test user-level and project-level canaries separately. Keep discovery effects, loaded
instructions, model tool catalogs, and runtime persistence as separate assertions. A future Codex
adapter needs verified runtime/auth separation or a supported completion interface; copying tokens
into an empty client-state directory is not an acceptable workaround. Codex subscription is deferred
under the user-authorized C2 scope change.

Discovered: 2026-09-09, synthetic NW0-C2 offline thread probe and pinned source inspection.
See the [C2 audit](../../audits/2026-09-09-ob1-nw0-c2-codex-preflight.md).

### INTEGRATION-006: Claude login preference and parent policy do not establish subscription isolation

Symptom: Claude Code `2.1.265` initializes with an API-key source despite
`forceLoginMethod: claudeai`. Valid parent policy retains permission denials but drops
`disableAllHooks`; a separate SDK policy resolver can return admin hooks and routing unchanged.

Cause: Login selection, active credential precedence, and managed policy are distinct controls.
Host-supplied policy is filtered and can be displaced by admin policy. The inspected resolver uses
an older bundled client version and does not execute policy helpers. A no-auth startup cannot
establish the account's effective policy.

Fix: Check active credential source and effective policy before note bytes. Reject missing,
stale, incompatible, or unverified policy; preserve organizational restrictions. Pair version-matched
observation with independent runtime containment and client-owned authentication. Include
session-discovery metadata in crash cleanup checks: persistence-off left such a file in C3,
although no synthetic context was retained in the tested startup/control paths.

Discovered: 2026-09-09, synthetic NW0-C3 offline Claude and policy-resolver probes.
See the [C3 audit](../../audits/2026-09-09-ob1-nw0-c3-claude-preflight.md).

### INTEGRATION-007: A matching settings snapshot is not a dispatch authorization

Symptom: A native settings read looks compatible, but does not establish complete policy,
subscription identity, an empty tool catalog, or runtime confinement. Settings can change after
the read. A replayed or fake permit can appear to fill those gaps unless its trust boundary is explicit.

Cause: Metadata observation, independent enforcement, and engine consent are separate inputs.
A timestamp records when metadata was observed; it does not establish remote-policy freshness.

Fix: Keep content out of client startup, reject missing controls, and bind any future authorization
to the request, client instance/version, accepted revisions, current policy generation, and lifetime.
Recheck before release and reject observed changes. A second read alone does not close the external
policy-change race. Keep fake-only witnesses out of native transports. C4's live rejection precedes
termination; its first post-stop iteration remains labeled as development evidence.

Discovered: 2026-09-09, NW0-C4 supervisor contract tests and native metadata rejection probes.
See the [C4 design and probe](../../audits/2026-09-09-ob1-nw0-c4-claude-supervisor.md).

### INTEGRATION-008: Local login metadata can succeed with a synthetic credential

Symptom: Claude's offline status reports a logged-in subscription from a nonfunctional synthetic
credential file. The initialization response carries a subscription label but lacks the source field
required by the prototype. Neither observation verifies a real subscription or authorizes content.

Cause: Local credential selection, server authentication, and same-process dispatch authority are
different checks. A settings/status response also does not prove that denied bootstrap writes worked.

Fix: Preserve those distinctions in adapter diagnostics and acceptance criteria. Keep native dispatch
closed without source/policy/runtime evidence. Test the existing official namespace and Keychain
behavior separately from file-backed fixtures; helper replacement writes do not prove native refresh.
Treat loader/system-data failures as runtime failures, and retain their evidence when refining a
file-data allowlist. A profile that prevents startup has not passed integration.

Discovered: 2026-09-09, synthetic NW0-C5 native runtime-layout and status probes.
See the [C5 runtime-layout proof](../../audits/2026-09-09-ob1-nw0-c5-native-runtime-layout.md).

### INTEGRATION-009: SDK lifecycle support does not supply fresh policy authorization

Symptom: The real SDK starts with held input and returns native metadata, but `accountInfo()` retains
the initial account response. Runtime JavaScript has a settings getter absent from public Query types.

Cause: A usable completion lifecycle, cached metadata, and a supported live policy contract are
different interfaces. An internal wire request type does not prove completeness or freshness.

Fix: Reuse the lifecycle with held asynchronous input and supervisor-owned rejection/cleanup. Treat
cached account data as an initial observation. Record missing settings as unknown and reject before
release; do not cast to an internal method to imply a supported contract. Keep native authorization
separate from fake-only release tests. On the tested Mac, use the proven sibling-process prototype
for further interface work: applying a nested sandbox from the confined SDK host failed.

Discovered: 2026-09-09, NW0-C6 real-SDK/fake-process contracts and native supervisor bridge.
See the [C6 interface check](../../audits/2026-09-09-ob1-nw0-c6-sdk-supervisor-interface.md).

### INTEGRATION-010: Frozen extraction can work while bounded artifact inspection is incomplete

Symptom: The pinned Graphify Markdown fixture executes in signed Mac binaries, but content auditing
finds upstream home-path examples and a tree-sitter binary match, then stops at the object budget.
The combined archive reports `artifact-invalid`; the separate helper reports `artifact-limit-exceeded`.

Cause: Installed dependency closure, exercised imports, compressed size, and inspection work are
different measurements. The combined candidate's container-length check fails with 499,997 objects
already decoded; the helper reaches 500,001. Collecting every dependency root adds unrelated modules
and resources. Even the narrow candidate's Markdown root lookup still imports broad Graphify code.

Fix: Preserve failed/incomplete audit results and inspect bounded diagnostics before interpreting
their labels. Keep the strongest separate-helper option visible, but audit that helper too. Pursue
a supported root-aware component boundary or a concrete reviewed dependency/content-policy change;
do not raise limits, waive findings, or remove validation just to obtain a green probe. Treat observed
findings as a lower bound until the entire artifact is inspected.

Discovered: 2026-09-09, NW0-B2 frozen Graphify builds and unchanged native content auditor.
See the [B2 packaging record](../../audits/2026-09-09-ob1-nw0-b2-frozen-graphify.md).

### INTEGRATION-011: A direct Markdown import can still load every language extractor

Symptom: Removing Markdown's root lookup back-edge reduces the frozen closure, but still leaves
36 Graphify modules and an incomplete content audit.

Cause: The extractor package initializer eagerly loads its language registry. Markdown also reaches
runtime discovery and path code through shared skip rules and sanitization. A per-file function name
does not establish a small import boundary. Separately, undeclared optional PyYAML changes frontmatter
behavior: the pinned closure drops nested metadata even though basic synthetic extraction passes.

Fix: Verify both runtime and frozen module inventories. B3's private prototype passes root context
explicitly, makes compatibility exports lazy, and moves unchanged shared logic into small modules.
Preserve source provenance and upstream tests when evaluating adoption; keep pre-existing failures
visible. A clean artifact audit cannot establish nested-frontmatter support or a supported dependency.

Discovered: 2026-09-09, NW0-B3 component, artifact and original/patched upstream test comparisons.
See the [B3 component record](../../audits/2026-09-09-ob1-nw0-b3-markdown-component.md).

### INTEGRATION-012: A YAML parser does not make sanitized metadata an alias catalog

Symptom: Adding PyYAML fixes nested metadata, but cyclic values raise recursion errors, duplicate
keys keep the last value, unquoted alias names become booleans, and Graphify escapes alias text.

Cause: Parser availability, bounded construction, property-specific types and display sanitization
are separate concerns. Graphify's string/list caps and HTML escaping are unsuitable for canonical
alias identity. The optional parser accelerator can also introduce a failing native-artifact path.

Fix: Pin and audit the actual parser profile. Preflight eligible snapshots before extraction;
reject duplicate/cyclic/oversized input and preserve literal alias names in the Engine-owned catalog.
Keep original source bytes and display metadata separate. Verify native content and startup limits
independently: B4's pure parser passes content inspection but still exceeds the warm budget.

Discovered: 2026-09-09, NW0-B4 parser, frontmatter, native-artifact and startup comparisons.
See the [B4 audit](../../audits/2026-09-09-ob1-nw0-b4-component-adoption.md).

### INTEGRATION-013: Portable validation and import do not prove workspace revision semantics

Symptom: A synthetic page with two known capture provenance references validates and imports,
but authoritative reconciliation rejects its provenance. An archived page's original source capture
remains searchable after import. Its canonical row is omitted, which also fails reconciliation.

Cause: Portable syntax, import success and active Engine behavior enforce different contracts.
The current reconciliation path requires one indexed capture reference. Existing archival status
does not implement managed-workspace deletion from search and graph. A2's active single-provenance
page and generic event carrier pass reconciliation, but embedded policy flags remain ordinary content.

Fix: Test capture, export, import, authoritative reconciliation and retrieval together before
selecting a same-page revision or inactive-state representation. Keep private lifecycle simulations
distinct from Portable compatibility evidence; do not infer that every v1 encoding is impossible
from one failed candidate. No shipping schema change was made by this probe.

Discovered: 2026-09-09, NW0-A real Engine compatibility cases in the
[parallel checkpoint](../../audits/2026-09-09-ob1-nw0-parallel-feasibility.md), isolated by the
[A2 comparison](../../audits/2026-09-09-ob1-nw0-a2-portable-representations.md).

### INTEGRATION-014: A native helper needs a distribution contract as well as a clean binary

Symptom: Both executables pass individual content inspection, but a two-executable bundle fails.

Cause: Generic archives have a smaller member cap; the native archive contract expects one
executable named `open-brain`. A helper also needs distinct asset identity, installation and updates.

Fix: Keep the auditor unchanged. Compare separately audited native resources with an explicitly
versioned layout. Verify resource naming, manifest/formula and installation before shipping.
Measure ordinary base status separately from helper execution; do not equate it with cold startup
or the full five-minute journey. Same-input archive comparisons must use the same gzip filename.

Discovered: 2026-09-09, [NW0-B5](../../audits/2026-09-09-ob1-nw0-a3-b5-feasibility.md).

### INTEGRATION-015: Disabling Homebrew API installation can trigger a shared core clone

Symptom: A local-only resource smoke begins cloning the core tap while indexing its private tap.

Cause: `HOMEBREW_NO_INSTALL_FROM_API` requests a local core checkout; it is not an offline switch.
Stopping the harness process group did not stop the observed orphan git clone automatically.

Fix: Omit that flag for the local resource proof. Keep auto-update disabled and cache/log/home state
isolated. On interruption, identify owned descendants and verify cleanup and product preservation.
Do not infer that a local archive URL confines every Homebrew side effect or that killing the parent
reaps all work. Preserve the failed receipt and charge its effort.

Discovered: 2026-09-09, [NW0-B6](../../audits/2026-09-09-ob1-nw0-b6-resource-install.md).

### INTEGRATION-016: Source authority can change inside adapter construction

Symptom: A private constructor accepts an edit after the handoff policy check. Stale synthetic input
reaches the loopback endpoint, even though later publication is rejected.

Cause: A reentrant coordinator lock serializes other threads but permits same-thread callbacks.

Fix: Revalidate authoritative body, revision, privacy and policy after construction and before the
trusted held-input start. Keep the failing regression; require zero requests, not only zero results.

Discovered: 2026-09-09, [NW0 private slice](../../audits/2026-09-09-ob1-nw0-private-vertical-slice.md).

### INTEGRATION-017: A normalized graph identifier is not evidence that a target exists

Symptom: A missing reference normalizes to the identifier of a different selected page.

Cause: Upstream identifiers can collide across missing and existing targets.

Fix: Join an existing-file target stamp to the eligible inventory. Reject unresolved references;
do not promote them using the normalized identifier alone. Test the actual frozen helper.

Discovered: 2026-09-09, [NW0 private slice](../../audits/2026-09-09-ob1-nw0-private-vertical-slice.md).


### INTEGRATION-018: Test-only native routes can bypass the public input contract

Symptom: A helper advertises selected-body extraction, but a bundled diagnostic accepts a directory
and reads fixture files directly. Reusing the base archive writer also gives the helper the wrong
executable name and leaves its distribution contract implicit.

Cause: Private diagnostic entrypoints and base-product packaging assumptions survive a proof's
move into reproducible CI support.

Fix: Keep filesystem diagnostics source-only and test rejection by the actual frozen executable.
Use an explicit helper envelope with a fixed executable name and bounded legal-file inventory.
B12 adds that narrow envelope to the native auditor; it does not widen the base one-member contract,
content exceptions, decompression limits, or generic archive member cap. Test both accepted envelopes
and missing, extra, duplicate, oversized, symbolic-link, and incorrectly named members.

Discovered: 2026-09-10, independent review of the
[B12 portable native proof](../../../tools/nw0_graphify_probe/README.md).

### INTEGRATION-019: A disposable desktop can depend on its live boot medium

Symptom: A preserved live guest needs new test assets, but its data image is fixed and only the
boot CD can be exchanged through the available controls.

Cause: Disposable changes are not persistent across shutdown, and the live filesystem may still
read its boot image. A paused VM is not a portable snapshot of the current desktop.

Fix: Preserve the running guest and its boot medium. Prepare a separate isolated test environment
with a distinct removable read-only data CD before boot. Verify actual saved configuration rather
than assuming a wizard click added the drive. Do not count the new environment as prior GUI evidence.

Discovered: 2026-09-10, [NW0 continuation](../../audits/2026-09-10-ob1-nw0-continuation.md).

### INTEGRATION-020: An asynchronous child API can still wait before returning its PID

Symptom: A parent starts its timeout before process creation but cannot cancel an already-created
child because the launch call has not returned the child's PID.

Cause: Python's fork-based `Popen` path waits on an exec-error pipe. Replacing it with direct
`posix_spawn` removes that Python read but does not establish early ownership on glibc Linux:
`CLONE_VFORK` suspends the caller until child exec or exit. API naming and normal fast-start samples
do not prove the controlled pre-exec-stall case.

Fix: Trace PID acquisition before relying on cancellation or cleanup claims. Test a controlled
child-side pre-exec stall, retain the original deadline, and confirm actual terminal wait rather
than eventual process disappearance. A native async-signal-safe fork/exec routine in the existing
owner is an unproved alternative, not an approved implementation. Verify each supported platform;
do not hide a concrete child-side wait inside a general scheduling assumption.

Discovered: 2026-09-10, independent NW0 launch review; see the
[Linux spawn](https://man7.org/linux/man-pages/man3/posix_spawn.3.html) and
[vfork semantics](https://man7.org/linux/man-pages/man2/vfork.2.html).

### INTEGRATION-021: Validated usage can disappear at a narrower result projection

Symptom: A provider response passes strict token-usage validation, but the final source-bound
operation result has no usage fields.

Cause: A reused graph-result projection keeps source evidence and model attribution but intentionally
omits transport metadata. Validation before that projection does not preserve the discarded value.

Fix: Attach only the normalized, validated usage to the bound result before its final encoding and
byte-limit check. Test all provider shapes through the actual authority return boundary, including
cache and reasoning counters. A parser-only test cannot prove the returned result retains them.

Discovered: 2026-09-11, [NW0 continuation](../../audits/2026-09-10-ob1-nw0-continuation.md).

### INTEGRATION-022: An explicit lint configuration can still depend on the working directory

Symptom: The same frozen sources and Ruff configuration pass in the candidate directory but report
different import-order findings from a receipt directory.

Cause: Selecting the configuration file does not also freeze default source roots used to classify
first-party imports.

Fix: Record or explicitly set the source roots as well as the binary, configuration and source
hashes for standalone proof checks. Preserve the failed check; do not rewrite frozen imports to
chase unrelated working directories or claim that test execution verified a later formatting edit.

Discovered: 2026-09-11, [NW0 continuation](../../audits/2026-09-10-ob1-nw0-continuation.md).
