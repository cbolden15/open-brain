# Markdown import design

Default Open Brain imports one local Markdown directory through:

```sh
open-brain import /absolute/path/to/notes [--yes] [--allow-large-vault]
```

The command reads the selected directory once, captures eligible files through the existing
Portable Brain record model, and leaves the source tree unchanged. It adds no watcher, plugin,
daemon, network call, model, YAML dependency, or general importer framework.

This document specifies OB1-W4 in the [Open Brain product-completion
plan](plans/2026-09-08-ob1-product-completion.md). It applies only to the five-minute default Open
Brain product and must preserve the exclusions and timed journey in
[five-minute install acceptance](acceptance/five-minute-install.md).

## Public command and engine boundary

`DIRECTORY` must be an existing absolute directory. Relative paths, `~`, lexical `.` or `..`
components, NULs, and the filesystem root are rejected without echoing the supplied value. The
owner-supplied root may contain an intentional host alias such as macOS `/tmp`; the race-aware
canonicalization and descriptor checks below apply before traversal. No child traversal or file
open follows a symlink.

The CLI may initialize the normal private Brain before import preflight, as every default command
does. A failed import preflight creates no import-root, import-file, revision, capture, blob, or
search state. The task holds the existing single-writer lease once from root validation through
enumeration, registration, per-file commits, and missing-path finalization. This lease is
non-reentrant and nonblocking. Another local mutation waits zero seconds, performs no import
mutation, and returns 75 with `local_operation_busy`.

The command calls one `MarkdownImportTask` on the local `EngineTaskSet`. It does not join the
retained Phase 1 task set or Secure Node composition. `--allow-large-vault` applies only to this
invocation and only to the three aggregate enumeration limits described below.

Before registering a new root, the command finishes aggregate preflight and shows a confirmation
summary. Interactive human mode writes the sanitized canonical root, selected-file count, aggregate
metadata size, and this warning to stderr:

```text
Imported revisions remain in history and export after source removal.
```

It then asks `Continue? [y/N]`; only ASCII `y` or `yes`, matched case-insensitively, proceeds.
The prompt waits at most 60 seconds while the lease is held. Any other answer, timeout, or
end-of-input prints
`Markdown import cancelled; no import state was written.` and returns 0. `--yes` records the same
acknowledgment without a prompt. JSON mode and non-interactive human mode never prompt; a new root
without `--yes` returns 78 with `import_confirmation_required`, observed counts, and the literal
retry flag without exposing the root. Its message is exactly:

```text
Import requires confirmation. Review the selected directory and retention warning, then retry with --yes.
```

Import has no standalone preview, post-import rollback, or all-or-nothing mode. The new-root
confirmation is an aggregate safety check, not a file-by-file preview. Each accepted file becomes
durable as it completes, and reruns resume rather than roll back completed captures.

The authoritative operation order is:

1. Validate the raw CLI argument and open the normal private Brain.
2. Acquire the single-writer lease, pin the root, and recheck every root relationship.
3. Generate prospective root and scan IDs in memory, then enumerate and enforce all aggregate gates
   without an import-state write.
4. For a new root, obtain the confirmation above. Register the root only after confirmation.
5. Process files one at a time through the lock-held capture path.
6. Revalidate the canonical root identity, then commit missing-path finalization.

## Root and source identity

The importer resolves the owner-supplied root strictly, opens that canonical path one component at a
time from `/` with directory and no-follow flags, and pins its device and inode. It then resolves
and descriptor-opens the supplied root independently a second time. Both canonical strings and both
descriptor identities must match. All traversal uses only the first pinned descriptor. The stored
canonical path is reopened and checked again before registration and before missing-path
finalization. These checks detect ordinary resolve/open replacement races without claiming
integrity against malicious code already running as the owner.

The same directory identity reuses the same random `import_root_*` ID, including a case alias on a
case-insensitive macOS filesystem, only while the stored canonical path still opens to that identity.
A different directory at the same stored path fails with `import_root_changed`.

Moving, rebinding, or folding a registered root is not supported in version 0.1.0. A rename or move
that preserves device and inode is refused at its new path with `import_root_changed` and must be
restored to its registered path. A copied or recreated tree with a genuinely new identity may
register at a new, disjoint path. A different identity at the registered path is also
`import_root_changed`. There is no root-rebind command.

