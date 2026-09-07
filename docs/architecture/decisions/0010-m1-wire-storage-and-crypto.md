# ADR 0010: M1 wire, storage, and cryptography

Status: accepted

Date: 2026-09-06

## Context

M1 needs one public encoding and one local encrypted storage profile before semantic or persistence
workers begin. Choosing either inside a later implementation wave would make receipts, replay,
migration, and erasure incompatible.

## Decision

### Canonical JSON and dependencies

The engine requires `rfc8785>=0.1.4,<0.2`. Request bodies and commit batches are canonicalized with
that package and hashed with SHA-256. The strict protocol decoder rejects duplicate JSON names,
non-finite numbers, invalid UTF-8, and values outside the RFC 8785 data model before operation
dispatch. It does not substitute `json.dumps` or another encoder.

All wire timestamps use canonical UTC `Z` with either whole-second precision or exactly three
fractional digits. Offsets and every other fractional width are rejected before semantic time
comparison. Millisecond precision is exact across the supported protocol implementations and avoids
language-specific truncation of RFC 3339 fractional seconds. JSON Schema `format` is not a security
boundary because implementations may treat it as an annotation. The required semantic validator
accepts hours `00` through `23` and minutes and seconds `00` through `59`, constructs the instant
from those components, and checks the calendar validity of every declared wire timestamp. Hour `24`
and leap-second `60` are outside this protocol profile. Validation does not depend on a language
parser's normalization rules and does not inspect timestamp-like fields inside opaque application
bodies.

The Reference Node uses `sqlcipher3==0.6.2`, which bundles SQLCipher 4.12.0 Community Edition, for
the encrypted SQLite ledger, replay store, and FTS projections. `sqlcipher3`, `cryptography`,
`argon2-cffi`, `keyring`, and the macOS LocalAuthentication bridge are in the engine `node` extra.
The top-level application selects that extra and directly includes Starlette and Uvicorn. A user
installs `open-brain` without naming an extra.

The compatibility summary is `release/m1-compatibility.json`; the sanitized per-command receipts,
stdout, stderr, setup hashes, and exact cell results are in
`release/m1-compatibility-receipts.json`. The summary pins the raw-receipt and probe SHA-256 values.
`tools/m1/compatibility_matrix.py` reproduces the matrix with a 900-second bound per cell. It records
Python 3.12, 3.13, and 3.14 on macOS ARM64 and Linux x86_64 for both wheel-only and source builds.
Every cell runs RFC 8785, keyed reopen, wrong-key rejection, FTS5, logical purge, plaintext residue,
unclean-process recovery, commit, and paged-read probes. Logical purge checks both canonical and FTS
queries before and after checkpoint and vacuum; encrypted-file residue scanning is a separate
check. Windows is outside M1. Phase 4 evidence stays unchanged.

### Ledger-history commitment profile

The wire form of a ledger head is a single `history_commitment`. It never serializes the prior
cursor, commit ID, or canonical commit digest. Its exact value is `lhc_v1_` followed by the
unpadded Base64URL encoding of:

```text
SHA-256(UTF-8("open-brain-ledger-head-v1") || 0x00 || RFC8785(complete_signed_prior_receipt))
```

The domain separator and the complete receipt, including its Node signature, are mandatory. A
client that holds the prior receipt can recompute the commitment. A later committer that does not
hold that receipt learns no prior commit identity or canonical commit digest from the commitment.
The commitment is continuity evidence, not an entity identifier or lookup key. A stop proof,
cold-transfer certificate, and following Node epoch certificate carry the same value.

### Encrypted database profile

Each Brain has a separate versioned v1 namespace. The database key is a 256-bit value derived from
the Brain root key by HKDF-SHA-256 with the Brain ID, store role, and crypto version in `info`.
SQLCipher receives the derived value through raw-key syntax. The compatibility probe uses a fixed
synthetic raw key so installation modes measure the same encrypted database behavior.

SQLCipher compatibility is 4, page size is 4096 bytes, page HMAC is HMAC-SHA-512, KDF compatibility
is PBKDF2-HMAC-SHA-512 with 256,000 iterations for any password-keyed migration, plaintext header
size is zero, and memory security is on. Production v1 stores use the derived raw key and therefore
do not apply the password KDF during ordinary opens. All settings and the `crypto_version=1` marker
are persisted and checked before application tables are read.

Temporary tables and sort state use memory. WAL, SHM, rollback journals, database backups, and FTS
files remain inside the encrypted Brain boundary with owner-only permissions. A connection that
cannot prove `cipher_version`, FTS5, and the expected crypto marker fails before use.

### Payload and key envelope profile

