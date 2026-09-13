# Owner-approved inherited-history exception

Date: 2026-09-09

The owner approved a history-only exception for two already-public document versions and their
90 reviewed commit/path occurrences. The matched passages contain generic infrastructure labels
in copied authorization and restriction prose. Independent review found no credential, token,
private address, concrete hostname, account identifier, or rotatable secret in those passages.
The decision accepts the retained operational context in public history; it does not claim removal
from that history.

## Reviewed scope

Both exact document versions are present on public `main` at
`10769806361e7ed419b653534e1303b623d84673` and predate W5. Their authoritative occurrence inventory
comes from a disposable single-branch clone of W5 candidate `302ee0b`.

| Historical document | Content SHA-256 | Reviewed occurrences |
| --- | --- | --- |
| `docs/ai/workstreams/20260901-open-brain-public-phase3-planning-3c6a30/PHASE-3-GOAL-CONTRACT.md` | `9d368a4d0a0d3bf60b4bc51c99cbc4938ef063ee81b7508041d675323fb2615e` | 46 |
| `docs/ai/workstreams/20260901-open-brain-public-produce-an-executable-independently-reviewed-phase-4-plan-and-child-goal-contrac-d909e4/PHASE-4-GOAL-CONTRACT.md` | `3252188979827826f6192ba59039defe953374d624cce8c225807d6bff139d80` | 44 |

The forward cleanup commit `16702af` removes the copied standing-authority context from current
documents and retains the goal-specific restrictions. Cleanup precedes the policy commit, so the
policy commit introduces no new occurrence requiring its own approval. Public `main` is not rewritten.

## Enforced boundaries

Version 2 of `release/public-history-allowlist.json` retains the existing version-1 allowances.
Each denylist allowance additionally requires an exact reviewed commit ID and the fingerprint of
the full canonical denylist. The two entries enumerate all 90 approved occurrences; no other
repository ref, path, blob version, rule, or later occurrence is covered.

The fingerprint is SHA-256 of an ASCII JSON array of sorted, deduplicated normalized terms, using
`ensure_ascii=True` and separators `(',', ':')`. Normalization is the scanner's existing NFKC and
case-fold operation. Changes to comments, ordering, and duplicate lines preserve the semantic
fingerprint. Adding, removing, or changing a term invalidates approval and makes the audit fail
closed until the new term set is reviewed. All 12 consolidated owner terms remain active.

Version-1 policies still reject denylist allowances. Version-2 denylist entries without complete
metadata are rejected. Reviewed occurrence sets are bounded to 256 total IDs and reject duplicates,
invalid commit IDs, and invalid fingerprints. Other rule categories retain their existing policy.
The exception does not apply to current-tree or release-archive auditing.

## Verification contract

Regression tests cover exact blob/path/commit binding, later reintroduction at the same or another
path, other rule findings, unchanged current-tree and archive rejection, normalized term-set
equivalence, changed terms, malformed metadata, duplicates, and bounds. Existing version-1 tests
remain in place, including rejection of a broad denylist-rule allowance.

Before publication, run `make verify`, `git diff --check`, and actionlint. Run both canonical audits
against the exact integrated candidate in a disposable single-branch clone. The shared coordinator
checkout contains unrelated refs and is not the candidate-history audit boundary. Record final
verification in the pull request; this decision does not waive any other W5 or release gate.