Before every scan or import-state mutation, the importer classifies the candidate as the exact
stored root, a live alias, a moved known identity, a replaced stored path, or a new identity. It
walks opened parent-directory identities in both directions against every currently reachable
registered root. An ancestor, descendant, or exact overlap fails with
`overlapping_import_root`; the candidate's own identity is excluded only after its stored path has
been revalidated. The same check includes the active Brain directory, so the Brain cannot import
itself, contain an import root, or sit below one.

An unrelated unreachable registered root does not block the candidate. If its stored canonical path
is equal to, above, or below the candidate path, the importer cannot disprove overlap and fails
closed with `overlapping_import_root`. All relationships are checked again before a new root row is
committed.

Absolute host paths stay only in operational SQLite state. A Portable source reference contains the
opaque root ID and percent-encoded normalized relative path:

```text
urn:open-brain:markdown-import:<root-id>:<relative-path>
```

It contains no absolute path or content digest. The relative path is encoded with
`urllib.parse.quote(relative_path, safe="/")`: separators remain literal, `%` becomes `%25`, and
non-ASCII UTF-8 bytes use uppercase percent triplets. The full ASCII source reference is limited to
65,536 characters by the existing capture contract. Given the fixed 31-character prefix,
48-character `import_root_<uuid>` ID, and separating colon, the encoded relative path may contain at
most 65,456 characters. A longer value is an `invalid_path` failure found during enumeration before
revision reservation; `--allow-large-vault` never bypasses it. Search output never returns the raw
reference.

## Operational schema

W4 adds three tables to the unreleased `PRAGMA user_version=1` database. W5 later adopts them into
the first supported migration ledger.

`markdown_import_roots` stores the random root ID, absolute operational path, device and inode as
validated decimal text, creation time, and the last complete scan ID and time. Root identity and
stored path are unique.

`markdown_import_files` stores a random logical file ID, root ID, NFC-normalized POSIX relative
path, active revision ID, last observed scan ID, and the last safely observed file identity. The
pair of root ID and relative path is unique. File identity follows the logical path, so a rename is
an inactive old path plus a new path even if the filesystem inode is unchanged.

`markdown_import_revisions` stores a random revision ID, file ID, SHA-256 content digest,
deterministic delivery ID, optional capture ID, and first observation time. File ID plus digest is
unique. A nullable capture ID is a resumable reservation: a crash after reservation or capture is
finished by the next run with the same delivery ID.

The `markdown-import.` delivery namespace is reserved for this task. Capture accepts a delivery in
that namespace only when a matching pending revision exists and the request carries the fixed
importer actor, role, and deterministic request fields. Projection suppression depends on that
matching reservation, not on the prefix alone. Other capture contexts cannot use the namespace.

These tables are operational and excluded from Portable export. Captures and blobs remain the
portable history.

## Relative paths and eligibility

Directory traversal is iterative and deterministic. Entries within each directory sort by their
raw filesystem bytes. Valid paths process in normalized relative-path order. Invalid raw path bytes
sort after valid paths by their original byte sequence and are rendered only as stable run-local
tokens such as `<invalid-path-000001>`; raw invalid bytes never reach terminal or JSON output. Every
visited directory entry counts once. The supplied root itself does not count.

Each relative component must be representable as UTF-8, normalize to Unicode NFC, and contain no
NUL, slash, backslash, C0/C1 control, or Unicode format character. Empty, `.` and `..` components are
invalid. If multiple raw paths normalize to one relative path, every member is a
`path_collision` failure. Paths are not case-folded, so distinct Linux files remain distinct.

Only regular files whose names end in exact lowercase `.md` are selected. Empty Markdown files are
eligible. Dot-directories, including `.obsidian`, are counted as skipped and are never opened or
descended. Non-Markdown leaves, symlinks, hardlinks, FIFOs, sockets, devices, and other special
objects are skipped. Dot-files outside a dot-directory remain eligible when their names end in
`.md`.

The importer never follows a Markdown link, wiki link, image, embed, HTML reference, frontmatter
value, or Obsidian plugin reference. Markup is inert text.

## Bounds and scan order

Before the first import-state or capture write, metadata enumeration enforces:

| Limit | Default boundary | `--allow-large-vault` |
|---|---:|---|
| Visited entries | 100,000 pass; 100,001 refuses | Bypassed |
| Selected Markdown files | 10,000 pass; 10,001 refuses | Bypassed |
| Aggregate observed selected bytes | 536,870,912 pass; the next byte refuses | Bypassed |
| One file | 1,048,576 bytes pass; the next byte fails that file | Never bypassed |