Every payload or blob revision receives a new random 256-bit data key. Its content uses AES-256-GCM
with one random 96-bit nonce and protocol-versioned associated data containing the Brain ID, object
role, object ID, revision, media type, and plaintext length. One data key encrypts one content value,
so a nonce is never reused under that key. A retry reuses staged ciphertext only for the same
delivery and digest; otherwise it creates a new data key and nonce.

Data keys are wrapped with RFC 3394 AES-256 Key Wrap under a versioned Brain wrapping key. The wrap
format is represented by two typed envelopes. `WrappedKeyEnvelope` contains the crypto version,
wrapping-key ID, data-key ID, and wrapped data key. `CiphertextEnvelope` contains the crypto
version, data-key ID, AEAD nonce, lowercase hexadecimal SHA-256 associated-data digest, lowercase
hexadecimal SHA-256 digest of the ciphertext including its authentication tag, and ciphertext.
Rotation rewraps data keys without decrypting payload bytes. Erasure destroys scoped wrapped keys
and verifies that no remaining key path decrypts the target.

Encryption receives the complete associated-data bytes. Decryption receives those expected bytes
again, checks their digest against the envelope, checks the envelope's data-key ID against the key
handle, checks the ciphertext digest, and passes the complete bytes to AES-GCM. It never substitutes
the associated-data digest for the associated data. A mismatch fails before plaintext is returned.

### Root key custody and passphrase fallback

`RootKeyCustodian` owns bootstrap, explicit unlock, restart, derivation, data-key generation,
public-key access, signing, authenticated encryption and decryption, key wrapping and unwrapping,
rotation, and destruction for Brain root, owner, issuer, Node signing, database, wrapping, and data
keys. Secret key bytes never cross the port; callers receive purpose- and Brain-scoped opaque
handles and typed envelopes. A rotation call identifies the exact key handle being rotated.
Rotation and destruction require fresh user-presence evidence. The production adapter prefers an
OS secret store. Plaintext root keys inside the Brain root are forbidden. The in-memory provider is
test-only and cannot satisfy acceptance.

The portable fallback stores an owner-only passphrase envelope outside the Brain root. It derives a
256-bit envelope key with Argon2id version 19, 64 MiB memory, 3 iterations, 4 lanes, a fresh 128-bit
salt, and a 256-bit tag. These are the RFC 9106 memory-constrained parameters. The envelope uses
AES-256-GCM and records every parameter and version. Failed unlocks use exponential delay from 250
milliseconds through 30 seconds and persist only a metadata failure counter and UTC high-water
time. Parameter increases create a new envelope after a successful unlock; downgrades are rejected.

### Namespace, migration, and recovery

The v1 namespace starts at schema version 1 and never reinterprets `phase1.sqlite3`. A migration
opens the old version read-only, checks its authenticated schema marker, stages a new encrypted
database, copies semantic rows in one ordered transaction, runs integrity and residue checks, then
atomically replaces the namespace manifest. The old encrypted files remain until the new manifest
is durably visible and are then removed by a restart-safe cleanup job.

Each transaction records a migration or commit intent before mutating canonical tables. Restart
rolls back incomplete SQLite transactions, discards unreferenced staged blobs, resumes committed
cleanup intents, and rebuilds disposable projections from ordered commits. Crash recovery never
advances a cursor twice.

### Brain roots and sequencing

The default Brain root is the platform application-data directory. Bootstrap refuses known network
filesystem types and known synchronized roots. Unrecognized synchronization tools are unsupported.
The root and runtime directories use mode `0700`; regular sensitive files use `0600`.

The sequencer lease binds Brain ID, generated machine-instance ID, Node ID, epoch, and last cursor.
A copied identity is write-paused. Cold transfer requires a Node-signed stop proof binding the old
Node, machine instance, epoch, and ledger head, plus an owner-signed certificate binding that proof,
the next Node and public key, both machine instances, the same prior head, and exactly the next
epoch. The transfer cannot predate the stop proof. The old epoch cannot resume writes.

### Durable and portable state

Permitted application-controlled durable sinks are the encrypted ledger and replay databases,
encrypted FTS files, encrypted blob and staging files, versioned key envelopes, root manifest,
lease and transfer proofs, metadata-only security events, and opaque delivery-erasure tombstones.
Logs and errors are not payload sinks.

BrainPack-required state is the inventory in `docs/architecture/brain-protocol-v1.md`. Host locks,
grants, nonces, jobs, checkpoints, and indexes are not semantic pack data.

## Consequences

M1 can proceed without a private dependency or native compilation on supported wheel targets.
Source builders need a working C toolchain and Conan cache, which the compatibility record states.
Later storage workers must implement these profiles as written or propose a new ADR before changing
wire bytes, key formats, database settings, or erasure behavior.
