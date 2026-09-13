# M1 plan review

Verdict: fix-first. 7 actionable finding(s) and 0 FYI item(s) survived.

Model refutation was enabled, but at least one verifier was unavailable or omitted a verdict;
those findings remain unverified. Every evidence quote passed deterministic lookup. The review is
partial because the security lens failed.

## Actionable

### P1: Encryption-at-rest is an untraced milestone gate

- Lens: scope-guardian
- Location: M1-W0: close implementation decisions and freeze executable schemas
- Why it matters: M1 can stop before ledger implementation for a storage property that is absent
  from the stated objective, source-of-truth summary, and success criteria. This turns a substantial
  dependency, packaging, and platform-compatibility effort into a must-have without showing which
  accepted semantic contract requires it.
- Suggested fix: Either cite the exact M0 requirement that makes application-managed encryption at
  rest mandatory and add it to the success criteria, or defer SQLCipher/VFS and envelope encryption
  to a dedicated security milestone while keeping a key-provider-compatible storage seam.

### P1: Independent read-back does not prove SQLite physical cleanup

- Lens: adversarial
- Location: M1-W6: provenance-closed purge
- Why it matters: SQLite does not zero freed pages by default, and WAL or rollback-journal frames
  can retain deleted bytes. A purge that verifies only through SQL can certify cleanup while raw
  bytes remain recoverable.
- Suggested fix: Define physical cleanup at file level, including secure-delete behavior, WAL
  checkpoint/truncation, or equivalent controls, and test raw bytes after purge rather than only
  query results.

### P2: Query ranking depends on FTS built in a later wave

- Lens: feasibility
- Location: M1-W4 and M1-W5
- Why it matters: W4 promises authorization before ranking, counts, and snippets before W5 builds
  the compartment-partitioned search mechanism. W4 would need a stub or would reach into W5 scope.
- Suggested fix: Scope W4 query to structured retrieval and defer ranking, or build the minimum FTS
  shard-selection primitive before closing query behavior.

### P2: BrainPack semantic preservation has no acceptance gate

- Lens: scope-guardian
- Location: Source of truth and M1 success criteria
- Why it matters: M1 can pass without proving that its ledger retains every semantic field required
  by a later BrainPack v2 exporter. Missing state discovered later may require a storage migration.
- Suggested fix: Add the minimum BrainPack semantic-state inventory and prove it survives commit,
  restart, purge, and projection rebuild while leaving pack encoding/import/export out of M1.

### P2: Exact-label-set FTS can create unbounded shard fan-out

- Lens: adversarial
- Location: M1-W5
- Why it matters: A reader holding many compartment labels may need to search many qualifying exact
  label-set shards. The plan has no cardinality, shard, or query-fan-out bound.
- Suggested fix: Bound label/shard fan-out or specify another authorization-aware index strategy,
  then test realistic multi-label behavior and ranking isolation.

### P2: Cold-transfer proof and certificate schema are undefined

- Lens: adversarial
- Location: M1-W0 and M1-W3
- Why it matters: W0 omits a transfer-certificate schema, and W3 does not define enforceable proof
  that an old sequencer stopped, especially across machines.
- Suggested fix: Freeze the certificate and stop-proof contract in W0. State whether M1 supports
  only same-root transfer or includes a concrete cross-machine attestation mechanism.

### P2: RFC 8785 dependency failure has no stop behavior

- Lens: adversarial
- Location: M1-W0
- Why it matters: Request and commit digests depend on canonical bytes, but the plan does not say
  what happens if no maintained RFC 8785 implementation works on supported Python 3.14 targets.
- Suggested fix: Add the same hard stop used for encrypted SQLite. Do not hand-roll or silently
  substitute another encoding inside an implementation wave.

## Operator decisions

On 2026-09-04 the operator accepted all seven recommended resolutions:

- `M1-D1`: keep encrypted SQLite, FTS, and envelope-encrypted payloads in M1.
- `M1-D2`: define and test purge cleanup at the raw-storage level.
- `M1-D3`: build compartment-safe FTS before closing complete query semantics.
- `M1-D4`: add a BrainPack semantic-state inventory gate.
- `M1-D5`: use observed exact-label-set shards with measured limits.
- `M1-D6`: implement same-storage-root stop proof and block cross-machine activation.
- `M1-D7`: make RFC 8785 feasibility a hard stop gate.

These decisions authorize plan revision, not implementation. The revised plan still requires a
successful independent security review before M1-W0 begins.

## Review coverage

- Completed lenses: coherence, feasibility, scope-guardian, adversarial
- Failed lens: security
- Deterministic evidence lookup: passed for every surviving finding
- Final status: partial and fix-first

The operator decision form was a local-only artifact outside the repository.
The operator accepted all seven choices directly in the governing session. The security lens must
run successfully after revision and before implementation.
