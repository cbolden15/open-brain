# Operations

Open Brain has no service lifecycle. Start a command when work is needed and wait for it to exit.

## Normal checks

```sh
open-brain status --json
open-brain doctor --json
```

Status identifies the local profile, SQLite storage, foreground-only operation, and lack of
application-level encryption. Doctor verifies the private data directory, dependency closure, and
search index. A healthy result requires no daemon restart or service repair.

## Data location

The default Brain root is `$HOME/Library/Application Support/open-brain/brain` on macOS and
`${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain` on Linux. Use `--data-dir` only with an
absolute path. Open Brain creates owner-only directories where POSIX permissions are available.

The live database is `.open-brain/state/phase1.sqlite3`. The name is retained for storage
compatibility and does not refer to a background Phase 1 process.

## Writer contention

Open Brain uses bounded SQLite waits and process locks. A competing local writer can return
`database_busy` or exit 75. Wait for the other foreground command to finish, then retry a capture
with the same idempotency key when one was supplied.

There is no daemon-authority lease, supervisor, scheduler, service unit, or control socket to inspect.

## Backup and transfer

Use `open-brain export ABSOLUTE_DIRECTORY --verify` for a complete Portable Brain copy. Keep the
finished export on storage protected for its sensitivity. Verify an export before depending on it.
Open Brain does not schedule backups or copy live SQLite files as a product workflow.

## Uninstall

Homebrew removes only the executable. User data remains. Deleting a Brain is a separate destructive
action and must not be folded into uninstall.

## Historical operations

The previous appliance, scheduler, supervisor, launchd, systemd, recovery, and lifecycle source is
quarantined in `archive/open-brain-secure-node`. It is preserved for history and is not an
operational surface of this repository.
