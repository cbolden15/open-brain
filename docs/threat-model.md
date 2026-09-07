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
