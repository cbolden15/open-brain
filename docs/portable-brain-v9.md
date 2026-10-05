# Portable Brain v9

The current unreleased historical compatibility candidate uses local schema 14,
runtime session 9 and Portable9. Product version `0.1.0` does not establish these
coordinates or release acceptance. Use the exact artifact and catalog. Schema 13,
runtime 8 and [Portable8](portable-brain-v8.md) retain their frozen meanings.

## Required mixed history

Portable9 requires `history/historical-authority/reconciliation-v2.json` instead
of the V1 history sidecar. It retains every inherited source, privacy, issuer,
sharing, original capture metadata and journal custody witness. The closed
history sidecar has schema version 2 and a 64 MiB bound. Empty authority is
explicit; omission refuses validation.

The sidecar keeps an ordered V1/V2 operation chain, exact typed requests and
receipts, previous/proposed registry commitments and transition digests. V1
canonical bytes and hash domains stay unchanged. V2 requests, receipts and
transitions use separate version tags and digest domains. V2 observations and
requests have a separate 512 KiB encoded ceiling. The core TextPayload limit stays
65,536 characters: a JSON-escaped payload can occupy 393,216 bytes before bounded
metadata. Transition records retain a separate 9 MiB limit, covering two independently
bounded 4 MiB registries plus the request and receipt. Both versions share the same
registry generation sequence, operation ID uniqueness and pending fence.
Schema 14 stored checks retain V1 request/envelope limits at 64 KiB and admit
V2 request/envelope limits at 512 KiB. A baseline envelope also checks its parent
operation version. A wrong version, duplicate ID, missing transition or altered
witness refuses.
The V1-only exporter refuses mixed history, and a Portable8 reader refuses a
Portable9 archive.

## Retained evidence schemes

V2 separates the original capture request, source revision request and alias
commitments. Callers select one closed correspondence scheme; the engine does
not guess lineage or retry a failed scheme using another one.

| Scheme | Required stored evidence |
|---|---|
| `direct_revision_alias` | Alias equals revision request; capture request is bound independently |
| `schema_seven_alias` | Capture equals revision request; alias is SHA256 of the canonical JSON string representing that digest |
| `portable_import_projection_v1` | The exact supported Portable import projection described below, with genuinely null revision request/key/ordering and no derived-delivery alias |

The schema-seven tag describes the existing migration transformation. It does
not reconstruct an original envelope or infer a sender replay grant. Direct and
schema-seven evidence require nonnull revision and alias commitments and no
accepted-receipt field in the V2 witness.

The import projection tag represents facts intentionally materialized by the
supported Portable5 importer. Its capture delivery is `import.capture.` followed
by SHA256 of the capture ID's UTF8 bytes. The capture request equals the source
digest and SHA256 of the canonical retained record. Its source revision request,
revision key and ordering remain null, and no alias exists for that derived
delivery. The closed witness includes the exact accepted receipt ID.

Validation requires completed TextPayload/import admission and exact source and
capture membership, source path, canonical record hash, payload, stored privacy,
provenance, role claim, actor, origin, reference, intent, capture reason and
acceptance fields. The retained record must contain exactly valid canonical
acceptance evidence binding receipt identity, capture subject and payload.
Current-root verification also requires confined file bytes to equal immutable
revision bytes. No hash, alias or ordinary revision key is synthesized.

These schemes prove retained correspondence. They do not prove current upstream
selection, authentic old approval, independent recovery custody, current consent
or transport replay. Those proofs remain separate.

## Admission and current eligibility

V2 baseline admission allows a retained import original. The original must be
public with cloud and external egress disabled. A relation requires an existing
public-job copy with exact payload correspondence, public privacy and external
egress enabled; cloud may be enabled. Redaction findings and the core character limit still
refuse admission; a whole encoded envelope is checked against its separate byte
ceiling. Linking creates historical facts without recapturing either
body, relabeling capture admission or making a new sharing decision.

The trusted private caller verifies the current installed binding and upstream
observation before baseline admission. Before linking it also verifies authentic
old approval bytes and explicit provider coverage. The engine separately checks
owner-exclusive admission, current source CAS, generation, exact retained
evidence and current provider consent. Pending link recovery reloads consent
before remaining writes. Replaying an already completed receipt reports history;
it does not restore revoked consent or current eligibility.

External read and search eligibility require the complete immutable chain,
settled fence, exact SQL projection and current original/copy witnesses.
Revocation, source advancement, missing authority or projection loss denies the
copy. Owner retained history remains readable. Versioned baseline checkpoints
resolve the exact stored observation and receipt without creating capture
custody. A real successor can use the separately observed predecessor key while
the retained import revision remains null and unchanged.

## Clean restore

Portable9 validation cross-binds mixed history to every retained archive witness.
Restore uses a new hidden root, installs source/capture/journal state before
history, completes each transition through the existing forward fence protocol
and audits the settled result before no-replace promotion. An interrupted hidden
restore does not promote; exact retry neither drops authority nor duplicates it.
Re-export retains all non-manifest evidence bytes. Manifest export identity and
time describe the new export.

Portable1–8 validators, catalogs and record interpretations stay unchanged.
Portable8 archives can restore into schema 14 using a dedicated audit that
reconstructs their original V1 representation; the frozen Portable8 exporter
still only exports schema 13. Standalone owner restore supports Portable5–9.

An archive alone cannot prove freshness or independent protection. Deployment
must reconcile authenticated records accepted after the snapshot and verify the
separate protection authority before activation.

Existing source adapters keep their own ingress limits. Supporting a large retained
V2 observation does not change an adapter’s source-file ceiling or make that item
checkpoint-seedable through an unverified adapter.
