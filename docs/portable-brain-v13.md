# Portable Brain13 and capture lifecycle integration

The unreleased engine candidate uses state schema18, minimum foreground runtime13,
and Portable contract/layout/schema13. An existing schema17 Brain upgrades through
the explicit exclusive-admission migration coordinator. Ordinary opening does not
silently upgrade it. Runtime12 and earlier refuse schema18 before engine work.
Migration1–17 SQL/checksums and Portable1–12 validators/catalogs remain unchanged.

## Engine boundary

`BrainEngine.lifecycle`, also available through `engine.tasks.lifecycle`, connects
the immutable journal to real capture materialization under the normal writer
lease and engine admission checks. This internal seam accepts local-owner quick
text evidence. It does not expose a consumer importer command or claim that an
owner submission supplies third-party consumer provenance.

`prepare_segment` retains the normalized admission envelope and original capture,
receipt and acceptance-time reservation. `begin` binds exact group manifests.
`stage_segment` transfers body/envelope custody before materialization. Pending
physical captures have retained source metadata marked history-only, which keeps
the Portable inventory closed while lifecycle eligibility blocks all reads,
including owner history. `recover_pending` resumes retained custody with original
identities; activation remains explicit. Ordinary ingestion cannot reuse a staged
delivery to bypass this barrier.

`activate_group` verifies every member's immutable reservation, admitted privacy,
completed capture and physical source bytes. One transaction publishes the whole
group, updates session membership and generations, and replaces the current group
revision. The previous group stays current throughout staging. A publication
failure rolls back the whole activation. Groups have at most128 segments; the
session membership bound remains20000.

Capture/session retraction records immutable tombstones and future-import barriers.
Current retrieval, canonical provenance, graph dependencies and semantic selection
apply the shared lifecycle predicate. Retained admitted evidence remains available
to authorized owner history, including canonical history resolved independently of
live search rows. Retraction of a historical member does not retire an unrelated
newer current group. Exact terminal replay returns the original receipt. Explicit
halt retains completed groups and keeps unactivated custody hidden.

## Portable evidence

Portable13 extends the frozen Portable12 inventory with required lifecycle metadata
at `history/capture-lifecycle/journal-v1.json` and separately checksummed raw payload
files under `history/capture-lifecycle/payloads/`. Individual request/envelope
contracts retain their canonical bounds; accumulated bodies are not encoded into
one component-sized message. The isolated journal's frozen16MiB fixture format
remains available for component tests.

Validation replays requests and chained events in a disposable database, checks
exact payload closure, and binds activated captures to retained real source
metadata, admission envelopes and reservation identities. Pending operations block
export. Halted custody can lack a materialized capture after an interruption, but
never becomes admitted history on restore. Missing, orphaned or changed lifecycle
evidence refuses validation before destination creation.

Restore installs lifecycle evidence before rebuilding current projections, then
rebuilds again after historical/development dependency evidence is installed.
Re-export preserves lifecycle records, tombstones and receipts. The inherited
privacy sidecar retains its frozen base-projection meaning; current search adds
lifecycle eligibility. Provider grants, local model execution and new consent are
not activated by restore.

## Scope and evidence

Synthetic tests cover exclusive17→18 migration and old-client refusal, staging
across restart, whole-group rollback, previous-current preservation, exact custody
recovery, retained source/canonical owner history, dependency exclusion, terminal
replay and Portable roundtrips beyond the isolated component's aggregate size.
Full verification evidence belongs to workstream
`$REPO/.worktrees/competitive-gap-reconciliation/docs/ai/workstreams/20261015-open-brain-public-lifecycle-engine-integration-915bb7/`.

Journal projections still rebuild from immutable evidence; indexed production-size
performance acceptance remains open. Consumer format parsers, owner preview/commit
commands, provider provenance admission, the planned64MiB normalized importer and
embedding resources are separate work. This slice does not complete G1 or certify
an installed client, private source, real model or release artifact.
