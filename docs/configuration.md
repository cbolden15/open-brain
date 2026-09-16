# Configuration

The Open Brain core needs no configuration. The first stateful command selects the platform data directory,
creates one owner and one Brain, opens SQLite, performs its work, and exits.

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

## Desktop companion boundary

The D0 desktop proof selects a disposable synthetic Brain and a digest-checked packaged runtime.
It exposes no account setup, existing-Brain selection, scheduling preference, or service registration.
It does not change the core defaults above or the installed CLI.

Future source configuration, checkpoints, credential references, and collector controls belong to
the companion/collector, separately from Brain content. Enabling collection and starting it at login
are separate choices. See [ADR 0017](architecture/decisions/0017-desktop-companion-boundary.md) for
the permission and lifetime contract; these settings are not working D0 features.
