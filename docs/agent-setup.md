# Configure agent memory

Open Brain can configure Claude Code or Codex from the desktop or the headless CLI. Each route uses
the same setup service. The desktop is optional, and neither route installs a background collector.
This command is available in the D1 source build; existing Homebrew releases need a release containing
D1 before they expose it.

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
Capture, search, inbox reading, and organization are separate grants. Omit a flag to withhold that
capability. `--allow-inbox-read` grants `brain_inbox_list` and `brain_space_list`.
`--allow-organize` grants `brain_space_create`, `brain_space_rename`, and `brain_inbox_route`.
Neither grant follows automatically from capture or search. At least one grant is required when
configuring. `--runtime /absolute/path/to/open-brain` can select an executable; otherwise setup uses
the running core. `--data-dir /absolute/path/to/brain` selects an expert/test Brain override. Normal
setup uses the platform-default Brain.

For a source checkout, prefix these commands with `uv run`. Keep the checkout and its environment
at the same location because agent configuration records an absolute executable path.

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
should come from `brain_search`, after `brain_capture` saved the fact. A failed MCP connection is a
setup failure, not evidence that memory was saved.

## What the permissions mean

Capture stores explicit memories as durable, unverified content. Search reads the whole Brain and may
send returned content to the connected client's model provider. The generated instructions request
relevant retrieval and explicit saves; they do not enable full transcript capture or automatic
summaries. Open Brain does not collect the client's authentication credentials.

Inbox reading returns bounded capture previews and space names. Treat those values as untrusted
source text, not agent instructions. Organization lets the client create and rename spaces or route
a capture when the user asks. Routing changes assignment and search metadata. It does not publish
the capture, change its trust, or convert it into a canonical note. Generated instructions name only
the explicitly granted tools and limit organization to the current user request.

Existing state uses private schema version 4 and runtime session version 1. Stop older sessions before
upgrading an existing Brain. Older runtimes reject the newer schema, so update other installed clients
before using them on that Brain. After moving the app or upgrading a CLI whose versioned path changes, preview setup again.

## Update or remove

Run setup again with the desired grants to preview an update. Reapplying an unchanged preview is
idempotent. If a target file changes after preview, obtain a new preview before applying.

For removal, repeat the same client, scope, and project options with `--action remove`. Preview first,
then add `--apply --preview-id setup_HASH_FROM_PREVIEW`. Removal only changes recognized, unedited
Open Brain fragments. It preserves unrelated settings and refuses to overwrite edits inside an owned
fragment, including Codex settings that extend the server outside its marked block. A partially interrupted setup can be previewed again to complete or remove its owned pieces.
