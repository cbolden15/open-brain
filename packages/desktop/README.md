# Open Brain Desktop

The optional desktop companion provides Search, Capture, Sources, Activity, and Settings.
Search and capture use the same platform-default Brain as the CLI. Settings can configure Claude
Code and Codex to use the desktop's exact packaged runtime and Brain. Source accounts and recurring
collection are planned; their cards do not imply a connection.

## Build and verify

Run commands from the repository root. Install the core contributor prerequisites, Node.js 24.15+
or 26+, stable Rust, and [Tauri's platform prerequisites](https://v2.tauri.app/start/prerequisites/).

```sh
uv sync --frozen --group dev --group native-build
make verify
make native-audit homebrew-smoke desktop-native-proof
```

The last command shares the native-build prerequisite. Do not run another Homebrew smoke check
concurrently. On macOS arm64, the app is at:

```text
packages/desktop/src-tauri/target/release/bundle/macos/Open Brain Desktop.app
```

Opening the app selects the normal Brain. For isolated native verification, launch its executable
with `--data-dir /absolute/path/to/synthetic-brain`. Never use real notes or personal client
configuration for contributor tests. The `--d0-proof-json` mode remains a separate disposable capture,
search, helper, and cleanup check. It does not prove that the window works.

## Use the companion

The M2 source build adds source routing, draft inspection, publication approval and opening the
managed vault. Search supports space, payload-family and record-type filters, explicit continuation
and complete record reads. See [records and history](../../docs/records-and-history.md).

1. Capture a note, then use **Find this note** to verify retrieval.
2. Open **Settings** and select Claude Code or Codex, project or user scope, and the desired capture
   and search permissions. Both grants start off. Project scope requires an absolute project path.
3. Preview the owned configuration and instruction fragments, then apply them. Restart the client
   and complete its own project trust or MCP approval when prompted.
4. Ask the client to remember a synthetic fact. Start a fresh client session and ask for that fact.

Search permission reads the whole Brain. A network-backed client may send retrieved text to its
provider. Setup writes no login credentials and enables no transcript collection. Instructions ask
for explicit memory saves and relevant retrieval. Activity shows only this app session and resets
on close. Quitting stops the app's child; configured agent sessions run independently when invoked.

The equivalent [headless setup](../../docs/agent-setup.md) needs no desktop. Removing an integration
uses another preview and preserves unrelated client settings. Moving the runtime or app requires
setup again because the configuration contains an absolute executable path.

## Runtime boundary

The renderer has only named native operations, no general shell or database interface. The host
verifies the exact packaged core/Graphify pair before launch and checks protocol version 1, runtime
session version 2, and state schema version 7. The native proof uses the same handshake validator.
Existing state migrates through the core; incompatible older readers reject the newer private schema. Stop older sessions before upgrading existing state.

Cold startup has a 15-second handshake deadline; interactive requests have a 10-second deadline.
Malformed replies, lost transport, deadlines, and the 2,000-request session limit close the bridge.
Negotiated search pages and current reads share a 500-call, 16 MiB encoded-output bucket;
history listing and reads have a separate bucket with the same limits. Each negotiated response is
capped at 1 MiB including its envelope. Complete-read clients stop when these limits are reached.
An uncertain capture retains its text and request ID for an explicit retry. Reconnection does not
replay mutations or restore provider consent. Native exit stops the owned process group.

The Python control fixture proves private Unix-domain transport and one lock-held instance. It does
not implement a collector. Core tests continue to prohibit socket access.

## Release status

This is a contributor build, separate from Homebrew. Public macOS distribution still needs
Developer ID signing, notarization, and installation evidence. `make desktop-native` defines a Linux
x86_64 AppImage build with a matched runtime pair; clean-host native UI and helper acceptance remain
a separate Linux gate. See [ADR 0017](../../docs/architecture/decisions/0017-desktop-companion-boundary.md)
and the [desktop plan](../../docs/plans/2026-09-14-desktop-companion.md).
