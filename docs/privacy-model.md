# Privacy model

Every capture receives an immutable privacy decision before persistence. The decision includes a tier, deterministic reason, and cloud/egress authority.

## Product profiles

Default Open Brain is one trusted local operating-system user and one automatically created Brain.
It uses owner-only paths, provider mode `none`, and no external egress. Its SQLite state and Portable
Brain files may contain readable content. These controls are local privacy defaults, not
application-level encryption, cryptographic erasure, compartment isolation, or protection from
another process running as the same user.

Secure Node is the opt-in profile that may claim encrypted custody, grants, compartments, signed
receipts, fencing, and certified purge only after its separate conformance gates pass. Plain
`open-brain` must not install, initialize, advertise, or imply those controls.

## Default-product MCP

The owner explicitly launches `open-brain mcp` with `--allow-capture`, `--allow-search`, or both.
The inherited stdio channel and invoking OS user are the trust boundary. No token, user-managed
grant, listener, daemon, connector, or Secure Node capability is created. EOF stops the process and
closes access. Open Brain itself performs no network egress.

`--allow-search` grants the connected client the same whole-Brain read scope as the CLI. Ten results
per call is a response bound, not compartment isolation: repeated queries can return private note
content. A network-backed client may send that content to its model provider. Adding the search flag
is the owner's explicit choice to allow that client to receive results. Public-text projection
removes protected paths, credentials, source references, and digests; it does not promise that every
remaining note excerpt is non-sensitive. CLI help and each README client example disclose this choice.

`--allow-capture` injects a separate non-owner sink with only `capture.accept` authority. Automated
text carries unknown content origin, automation-absent owner context, personal-local-only privacy,
and unverified trust. It cannot publish canonical owner content or perform actions. Search results
show `trust=unverified` and `source_origin=unknown` without a raw source reference.

Automated captures are immutable, searchable, and included in full Portable Brain export. Version
0.1.0 has no selective deletion, session rollback, or certified purge. An untrusted or looping client
can leave unwanted durable content. Stopping the process prevents further writes but does not remove
completed captures. This retention limitation is accepted for the first release and disclosed in the
capture flag help and README configurations.

A process permits 500 valid capture attempts, 16 MiB of aggregate UTF-8 capture input, and 2,000 valid
search attempts. Duplicate deliveries, conflicting deliveries, and backend failures count. The next
over-limit call fails before engine work. Invalid arguments do not count; restarting resets the
counters. These limits bound accidental loops, not another process running as the same OS user.
Idempotency keys are hashed into a distinct MCP namespace and bound to exact input text. They are
never returned or stored as raw identifiers. Conflicts add no capture but retain the engine's bounded
quarantine evidence. Results remain untrusted data, not instructions or authorization.

## Retained appliance and connector controls

The retained single-user appliance profile is one owner and one private Brain root. Its provider mode is `none`, and
its connector allow-list is empty with egress disabled by default. The app wheel has no connector
dependency and passes its installed contract with the connector distribution absent. An explicit
`JOB-029` configuration can enable the bounded YouTube reference proof, but it receives only a
capture-accept capability. It cannot route to a space, approve a review, publish owner output, or
perform an action.

Connector discovery reads installed entry-point metadata and returns only explicitly enabled
names. It does not load connector code in the app process. The provisional v1 worker request binds
the exact manifest, count budgets, and `host_mediated` network mode. The child starts with an empty
environment, direct socket APIs disabled, CPU/process/memory/time/output limits, and metadata-only
responses. The current reference conformance uses synthetic host-mediated media, commits a bounded
checkpoint receipt, and proves replay without a second submission. Secret values never cross the
worker protocol.

Missing, invalid, or ambiguous classification is local-only `hold` with no cloud or external egress authority. Later components may narrow authority but cannot broaden it.

Tool-capable model processing must run through a staged-asset execution boundary with explicit readable assets, no inherited credentials, no access to host, home, or source trees, no host sockets, bounded network authority, and redacted failures.

