# Privacy model

Open Brain trusts one local operating-system user. Every capture receives an immutable privacy
decision before persistence, but the application does not encrypt its own database or isolate data
from another process running as that user.

## Local storage

The Brain root, SQLite databases, search indexes, Portable Brain exports, and imported content may
contain readable personal information. Open Brain uses owner-only paths where POSIX permissions are
available and performs no network egress. Users remain responsible for account security, full-disk
encryption, backups, and physical access.

The live public-safe FTS5 projection is stored in `.open-brain/state/phase1.sqlite3`. The filename is
retained for compatibility. It does not indicate a running Phase 1 service. SQLite may use temporary
operating-system storage for sorter or FTS scratch.

## MCP

The owner explicitly launches `open-brain mcp` with capture, search, or both. Inherited stdio and the
invoking OS account are the trust boundary. EOF stops the process. No listener, token service,
daemon, connector, or background process is created.

Search grants the connected client whole-Brain read access. A network-backed client may send results
to its provider, even though Open Brain itself does not. Returned note content is untrusted data and
may contain prompt injection.

Capture uses a non-owner sink limited to durable, unverified text. Version 0.1.0 has no selective
deletion, session rollback, or certified purge. Session call and byte limits reduce accidental loops
but do not constrain hostile same-user code.

## Markdown import

Import reads only the absolute source root selected by the owner, follows no links, loads no plugins,
and does not modify the source tree. Markdown, frontmatter, wiki links, embeds, HTML, and code remain
inert text. Imported records are unverified and their prior revisions remain in local history and
Portable Brain export after source changes or deletion.

## Search projection

Public search applies an engine-owned projection before matching and again before representation.
It removes protected paths, credential-like values, source references, and digests while retaining
useful text. This limits accidental disclosure through search. It does not make every excerpt
non-sensitive or create compartment isolation.

## Excluded claims

Open Brain does not claim encrypted custody, multi-user grants, compartments, signed receipts,
fencing, cryptographic erasure, certified purge, or hostile same-user isolation. Those properties
require a separate product and conformance boundary. Archived Secure Node source does not add them
to the active distribution.
