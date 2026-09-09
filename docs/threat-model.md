# Threat model

Primary threats include credential disclosure, private-content disclosure, path traversal, SSRF, prompt-injected filesystem access, unredacted logs, unauthorized cloud routing, review-gate bypass, duplicate writers, and unsafe migration or rollback.

Security controls are contract tests and release gates, not documentation claims. Public fixtures are synthetic. Unknown classifications fail closed. Deployment templates cannot contain private host values.

## Local filesystem authority

The self-hosted local profile assumes one trusted owner account and owner-only Brain and staging directories. Portable import and export defend against malformed input, pre-existing symlinks and hardlinks, path traversal, special files, target-creation races, crashes, and cooperating concurrent Open Brain processes. Leases coordinate those Open Brain processes. Native no-replace rename makes a new target name visible atomically only after the staged root passes validation.

Code already running as the Brain owner's operating-system user is inside the local trust boundary. Such code can change file permissions, replace directory entries between system calls, or alter the Brain after promotion. A user-space filesystem protocol cannot make the tree immutable against that principal. Here, “atomic promotion” means the target name changes from absent to a complete staged directory in one native rename under the trusted-owner boundary. It does not claim integrity against arbitrary concurrent mutation by another process with the same user ID.

Deployments that run untrusted code must use a separate operating-system account, container or virtual-machine boundary, or a platform-specific immutable snapshot service. Expanding the default profile to hostile same-user execution requires a separate design and conformance gate.

Multi-document ledger writes become visible only through an applied durable manifest that binds the complete document set and sink digests. IDs, dispositions, deterministic rendered-byte digests, and exact read-back through a separate approved root-confined reader must all verify first. Slimming uses the same split writer/reader rule for its transcript-free successor, derives authority from the durable ledger row, and records archive and successor digests before `slimmed`. Model synthesis requires three persisted citation IDs with deterministic destinations, the approved SQLite store with typed durable confirmation, an authoritative lock probe, and no held transaction or writer lock. Valid output persists its evaluating row, page, and link-backs in one SQLite transaction.

Review creation and delivery treat receipts as untrusted. Creation binds the canonical initial aggregate digest. Delivery binds the expected output ID, canonical digest, and created/duplicate disposition before the outbox can be marked delivered.

## Connector worker boundary

The optional connector distribution is trusted shipped code, but it does not load in the app
process. The app validates entry-point metadata, explicit enablement, manifest identity, and
declared capabilities before starting a fixed worker bootstrap. The child receives no inherited
environment or secret values. Direct sockets are disabled, process groups are reaped on timeout or
output overflow, and the response schema contains bounded metadata only.

The connector wheel scanner rejects app composition, unpublished app extension values, private
engine modules, undeclared dependencies, and dynamic import authority. This boundary limits the
shipped provisional connector. It is not containment for hostile code running as the Brain owner.
Untrusted third-party connectors require a separate operating-system account, container, or virtual
machine gate.

## Default Open Brain boundary

Default Open Brain trusts one local operating-system user and runs direct commands against one
platform-local Brain. It creates no listener, grant service, daemon, launchd job, systemd user unit,
or multi-client authority boundary during the five-minute journey. Its SQLite databases and
portable files may contain readable user content.

Owner-only directory and file permissions, path confinement, SQLite durability, no-egress defaults,
and Portable Brain validation remain required. Existing runtime artifacts and root-level held or
malformed writer leases are checked read-only before profile compilation, even when `brain.toml` is
absent. Conflicting direct commands do not create Brain identity, layout, or SQLite state. These
controls are not application-level encryption. Default Open Brain does not claim encrypted-at-rest
SQLite or FTS, cryptographic key destruction, compartment isolation, signed receipts, certified
purge, or resistance to another process running as the same user. Those claims belong only to a
conforming Secure Node installation.

The live FTS5 table indexes only the NFC-normalized public projection of title and body text. Public
projection happens before matching so protected paths, credentials, source references, and digests
cannot act as retrieval selectors, then runs again on returned fields as defense in depth. Generated
literal queries prevent quotes, operators, prefix markers, parentheses, and column-looking input
from becoming FTS5 syntax. Space authorization and metadata filters run in the same SQL statement
before ranking and `LIMIT`.

