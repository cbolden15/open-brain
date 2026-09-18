# Portable Brain v3

This describes the historical v3 format. The M2 source build exports
[Portable v4](portable-brain-v4.md), preserving the review evidence described here.

Portable Brain v3 adds durable review bindings for canonical page creation and updates. Existing
capture, proposal, decision, publication, page, and managed-workspace bytes keep their established
record shapes. A Brain exports v3 once it contains a bound review proposal, including a pending or
rejected proposal.

Each bound proposal has one immutable record at:

```text
history/review-bindings/YYYY/MM/proposal_<uuid>.json
```

The record identifies the stable page, whether the proposal creates or updates it, the exact parent
publication and page digest for an update, the explicitly selected captures, and the cumulative page
provenance. It also freezes each source capture record digest and its route state at proposal time.
The review digest covers the complete proposal record and binding. The terminal decision repeats
that digest as `expected_state_digest`, so approval cannot be moved to another page, predecessor, or
source set.

An approved publication is the page revision identity. An update names the preceding
`publication_id`; no second revision record is created. Pending and rejected proposals may name the
same predecessor, but approved publications form one linear chain. The current canonical Markdown
must equal the chain head, retain the same page ID and location, and contain the cumulative ordered
provenance.

The v3 manifest reports layout and schema version 3, contract version `3`, compatibility from `1`
through `3`, and the v3 schema-catalog digest. A v3 root contains at least one review binding and may
also contain the single managed-workspace record introduced by v2. Readers that support only v1 or
v2 reject the v3 manifest rather than partially importing its review history. V3 readers continue to
accept historical v1 and v2 roots unchanged.

Clean import restores the exact Portable files, frozen review contexts, source membership, current
page heads, and the disposable search projection. Re-export preserves every non-manifest Portable
byte. Page replacement uses the predecessor page digest as a compare-and-swap condition; replaying
already-written revision bytes succeeds without creating another publication.
