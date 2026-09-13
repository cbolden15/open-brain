# M1 open-source plan review

Status: all eight reconciled decisions accepted, integrated, and independently verified;
implementation not started

Date: 2026-09-05

## Review coverage

- The portable workflow completed coherence, feasibility, security, scope-guardian, and
  adversarial lenses with no failed lens.
- Deterministic evidence lookup passed. Model refutation ran where an independent family was
  available; findings without a verifier were retained conservatively.
- Result: 16 actionable findings, 4 FYI observations, and 5 findings removed by audit.
- `git diff --check` passed immediately before review.

## Coordinator reconciliation

The review findings reduce to eight decisions:

1. M1-D26: Freeze an honest M1 support matrix that preserves P4 evidence without claiming Windows
   or partially passing Python combinations.
2. M1-D27: Name achievable macOS/Linux user-presence mechanisms and a separate owner-control channel
   that delivers a principal-bound grant without writing it to disk.
3. M1-D28: Protect principal private keys and make receipt signing independently verifiable through
   the owner-to-Node certificate chain.
4. M1-D29: Freeze clocks and nonce durability, including rollback behavior and read-request
   contention.
5. M1-D30: Define purge, retain, replacement, post-purge replay, in-flight continuation, and
   cryptographic cleanup behavior separately.
6. M1-D31: Refuse network or synchronized Brain roots and bind the sequencer lease to one machine
   instance as well as one Node identity.
7. M1-D32: Make projections asynchronous with an explicit cursor watermark and a high-level
   read-your-writes wait.
8. M1-D33: Freeze the HTTP stack, literal query grammar, artifact-policy additions, compatibility
   record, and public threat-model update in W0.

## Operator resolution

On 2026-09-05, the operator accepted M1-D26 through M1-D33 without alternates. The accepted plan
therefore requires the complete macOS ARM64 and Linux x86_64 Python 3.12 through 3.14 matrix,
production user-presence adapters and an owner-control Unix socket, protected principal keys and an
owner-to-Node receipt chain, a separate encrypted replay store with frozen clock behavior, explicit
purge/retain/replacement outcomes with erasure overriding receipt replay, local unsynchronized Brain
roots, asynchronous projections with cursor watermarks, and the named public integration artifacts.

## Findings absorbed into those decisions

- User-presence production and test mechanisms were not executable.
- Grant delivery and client-key storage were unspecified.
- Python/package claims conflicted with the existing P4 compatibility record; Windows is currently
  classified unsupported.
- Nonce consumption on rollback, clock regression, and read-operation contention were undefined.
- Purge resolution effects, post-purge idempotent retry, continuation invalidation, and proof of key
  destruction were incomplete.
- A copied Node identity on a synchronized root could bypass a machine-local lock.
- Commit-to-query projection visibility was undefined.
- The HTTP dependency, receipt-signing chain, FTS query grammar, artifact-policy additions, and M1
  threat-model changes lacked owners.

## Independent verification

The bounded read-only verifier initially rejected two integration details. `REPLACE` did not repeat
ADR 0006's requirement that an owner-reviewed replacement no longer contain the erased target data,
and the query-visible projection watermark could have exposed a Brain-wide checkpoint affected by
unauthorized commits. The coordinator added the missing replacement rejection and separated the
internal Brain-wide checkpoint from an opaque grant-scoped watermark that advances only from
authorized commits.

The same independent verifier then performed a focused recheck and returned `PASS`, no remaining
P0-P2 findings, M0 ADR 0006 and ADR 0008 preserved, and high confidence.

M1-D26 through M1-D33 are resolved, integrated, and independently verified. M1-W0 may begin from a
clean local checkpoint; no later implementation wave may bypass W0's stop gate.
