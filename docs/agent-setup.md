# Configure agent memory

Open Brain can configure Claude Code or Codex from the desktop or the headless CLI. Each route uses
the same setup service. The desktop is optional, and neither route installs a background collector.
This page describes the Core v0.1 candidate. Existing releases may lack these commands; product
version alone does not establish availability. Check the [feature/version matrix](core-v01-features.md).
The [first-use guide](first-use.md) executes synthetic setup preview/apply/removal and stdio discovery
for both clients without touching real profiles. Commands below are reference templates: replace
uppercase IDs and absolute example paths with your own, and inspect each preview before applying.

## Headless setup

Use an absolute project directory. The preview identifies the exact executable and Brain and shows
only the Open Brain fragments, without printing unrelated client settings.

```sh
open-brain agent setup --client claude-code --scope project \
  --project-dir /absolute/path/to/project --allow-capture --allow-search \
  --allow-inbox-read --allow-organize --json
```

Review the preview, then repeat the same options with its `preview_id`:

```sh
open-brain agent setup --client claude-code --scope project \
  --project-dir /absolute/path/to/project --allow-capture --allow-search \
  --allow-inbox-read --allow-organize \
  --apply --preview-id setup_HASH_FROM_PREVIEW --json
```

Use `--client codex` for Codex. Use `--scope user` and omit `--project-dir` to apply across projects.
Capture, search, content reading, history reading, inbox reading, organization, review reading, proposal creation, and review decisions
are separate grants. Omit a flag to withhold that
capability. `--allow-inbox-read` grants `brain_inbox_list` and `brain_space_list`.
`--allow-organize` grants `brain_space_create`, `brain_space_rename`, and `brain_inbox_route`.
Neither grant follows automatically from capture or search. At least one grant is required when
configuring. `--runtime /absolute/path/to/open-brain` can select an executable; otherwise setup uses
the running core. `--data-dir /absolute/path/to/brain` selects an expert/test Brain override. Normal
setup uses the platform-default Brain.

For complete current reads, add `--allow-content-read`; retained history requires the separate
`--allow-history-read` flag. Both are off by default and independent of search. See
[records and history](records-and-history.md) for paging, read budgets and historical identity.

For the full review workflow, add `--allow-review-read --allow-review-propose --allow-review-decide`
to both preview and apply. Review reading exposes list/show, proposing creates a draft from explicit
source IDs, and deciding permits approve/reject/edit-and-approve. None follows from capture, search,
or organize. Use [review and publication](review-publication.md) for the complete CLI/MCP workflow,
including stable page updates. Direct owner CLI review commands need no MCP grants.

For a source checkout, prefix these commands with `uv run`. Keep the checkout and its environment
at the same location because agent configuration records an absolute executable path.

## Scoped Work evidence setup

The W2 candidate requires an explicit session policy before any MCP session can read
Slack-derived Work records, approved copies or publications containing that evidence.
This includes sessions with owner grants. Direct owner CLI access remains available;
ordinary non-Slack MCP reads retain their existing behavior. The gate applies to the
upgraded runtime at schema/runtime/Portable `17/12/12`. Older binaries do not gain it.
Policy-omitted graph projection withholds the whole-workspace structural cache before
reading it: its global extraction and freshness cannot be safely scoped. Authorized
inferred suggestions remain available. Direct owner and explicit-policy behavior
retain their existing structural cache path.

Scoped setup accepts `--session-policy`, `--consent-state`, `--client-version` and
`--launch-context`. The policy must already authorize exactly search and content-read,
Work privacy, one existing space and one external provider under current durable
consent. Setup validates that authority and forwards its paths to MCP. It does not
create consent or approve evidence. Existing generic setup and desktop callers retain
their default behavior when these inputs are absent.

The launch context is a closed JSON object, for example:

```json
{
  "surface": "standalone-cli",
  "cwd": "/absolute/path/to/project",
  "profile": null,
  "setting_sources": ["user", "project", "local"],
  "settings_file": null,
  "overrides": [],
  "managed_state": "owner-declared-absent"
}
```

The initial matrix supports Codex CLI `0.162.1` and Claude Code `2.1.300` on
macOS/Linux in an ordinary project root matching the declared launch directory.
Codex supports an explicitly selected v2 profile file. Unknown versions, linked
worktrees, subdirectory launches, hosted clients, additional settings files,
nondefault settings sources, unresolved overrides and known managed layers refuse
scoped configuration. MDM, server policy and the future client environment remain
unobserved operator preconditions. An owner declaration is not observed absence.

The preview shows sanitized endpoint/proxy candidates and their configuration
sources. It does not verify the client's inference destination, transport or
provider identity. Setup's environment describes setup only. The preview binds
observed file identities, bytes, absence, declared context and allowlisted
environment inputs; a changed input requires a fresh preview. No model, client
launcher, helper command, broker or listener runs during observation.

