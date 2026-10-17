# Owner retraction

Preview the exact impact before retracting a capture or a trusted capture session:

```sh
open-brain retract preview --capture <capture-id> --json
open-brain retract preview --session <session-id> --json
open-brain retract preview --preview-id <preview-id> --item-page 1 --json
open-brain retract commit --preview-id <preview-id> --operation-id <uuid4> --json
open-brain retract status --operation-id <uuid4> --json
```

Choose exactly one target. Use the preview identifier returned by the first
command and a fresh canonical UUID4 for commit. Pages are zero-based, with at
most 100 objects per page. Counts describe the whole selection, including objects
on other pages. Neither summaries nor receipts include source bodies or paths.
There is no MCP, plugin or desktop retraction command or agent grant.
These commands open an existing current Brain without startup recovery or
automatic migration. If an older Brain needs the compatibility upgrade, run
`open-brain init` explicitly first. Status on a missing Brain creates no files.

Previews expire after one hour and never renew when read. An owner can keep at
most 16 live previews. A source head, route, effective privacy, group dependency
or session membership change requires a new preview. An affected approved copy
that has not been created yet refuses preview with `operation_pending`; unrelated
pending sharing work does not block it. A full capture session is bounded at
20,000 members. Old captures have no inferred trusted session membership.

Commit consumes the preview and atomically records the approval, journal
tombstone and immutable receipt. Retraction withholds current evidence immediately
through the shared eligibility rules; committing a session does not wait for
per-capture search cleanup. Exact replay with the same operation and preview
returns the retained receipt even after preview expiry, consumption or Portable
restore. Another preview with the same operation ID conflicts. Status needs only
the operation ID and current local-owner authority.

Retraction preserves history. It does not erase source files, edit published
pages, revoke delivered bytes or restore previously retracted material. A selected
member suppresses its incomplete current group; an older historical member does
not retire a newer unrelated head. Owner history reads retain existing privacy
and custody checks. A committed receipt records the historical decision, not a
promise that its captures are currently eligible.

Errors have a fixed body-free JSON envelope. Invalid arguments exit2, local
writer contention exits75, and preflight/stale/not-found refusals exit78.
Only `local_operation_busy` is retryable without a fresh preview. Interruption
exits130 and reports the operation ID when supplied; use status to distinguish
a committed decision from a rolled-back attempt. Do not infer rollback from a
lost response.

The current candidate requires schema19/runtime14. Portable14 adds required
`history/retraction/approvals-v1.json` to the unchanged Portable13 evidence.
Approvals retain the hashed preview binding and complete approved effects;
operational preview identifiers and rows are excluded. Restore validates approval
contracts against the terminal journal before destination mutation, installs
both in one transaction, and retains historical issuer receipts. Predecessor
inputs seed empty approvals. Older readers refuse the new container/schema.

The 20,000-member approval bound is covered by synthetic contract tests. Indexed
production-size latency acceptance remains open; this is not a production load
benchmark. Provider import profiles and local model execution remain separate
gates.

The exact evidence contract is
`/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/competitive-gap-reconciliation/docs/plans/2026-10-15-competitive-gaps-g1/OWNER-RETRACTION.md`.
