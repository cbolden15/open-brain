# Portable Brain v10

The facet candidate uses local schema15, runtime session10 and Portable10.
It follows main schema14/runtime9/Portable9. Earlier facet-branch schema11 and
Portable6 pins are historical development evidence; those numbers identify
different published source-lifecycle formats on main and cannot be reused.

Portable10 retains Portable9 mixed V1/V2 historical authority, original capture
metadata, receipt custody, sharing approvals, source lifecycle and admission,
issuer identity, privacy, relationships and managed workspace evidence. It adds
three required canonical sidecars:

- `content/facets/state-v1.json`: dimensions, values, subject versions,
  memberships, saved queries and publication attachments.
- `history/facets/operations-v1.json`: canonical requests and exact receipts.
- `history/facets/events-v1.json`: ordered mutation evidence.

The catalog commits to the Portable9 catalog, these three v1 formats, and review
binding version5 for facet-query publication. C0/O0 wire revisions and fixture
bytes remain unchanged. Portable1–9 validators retain their own manifest and
catalog meanings. A Portable10 archive cannot be relabeled as an older format.

Export reads inherited and facet evidence from one schema15 SQLite snapshot
under the existing writer fence. Validation checks closed structures, canonical
bytes, cross-file identities, operation/event replay, source references and
publication bindings. Generated vault indexes and local projection custody are
rebuildable local state, not archive authority.

Clean restore validates before materialization, restores facets with inherited
evidence in the hidden stage, and retains Portable9 historical fencing and
recovery. It audits the reopened state before promotion. Portable10 uses ready
record version4, binding inherited semantic state and facet state/operation/event
digests plus the next sequence. Earlier ready record versions keep their meanings.
Exact import-ID retries reject archive or restored-authority drift. Restored
queries are inactive; archives do not grant access or reactivate withdrawn sources.

Real compatibility tests cover facets alongside denied historical membership,
retained source evidence and receipt custody, clean restore, promotion
interruption and retry. Earlier Portable9 archives restore with an empty facet
catalog. The coordinator's reconciliation manifest and audit record exact
candidate verification; fake adapter tests provide no restore proof.
