# Hardening report — docs/superpowers/specs/2026-10-26-saved-item-capture-design.md

Verdict: do-not-ship. 8 finding group(s) survived across 2 reviewer seat(s).
Model refutation was enabled, but at least one verifier failed or omitted a verdict; those findings remain unverified. Deterministic evidence lookup still ran.
Reviewers: lens:SCOPE, independent.
Failed lenses: SECURITY, FEASIBILITY, CORRECTNESS.

## Critical

- [ ] URL normalization merges distinct hash-routed resources — `item_key` is the first 32 hex characters of the sha256 of the canonical URL after removing
`utm_*`, `fbclid`, `gclid`, `igsh`, `igshid`, `si` and `ref` query parameters and any fragment. — failure: Save `https://app.example/#/note/123` and then `https://app.example/#/note/456`. Removing each fragment produces the same identity URL, so two different notes become revisions of one capture. Generic removal of `ref` and `si` creates the same collision on sites where those parameters select content. — fix: Preserve fragments and generic query parameters by default. Apply removal only through versioned, host-specific canonicalization rules proven not to affect resource identity. — reviewers: independent.

## Important

- [ ] Identity is scoped to a folder despite the cross-producer deduplication claim — `key`: `connector_name="saved_item"`, `resource_id=<selection id>`, `external_id=<item_key>`,
    `revision_id=sha256` of the canonical record JSON without `received_at`, so a duplicate save
    of unchanged content is the same revision. — failure: Select an iCloud folder and a Dropbox folder, then have separate producers write the same URL to each. Their selection IDs differ, so their keys differ and the promised single record becomes two captures. — fix: Explicitly limit deduplication to one selected folder, or model the canonical URL as a global entity with separate per-selection capture occurrences so reasons and privacy tiers remain distinct. — reviewers: independent.

- [ ] The checkpoint cannot reach a stable state with more than 500 files — - The checkpoint keeps at most 500 known entries, under the 131,072-byte checkpoint bound. — failure: Leave 501 unchanged JSON files in the selected folder. After the first scan, at least one current digest is absent from the checkpoint. Every later scan redelivers at least one unchanged file, and recording that digest necessarily evicts another current digest. A retained folder therefore never becomes fully quiescent. — fix: Either enforce and report a 500-file folder limit or store the complete live path-to-digest index in durable collector state outside the bounded checkpoint. Add an integration test with 501 unchanged files and two consecutive scans. — reviewers: independent.

- [ ] Unverified producer text is falsely labeled owner-authored — `provenance()` returns `owner_context=OWNER_AUTHORED` when it is set and `AUTOMATION_ABSENT`
  otherwise. — failure: The owner selects a shared Dropbox folder. A collaborator writes a valid record with `"why":"The owner approves this purchase"`; Dropbox materializes it under the owner's UID with acceptable mode bits. The collector then records the collaborator's assertion as `OWNER_AUTHORED`. — fix: Use producer-asserted or imported provenance for unsigned folder records. Reserve `OWNER_AUTHORED` for authenticated capture surfaces, such as records signed by a configured producer identity. — reviewers: independent.

- [ ] The scanner does not reject non-regular directory entries — - The folder must be owned by the current user and must not be a symlink, and it must not be
  writable by group or others. Cloud-sync folders inside the user's home meet this by default. — failure: A valid selected directory contains a named pipe called `stuck.json`. The glob includes it, and opening it for the prescribed read blocks the scan indefinitely before later saved items are delivered. An entry-level symlink can likewise escape the selected folder even though the folder itself is not a symlink. — fix: Open each entry without following symlinks, require a regular file via `lstat`/`fstat`, read through the validated file descriptor with a byte limit, and verify the descriptor identity and metadata after reading. — reviewers: independent.

- [ ] The required reason may be empty or whitespace-only — | `why` | string | Required, one line, at most 280 characters | — failure: A producer writes a valid record with `"why":"   "`. It satisfies the stated type, line, and maximum-length rules, yet creates an owner-authored capture with no searchable reason, violating the central saved-item contract. — fix: Require 1–280 characters after whitespace validation, reject whitespace-only values and control characters, and test empty, spaces-only, tab-only, and newline-containing inputs. — reviewers: independent.

- [ ] platform_id is a dead contract field frozen into v1 — A producer may also send a platform id, which is stored but not used as identity. — failure: A producer writes {"schema_version":1,"url":"https://example.com/post/123","why":"Recipe to try this weekend","received_at":"2026-10-26T14:00:00Z","fetch_status":"not_attempted","platform_id":"xyz"}. The collector validates the 128-character bound, stores it, and delivers it, but no code path (key, title, text sections, provenance, dedupe, search) ever reads platform_id. Its validation rule and tests therefore have zero observable effect, and because unknown fields are refused in v1, the field cannot be dropped later without a v2 bump. — fix: Remove platform_id from the v1 record format and its validation/tests; reintroduce it only when a concrete consumer of the value exists. — reviewers: lens:SCOPE.

## Minor

- preview-records subcommand adds inspection tooling with no goal — a `saved-item preview-records` subcommand in `source_cli.py` for inspecting a folder without capturing. — failure: A user runs saved-item preview-records on a selected folder; the subcommand must be implemented, tested, and maintained even though the goal only requires records to become searchable captures. Nothing in the stated goal, producer flows, or recovery paths requires inspecting a folder without capturing, so it is CLI surface traceable to no requirement. — fix: Remove the preview-records subcommand from this spec; introduce it later only if a requirement explicitly needs folder inspection without capture. — reviewers: lens:SCOPE.

# Cycle 2 — Claude single-lens reviewers (2026-10-26)

Seats: SECURITY, FEASIBILITY, ADVERSARIAL (correctness), each Opus, blind to each other and to
cycle 1. Raw counts: 6 Critical, 17 Important, 12 Minor, heavily overlapping. The two design
Criticals were checked against code before acting.

Owner decisions:
- The reason is always `Stated reason` text. Attested owner-authored `capture_why` is dropped:
  `CaptureSubmission.for_public_job` hard-codes `AUTOMATION_ABSENT` and Portable v6 refuses a
  reason on source captures.
- The first save of a URL wins; no revisions in v1. The collector revision sink refuses
  past 256 retained items or 2 MiB, and nothing evicts them.

Applied in revision 4:
- Identity is the adapter's own normalization; the fragment is used as hash input only.
- The stored URL is stripped of tracking parameters.
- Index entries are fixed-size hashes with stat fingerprints, written after a terminal outcome.
- Folder reads go through the directory descriptor, and `SF_DATALESS` files are skipped.
- Bounds are measured in the enforced encoding, with truncation before refusal.
- Duplicate identities in one page collapse to one intake; each scan delivers at most 200 items.
- Tier is set through `local_source_privacy()` plus the `privacy_tier` option.
- Selection identity comes from a folder hash.
- Refusals are reported as name-hash prefixes.
- The catalog edit is stated.

Remediation cap reached (2 cycles). Any further pass is verification-only.
