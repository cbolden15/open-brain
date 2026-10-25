# W5 P4 brief: deferred items, docs and regression readiness

Repo root (worktree): `$REPO/.worktrees/w5-guided-slack`
Branch: `feat/w5-guided-slack-workflow`. P1–P3 and their review fixes are committed (`git log main..HEAD`). Never push, never switch branches, never touch another worktree.

## Sources of truth

1. Repo `CLAUDE.md`, `docs/engineering/gotchas/README.md`.
2. `docs/plans/2026-10-15-guided-slack-workflow-w5.md` P4 row and section 8.
3. P0 packet `docs/plans/2026-10-15-guided-slack-workflow-w5-p0/` plus `REANCHOR-P1.md`, `REANCHOR-P2.md`, `REANCHOR-P3.md`.
4. Review records in this workstream directory: `REVIEW-P2.md` and `REVIEW-P3.md` ("Assigned to P4" sections are your code scope).

## Part A: deferred code items (TDD, failing first)

From REVIEW-P2:
1. Collector state identity so `collector_binding` detects a collector state swapped in at the same path (today only path strings are hashed). Use existing on-disk identity (for example device/inode of the state root or the existing `live/capture/brain.json` binding digest); no new collector persisted format.
2. Decide INTERFACE:76 (preserve native safe failure codes in private operation evidence) vs the closed RECOVERY evidence schema, as recorded open in REANCHOR-P2. Implement the smaller compliant option (bounded closed-enum native code field in journal evidence, or document why the closed schema wins) and record the decision in REANCHOR-P2.
3. Integration test that reaches the real 256-item custody quota refusal through the guide with real collector components (only Slack HTTP mocked), asserting `capacity_reached`, retained evidence and no deletion.

From REVIEW-P3:
4. Give the launcher policy reader used by `scoped_policy_authoring.py` a public name (keep the private alias for existing callers; no behavior change).
5. Define when a journal becomes `completed` (today it stays `active` after setup-apply) consistent with RECOVERY.md and later member repair; implement and test.
6. Cross-journal race test: two journals, same Brain and source, interleaved writes; assert scope/currentness preserved and no duplicate or widened release.
7. Optional, only if small: status shows recorded release progress, explicitly labelled as recorded journal progress (HEALTH.md), never as source health.

## Part B: documentation

- NEW operator guide `docs/integrations/slack-guided-workflow.md`: prerequisites (disposable Brain for synthetic use; real-Brain export/backup/restore checklist), the three commands with COMMON flags, each owner checkpoint, what each write does, recovery (halt, reconcile, cancel), macOS path alias rule, exact limits, and the standing statement that Slack coverage beyond selected approved messages is unknown. Plain prose, sentence-case headings.
- Update `docs/integrations/slack-capture-workflow.md`, `slack-decision-recall.md` and `core-sources.md` only where they must point to the guide; keep existing content.
- Update the W5 plan status line and the P0 `ACCEPTANCE.md` status line to "implemented on branch, pending final gates" with the branch head.
- Add a gotcha entry to `docs/engineering/gotchas/README.md` for the `tests/release/test_harness_acceptance.py` `source_changed_during_run` failure when any file in the checkout changes during `make verify` (observed in this workstream's baseline).
- Synthetic usability transcript `docs/plans/2026-10-15-guided-slack-workflow-w5-p0/USABILITY.md`: one end-to-end synthetic run of `advance` from `start` to `setup-apply`, captured from the real test harness output with all IDs synthetic.

## Write scope

Guide modules and their tests, collector guide paths, the engine read helper added in P3 only if a deferred item requires it, the docs listed above, architecture inventories, and this workstream directory. No persisted-format or coordinate change (21/16/16). STOP and report on any STOP condition from earlier briefs.

## Verification (yours)

Touched/new test files, then `uv run --frozen pytest -q -n 2 -p no:cacheprovider packages/engine packages/collector packages/app/tests/unit packages/app/tests/integration tests/security`, `make lint`, `make typecheck`, `git diff --check main`, `actionlint .github/workflows/ci.yml` if available. Do NOT run `make verify`, `native-audit` or `homebrew-smoke`; the coordinator runs them on a frozen tree after you finish. Commit everything before you report; leave the tree clean.

## Commit policy

Local commits at green checkpoints, conventional messages written to a file and committed with `git commit -F`, ending with:

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

## Output contract

Final report (under 450 words): status, commits, files changed, exact commands and pass/fail counts, decision taken for item 2 and item 5, which optional items were done, remaining limitations to state in the PR.

## Sensitive data

Synthetic fixtures only. No real Brain, Slack, provider keys, `~/.dev-secrets.env`, real client config, or private hostnames. Never call WebFetch. Search with `rg`.
