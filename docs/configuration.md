# Configuration

The Open Brain core needs no configuration. The first stateful command selects the platform data directory,
creates one owner and one Brain, opens SQLite, performs its work, and exits.
Start with [capture and search](first-use.md). The current source tree also offers an optional
interactive `open-brain setup` guide; the published `v0.1.0` Homebrew release does not include it.

An optional absolute `--data-dir` names the Brain root for expert and test use. Relative paths are
rejected. Without that option, the defaults are:

| Host | Default Brain root |
|---|---|
| macOS | `$HOME/Library/Application Support/open-brain/brain` |
| Linux | `${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain` |

`XDG_DATA_HOME` affects the Linux platform default. `HOME` supplies the ordinary per-user base path.
`OPEN_BRAIN_ROOT` and the previous provider, service, UI, HTTP, lifecycle, and Secure Node settings
are not consumed by the active application package.

The core has no user-edited startup configuration file, listener bind, connector allow-list, daemon
setting, scheduler inventory, or service unit. Optional semantic-provider setup is an explicit
plugin-session operation described in [the install guide](install.md#use-the-managed-vault-in-obsidian).
The optional connector distribution has its own package boundary and does not change core startup.

## Optional integrations and archived desktop

Claude Code and Codex use the [agent setup CLI](agent-setup.md) when supported by the selected
executable. The optional [Obsidian plugin](install.md#use-the-managed-vault-in-obsidian) is a separate
desktop-only integration; it requires explicit activation in Obsidian.

The former Tauri desktop companion is archived under `archive/open-brain-desktop`. It was never
released and is excluded from active builds and tests. Its D0 proof and settings are historical;
there is no current desktop-companion setup route.

Optional source configuration, checkpoints, credential references, and collection controls belong
to the collector, separately from Brain content. Enabling collection and starting it at login are
separate opt-ins. See [ADR 0017](architecture/decisions/0017-desktop-companion-boundary.md) for the
permission and lifetime boundary. None of these controls is needed for local capture or search.
