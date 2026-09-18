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
