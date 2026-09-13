# Configuration

Open Brain needs no configuration. The first stateful command selects the platform data directory,
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

Open Brain has no configuration file, provider selection, credential reference, listener bind,
connector allow-list, daemon setting, scheduler inventory, or service unit. The optional connector
distribution has its own package boundary and does not change Open Brain startup.
