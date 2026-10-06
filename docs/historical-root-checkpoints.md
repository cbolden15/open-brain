# Historical checkpoints after device renumbering

An immutable historical observation contains the physical root fingerprint used
when it was admitted. A device number can change after a restart while the same
Brain remains at the same path and inode. Ordinary collector revision capabilities
correctly refuse that old binding.

`HistoricalRootCheckpointHost` provides a separate owner-local capability for
checkpointing an authenticated existing observation. It never opens `BrainEngine`,
replays pending ingestion, migrates storage, captures content, submits a revision,
or changes historical requests. It opens current supported state read-only and
uses the existing canonical writer lease while the caller persists a checkpoint.

The trusted caller supplies `HistoricalRootContinuity` and a required
`validate_continuity` callback. The callback must authenticate retained evidence
that the prior and current roots have the same path, inode, tenant, destination,
and issuer, and must enforce the current physical admission fence. A matching
inode or a prior fingerprint alone is insufficient. The host checks owner-local
authority, the current physical root and logical destination, complete settled
historical authority, exact original binding, full incoming envelope, retained
capture evidence and current source CAS. It calls the validator again on lookup
and under the writer fence. Witnesses carry the current root identity and selection
generation. No stored historical binding or envelope is rewritten.

`collector_historical_checkpoint_sink` reconstructs Saved observations from the
incoming content and the original historical coordinates. Its returned
`HistoricalSavedCheckpointSink` exposes baseline lookup and bounded page
checkpointing. It has no capture or revision submission method. Changed content,
privacy, namespace or current eligibility refuses checkpointing; the caller must
not turn that refusal into intake approval. Ordinary capture and revision sinks
retain their current physical root fingerprints and existing consent checks.

The caller still owns approved source selection, completeness, retained page and
cursor custody, independent BEFORE/AFTER durability, crash recovery, installation
pins and activation gates. This API grants none of those. A failed protected stage
must retain its exact page and genuine evidence. A checkpoint-only retry requires
the reviewed current admission and continuity evidence; an old BEFORE-only proof
must never be presented as a completed terminal result.

This capability supports V1 and V2 historical authority at the current supported
schema. It narrowly handles device renumbering; relocation, inode replacement,
tenant changes and destination or issuer changes remain refused. A broader stable
logical identity migration requires its own compatibility and recovery design.
