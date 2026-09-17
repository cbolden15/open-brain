# Recover an interrupted legacy workspace write

Use this owner CLI when ordinary startup reports `workspace_recovery_required`. An older runtime
may have recorded a note's move without retaining enough evidence to settle an interrupted write.
Recovery abandons that one pending write. It does not establish that the old target was correct,
write note bytes, or authorize another write.

These commands require a build that includes `workspace recover`. Use a matching core runtime;
an older installed build may not provide this command. Recovery is not exposed through MCP,
the Obsidian plugin, or the desktop bridge.

## Inspect the pending request

Use the existing Brain's absolute root. Inspection does not create a Brain, migrate its database,
register a session, or replay pending work. It accepts recognized local schemas 5 and 6.

```sh
open-brain workspace recover --data-dir /absolute/brain --json
```

The result contains `entries` and `next_after`. Entries contain bounded operation, workspace and
note IDs, status, eligibility, a reason, and a preview digest. They contain no note bodies or
filesystem paths. Listing returns at most 100 entries. To page through a larger result, use the
returned cursor:

```sh
open-brain workspace recover --data-dir /absolute/brain --limit 25 --after 'LAST_OPERATION_ID' --json
```

Inspect the exact operation you intend to abandon. Replace `PENDING_OPERATION_ID` with its returned
ID. Do not combine exact selection with pagination flags.

```sh
open-brain workspace recover --data-dir /absolute/brain --operation-id 'PENDING_OPERATION_ID' --json
```

Only a pending legacy standalone materialize request whose remaining problem is missing target
evidence is eligible. New bound requests, refresh children, unsafe roots or paths, damaged content,
wrong identities, and unsupported database formats cannot be repaired by this command.

## Abandon one request

Stop other Open Brain clients using this Brain, including desktop, plugin and MCP sessions.
Disabling the Obsidian plugin stops its child; the rest of Obsidian can remain open. Closing clients
is required for abandonment, not for inspection.

After reviewing an eligible entry, copy its exact operation ID and preview digest. Choose a fresh
recovery request ID and retain all three values until the result is confirmed. Replace the two
uppercase placeholders below; `owner.recovery.1` is an example of your chosen recovery ID.

```sh
open-brain workspace recover --data-dir /absolute/brain \
  --operation-id 'PENDING_OPERATION_ID' --abandon \
  --expected-digest 'PREVIEW_DIGEST' --request-id 'owner.recovery.1' --json
```

There is no bulk abandonment or force option. A changed preview refuses the decision. The command
records the owner decision and marks the original operation cancelled in one transaction. Its
original identity, target, revision, preimage and body fields remain intact. The command does not
edit Markdown or create a new revision.

A successful receipt has `status: "abandoned"`, `request_id`, `operation_id`, `duplicate` and
`schema_upgraded`. Repeating the same recovery ID with the same operation and digest returns the
original receipt with `duplicate: true`. Reusing that ID for different arguments fails. A fresh
recovery ID for the same already-abandoned operation and original digest returns the existing
receipt without another audit decision.

Ordinary session cleanup still runs: reserved inference requests are cancelled, dispatching
requests become uncertain, and active consent is revoked. Review consent before intentionally
enabling inference again. Recovery makes no provider calls.

## Confirm normal opening

After the receipt, open the workspace normally:

```sh
open-brain workspace status --data-dir /absolute/brain --json
```

Normal opening may finish valid unrelated pending work. Other eligible legacy blockers still need
their own inspected owner decisions. Retrying the original materialize ID returns `stale_request`;
it never becomes a successful materialization. A desired later write needs a new ordinary request,
which validates current state and retains normal conflict checks.

## If recovery refuses

| Result | Next action |
| --- | --- |
| `runtime_in_use` or `database_busy` | Stop the other client or wait for the current writer to finish, then retry. Exit status is 75. |
| `stale_preview` | Inspect the exact operation again and review the new digest before deciding. |
| `request_replay_mismatch` | Use the original arguments for an existing recovery ID. A genuinely new decision needs a fresh ID. |
| `not_eligible`, `unknown_operation` or `operation_replay_mismatch` | Check the selected operation and runtime. Do not change database fields to make it eligible. |
| `private_data_unavailable` or `recovery_unavailable` | Check that the selected existing root is available and private. Preserve its state for diagnosis. |

Other failures exit with status 78. Read `schema_upgraded` in a failure response: a successful
schema-5-to-6 migration can commit before a later abandonment or cleanup failure. The upgrade
is not rolled back with the decision. If a response was lost or cleanup failed after a decision
committed, retry with the original recovery ID, operation and digest to obtain its durable result.

A hot SQLite rollback journal can prevent read-only preview. Preview never repairs or deletes a
journal. The existing guarded writable-opening path lets SQLite restore committed state before
classification; normal startup can then report the workspace blocker for explicit recovery.
Do not remove journal files or copy a live SQLite database as a repair. See the
[local migration contract](schema-migrations.md) for that separate storage-recovery boundary.
