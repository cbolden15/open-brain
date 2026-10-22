# Optional harness memory setup

Open Brain can capture useful authored material at the end of a turn, recall
authorized memory before a related task, and offer on-demand memory search.
Codex, Claude Code, and Pi are separate optional integrations. Installation
does not grant access to a privacy tier. Each capture is tiered when it is
written. The Brain's owner egress policy and current provider consent decide
what each cloud reader can receive; see `docs/harness-memory.md`.

Install the public Open Brain package and the selected harness first. Codex and
Claude hooks need Node. Keep configuration, policy, consent, and spool files
outside source repositories. A bridge configuration binds the existing Brain
root and identity, owner policy, consent state, and the `outbox` directory that
holds the retry spool. See the
`HarnessBridgeConfig` schema in `packages/app/src/open_brain/services/harness_bridge.py`.
Setup does not create a second Brain, read transcripts, or open the configured
Brain. No private repository, fixed developer path, or remote host is required.

## Preview, apply, and remove

Use absolute paths for the installed runtime, Node executable, and owner bridge
configuration. For example, replace the sample paths below with your installed
paths and choose `codex`, `claude-code`, or `pi`:

```sh
open-brain harness setup --mode preview --setup-action install \
  --client codex --config /absolute/path/bridge.json \
  --runtime /absolute/path/open-brain --node /absolute/path/node
```

Read the returned fragments, destination paths, notices, and `preview_id`.
Apply the same request with the exact preview ID:

```sh
open-brain harness setup --mode apply --setup-action install \
  --client codex --config /absolute/path/bridge.json \
  --runtime /absolute/path/open-brain --node /absolute/path/node \
  --preview-id harness_setup_REPLACE_WITH_PREVIEW_DIGEST
```

Repeat preview and apply with `--setup-action uninstall` to remove unchanged
owned fragments. Setup preserves other hooks, settings, and MCP servers. It
refuses edited owned fragments and stale previews, including changed runtime,
Node, asset, bridge configuration, and settings preimages. A failed multi-file
apply attempts to restore only files still matching its own writes.

Applying installation first launches the selected runtime with `harness --help`
and allows up to fifteen seconds for native library validation. This command
does not open the Brain. Failed warm-up leaves hooks inactive; settings and
package preimages are checked again afterward. Preview and uninstall do not
launch the runtime. Warm-up happens before the shorter lifecycle deadlines.

Restart the selected harness after applying setup. Codex requires its native
review and trust of non-managed hook definitions before they execute. Setup
does not bypass that review or alter managed settings.

## Installed files and commands

| Harness | User-wide setup |
| --- | --- |
| Codex | `$CODEX_HOME/hooks.json` and `$CODEX_HOME/config.toml`; default `$HOME/.codex` |
| Claude Code | `$HOME/.claude/settings.json` hooks and `$HOME/.claude.json` MCP; with `CLAUDE_CONFIG_DIR`, `settings.json` and `.claude.json` beneath that directory |
| Pi | `$PI_CODING_AGENT_DIR/extensions/open-brain-memory.js`; default `$HOME/.pi/agent` |

Codex and Claude use `Stop` for capture and `UserPromptSubmit` for recall.
The command invokes the packaged `harness-hook.mjs` with fixed absolute
runtime/config paths and client identity. Recall adds at most five results and
eight KiB of untrusted reference context. Hook capture is bounded to two
seconds, recall to three. A queued status means the event is in the local retry
spool; it is not yet saved in the Brain. One bounded foreground drain invocation
provides a delivery attempt after enqueue. There is no recurring service.

The `open-brain-memory` MCP server runs over inherited stdio:

```sh
/absolute/path/runtime harness mcp --config /absolute/path/bridge.json --client codex
```

Its tools are `brain_search_page` and `brain_read_source`. Source reads use the
same read authority as search. Setup uses the optional fast foreground
helper at `PREFIX/libexec/open-brain-harness/open-brain-harness` when installed.
Persistent adapter assets live at `PREFIX/share/open-brain/harness`; commands
never point into a frozen executable's temporary extraction directory.

Owner-authorized conversation intake is available separately through the
`consumer-import` CLI when the agent has shell access. It is not an MCP grant
or a hook action. Both production export profiles remain inactive. See
`docs/consumer-intake-agents.md` for normalized input, preview/confirmation and
explicit recovery.

Pi loads the packaged `pi-entry.mjs` through the SDK's normal extension loader.
The entry uses active branch message IDs for capture provenance and exposes
`brain_search_page` and `brain_read_source`. Pi captures default to the
`unknown` tier. Pi reads `unknown` only under guardrail 1: the bridge
configuration declares Pi in `local_only_clients` with a loopback
`model_endpoint` (`127.0.0.1`, `::1` or `localhost`), and the bridge checks it on
every read. Without that, Pi gets the same cloud ceiling as Claude Code and
Codex, and a `local_only: true` read request is refused without content.

## Opt-out and troubleshooting

Launch the harness with `OPEN_BRAIN_HARNESS_DISABLED=1` to disable new hook/extension
capture and recall. Generic Pi's search tool also returns disabled without
invoking the bridge. Policy `enabled: false` denies bridge operations, including
on-demand MCP requests. Uninstall removes the integration without deleting
the retry spool or Brain data.

Run the metadata diagnostic to see how many captures wait in the spool and how
many failed their latest delivery:

```sh
/absolute/path/runtime harness status --config /absolute/path/bridge.json --client codex
```

After restoring a failed route, a later capture hook attempts another bounded
drain. Manual retry uses `harness drain` with the same configuration and client.
Missing hook trust, excluded sources, revoked grants, destination mismatch,
missing assets, and unsafe configuration are failures to resolve. They do not
prove that no relevant memory exists. Keep diagnostic receipts private; do not
publish transcripts or owner configuration.

## Format and verification evidence

The October 9, 2026 source implementation was checked against official
[Codex hooks documentation](https://learn.chatgpt.com/docs/hooks),
[Claude hook documentation](https://code.claude.com/docs/en/hooks),
[Claude MCP scope documentation](https://code.claude.com/docs/en/mcp), and
[Pi's extension loader source](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/src/core/extensions/loader.ts).
Pi's loader discovers `.js` and `.ts` entries; the generated `.js` file imports
the packaged `.mjs` module. Codex documents its transcript format as unstable,
so unsupported formats must be reported rather than silently treated as empty.

The disposable tests cover clean HOME installation/removal, unrelated setting
preservation, exact TOML restoration, stale previews, edited fragments, symlink
refusal, partial-write rollback, fast-helper selection, opt-out, and unverified
Pi local-read refusal. These fixtures do not establish native capture or
pre-inference injection. Tested client versions and exact installed digests
belong in a successful native acceptance receipt. Native acceptance remains a
separate gate; no client version is certified by these setup fixtures.