The selected-file count and aggregate size use no-follow metadata collected during deterministic
enumeration. Selected files that are later found oversized, unreadable, invalid, or changed still
count, so malformed inputs cannot evade an aggregate gate. A total-limit refusal uses
`large_vault_confirmation_required`, creates no import state, and includes observed counts, exact
limits, and the exceeded field names without including any path or content. Human output includes
this exact recovery sentence:

```text
Vault exceeds the default import limits. Retry with --allow-large-vault.
```

The JSON failure uses the normal fatal envelope with bounded details:

```json
{
  "error": {
    "code": "large_vault_confirmation_required",
    "details": {
      "exceeded": ["selected_markdown_files"],
      "limits": {
        "aggregate_bytes": 536870912,
        "selected_markdown_files": 10000,
        "visited_entries": 100000
      },
      "missing_finalized": false,
      "observed": {
        "aggregate_bytes": 10485760,
        "selected_markdown_files": 10001,
        "visited_entries": 10001
      }
    },
    "message": "Vault exceeds the default import limits. Retry with --allow-large-vault."
  },
  "status": "failed"
}
```

The retry flag does not weaken path, UTF-8, regular-file, hardlink, race, or per-file checks.

After aggregate preflight passes, files are processed one at a time in normalized-path order. The
root descriptor stays open. Each candidate's parent components are reopened without following
links and checked against the identities observed during enumeration. The file is opened with
no-follow and nonblocking flags so a regular-file-to-FIFO swap cannot hang. Descriptor metadata
must still describe the same regular one-link file. Reads stop after 1,048,577 bytes, and metadata
before and after the read must agree on device, inode, type, link count, size, modification time,
and change time. The byte count must equal the stable size. A mismatch is a `file_changed` failure;
outside or replacement bytes are never captured.

Normal reads can update access time on filesystems that track it. “Leaves the source unchanged”
means Open Brain issues no source write, rename, permission, or deletion operation. The same-user
trust limitation in the threat model still applies; these checks do not claim an immutable snapshot
against hostile code running as the owner.

## Content, title, and capture

The stable bytes must decode as strict UTF-8 and contain no NUL. The exact original bytes, including
an optional UTF-8 BOM, CRLF line endings, trailing whitespace, frontmatter, wiki syntax, and HTML,
are passed to `FilePayload` as `text/markdown`. `FilePayload` accepts empty bytes for this valid
case. Content-addressed blob storage deduplicates identical bytes without merging distinct source
paths.

Display-title extraction changes no source bytes and adds no YAML parser. One leading UTF-8 BOM is
ignored for title parsing only:

1. Treat the first logical line as a frontmatter opener only when it is exactly `---` and an exact
   closing `---` occurs within the first 100 logical lines and first 65,536 source bytes. Inspect that
   bounded block for one unindented, single-line `title:` scalar. Plain text, JSON-style double
   quotes, and YAML single-quote doubling are supported. Empty, duplicate, tagged, aliased,
   collection, and block scalar values are ignored.
2. If no supported frontmatter title exists, use the first ATX heading at level 1 through 6 after a
   recognized frontmatter block. When no bounded closing delimiter exists, treat the opening `---`
   as body text and search the whole document for the first ATX heading.
3. Otherwise use the filename stem.
4. Normalize to NFC. Replace each C0 control (`U+0000` through `U+001F`), DEL/C1 control
   (`U+007F` through `U+009F`), and Unicode `Cf` format character with one ASCII space
   (`U+0020`). Collapse every Unicode whitespace run to one ASCII space and strip leading and
   trailing spaces. Keep the first 200 Unicode code points. An empty result becomes `Untitled note`.

Each new revision is reserved before capture. Its delivery ID is `markdown-import.` plus SHA-256 of
canonical JSON containing a domain marker, root ID, normalized relative path, and content digest.
This makes reruns idempotent without merging identical content at different paths.

The importer submits through a fixed non-owner, capture-only public-job context. It uses
`source_origin=unknown`, `provenance.content_origin=unknown`, and
`provenance.owner_context=automation_absent`. The durable Portable record consequently uses
`source.origin=third_party` and `trust.label=unverified`. Imported content never gains owner trust,
automatic publication authority, or a space assignment.

While holding the outer writer lease, the importer constructs the same
`CaptureSubmission.for_public_job` value but calls an engine-private lock-held capture primitive.
It must not call `CaptureTasks.submit`, `PublicJobCaptureSink.submit`, or another public mutation
wrapper that reacquires the non-reentrant lease. The public wrappers retain their normal
lease-acquiring behavior; W4 does not make `FileLease` reentrant.

