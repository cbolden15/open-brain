# NW0-A2 portable representation comparison

Status: private comparison complete; durable representation is not frozen and NW0-A remains open.
This follows the [parallel checkpoint](2026-09-09-ob1-nw0-parallel-feasibility.md) at `a6363f1`.
Eight grouped synthetic tests pass. No shipping schema, dependency, migration or runtime changed.
No inference, desktop activity or startup measurements ran.

## What the current v1 format can preserve

An active canonical page can retain its original single-capture provenance while its body changes.
The private probe stores revision body/base digests, edited bytes, accepting-actor information and
lifecycle/policy audit data inside a bounded existing `EventPayload` capture. It then exercises
real Engine capture, reconciliation, export, validation, import, reconciliation and retrieval.

The edited page and event capture survive that sequence. Original capture bytes, including their
local-only privacy decision, remain unchanged. This is a concrete v1 carrier that avoids the
multi-provenance failure found earlier. It demonstrates byte preservation, not a new authority API.

The payload's inactive/excluded flags are not enforced. The page remains retrievable, and the
synthetic policy marker itself becomes searchable content. Current readers treat the carrier as a
capture; a new reader would need reviewed semantics to turn it into revision or policy state.
No cloud dispatch occurred, and this test does not establish unauthorized egress by the old runtime.

## Controls isolate the archival mismatch

| Case | Observed behavior |
|---|---|
| Unchanged active canonical export/import | Validation, import and authoritative reconciliation pass. |
| Edited active page plus generic event audit | Validation, import and reconciliation pass; body and audit bytes survive; embedded lifecycle restrictions have no effect. |
| Existing `archived` page | Validation and import pass. Import omits its canonical search row, but reconciliation still expects the retained page to have that row and refuses it. Original source text remains retrievable. |
| Future contract-version marker | Current validation and import reject the synthetic export as an unsupported Portable contract version. The import destination is not created. |

The first combined case mixed archival state with the event carrier and failed after import.
The unchanged control and separated cases show that failure belongs to archival reconstruction,
not all unchanged imports or every single-provenance edit. The relevant current paths are
`engine/materializer.py`'s archived-page skip and `engine/reconciliation.py`'s canonical-row check.
No shipping fix was made in this experiment.

## Explicit typed-state candidate

A separate private file uses typed revision and owner-policy records with stable note references,
body digests, ordered parent references and explicit acceptance provenance. Its tests cover retained
revision history, an accepted link bound to reviewed endpoint revisions and source quotes, inactive
state, explicit restore, stable-ID exclusions and positive consent staying inactive after import.
The tested invalid references, altered bodies, unknown families/versions, duplicate records/keys,
changed accepting-owner labels and unreviewed link edits are rejected.

The test adds that file to a synthetic export marked with contract version `2`. This number is a
negative-control value, not a selected shipping version. The current reader's version refusal works.
The separate replay validates only the bounded typed file, not the entire future Portable export.

These limits matter:

- Origin capture/page references are synthetic and are not joined to real exported base records.
- Digests and owner-label comparisons establish consistency, not authenticated owner intent.
- Identifier syntax is only prefix-checked; duplicate origin capture IDs and malformed consent
  timestamps are accepted by this prototype. They require validation in a complete representation.
- The private projection is not a new Engine importer, canonical materializer or search index.
- Full manifest/catalog/inventory validation, shared-envelope conversion, conflicts, folder rules,
  crash recovery, concurrent release/accounting and filesystem promotion remain unproven.

The proof's local envelope has its own 64 KiB cap. This does not change the 16 KiB selected-input
and accepted-output limits for provider attempts; no provider attempt is made here.

## Recommendation and alternatives

Prefer an explicit next Portable contract with required workspace semantics and typed durable
revision/policy records. Reuse existing v1 record bytes where their meaning is unchanged. Unsupported
readers should refuse the export rather than silently lose inactive state or restrictions. This is a
recommendation for the next proof, not adoption of a version number, schema or migration.

A v1-compatible carrier plus a mandatory new reader contract is another candidate. It still needs
an enforceable compatibility boundary. A label inside an event/action cannot supply that boundary;
adding required manifest semantics is itself a compatibility change. These results do not show that
every v1 composition is impossible.

Generic action records were considered read-only but were not exercised as policy carriers. Their
validator requires an `external_action` approval receipt and proposal/decision linkage; using them
as owner consent is not a drop-in semantic match. The existing shared-record envelope also has a
fixed v1 source contract and a closed family list. It preserves known v1 records, but does not prove
that older consumers preserve arbitrary new families. Any selected contract must include the
Portable/shared-record/Secure Node upgrade seam.

Before freezing a representation, bind typed revision and policy references to actual exported
records and validate the whole candidate, including import followed by authoritative reconciliation
and retrieval. Define restriction-preserving import, inactive destination consent, unknown-reader
and downgrade behavior, and shared-envelope conversion. Shipping integration and migrations remain
NW1 work after NW0's required proof and review gates.

## Verification and preserved gates

The coordinator ran eight grouped tests against the existing Engine and private replay, plus Ruff.
An independent reviewer reran all eight tests and confirmed the documented limits with focused
counterexamples. The review accepted the private proof's conclusions while keeping NW0-A open.
The initial combined-case failure remains recorded; the final cases separate the expected behaviors.
Public documentation passed bounded content/link checks, `git diff --check` and `actionlint`.
Full product/native/Homebrew checks were not rerun because shipping code and build inputs are unchanged.

B4's 213 ms warm-start failure, controlled-cold/Linux gaps, approved upstream-first/maintained-patch
policy, Claude C7 block and Codex deferral remain unchanged. B5 remains independent pending serialized
measurements. The real-provider matrix, desktop journeys and remaining NW0-A–E gates stay open.
