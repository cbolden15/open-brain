# Retraction impact foundation

The internal `engine.lifecycle.inspect_retraction` task resolves the effects of
one capture or trusted capture session in an admitted read snapshot. It requires
the actual local owner with unrestricted space scope. It takes no writer lease,
creates no journal event, and returns no approval or preview token.

Its immutable result retains exact ordered selected capture IDs, session
membership version/digest, sorted typed effects, complete counts and an internal
preimage digest. `page(item_page)` returns at most 100 body-free items; pagination
does not change the selected set. Its status is `impact`, not the proposed public
`preview` DTO. No CLI, MCP, plugin or desktop command exposes this task yet.

Effects include selected captures, newly suppressed current groups, newer heads
preserved when a historical member is selected, dependent canonical/managed
pages, and existing approved copy identities. A current group's unselected
siblings participate in dependency discovery. Discovery follows historical and
sharing copies and all three development dependency lists, then retained
canonical membership, every managed revision's provenance, and reverse managed
links, including inactive links. Already retracted captures are counted
separately. Retraction retains history and does not recall delivered bytes.

An optional `expected_preimage_sha256` recomputes the complete impact and rejects
changes with `revision_changed`. The read set binds Brain/issuer, session
membership, selected group/head state and barriers, source head/lifecycle/route
and effective privacy, applicable authorization/control coordinates, dependent
revisions/links, approvals and revocations. An unrelated capture does not stale
the snapshot merely by advancing the global retrieval generation. This digest
is an internal freshness check, never mutation authority. Body text and paths
are absent from returned objects and pages.

Freshness also binds the preserved newer heads and other sources supporting an
affected object. It follows forward dependencies, all page provenance members,
forward managed links and the relevant lifecycle group siblings. These objects
are evidence for revalidation, not additional captures selected for retraction.
Changing their lifecycle, routing or effective privacy requires a fresh impact.

This implementation scans retained managed revisions and replays journal
evidence. Indexed production-size performance acceptance remains open. Pending
approved deliveries with no copy identity have no `approved_copy` item; their
decision state is included in the internal preimage so later materialization
requires a fresh inspection. The future public preview contract must settle how
to display pending approvals before activation.

The frozen journal v1 retraction
payloads encode only the capture ID or session membership coordinates. They
cannot retain an approved impact set, preview binding or accepted counts. Those
use a versioned evidence/receipt contract and durable protected preview custody,
including expiry, paging revalidation and replay after preview expiry or restore.
Schema19/runtime14 now provides these through the owner CLI and supplemental
Portable14 approval evidence. See
`$REPO/.worktrees/competitive-gap-reconciliation/docs/owner-retraction.md`.
The inspection result alone still grants no mutation authority. Provider parsers
and consumer provenance admission remain inactive.

The contract checkpoint is
`$REPO/.worktrees/competitive-gap-reconciliation/docs/plans/2026-10-15-competitive-gaps-g1/PERSISTENCE.md`.
The implementation is
`$REPO/.worktrees/competitive-gap-reconciliation/packages/engine/src/open_brain_engine/engine/retraction_impact.py`.
Synthetic tests are in
`$REPO/.worktrees/competitive-gap-reconciliation/packages/app/tests/unit/engine/test_retraction_impact.py`.
