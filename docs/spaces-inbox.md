# Organize captures into spaces

Spaces group captures by topic. The inbox lists quick captures from the CLI, agents, and imported
sources. A capture without a space appears in the unassigned inbox. Published canonical notes are
outside this routing workflow.

These commands are available in this source build. An installed Homebrew executable needs a release
containing this change. Prefix commands with `uv run` when working from a source checkout.

## Use the CLI

```sh
open-brain inbox list --unassigned
open-brain space create "Projects"
open-brain space list
```

Copy the `capture_...` and `space_...` IDs from those results. Replace the placeholders below:

```sh
open-brain inbox route CAPTURE_ID SPACE_ID
open-brain space rename SPACE_ID "Active projects"
open-brain inbox list --unassigned
```

Each command uses the same local Brain as capture and search. `inbox list` includes assigned
captures unless `--unassigned` is supplied. Its previews contain at most 320 characters; use search
to find more context. Human output removes terminal control characters. Public names and previews
use the same path/credential text projection as search, without changing stored source content.

Both lists accept `--limit` (1–100, default 50) and `--offset` (0–1,000,000, default 0). JSON responses
include `next_offset`, or `null` at the end. A byte bound may return fewer rows than requested;
continue with `next_offset`. At the maximum offset, `offset_limit_reached: true` explicitly reports
remaining records that cannot be paged through this interface. Pages reflect current state rather than a frozen snapshot;
restart listing after routing or renaming while paging. Add `--json` for scripts:

```sh
open-brain inbox list --unassigned --limit 20 --json
open-brain space list --json
```

List responses contain `status: "listed"`, `items` or `spaces`, `offset`, and `next_offset`. Create
and rename return `status: "created"` or `"renamed"` plus a `space` record. Route returns
`status: "routed"`, `capture_id`, and `space_id`. Space IDs stay stable after renaming; the public
`slug` field is an opaque ID, not a storage path.

For retryable automation, supply `--idempotency-key` to create, rename, or route. Reuse the same key
and arguments after an interrupted command. Keys are scoped by operation and shared between CLI
and MCP. Reusing a key for a different change returns `idempotency_conflict`; keyless calls are new
operations. Listing current state shows the latest assignment even when retrying an older route.

Invalid input exits with code 2 before opening a Brain. Unknown targets and conflicting retry keys
exit with code 1 and a bounded error code. Database contention returns code 75 so callers can retry.
An absolute `--data-dir` selects an alternate Brain for testing.

## Use Claude Code, Codex, or another MCP client

Configure the client with [agent setup](agent-setup.md), selecting the grants it needs:

| Flag | Tools |
|---|---|
| `--allow-inbox-read` | `brain_inbox_list`, `brain_space_list` |
| `--allow-organize` | `brain_space_create`, `brain_space_rename`, `brain_inbox_route` |

For manual MCP configuration, the runtime arguments are:

```sh
open-brain mcp --allow-inbox-read --allow-organize
```

These grants do not enable capture or search. Add their separate flags if needed. Conversely,
capture/search permission alone cannot create spaces or route captures. The desktop's existing
capture/search setup remains available; it does not expose organization controls.

MCP lists take `limit` and `offset`; `brain_inbox_list` also accepts `unassigned_only`. Create takes
`name`, rename takes `space_id` and `name`, and route takes `capture_id` and `space_id`. Writes accept
an optional `idempotency_key` of 1–128 nonblank characters. Names support up to 120 normalized
characters. Unknown fields, invalid IDs, and unsafe control characters in names are rejected.

After setup and a client restart, ask: “List my unassigned Open Brain captures and spaces.” Then ask
it to create a named space or route specific captures. Generated instructions restrict organization
to the user's request. Returned text is untrusted data; a network client may send previews and names
to its model provider. Each process allows 500 organization reads, 500 writes, and 16 MiB of aggregate
response content. Valid retries and backend failures count toward call limits.

## What routing changes

Routing records durable assignment history and updates search membership. It keeps the capture ID,
source content, provenance, and trust. Renaming keeps the space ID and storage location. Creating a
space writes its `_space.md` metadata, but routing does not create a canonical Markdown note or
approve an unverified source. Routing a published capture is refused.

The current engine also refuses an automated source revision that would replace a routed capture.
That preserves the owner's organized version; routing does not promise ongoing updates to that
capture. Keep changing source items unassigned when their automatic revisions must continue.