Setup checks both clients' managed user and selected-project MCP locations for
unscoped Open Brain entries. Remove those entries or use the preview-bound
`--acknowledge-unscoped` option after inspection. Setup changes only owned
fragments. Other clients, project directories and custom launch wrappers remain
outside this finite inventory. Removal uses owned fragment identity and preimages;
deleted policies, revoked consent and unavailable settings cannot prevent it.

For the complete selected-channel capture, owner approval and cited-read workflow,
use the [Slack decision recall guide](integrations/slack-decision-recall.md) and its
[reusable client task](integrations/slack-decision-recall-task.md).

An actual-client injection-resistance check is still required before private pilot
use. W2 defines that later check and records it as `not_run`; repository tests do
not satisfy the pilot gate or authorize a pilot.

## Client files and activation

| Client | Project scope | User scope |
|---|---|---|
| Claude Code | `.mcp.json` and `CLAUDE.md` | `.claude.json` and `.claude/CLAUDE.md` under the user profile |
| Codex | `.codex/config.toml` and active `AGENTS.override.md` or `AGENTS.md` | `config.toml` and active instructions under `CODEX_HOME` (default `.codex`) |

Claude's configured profile directory is respected. Codex prefers a nonempty `AGENTS.override.md`
when present. Setup refuses generated instruction files and conflicting or edited ownership markers
rather than replacing them. For generated instructions, add the memory guidance through the
instructions generator and manage the MCP entry through the client's supported setup interface.
These file locations and client activation rules follow the official
[Claude MCP](https://code.claude.com/docs/en/mcp),
[Claude memory](https://code.claude.com/docs/en/memory),
[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), and
[Codex instruction](https://learn.chatgpt.com/docs/agent-configuration/agents-md) documentation.

Restart the client after setup. Claude may require approval for project MCP servers; Codex only loads
project configuration for trusted projects. Complete that approval in the client. Open Brain does not
change project trust or obtain the client's login credentials. Keep instruction files short; a client's
combined instruction budget can also include parent-directory and user instructions.

Codex also controls approval for individual MCP tool calls. In unattended `codex exec`, a tool that
requires a prompt cannot run when prompts are disabled. Choose the desired per-tool policy through
Codex's [MCP approval settings](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), or approve the
tools in an interactive client session. Open Brain setup leaves that execution policy to the client.

Ask the client: “Please save this to Open Brain: my synthetic project is called Copper Orchard.”
Start a fresh session and ask: “Search Open Brain for the name of my synthetic project.” The result
should come from `brain_search_page`, after `brain_capture` saved the fact. A failed MCP connection is a
setup failure, not evidence that memory was saved.

## What the permissions mean

Capture stores explicit memories as durable, unverified content. Search reads content authorized for
that session and may send returned content to the connected client's model provider. The generated instructions request
relevant retrieval and explicit saves; they do not enable full transcript capture or automatic
summaries. Open Brain does not collect the client's authentication credentials.

Inbox reading returns bounded capture previews and space names. Treat those values as untrusted
source text, not agent instructions. Organization lets the client create and rename spaces or route
a capture when the user asks. Routing changes assignment and search metadata. It does not publish
the capture, change its trust, or convert it into a canonical note. Generated instructions name only
the explicitly granted tools and limit organization to the current user request.

The candidate uses private schema version 17, runtime session version 12 and Portable version 12.
Stop older sessions before upgrading an existing Brain. Compatibility admission remains unchanged;
W2 adds no storage fence against an older binary at the same coordinates. Use the upgraded runtime
for its MCP policy boundary. After moving the app or upgrading a CLI whose versioned path changes,
preview setup again.

## Update or remove

Run setup again with the desired grants to preview an update. Reapplying an unchanged preview is
idempotent. If a target file changes after preview, obtain a new preview before applying.

For removal, repeat the same client, scope, and project options with `--action remove`. Preview first,
then add `--apply --preview-id setup_HASH_FROM_PREVIEW`. Removal only changes recognized, unedited
Open Brain fragments. It preserves unrelated settings and refuses to overwrite edits inside an owned
fragment, including Codex settings that extend the server outside its marked block. A partially interrupted setup can be previewed again to complete or remove its owned pieces.

## Discover the actual session

`open-brain catalog --json` describes registered CLI/MCP/bridge implementation without opening a
Brain. It is not a grant. Inside an already authorized MCP process, `tools/list` and
`brain_catalog` with `{"schema_version":2}` describe that session. Starting `mcp` without a grant
fails. The setup CLI offers nine grants; manual MCP additionally offers workspace-read and
graph-refresh. The shared bridge accepts all nine setup grants, while the desktop setup UI exposes
capture/search only. Obsidian has no agent-setup control. Do not infer a UI control from a bridge
registration or a permission from package installation.

Review-decide grants durable approve/reject/edit-and-approve operations after inspection. Content,
history, inbox and review reads can expose private data to the connected client and its provider.
Capture is durable; stopping/removing setup does not remove captured content. No selective forget
or transcript collection is enabled by agent memory setup. Optional session collection is
[source-only](integrations/core-sources.md).