Every imported capture uses privacy tier `personal`, privacy reason `personal_local_only`,
local-only authority (`cloud=false` and `external_egress=false`), and intent `hold`. Import cannot
create an idea, action candidate, review proposal, or network-capable operation.

An import reservation is not searchable. Capture processing recognizes its reserved delivery ID
and withholds the source projection until activation. This closes the crash window between capture
and the active-revision switch.

## Revision and missing-path behavior

Each file completes with one import-state transaction:

- A new logical path creates one capture and becomes `imported`.
- New bytes at an existing path create one immutable capture, activate it, remove the prior source
  projection, and become `updated`.
- The active digest at an existing path performs no capture and becomes `unchanged`.
- A previously observed digest at an inactive or changed path reuses its original capture, switches
  the active projection, and becomes `updated` without a new record. Revision lookup covers the
  file's full history, so an A to B to A change creates two captures and reactivates A.

Activation attaches the reserved capture to its revision, switches the file's active revision,
deletes any superseded source search row, and inserts the chosen public-safe source projection. A
retry after a crash uses the deterministic capture delivery and finishes the same transition.

Only a complete deterministic enumeration and completed per-file outcome loop finalizes absence. It
marks unobserved active paths inactive, removes their source projection, and reports them as
`missing`; their captures and blobs remain in Portable export. Every selected-file failure and
skipped object counts as observed at its safely normalized relative path, so one invalid, oversized,
unreadable, or race-changed file preserves its own prior active revision without freezing deletion
handling for unrelated paths. An unreadable directory, incomplete traversal, interruption, or fatal
storage failure can conceal paths and therefore withholds all missing-path finalization.

Full live-search re-derivation joins capture delivery IDs to every import revision, including a
pending revision whose capture ID has not yet been attached. A capture with a matching import
revision is projected only when that exact revision is the file's active revision. This must not
resurrect a pending, superseded, or missing imported capture. Ordinary non-import captures remain
searchable.

Portable export includes every immutable imported revision and exact blob, including inactive
history. Root paths and operational activation tables are excluded. The opaque root ID and relative
source path group revisions without locking the data to this SQLite layout. A future Secure Node
import may choose its own active-state policy; Open Brain does not claim that operational missing
state is a portable deletion or purge instruction.

Removing or changing a source file does not erase its prior captures from history or export. Version
0.1.0 has no per-root purge, per-file forget, unregister, rollback, or compaction command. W4 offers
no supported post-import erasure workflow. The mandatory first-root confirmation, CLI help, and
machine-readable summary state this retention rule before the owner relies on import as a cleanup
mechanism.

## Summary and failures

JSON output has this fixed shape:

```json
{
  "entries": [
    {"outcome": "skipped", "path": ".obsidian", "reason": "dot_directory"},
    {"outcome": "imported", "path": "notes/example.md"}
  ],
  "entries_omitted": 0,
  "failed": 0,
  "history_retained_after_source_removal": true,
  "imported": 1,
  "missing": 0,
  "missing_finalized": true,
  "selected": 1,
  "skipped": 1,
  "status": "completed",
  "unchanged": 0,
  "updated": 0
}
```

Outcomes are `imported`, `updated`, `unchanged`, `missing`, `skipped`, and `failed`. Reasons come
from a closed set: `dot_directory`, `non_markdown`, `symlink`, `hardlink`, `special_file`,
`file_changed`, `file_too_large`, `invalid_utf8`, `invalid_content`, `invalid_path`,
`path_collision`, and `unreadable`. NUL-bearing decoded content uses `invalid_content`, not
`invalid_utf8`.

The fixed `MAX_SUMMARY_ENTRIES` is 100 total entries. Failed outcomes consume that shared cap first
in normalized-path order, then every other outcome fills the remaining slots in normalized-path and
outcome order. Invalid paths follow valid paths in their raw-byte order and use the opaque tokens
described above. `len(entries)` is `min(total_outcomes, 100)`, and `entries_omitted` is
`total_outcomes - len(entries)`. Counts always cover the full run and are authoritative when the
list is truncated.

Human output prints `selected` plus the six outcome counts and `missing_finalized` on one line,
followed by the same capped relative entries. It prints
`No eligible Markdown files found.` when `selected=0`. When results are omitted, it appends
`1 additional result omitted; counts above include the full run.` or
`N additional results omitted; counts above include the full run.` It passes paths through
terminal-control sanitation. The interactive new-root confirmation is the only output allowed to
show the sanitized canonical source root. Completed summaries, JSON, errors, and progress include no
absolute root, digest, source reference, content excerpt, exception text, device, or inode.

