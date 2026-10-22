# W5 P3 brief: owner release and scoped setup

Repo root (worktree): `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/w5-guided-slack`
Branch: `feat/w5-guided-slack-workflow`. P1 and P2 are committed on it (`git log main..HEAD`). Never push, never switch branches, never touch another worktree.

## Sources of truth

1. Repo `CLAUDE.md`, `docs/engineering/gotchas/README.md`.
2. `docs/plans/2026-10-15-guided-slack-workflow-w5.md`: "Owner checkpoints" (Work routing and release, Scoped client handoff), "Identity and currentness contract", P3 row.
3. P0 packet `docs/plans/2026-10-15-guided-slack-workflow-w5-p0/`: `RECOVERY.md` (transition rows create-destination through setup-apply, "Scoped policy authoring gap", Journal v1), `INTERFACE.md`, `IDENTITY.md`, `ACCEPTANCE.md`; plus `REANCHOR-P1.md`. Old absolute paths map to the same relative path here. Current coordinates 21/16/16 must not change.
4. Existing services you must reuse, not reimplement: `InboxSpaceTasks.create_space`, `SourceTasks.route`, `SessionConsentStore.grant` / `DurableProviderConsentStore`, `SharingTasks.create_selection` / `preview` (Slack v2) / `decide`, launcher policy `parse_launcher_policy` / `validate_startup_policy`, `preview_agent_setup` / `apply_agent_setup` and the W2 scoped setup in `services/scoped_setup.py`.
5. The P2 guide code you extend: `services/source_guide*.py` (journal, flow, interactive, adapter).
6. P2 review findings and rulings: `REVIEW-P2.md` in this workstream directory, if present when you start; any item it assigns to P3 is in scope.

## Deliverable (P3 only)

Implement the remaining RECOVERY.md transitions in `advance` and the interactive `slack` flow: create-destination, route, consent, select, release-preview, approve, policy-preview, policy-apply, setup-preview, setup-apply, plus reconcile and cancel for these transitions. Rules that matter most:

- Each write has its own current intent and explicit owner confirmation; persist the exact canonical request in the journal before the authority call; replay/halt exactly as the table says. Policy-apply and setup-apply are HALT AND REPREVIEW after an unknown outcome; never reconstruct or replay them.
- No implicit consent or approval. No bulk approval: per-message full external projection inspection and approval, exact ordered unique selection members 1–24.
- The narrow create-only owner policy helper (new, app-local) builds the UNCHANGED launcher-policy.v1 document with the exact generated principal/session IDs, `capabilities=[search,content-read]`, one dedicated Work space, `allowed_read_tiers=[work]`, no capture tiers, one provider, current consent/authorization generation and owner context. Preview returns bytes+SHA ephemerally; apply regenerates from confirmed inputs, compares SHA, revalidates, then atomic create-only 0600 under a 0700 parent. Policy bytes never enter the journal. No overwrite, repair or widening; an existing policy is usable only if a fresh parse proves exact scope and consent plus owner confirmation.
- Setup uses existing preview/apply with search/content-read only, all other grants false, unrelated settings preserved, no client or model launch.
- Stale selection/policy/consent/config refuses; two-session races and exact retries preserve W4 scope/currentness; repair only the exact affected member.
- Show approved copy IDs, selection completeness, and the reusable recall task at the end; state that Slack coverage beyond selected approved messages is unknown.
- Halted-journal exit (carried from P2 fix `1a65720`): a halted journal currently refuses every action including `cancel` with `receipt_unresolved`. Check RECOVERY.md's cancel and halt rules and decide, with a test: either allow an explicit owner `cancel` from `halted` (journal-only, retains every receipt and the unresolved operation, makes no authority call, refuses any further advance), or keep refusing and make status/human output name the exact owner recovery path. Prefer the first if RECOVERY permits it. Apply the same refusal-is-not-non-commit rule to every P3 write.

## Tests (TDD, failing first)

ACCEPTANCE rows "Destination/consent/selection/provider scopes", "Scoped config preimage/current authority", and the P3 parts of "Crash/lost acknowledgement around EVERY write" and "Identity binding ... races". Extend `packages/app/tests/integration/services/test_source_guide.py` and `test_source_guide_recovery.py` (or add sibling files) using real owner authorities and synthetic client homes. Required: crash at each of the three boundaries for every P3 write; ambiguous policy/setup create halts; kill after first setup file write halts and repreviews; revoked consent refuses apply before config write; stale selection refuses; edited message requires new release preview; a full journey from `start` to `setup-apply` through `advance` with only the Slack HTTP boundary mocked and no client launch. Register new modules in architecture inventories.

## Write scope

App `services/source_guide*.py`, a new policy helper module under `packages/app/src/open_brain/services/`, `local_entrypoints.py` guide dispatch if needed, their tests, architecture inventory tests, and this workstream directory. Do not modify the existing sharing, routing, consent, launcher-policy, or agent-setup services' behavior or persisted formats; call them. Do not change existing `source`/`agent setup` CLI outputs.

STOP and report if P3 needs a new engine/Brain/Portable format, a changed launcher-policy format, changed consent/sharing receipts, stronger fencing, or an optional-collector import into the core.

## Verification

Touched/new test files first, then `uv run --frozen pytest -q -n 2 -p no:cacheprovider packages/collector packages/app/tests/unit packages/app/tests/integration tests/security`, `make lint`, `make typecheck`, `git diff --check main`. No native builds, no full `make verify`.

## Commit policy

Local commits at green checkpoints, conventional messages written to a file and committed with `git commit -F`, ending with:

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>

## Stop condition and output contract

Stop when P3 is implemented and focused verification is green, on a STOP condition, or after two identical failures. Final report (under 450 words): status, commits, files changed, exact commands and pass/fail counts, P0 deviations with reasons, P2-review items handled, open risks for P4.

## Sensitive data

Synthetic fixtures and synthetic client profiles only. No real Brain, Slack, provider keys, `~/.dev-secrets.env`, real client config, or private hostnames. Never call WebFetch. Search with `rg`.
