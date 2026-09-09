# Operations

The default Open Brain five-minute path has no daemon or service lifecycle. The procedures below
characterize retained appliance artifacts and Secure Node precursor behavior. They are not default
installation steps. See [`product-family.md`](product-family.md).

The operations package defines scheduler contracts and renders generic manifests. It does
not install, enable, start, stop, restart, load, unload, or inspect a service. Deployment
configuration and service actions remain outside the public application.

## Open Brain release verification

Run `make verify` for lint, type checking, tests, and source builds. Run `make native` to build and
audit one platform-native default executable. Run `make homebrew-smoke` to install that archive with
a temporary local Homebrew formula and exercise capture, search, verified export, status, and doctor.
The five-minute timer ends at verified export; status and doctor are post-export checks. The smoke
then imports the committed synthetic Markdown fixture, proves an unchanged rerun, searches one
nested record, and verifies its exact exported bytes and unverified provenance.

The current release process has no installer script, VM matrix, service lifecycle, receipt,
attestation, notarization, or evidence assembly. See
[`artifact-characterization.md`](artifact-characterization.md) for the complete shipping surface.

## Capture publication

`JOB-010` publishes only after raw persistence, a redacted extraction event, and durable
local distillation all succeed. Owner-authored work text is written atomically to
`work_root/inbox/open-brain/<capture-id>.md`. Third-party web, social, and YouTube captures
are written atomically to `saved_content_root/inbox/open-brain/<capture-id>.md` with their
source URL, extracted content, owner-authored reason, and distilled summary. Personal
captures keep their separate local-only path at
`personal_root/captures/<capture-id>.md`.

Publication happens before the distillation queue item is acknowledged. A failed or
conflicting write leaves the item retryable. Replay accepts only the exact existing bytes;
it never overwrites a changed file. Third-party text cannot publish as owner-authored work,
and saved-content publication does not create an idea or action. Those promotions remain
review-gated.

These paths are canonical-writer outputs. Capture ingress may run elsewhere, but only the
host holding the configured writer record and shared-writer lease may execute `JOB-010`.

## Synthetic shadow observations

The Phase 7 shadow observer accepts two caller-supplied, immutable synthetic metadata
snapshots. It performs no filesystem, service, network, predecessor, or production read. Its
API has no writer, sink, fetcher, provider, path, credential, or service-control capability.

Each snapshot contains closed extraction, routing, content-kind, provenance, provider,
privacy, resource, and redaction classes. The observer validates read-only receipts, builds
the dedicated synthetic shadow manifest, and delegates the only comparison decision to
`compare_synthetic_parity`. Failed extraction, resource limits, redaction failures, raw
residue, missing provenance, shared reader identity, writer capability, or any metadata
mismatch remains blocked.

Synthetic snapshot results are implementation evidence only. They do not authorize live
mirrored reads, production reads, writer acquisition, deployment, cutover, or release. Those
actions require a separate explicit owner gate. Until that gate exists, evidence must record
both live and production read status as `not_run_owner_gated`.

## Synthetic ordered cutover rehearsal

The P7-W2 rehearsal is a pure metadata state machine. It accepts a synthetic-ready Phase 6
doctor result, one closed scenario, an immutable prior receipt ledger, and externally verified
artifact-attestation evidence. It performs no filesystem, network, process, service, reader,
writer, snapshot, restore, or deployment action.

The fixed surface order is:

1. read-only CLI status, doctor, and query;
2. MCP and UI reads;
3. iOS share and raw capture;
4. YouTube playlist polling;
5. social and web draining;
6. ledger and review jobs;
7. remaining scheduled writers;
8. backup, retention, and restore tooling.

Each surface must finish these receipts before the next surface starts: `snapshot`,
`old-writer-disposition`, `new-service-enabled`, `one-writer-proof`, `synthetic-smoke`,
`verification`, and `green`. Read-only surfaces use `not-applicable-read-only`; recovery
tooling uses `not-applicable-tooling`; writing surfaces require one synthetic writer identity
and zero legacy synthetic writers. The first receipt binds the all-zero genesis digest. Every
later receipt binds the complete digest of its predecessor.