A completed deterministic scan sets `missing_finalized=true` only after the absence transaction
commits. It returns 0 and `status=completed` when `failed=0`; skips and zero selected files are
allowed. It returns 1 and `status=partial` when `failed>0`; deterministic file failures preserve
their own prior revisions but do not prevent unrelated missing paths from finalizing.

Usage errors return 2. A retryable writer conflict waits zero seconds and returns 75 with
`local_operation_busy` and this message:

```text
Another Open Brain command is using this Brain. Retry after it finishes.
```

A fatal root, confirmation, overlap, scan, or aggregate-bound failure returns 78. Its closed codes
are `import_directory_unavailable`, `import_confirmation_required`,
`overlapping_import_root`, `import_root_changed`, `import_scan_incomplete`,
`large_vault_confirmation_required`, and the existing `local_operation_failed` fallback.
`overlapping_import_root` identifies only a safe conflict kind and, for another import, its opaque
root ID. Its human message is one of:

```text
The selected directory overlaps Open Brain data. Choose a directory outside the Brain data tree.
The selected directory overlaps registered import <root-id>. Use that registered import or choose a disjoint directory.
```

`import_root_changed` identifies the opaque root ID and uses:

```text
The selected directory no longer matches registered import <root-id>. Restore it at its registered location, or copy the content to a new disjoint directory.
```

`import_scan_incomplete` uses:

```text
Markdown import could not complete the directory scan. Fix directory access or filesystem changes, then retry.
```

Completed-run JSON uses the summary shape above. Fatal JSON instead uses
`{"error":{"code":"...","message":"..."},"status":"failed"}`, with the bounded `details`
object shown above only when safe metadata helps recovery. Every import-specific fatal envelope
states `"missing_finalized":false` in `details`. A new-root confirmation failure also provides
`selected_markdown_files`, `aggregate_bytes`, and
`history_retained_after_source_removal=true`. Fatal human errors go to stderr.

Interactive human mode writes metadata-only progress to stderr after every 10,000 visited entries
and every 100 processed Markdown files. It prints no progress when stderr is not a TTY, and JSON
stdout remains clean.

The CLI installs an import-specific SIGINT handler that records an interruption request instead of
raising in the middle of a transaction. Enumeration checks the flag after each visited entry, once
its counters and candidate state agree. The confirmation input loop polls at most every 250
milliseconds so it can honor the flag. The task checks again immediately before root registration,
after each stable file read but before revision reservation, after each durable per-file outcome and
its in-memory count agree, and immediately before missing-path finalization. A request observed at
any safe point stops before the next mutation, retains completed per-file commits, and returns 130.
A `KeyboardInterrupt` raised before that completion boundary has the same result.

Starting the missing-path transaction crosses the completion boundary. From that point through
completed-summary delivery, SIGINT is deferred and cannot change the run to an interrupted result.
If a `KeyboardInterrupt` is raised after the transaction commits, the command reads the committed
finalization state and emits the actual completed or partial summary with
`missing_finalized=true`. Tests inject interruption immediately before and immediately after the
commit boundary.

The pre-boundary interruption writes this exact envelope to JSON stdout and nothing to stderr:

```json
{
  "error": {
    "code": "import_interrupted",
    "details": {"missing_finalized": false},
    "message": "Markdown import interrupted. Completed file commits were kept; rerun the same command to resume."
  },
  "status": "interrupted"
}
```

Human mode writes this exact message to stderr, after any earlier TTY progress, and writes no summary
to stdout:

```text
Markdown import interrupted. Completed file commits were kept; missing paths were not finalized. Rerun the same command to resume.
```

Interrupted output intentionally has no counts. Completed file commits remain durable, and rerunning
the same command produces an authoritative completed summary.

## Verification boundary

W4 tests build every symlink, hardlink, FIFO, socket, invalid byte stream, oversize file, and swap
race under a temporary directory. The committed fixture contains only synthetic regular Markdown.
Tests cover the exact bound and one-past-bound behavior with injected smaller limits while asserting
the production constants.

The native smoke copies the committed fixture into its temporary workspace, imports it through the
frozen executable, finds a nested token, proves an unchanged rerun, and verifies exact exported bytes
and unverified provenance. This runs after the unchanged timed five-minute acceptance journey.

NFS, FUSE, synchronized source trees, and hostile same-user mutation have no stronger guarantee in
the first release. Supporting them as trusted snapshots would require a separate filesystem
conformance design.
