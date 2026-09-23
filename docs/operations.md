# Operations

The Open Brain core has no service lifecycle. Start a command when work is needed and wait for it
to exit. Optional source-only collector operations are separate; see [core sources](integrations/core-sources.md).

## Normal checks

```sh
open-brain status --json
open-brain doctor --check search-index --json
```

Status identifies the local profile, SQLite storage, foreground-only operation, and lack of
application-level encryption. It also reports schema-10 journal counts. Doctor verifies the private
data directory, dependency closure, search index, and the same bounded journal summary. Run one
named check at a time; see the [four executable doctor examples](doctor.md).
These commands describe the Core v0.1 candidate, not a release certification. A healthy result
requires no daemon restart or service repair.

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

The core has no daemon-authority lease, supervisor, scheduler, service unit, or control socket to inspect.

## Backup and transfer

Use `open-brain export ABSOLUTE_DIRECTORY --verify` for a complete Portable Brain copy. Keep the
finished export on storage protected for its sensitivity. Verify an export before depending on it.
Open Brain does not schedule backups or copy live SQLite files as a product workflow.

## Mac Mini cutover runbook

The Mac Mini is the final canonical Brain. Perform this sequence during a planned window:

1. Stop every capture launcher, collector checkpoint, connector outbox drain, desktop client, and
   MCP client that can submit to this Brain. Do not start migration with a live writer.
2. Create the supported pre-migration Portable export and verify it. Keep that snapshot until the
   post-migration capture, search, restart-recovery, collector-custody, and connector-outbox smoke
   checks pass.
3. Install the verified build, open the existing root once, and allow the guarded schema-9 to
   schema-10 migration. Confirm `init --json` reports `state_schema_version: 10`, `status --json`
   reports the expected journal counts, and any custody receipt binds the current issuer epoch.
   Do not downgrade in place; schema-10-aware code is required.
4. Run one bounded owner drain, then synthetic capture and search checks. A queued response is
   successful custody, not canonical completion; resolve or explain every pending/quarantined item.
5. Resume launchers only after custody verification is proven. A Portable export is permitted only
   when pending and quarantined journal payload counts are zero.

If acceptance fails, keep launchers stopped and restore the pre-migration snapshot with the prior
binary. Do not attempt an in-place schema downgrade or delete journal tables manually. This public
runbook contains no hostnames, account identifiers, credentials, or captured content.

## Uninstall

Homebrew removes only the executable. User data remains. Deleting a Brain is a separate destructive
action and must not be folded into uninstall.

## Historical operations

The previous appliance, scheduler, supervisor, launchd, systemd, recovery, and lifecycle source is
quarantined in `archive/open-brain-secure-node`. It is preserved for history and is not an
operational surface of this repository.
