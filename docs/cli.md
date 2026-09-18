# Command-line interface

`open-brain` is one foreground command. Stateful commands create or reopen the platform-local Brain,
perform the requested operation, close SQLite resources, and exit. No separate init, daemon, HTTP
server, service unit, container, or privileged setup is required.

## Commands

This reference describes the Core v0.1 candidate. `open-brain --help` lists command families and
`open-brain COMMAND --help` gives exact options. Product version alone does not prove the installed
release contains them. `catalog --json` reports actual registrations and compatibility without
opening a Brain. See the [feature/version matrix](core-v01-features.md).

| Command family | Purpose / reference |
|---|---|
| `init`, `capture`, `import`, `status`, `export` | Local storage lifecycle; [first-use](first-use.md), [install](install.md) |
| `catalog` | Versioned metadata; no grants or public certification |
| `search`, `search-page`, `read` | Lexical retrieval and complete projected text; [records](records-and-history.md) |
| `history list/show`, `relationship list/decide`, `decision history` | Retained evidence; owner relationship mutations |
| `space create/list/rename`, `inbox list/route`, `source route` | Explicit organization; [spaces](spaces-inbox.md) |
| `review propose/list/show/approve/reject/edit-and-approve` | Source-bound drafts and inspected decisions; [review](review-publication.md) |
| `agent setup` | Preview/apply/remove client fragments with explicit grants; [agent setup](agent-setup.md) |
| `workspace` | Setup/status/refresh, note lifecycle/conflicts and recovery; [workspace recovery](managed-workspace-recovery.md) |
| `obsidian-plugin install/status/remove`, `plugin` | Packaged assets and foreground stdio bridge; [install](install.md) |
| `graph` | Existing structural graph/consent/exclusion/suggestion operations; provider readiness remains unassessed |
| `doctor --check NAME --json` | One bounded check; [doctor](doctor.md) |
| `mcp` | Explicitly granted foreground stdio session |

Table slashes denote alternative actions, not literal shell syntax. Detailed placeholder templates
are in the linked references; the first-use and doctor guides contain the executable examples.
There is no `vault` command; the managed vault is the `Open Brain Vault` sibling of the Brain root.

Commands that support machine output accept `--json`. The shared `--data-dir` option must be an
absolute path. Without it, the CLI uses the platform data directory. `OPEN_BRAIN_ROOT` is ignored.

`workspace setup` creates or reopens the dedicated `Open Brain Vault` sibling beside the private
Brain directory. Explicit `workspace refresh` adds newly accepted canonical pages and advances
eligible existing notes to the current approved publication, keeping their registered paths.
Unchanged publications do not rewrite bodies; representative-capture metadata changes can create
an identical-body revision without a file write. Owner edits and links are retained; divergence
requires explicit conflict resolution and materialization. Missing, inactive, excluded, and already
conflicted notes are not overwritten. Retrying a refresh request finishes only its original bound
work. Observation, acceptance,
materialization, deactivation, restoration, conflict resolution, consent, exclusions, and suggested
link acceptance are explicit owner operations. State-only actions do not rewrite Markdown.

## Status and doctor

Status reports profile `local`, storage `sqlite`, daemon false, and application encryption false.
Doctor checks the private data directory, foreground runtime boundary, base dependency closure, and
search index. The four valid names are `private-data-directory`, `foreground-runtime`,
`base-dependency-closure`, and `search-index`; bare `doctor` is invalid. JSON returns `check` and
`status` (`ok` or `failed`), with exit 0 or 1 respectively. Neither command probes or manages a service.

## MCP

`open-brain mcp` speaks MCP over inherited stdio. At least one capability flag is required. Workspace
read exposes only path-free status and pending suggestions. Graph refresh uses the already configured
provider and current owner consent; it cannot select a provider, change consent or exclusions, accept
a link, resolve a conflict, or mutate a note. Until a provider adapter is configured, refresh returns
`provider_not_configured` without an inference attempt. The process owns no listener and ends at EOF.

Capture, search, content-read, history-read, inbox-read, organize, review-read, review-propose and
review-decide are independent `--allow-...` flags; workspace-read and graph-refresh are additional
manual MCP grants. Search alone does not grant full content or history. Review decisions publish
only after token-bound inspection. `tools/list` reflects session grants, while `brain_catalog`
accepts `{"schema_version":2}` for metadata. Neither catalog discovery nor source text can widen
permissions. CLI setup and the shared bridge support nine grants. The desktop setup UI exposes
capture/search; Obsidian has no agent-setup control. See [agent setup](agent-setup.md) for privacy
and client activation.

Per process, workspace reads are limited to 500 calls and 16 MiB of serialized results. Graph refresh
is limited to 20 requests, 40 actual model attempts, and 1 MiB of selected note input. These limits are
in addition to the engine's durable per-workspace provider budget. Restarting an explicitly launched
MCP process resets only the process limits.

## Exit behavior

Invalid usage returns 2. Temporary SQLite writer contention returns 75 for capture, search, import,
or MCP work. A private-directory or operation failure returns 78 without exposing sensitive paths.
Interrupted Markdown import returns 130.

Secure Node and predecessor command families are historical source under `archive/`. They are not
installed commands and are not supported through the Open Brain executable.
