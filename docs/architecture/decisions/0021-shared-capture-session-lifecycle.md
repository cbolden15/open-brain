# Shared capture and session lifecycle

Date: 2026-10-15. Status: proposed for implementation review. No runtime change.

Consumer conversation imports can span several captures. Capture/session undo must remain effective after reimport and Portable restore. The current per-capture source intake and source-withdrawal receipts cannot represent complete conversation activation or trusted session membership on their own.

Use one engine-owned lifecycle for accepted grouped intake, trusted session membership, tombstones, exact operation replay and eligibility. Keep app parsers outside authority and derived embedding vectors outside authoritative persistence. Pending segments remain unavailable even to owner history until their complete conversation activation commits. Admitted retracted evidence remains retained history under existing authority.

Reserve schema18/runtime13/Portable13 against the current17/12/12 baseline, subject to rechecking the target catalog at implementation. Preserve all predecessor contracts and fence older readers before mutation. Concrete DDL/evidence schemas and migration fixtures must be reviewed together before any new state is admitted.

A source-withdrawal wrapper is smaller but cannot implement arbitrary capture/session undo. Separate importer/retraction ledgers duplicate authority and can disagree after recovery. The shared lifecycle is the selected design, with additional migration, read-path and Portable acceptance work.

Detailed decisions and remaining gates are in [the persistence contract]($REPO/.worktrees/competitive-gap-reconciliation/docs/plans/2026-10-15-competitive-gaps-g1/PERSISTENCE.md). This ADR does not authorize accessing private exports/Brain state, executing models or publishing the candidate.