FTS rows are derived but still sensitive plaintext. The authoritative live table resides in
`phase1.sqlite3`; the separate portability index is a non-authoritative snapshot that may be stale.
Default status distinguishes them without exposing paths or content. SQLite may place transient FTS
or sorter scratch in operating-system temporary storage. Secure Node must separately prove that
durable indexes and scratch stay inside its encrypted and purgeable boundary.

Markdown import reads an owner-selected source tree outside the Brain. It repeats canonicalization
and independent descriptor opens around the explicit root so ordinary host aliases work without a
single resolve/open race, then uses only a pinned root descriptor. Traversal compares directory
identities, rejects overlap with the Brain and other registered import roots, refuses changed root
identities, skips symlinks, hardlinks and special files, and uses nonblocking file opens plus
before-and-after metadata checks so a regular-file-to-FIFO or replacement race cannot hang or escape
the root. An unreachable registered root fails closed when its stored canonical path could overlap
the candidate. Aggregate preflight and first-root confirmation run before import-state or capture
writes. The explicit large-vault flag bypasses only visit, file-count, and aggregate-byte ceilings;
it never bypasses the one-file limit or confinement checks.

Imported filenames and contents are attacker-controlled text. Paths are normalized and collision
checked, human output removes terminal controls, and fatal errors do not echo the absolute root.
Content is stored as inert unverified source. The importer never parses content into commands,
follows links or embeds, evaluates HTML or code, or loads Obsidian configuration and plugins.

The existing non-reentrant single-writer lease excludes other local mutations without waiting. The
importer uses a lock-held internal capture primitive so it never reacquires that lease. An
interrupted traversal or storage failure cannot finalize missing paths. A deterministic failure for
one observed file preserves that path's prior active revision while allowing unrelated absent paths
to be finalized.

Descriptor checks prevent accidental traversal and ordinary replacement races. They do not create a
snapshot against malicious code already running as the same operating-system user, and reads may
update source access time on some mounts. NFS, FUSE, synchronized source trees, and other filesystems
with weaker identity or metadata semantics are not trusted snapshot sources in the first release.
Removing a source file is not a confidentiality purge because immutable capture history remains
locally readable and exportable.

## Default-product MCP boundary

`open-brain mcp` is an explicit stdio process with no listener, daemon, child service, connector,
action execution, user-managed grant, or Secure Node composition. The OS user and inherited stdio
channel are its trust boundary. Capture and search are independently absent unless their matching
`--allow-capture` and `--allow-search` flags are supplied. Launching with neither flag returns a
bounded error.

Search intentionally grants whole-Brain read authority. A network-backed client may send returned
content to its model provider, including private imported note excerpts. Enabling `--allow-search`
authorizes delivery to that client; Open Brain itself does not perform the network egress. Ten
public-safe results per call limits response size, not total readable content. Repeated queries can
extract the Brain. The default product offers no compartments or selective search grants.

Captured and retrieved text may contain prompt injection. MCP returns it as untrusted data with
visible trust and source-origin fields. No returned record is an instruction or authorization for
Open Brain. Injection can still influence a connected model, which may disclose content or use other
tools enabled by its client. Those client permissions determine the wider blast radius. With both
flags, a misled client can also persist more unverified text in the Brain.

The write path uses an injected non-owner capture-only sink, separately from read authority. It
cannot publish canonical content, approve reviews, route to compartments, or invoke actions and
connectors. Automated records have unknown origin and unverified trust. This prevents trust
promotion, but cannot prevent poisoning through unwanted immutable captures. Version 0.1.0 accepts
that it has no selective deletion, session rollback, or certified purge. Stopping the process blocks
further work and does not erase completed captures or exported copies. Both choices are disclosed in
CLI help and alongside capture-only, search-only, and combined README configurations.

