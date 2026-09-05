# M1 plan final review

Status: operator decisions resolved with open-source usability amendments; final plan gate pending

Date: 2026-09-05

## Review evidence

- The first portable rerun was stopped after the coherence lens timed out twice on different routed
  models. Completed lens output was not treated as a complete review.
- The changed-strategy rerun denied third-party lanes and completed coherence, feasibility,
  security, scope-guardian, and adversarial lenses. No lens failed. It returned 17 actionable and
  7 FYI findings. Deterministic evidence lookup passed; independent model refutation ran where an
  independent family was available and otherwise retained findings conservatively.
- A separate read-only verifier returned `REWORK` with five P1 findings. It explicitly confirmed
  that M1-D8 through M1-D16 are all represented in the revised plan.
- A separate read-only repository explorer confirmed the package dependency, Python support,
  source-classification, compatibility-test, and shared-file ownership constraints.

## Coordinator reconciliation

The surviving issues were grouped into nine proposed operator decisions. The later operator
resolution supersedes the original D18, D23, and D24 recommendations where their wording differs:

1. M1-D17: Preserve Python 3.12 through 3.14 while separating the small canonicalization dependency
   from optional Reference Node native and OS integrations.
2. M1-D18: Require fresh owner presence for grant issuance and freeze a memory-hard passphrase
   envelope KDF, throttling, permissions, and migration rules.
3. M1-D19: Bind each grant to a principal key and require proof of possession on every request.
4. M1-D20: Pin the sanitized M0 source bundle and additively classify every M1 runtime and discovered
   Phase 4 subject under coordinator ownership.
5. M1-D21: Preserve the accepted Brain/delivery/digest idempotency tuple, authorize receipt replay,
   and keep changed-digest conflicts as typed results rather than inventing an inspectable conflict
   entity.
6. M1-D22: Persist active exact-label-set reference counts, validate and update them inside the
   commit transaction, reclaim empty sets after purge, and derive provisional limits from a
   specified synthetic corpus.
7. M1-D23: Bind query continuation to a stable authorized-shard snapshot, accumulate bounded
   cross-shard ranks without unauthorized corpus statistics, and freeze executable evidence-link
   requirements.
8. M1-D24: Keep the accepted minimal durable-job and loopback HTTP requirements in M1 while denying
   generic scheduling, non-loopback binding, browser-origin access, and unauthenticated HTTP routes.
9. M1-D25: Close the remaining read, purge, and operational checks: `changes` isolation, concurrent
   purge descendants, nonce retention, file permissions, crash-safe same-Node lease recovery,
   application-controlled sink boundaries, and focused key-custody verification.

## Findings not carried forward as choices

- Deferring HTTP or durable jobs conflicts with the accepted Reference Node target.
- Deferring redacted security events conflicts with accepted decision M1-D16.
- Adding principal identity to the idempotency key conflicts with accepted ADR 0003. Authorization
  must gate receipt replay without changing the Brain, delivery identity, and digest tuple.
- Real data is out of scope for M1. Bounds must use a reproducible synthetic corpus and remain
  provisional until a later authorized real-data milestone.
- M1 can verify only application-controlled durable sinks. Swap, hibernation, core dumps, and OS
  crash-report policy belong to the deployment threat model and cannot be certified by the Node.

## Operator resolution

On 2026-09-05, the operator approved M1-D17 through M1-D25 with the explicit requirement that the
open-source product be usable by people who should not need to understand its cryptography,
dependency extras, shard layout, or continuation machinery.

1. M1-D17 accepted with a one-command installation contract: keep Python 3.12 through 3.14, make
   RFC 8785 a required engine dependency, put native Reference Node dependencies in an engine
   `node` extra, and make the top-level app select it automatically.
2. M1-D18 amended for usable owner authentication: interactive unlock creates a bounded in-memory
   owner session, while broad or destructive grants require fresh step-up authentication. A
   background keyring read is not proof of human presence. The passphrase envelope uses frozen
   Argon2id parameters and owner-only permissions.
3. M1-D19 accepted with hidden complexity: grants use Ed25519 proof of possession, while the
   high-level reference client handles principal keys and request signing automatically and public
   vectors support independent clients.
4. M1-D20 accepted: publish a sanitized content-hash manifest and self-sufficient public ADRs, then
   additively classify every discovered M1 subject under coordinator ownership.
5. M1-D21 accepted: preserve the Brain/delivery/digest idempotency tuple, authorize receipt replay,
   and return typed conflicts without adding an implicit inspectable conflict entity.
6. M1-D22 accepted: maintain active label-set counts inside the writer transaction, retire empty
   shards after purge, and derive provisional limits from a reproducible synthetic corpus.
7. M1-D23 amended for usable search: bind continuation to a stable shard snapshot, mark intermediate
   ranking provisional, produce a deterministic final rank with authorized evidence, and make the
   high-level client follow continuation automatically.
8. M1-D24 amended for operations: keep minimal durable jobs and secured loopback HTTP, and permit a
   static anonymous `/healthz` that reveals process liveness only.
9. M1-D25 accepted with actionable errors: close changes isolation, concurrent purge, nonce,
   permissions, process-lock, sink-boundary, and focused key-custody checks; fail-closed responses
   explain retry safety and corrective action.

The revised plan must pass the final independent plan gate before any M1-W0 implementation starts.