A rehearsal stops and enters rollback when it records data loss or a duplicate write, a
privacy-tier mismatch, unredacted log residue, an undrainable or unrecoverable queue, review
gate bypass, or a required red health check. Rollback must record, in order:
`rollback-triggered`, `new-service-disabled`, `snapshot-restore-disposition`,
`old-service-reenabled-disposition`, `redacted-diagnostic-preserved`, and
`rollback-verified`. Missing, failed, reordered, duplicate, disposition-inconsistent, or
post-trigger evidence returns `rollback-blocked`. No later surface may be attempted after a
trigger.

`synthetic-green` means all eight synthetic surface chains are complete. It does not mean
cutover-ready or production-ready. `rolled-back-synthetic` means one trigger produced a
complete synthetic rollback chain. `blocked` means forward evidence failed. Neither
`rolled-back-synthetic` nor `rollback-blocked` resolves the rehearsal.

Receipts are append-only and keyed by run, surface, attempt, chain, and stage. A prior ledger
is revalidated before use. Resume starts at the first surface without a verified green chain;
an earlier or later cursor is rejected. The immutable scenario-plan digest excludes only the
advancing resume cursor, which lets receipts remain bound to the same plan across resume
calls. Reusing a receipt identity with changed content is a conflict.

Every result keeps `real_capture`, `live_transition`, and `production_cutover` at
`not-performed-owner-gated`. Live or production reads, real capture, writer acquisition or
transition, predecessor writes, deployment, cutover, rollback execution, retirement, archive,
publication, and release require separate owner authorization. Synthetic receipts cannot grant
that authority.

## Synthetic operational checkpoint and Phase 7 reconciliation

P7-W3 adds a pure metadata checkpoint over eight operational flows. The control-plan source has
seven bullets because review approval and rejection share one bullet, but they are separate
terminal flows:

| Flow | Runbook step | Operational flow |
|---|---|---|
| `FLOW-001` | `P7W3-OPS-01` | capture to ledger |
| `FLOW-002` | `P7W3-OPS-02` | review approve |
| `FLOW-003` | `P7W3-OPS-03` | review reject |
| `FLOW-004` | `P7W3-OPS-04` | complete nightly |
| `FLOW-005` | `P7W3-OPS-05` | playlist poll |
| `FLOW-006` | `P7W3-OPS-06` | social and web capture |
| `FLOW-007` | `P7W3-OPS-07` | backup |
| `FLOW-008` | `P7W3-OPS-08` | temporary restore |

One bundle contains exactly one synthetic receipt for each flow in that order. The first receipt
binds the all-zero genesis digest. Each later receipt binds the complete digest of its predecessor.
All eight receipts share the run, attempt, artifact, P7-W2 result, scope, and evaluation time. A
failed or blocked bundle is immutable. A retry uses a new run ID and bundle.

`reconcile_phase7` accepts only four keyword inputs: the operational bundle, the separate
eight-surface stale-reference inventory, the current P7-W3 artifact attestation, and the evaluation
time. Prior P7-W0, P7-W1, and P7-W2 trust is fixed in the module, including all 46 authoritative
source rows. Callers cannot supply parity dispositions, comparison results, or approval state.

Missing flow or surface inventory, missing terminal evidence, and absent prerequisites return
`blocked-missing-evidence`. Digest conflicts, duplicates, wrong ordering, broken receipt chains,
scope or artifact mismatches, trust-lineage drift, raw-residue claims, failed synthetic checks, and
production claims return `blocked-contradiction`.

The positive result is a synthetic operational checkpoint. It preserves prior authoritative
`match` and `blocked-difference` facts and keeps Phase 7 production completion false. Production
parity, all eight live surfaces, all eight real flows, old-writer shutdown, rollback availability,
loaded-service-reference checks, and Phase 7 production completion remain
`not-performed-owner-gated`.

The checkpoint sequence is non-circular: commit verified source, tests, and this documentation as
`C_impl`; rebuild and independently hash the wheel; generate deterministic metadata-only evidence
bound to `C_impl`; run the pre-commit verifier and security review; commit only that evidence as
`C_evidence`; then verify its exact bytes, mode, parentage, wheel binding, and clean-tree state.