The bounded transport accepts at most 1 MiB per newline-delimited message. Strict tool arguments,
redacted failures, 500 valid capture attempts, 16 MiB of aggregate UTF-8 capture input, and 2,000 valid
search attempts constrain one process. Valid duplicates, conflicts, and backend failures consume the
budget before engine work; calls exceeding a bound do not partially submit. Limits reset on restart
and do not constrain hostile code already running as the same OS user. Hashed idempotency keys live
in a distinct MCP delivery namespace and bind exact input text; conflict quarantine evidence retains
no new capture.

Concurrent CLI/MCP mutations retain the existing nonblocking writer lease, WAL mode, and five-second
SQLite busy timeout. Transient shared-writer contention returns `database_busy`; daemon authority,
runtime artifacts, and malformed leases remain separate refusal conditions. EOF exits without a
runtime registration file. The retained scoped MCP adapter still denies an empty grant and omits
results from nonmatching spaces.

## Secure Node M1 boundary

Secure Node binds HTTP only to a numeric loopback address and validates Host on every
request. Same-user processes can reach loopback and are inside the host access boundary, but they do
not receive protocol authority from that fact. Every protocol route still requires a short-lived,
principal-key-bound grant and an Ed25519 request signature. CORS is disabled, browser Origin is
rejected unless explicitly configured, and anonymous `/healthz` returns static liveness only.

The protocol decoder rejects duplicate JSON names, non-finite numbers, invalid UTF-8, and values
outside the RFC 8785 data model. Structural schema validation and cross-field semantic validation
both run before an operation body can be trusted. Ed25519 keys and signatures use exact canonical
unpadded base64url lengths. Signed documents cover their full RFC 8785 object except for the
signature field itself. Receipt verification follows the ordered owner-key history from a pinned
genesis fingerprint to the Node epoch certificate and active receipt key.

Owner control is not exposed over HTTP. It uses a mode-`0600` Unix-domain socket beneath a
mode-`0700` runtime directory, checks peer UID, and returns principal-bound grant bytes only in
memory. LocalAuthentication on interactive macOS or audited passphrase re-entry on Linux and
headless macOS proves fresh user presence. Reading an OS keyring entry does not prove presence.

Secure Node M1 protects application-controlled storage. The encrypted ledger, replay database, FTS files,
database journals, blob and staging directories, caches, temporary files, versioned key envelopes,
metadata-only security events, and delivery-erasure tombstones are named purge surfaces. The Node
keeps SQLite temporary and FTS scratch state in memory or inside the encrypted Brain boundary. It
does not claim to erase operating-system swap, hibernation images, filesystem snapshots, core dumps,
or external crash reports; deployments must control those surfaces separately.

Purge acceptance requires both logical deletion and residue checks. Canonical rows and FTS matches
must disappear before and after checkpoint and vacuum; scanning encrypted files for a plaintext
canary is separate evidence and cannot substitute for querying the logical stores.

The default Brain root is platform-local application data. Bootstrap refuses known network
filesystems and known synchronized roots. An unrecognized synchronization product is unsupported.
The sequencer lease binds a generated machine-instance ID, Node ID, and epoch so a copied identity
cannot write until an owner-authorized cold transfer advances the epoch. The owner-signed transfer
must bind the old Node's signed stop proof, prior ledger head, destination machine and Node key, and
exactly the next epoch; uncertainty pauses writes. The wire ledger head contains only a
domain-separated commitment to the complete signed prior receipt. It does not disclose that
receipt's cursor, commit ID, or canonical commit digest to a later committer.

Owner sessions and step-up freshness use a monotonic process clock. Grant expiry uses a persisted
UTC high-water mark with 300 seconds of skew. Backward wall-clock movement fails closed, and replay
nonces remain until expiry plus skew. Read requests reserve nonces before execution, so a failed read
cannot reuse its nonce.

Authorization occurs before body decoding, entity selection, change-page construction, or query
candidate selection. `inspect` deliberately returns the same response content for absent and
unauthorized entities. Secure Node M1 does not claim constant-time database behavior, timing indistinguishability,
or resistance to an attacker already able to inspect the owner's process memory.
