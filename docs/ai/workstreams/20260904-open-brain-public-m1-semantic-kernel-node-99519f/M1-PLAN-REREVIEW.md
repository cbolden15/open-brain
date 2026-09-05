# M1 plan rereview

Verdict: rework. 8 actionable findings and 4 FYI items survived.

Model refutation was enabled, but at least one verifier was unavailable or omitted a verdict;
those findings remain conservatively unrefuted. Every evidence quote passed deterministic lookup.
All five requested lenses completed, including security.

## Actionable

### P0: Root key custody is undefined

- Lenses: feasibility and security
- Location: M1-W0, M1-W2, M1-W7, and success criteria
- Finding: The plan requires an injected Brain key provider but does not decide where the root key
  lives, how a clean setup provisions it, how restart unlock works, or what destruction means. A
  test-only in-memory provider or a plaintext key beside the database could satisfy the current
  wording without providing meaningful encryption at rest.
- Suggested fix: Freeze root-key custody and bootstrap in W0. Reject plaintext keys beside the
  ledger and prove clean-root provisioning plus restart unlock through the production provider.

### P0: Purge proof excludes logs, errors, and temporary files

- Lenses: security and adversarial
- Location: M1-W2, M1-W6, and success criteria
- Finding: The listed purge surfaces omit application logs, exception reports, SQLite/FTS spill
  files, and temporary files. A purge could certify completion while those surfaces retain payload
  text, digests, or sensitive metadata.
- Suggested fix: Keep SQLite temporary and FTS scratch state inside the encrypted root or memory,
  prohibit sensitive values in logs/errors, scan every permitted sink, and include those surfaces
  in purge completion tests.

### P1: Grant issuance and custody lack a root of trust

- Lens: security
- Location: M1-W3 and M1-W7
- Finding: Verification is specified, but the plan does not say who mints grants, where signing keys
  live, how a caller obtains a grant, or how grant secrets are protected. An implementer could add
  an unauthenticated loopback issuance endpoint.
- Suggested fix: Freeze an owner-authenticated, out-of-band issuance path; keep issuance unreachable
  through protocol transports; define grant lifetime bounds; and prohibit plaintext grant storage
  or logging.

### P1: Inspect lacks authorization and compartment filtering

- Lens: security
- Location: M1-W4, M1-W8, and success criteria
- Finding: `inspect` can expose records, receipts, conflicts, jobs, purge state, labels, and
  provenance, but its section has no explicit all-of compartment or metadata-isolation rule.
- Suggested fix: Authorize before selecting an entity, return non-discriminating typed not-found,
  hide bodies/content-derived identifiers, and add inspect-isolation conformance tests.

### P1: Label and shard limits are not enforced at commit

- Lens: adversarial
- Location: M1-W0, M1-W1, M1-W2, and M1-W5
- Finding: W0 freezes limits and W5 enforces query fan-out, but no transition or batch rule rejects a
  commit that would exceed label-per-record or observed-shard limits. The invalid state would
  already be immutable before the projector sees it.
- Suggested fix: Validate the limits before cursor allocation and reject the complete batch with a
  typed error.

### P2: Disjoint wave scopes conflict with shared schema evolution

- Lens: feasibility
- Location: commit/review rules and M1-W2, W3, W6
- Finding: W3 needs nonce, lease, and receipt-binding state in W2's repository; W6 needs projection
  invalidation hooks in W5 files. Literal disjoint file ownership would force duplicate stores or
  unplanned schema edits.
- Suggested fix: Keep schema/repository files coordinator-owned across waves with explicit additive
  migrations, or move every later storage field and hook into the earlier owning wave.

### P2: Broad authorized queries have no fan-out degradation path

- Lens: adversarial
- Location: M1-W5
- Finding: A single fail-closed shard cap can eventually reject legitimate owner queries as observed
  label combinations grow.
- Suggested fix: Define bounded continuation or staged shard pagination instead of an unconditional
  refusal, or justify a separate owner bound without weakening authorization.

### P2: Receipt digest binding conflicts with the public-identifier rule

- Lens: adversarial
- Location: M1-W0 and M1-W3
- Finding: The plan says content hashes are not public identifiers while receipts bind to a commit
  digest. It does not say whether that digest is serialized or internal.
- Suggested fix: Freeze whether the authorized mutating receipt exposes the digest as proof or uses
  an internal binding, and state that a digest can never serve as a queryable public object ID.

## FYI

- Cipher suite, nonce uniqueness, key wrapping, and SQLCipher/VFS KDF parameters need an explicit
  owner in W0.
- Resource limits should also cover commit cardinality, staging disk use, concurrent requests, and
  nonce-table growth.
- Security audit events need a redacted durable sink for denials, revocations, fencing, and key
  destruction without logging grants or payload material.
- Protocol security tests should use one directory convention under
  `packages/engine/tests/security/protocol_v1/`.

## Review coverage

- Completed lenses: coherence, feasibility, security, scope-guardian, adversarial
- Failed lenses: none
- Deterministic evidence lookup: passed for every surviving finding
- Final status: verified review topology with conservative unrefuted findings; rework required

## Operator decisions

On 2026-09-05, the operator accepted all nine presented choices. These decisions revise the plan;
they do not change the original review verdict above.

1. M1-D8: Add a `RootKeyCustodian`; prefer OS-backed secret storage and support an audited,
   passphrase-encrypted fallback; require explicit unlock; reject plaintext root keys in a Brain
   root; exclude the in-memory test provider from acceptance.
2. M1-D9: Treat logs, errors, staging, and SQLite/FTS temporary state as purge surfaces; prohibit
   sensitive values there and raw-byte scan every permitted durable sink before the purge receipt.
3. M1-D10: Keep owner/issuer signing keys under root-key custody; mint short-lived grants only
   through an owner-authenticated local control capability outside all protocol transports; treat
   grant bytes as credentials.
4. M1-D11: Authorize `inspect` before target selection; use one typed not-found result for absent and
   unauthorized targets; require explicit body scope for bodies or content-derived values.
5. M1-D12: Compute post-batch label and observed-shard state during pure validation; reject the
   entire batch with a typed limit failure before allocating a cursor.
6. M1-D13: Keep schemas, repository adapters, public facades, and projection hooks coordinator-owned;
   predeclare later-wave state where practical and integrate later changes as additive migrations.
   Disjoint writer scopes apply to concurrent work, not all future serial waves.
7. M1-D14: Freeze opaque query continuations and traverse authorized shards in bounded,
   deterministic rounds; re-authorize each page and provide no owner exemption.
8. M1-D15: Return the canonical commit digest only as signed verification evidence to the authorized
   committer; never use it as a public ID, lookup key, query identifier, or bodyless inspect field.
9. M1-D16: Freeze cryptographic parameters in W0; bound commit, staging, concurrency, and nonce
   resources; persist only redacted security events; and consolidate protocol security tests under
   `packages/engine/tests/security/protocol_v1/`.