No P7-W3 public API reads a file, inspects a service, runs a command, performs an operational flow,
or changes production state. Direct production evidence requires a separately authorized,
versioned contract.

## Job contract

`JobSpec` is immutable. Each job has a `JOB-###` identifier, a direct `open-brain` argv
tuple, an abstract deployment target, an allowed scheduler-platform set, a host role,
trigger, writer scope, lock scope, bounded timeout, retry policy, environment-reference
names, output policy, and state. Shell command strings and wrapper scripts are rejected.

The fixed public deployment map uses no real host labels. `JOB-001` through `JOB-009`
target `edge-operator` on launchd, `JOB-010` through `JOB-027` target
`canonical-writer` on launchd, and `JOB-028` through `JOB-030` target `ingress-node` on
systemd. Only `canonical-writer` jobs can hold canonical writer authority. `JOB-004` is an
enabled dry-run probe and never acquires backup-writer authority. It snapshots every confined
state SQLite database through SQLite's backup API into memory, verifies each snapshot, and
persists no backup artifact.

The catalog contains `JOB-001` through `JOB-030` exactly once. Its ownership rules are:

- probes and read-only services have no writer or writer lock;
- ingress can append capture envelopes and uses a separate ingress lock;
- index, content, and state writers use their approved index or shared-writer lock;
- backup writers use an independent backup-profile lock;
- canonical index and NOW output each have one enabled writer.

Read-only duplicate probes are enabled but cannot acquire writer authority. Configured services
and integration jobs are enabled.
Close-day preparation, hook synchronization, and retention are manual and render their
dry-run command. Retention requires an owner-only canonical `OPEN_BRAIN_RETENTION_CONFIG`,
binds candidates to a closed Brain root, and keeps interactive apply disabled until a separate
exact plan approval is supplied through the lower-level retention contract. Signal scanning and
external fetches are ingress-only. The optional social-ledger writer is assigned to the
canonical writer role and the shared-writer lock.

### Candidate ownership and disposition checkpoint

This table records the declared pre-cutover ownership boundary from the owner-provided starting
state. It is an implementation checkpoint, not direct production evidence. The predecessor must
remain the only active owner until a separately approved canary proves the replacement and the
corresponding writer handoff completes.

| Shared surface | Declared active owner | Open Brain disposition | Transition restriction |
|---|---|---|---|
| Approved-content promotion | Cutover-controlled | `JOB-012` assembles durable review, event, target, and ledger bindings before its approval-bound runtime | Handoff only after its predecessor writer is stopped |
| Eligible repository state | Cutover-controlled | `JOB-015` uses an owner-only inventory, local-only personal commits, and digest-bound push targets | Handoff only after repository inventory verification |
| Search index | Cutover-controlled | Deterministic local composition and replay are implemented | Handoff atomically; never overlap index writers |
| Planning state | Cutover-controlled | `JOB-017` through `JOB-019` use durable local runtimes | Keep review gating and one shared writer |
| Message inbox | Cutover-controlled | Separate ingress-owned immutable SQLite input | Hand off the inbox surface atomically |
| Message review proposals and cursor state | Cutover-controlled | Canonical review and cursor databases; sync remains a dry-run route | Never apply message-derived actions directly |
| Current-state projection | Cutover-controlled | `JOB-022` builds a bounded work-only projection with replay | Replicas remain read-only probes |
| Backup snapshots | Cutover-controlled | Capture, full, personal, and runtime-state profiles | Handoff one profile at a time with restore proof |

The writer identity is one machine-level authority, not one authority per job. Composed candidate
write paths validate the same canonical-writer record and acquire their declared scoped lease;
standalone effect boundaries require injected shared-writer authority. A surface cannot disable
its predecessor until direct production evidence and rollback verification pass.