Production staged execution is available only where the operating-system confinement matrix passes. The Linux local-model runtime uses bubblewrap with an empty environment, no network, explicit read-only assets, bounded output, and process, CPU, memory, and file limits. Darwin downloaded-media execution uses the separately verified native sandbox boundary. Unsupported hosts fail closed.

Work-tier capture events use the built-in, policy-version-locked redactor. Its receipt binds the exact normalized extraction and redacted output. Private raw captures remain unchanged.

Only owner-authored work text can publish directly to the work inbox. Third-party web,
social, and video captures publish to saved content with provenance. Third-party text does
not become owner-authored work, and derived ideas or actions still require owner review.

Provider selection receives the full immutable privacy decision. An authorized cloud route still scans the final prompt for credential, contact, network, and private-path findings before constructing an adapter or resolving a credential. Failure selects no fallback provider.

Connector counters and checkpoint claims are untrusted connector output. The host owns the
budget meters and metadata receipt, records the exact sink-issued capture receipt with its
delivery ID and source reference, and advances a checkpoint only when that host evidence matches.
Rejected rows may advance past an ineligible input; an eligible row whose sink submission fails
keeps the retry cursor so later work is not starved and the evidence remains retryable.

Ledger model text is untrusted. Sanitized leaves are one line, escaped, redaction-checked, directive-checked, and revalidated at merge and synthesis boundaries. Third-party source text can enter review records for audit, but owner-authored output contains only owner text and a deterministic opaque capture reference.

All public search text uses an engine-owned projection before indexing and matching, then crosses
the same projection again before representation. This prevents a protected value from selecting or
ranking a result even when the returned excerpt would have been redacted. The projection protects
raw and bounded percent/HTML-encoded source references, bare SHA-256-shaped tokens, absolute
POSIX/Windows paths, credential assignments, storage-derived space slugs and canonical paths, and
other protected literals while retaining useful searchable text, opaque IDs, and bounded
provenance. Query explanations never echo query terms, and MCP retrieval IDs are random opaque
values rather than hashes or other derivatives of the query. Renderers consume the projection;
they do not implement separate redaction. Portable/source bytes and internal trusted records remain
unchanged.

Default Open Brain stores its live public-safe FTS5 projection as plaintext in
`.open-brain/state/phase1.sqlite3`. The separate `.open-brain/indexes/search.sqlite3` portability
snapshot is also plaintext, non-authoritative, and potentially stale. SQLite FTS and sorter scratch
may use operating-system temporary storage outside the Brain root. Owner-only permissions protect
these surfaces from other operating-system users, but Open Brain does not claim application-level
encryption, encrypted temporary storage, or resistance to another process running as the owner.

Markdown import adds one explicit local read surface. The command canonicalizes and descriptor-checks
only the absolute root the owner names, then follows no links during traversal or file reads. It
ignores Obsidian metadata directories, loads no plugins, and performs no source-tree writes. The
canonical source root, device, inode, and active-revision bookkeeping stay in operational SQLite and
are excluded from search and Portable export. Portable capture provenance retains only a random
import-root ID and normalized relative path. The sanitized canonical source root appears only in the
interactive new-root confirmation; JSON, errors, progress, search, and completed summaries
exclude it.

Imported bytes, filenames, derived titles, historical revisions, blobs, and public-safe search text
can contain personal information. They receive the same owner-only filesystem protection as other
default Open Brain data and remain readable plaintext. Every imported capture is third-party source
with unknown content origin, automation-absent owner context, unverified trust, personal-local-only
privacy, and hold intent. Markdown, frontmatter, wiki links, embeds, HTML, and code blocks remain
inert content; import does not treat them as instructions or authorization.

Source deletion removes an imported note only from live search. Its immutable capture, filename,
bytes, and prior revisions remain in local history and Portable export. Version 0.1.0 has no
per-root or per-file purge and no supported post-import erasure workflow. First registration
requires an explicit retention acknowledgment before any import state is written.
