# Inspect and retry collector quarantine

The optional collector retains a replayable intake when an individual source revision conflicts
with immutable captured evidence. Other independent items in the same batch can still finish.
The provider checkpoint advances only after every item has a durable capture, duplicate, or
quarantine receipt. Provider acknowledgement follows that local checkpoint commit.

These are local contributor commands. They do not establish live-source or release acceptance.
Use the existing absolute Brain and collector-state paths from
[priority capture setup](priority-capture-operator.md). Installing the core does not install or
start the collector.

## Priority sources

```sh
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground custody-status
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground custody-inspect --receipt-id REPLACE_FROM_CUSTODY_STATUS
open-brain-collector sources --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  --foreground custody-retry --receipt-id REPLACE_FROM_CUSTODY_STATUS
```

Add `--source-id REPLACE_FROM_CONFIGURE` after `custody-status` to select one source. When an
optional collector is already running, omit `--foreground` to use its private control channel.
The operations are also available through the shared manager as `sources.custody_status`,
`sources.custody_inspect`, and `sources.custody_retry`.

## Legacy collector sources

The legacy `source` commands use their own custody store:

```sh
open-brain-collector source --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  custody-status
open-brain-collector source --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  custody-inspect --receipt-id REPLACE_FROM_CUSTODY_STATUS
open-brain-collector source --state "$CAPTURE_STATE" --brain-root "$CAPTURE_BRAIN" \
  custody-retry --receipt-id REPLACE_FROM_CUSTODY_STATUS
```

## What the results mean

Status reports counts, retained serialized bytes, and a bounded list of pending/quarantined
receipt IDs. Inspect reports the exact receipt's opaque identities, intake digest, outcome,
capture identity where available, safe reason code, and control epoch. It does not print the
captured body, title, upstream URL, or raw provider exception.

Retry submits the retained intake through the ordinary capture boundary. It does not fetch a
newer provider version, overwrite evidence, invent revision order, or enable a source schedule.
If the same immutable conflict remains, the receipt stays quarantined. A successful retry
releases its payload budget once no unfinished batch needs those bytes, and retains bounded
receipt metadata. Checkpoint cleanup is recoverable after an interruption.

Pause, disable, or selection reset fences earlier work. Already quarantined payloads remain
available for inspection. Cancelled, unacknowledged pending work is discarded after its batch is
durably detached; its provider checkpoint stays unchanged. A stale-generation receipt returns
`collector_custody_stale`; retry cannot silently attach it to a different selection or Brain. An unchanged conflict requires a
separately supported source-resolution workflow; repeatedly retrying does not resolve it.

Each custody store admits at most 256 retained items and 2 MiB of serialized unresolved custody,
including reserved terminal-receipt metadata. Resolved-receipt pruning targets 512 records;
unfinished cleanup protects its referenced metadata, so that count can temporarily exceed the
target until a later release prunes it. The complete custody document has a 4 MiB storage limit.
`collector_custody_quota_exceeded` stops the batch without checkpoint advancement. Disk, authentication, locking, compatibility, and generic
capture failures also stop the batch; they are not treated as content quarantine. Do not delete
custody files to bypass backpressure: after provider acknowledgement they may contain the only
replayable copy of an item.

## Failed saved-Markdown inventory

The saved-Markdown runtime refuses a final checkpoint with
`collector_scan_incomplete` when enumeration, binding or file validation fails.
Its active run and cached page remain retained. Restoring the files or restarting
the process does not make the failed inventory successful. A bounded continuation
page with no errors remains eligible; an unfinished scan is not absence evidence.

The local host API `CollectorController.restart_failed_scan` takes the selected
`source_id`, its `SavedMarkdownCollectorRuntime`, and the exact `expected_run_id`
from retained controller state. This is not a collector CLI command or an engine
owner operation. The host must bind the existing Brain and selection as usual.
The operation checks that every page item still has exact replayable terminal
custody under the current destination, selection generation and control epoch.
Pending custody refuses with `collector_incomplete_custody`; resolve its original
envelope through the existing replay path before restarting inventory.

Recovery persists an exact-request reset marker before detaching the active run.
If controller persistence fails after that reset, retrying the same failed run
finishes the reset without changing custody. A changed run or selection refuses;
a valid inventory cannot be reset by this operation. The reset advances the local
control epoch, clears the failed cursor and absence candidates, and leaves known
sources, prior success time, every retained custody body and canonical receipt
unchanged. The run remains recorded as failed. The next eligible invocation begins
a fresh scan and uses ordinary exact replay or current revision admission.

This path does not acknowledge independent protection, release payload budget,
infer withdrawal, grant sharing, or enable a paused or disabled schedule. Retained
recovery custody remains subject to the normal aggregate limits. Do not delete it
to create capacity; independent protection and an authorized cleanup mechanism
are separate requirements.

## Observed revision custody

Observed source revisions retain their serialized intake after terminal completion,
retry cleanup, pause, disable or selection reset. A capture ID, local terminal
receipt or historical baseline is not independently protected revision replay.
The capture-only `receipt-protection.v1` contract also cannot authorize removal
of managed source ordering, lifecycle and whole-envelope evidence.

This retention guard uses the existing aggregate custody limits and no eviction.
When accepted retained revisions fill capacity, admission stops rather than
discarding them. Full saved-revision sender envelopes and prior versions remain
in their existing bounded cache/archive as well. These local copies are not an
independent disaster-recovery proof. A separate managed-record protection and
verified import protocol is required before observed revision compaction can be
enabled; no such release acknowledgement is implemented here.