`JOB-015` reads exactly one `git_inventory` file reference. The canonical owner-only JSON
allocates each repository to the work, personal, or development root and binds any permitted
push remote by SHA-256. Personal bindings cannot carry push authority. The subprocess adapter
uses a fixed Git executable, direct allow-listed argv, bounded output, no terminal prompts, and
the inventory's private home root. A missing, readable-by-group, non-canonical, conflicting, or
unbound inventory returns the stable scheduler configuration exit before mutation.

`JOB-012` selects due approved review outbox rows through the prior-day cutoff, verifies the
exact terminal review and redacted capture event, requires the curation target to resolve through
the configured ledger taxonomy at the same privacy tier, and sanitizes the owner-authored leaf.
Only then does it enter the shared-writer lease and the existing durable curation effect. Missing
events, targets, or routes create a digest-only follow-up item and publish no Markdown. Corrupt or
contradictory bindings fail the run. Output delivery is one transaction, and a completed daily
window with no remaining due output replays without rebuilding a conflicting empty batch.

The unaudited mobile synchronization peer is explicitly excluded from this candidate and must
remain disconnected. It owns no required replacement capability and is not used as a recovery
replica.

Scheduled backup writers use a root-confined filesystem source and an immutable backup
store under `backup_root`. The full profile includes work, personal, capture, saved-content,
and runtime-state tiers. A durable SQLite replay journal binds each schedule window to one
request digest and the matching canonical-writer generation. Live SQLite databases are
copied through SQLite's backup API; WAL, SHM, and journal sidecars are never copied raw. The
reservation freezes the exact object inventory, bytes, backup ID, and manifest before
publication, so replay never rereads mutable sources. Complete manifests and every object
digest are checked by the doctor. Backup collection is currently in memory, so streaming
large source trees is future work. Index uses a deterministic local embedding adapter over
the approved work and saved-content roots; its output root must already exist, so state
initialization remains an explicit gate. NOW uses a bounded work-page projection source and a
durable reserve/apply/read/replay effect boundary. Edge and ingress copies remain read-only and
are checked against the same generation.

## Exit and run metadata

Stable scheduler exits are `0` for success, `75` when a lock is held, and `78` for a
configuration or preflight failure. Other nonzero values are job failures.

Run metadata can only be created through its factory. Outcome and duration are derived,
and each outcome requires its exact closed error class; only success permits `None`.
Numeric metrics are limited to eight names from the job's own allow-list. Run metadata has
no field for stdout, stderr, content, paths, URLs, credentials, or arbitrary error messages.

The installed scheduler composition writes each completed run to
`state_root/runlog/<job-id>/<sha256>.json`. The file name binds canonical JSON bytes, repeat
writes are replay-safe, and reads reject non-canonical, malformed, oversized, renamed, or
symlinked records. Status reads at most 4,096 metadata-only records from the previous seven
days. A run-log write or validation failure fails the scheduler invocation instead of
silently losing production evidence.

## Manifest rendering

`render_launchd`, `render_systemd_service`, and `render_systemd_timer` are deterministic
pure functions. Launchd uses `ProgramArguments`; systemd uses a direct `ExecStart`. Both
carry the exact job ID, role, writer scope, lock scope, timeout, output policy, state, and
environment-reference names. Systemd arguments use unit-file quoting, with literal `%` and
`$` doubled to prevent specifier and environment expansion; launchd argv remains unchanged.

Public output contains generic references such as `<WORKING_DIRECTORY>` and
`<LOG_DIRECTORY>`. An environment reference named `OPEN_BRAIN_EXAMPLE` renders as
`<OPEN_BRAIN_EXAMPLE>`; no value is resolved. Enabled jobs render enabled, while manual jobs
remain disabled in manifest metadata. The renderer has no enable/install API.

`validate_rendered_manifest` rerenders the immutable contract and compares the complete
service and timer output. It reads no files or environment and does not query either
scheduler. These contracts and synthetic tests are implementation evidence only, not a
live-health, cutover, or parity claim.

Each renderer rejects jobs that do not allow its platform. Persistent systemd schedules
use `OnCalendar` cadence plus `Persistent=true`, so one missed activation is recovered
after scheduler downtime. Monotonic `OnBootSec`/`OnUnitActiveSec` intervals cannot claim
missed-run persistence.
