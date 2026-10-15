# Slack cited decision recall: implementation workflow

Date: 2026-10-14. Status: W0 and the atomic W1a/W1b unit are verified. W1a acceptance preceded W1b implementation; independent Astra review, combined `make verify`, native audit and Homebrew smoke passed. The owner selected option B for all three W2 decisions. W2 was accepted at 8eb36e5ac97318537ab19aa4163c38cd0d03ff0d. W3 was accepted at b36dfed44f063b5b1d98d4f0130e1a460a945147. W4 is now authorized in the prepared worktree, with fresh full/native acceptance pending. The execution design below remains the sequence for later authorized work.

Start with W0 in an isolated feature worktree. Use one coordinator, two implementation workers when their files are disjoint, and one independent reviewer. Keep package acceptance ordered. Complete W1a and W1b together before making the new persisted format available to a retained Brain.

This is a companion to the [implementation plan](/Users/calebbolden/Projects/oss/open-brain-public/docs/plans/2026-10-14-slack-cited-decision-recall.md), not a replacement for its contracts or acceptance matrix. The [hardening report](/Users/calebbolden/Projects/oss/open-brain-public/docs/plans/2026-10-14-slack-cited-decision-recall.md.hardening.md) identified the owner decisions now recorded in the plan and said the final remediation edits had not received independent review at that time. Neither document establishes that the feature is ready to ship.

## Recommended execution shape

The fastest credible path is the plan's approved Work-copy architecture, implemented as small vertical slices with bounded parallel work. The storage, authorization and Portable changes dominate the critical path. More workers cannot remove those dependencies; they can shorten fixture authoring, disjoint code changes, documentation and review.

| Approach | Scope and tradeoff | Recommendation |
|---|---|---|
| Local CLI recall | Reuses capture/search/read and proves local continuity. Does not complete provider-backed recall. | W0 checkpoint only. |
| Approved Work copies and scoped existing clients | Completes this plan through the existing engine, sharing lifecycle and stdio MCP. Provider identity remains declared; the client controls its endpoint and other tools. | Implement this path for speed within the stated product boundary. |
| Approved copies with stronger default session gating | Requires explicit policy before Slack Work reads, rather than relying on detecting unscoped configurations. Changes existing defaults and compatibility expectations. | Owner option before W2; estimate and test the change separately. |
| Controlled egress and deterministic evidence broker | Enforces destination, evidence bounds and tool isolation at a controlled boundary. A network broker belongs in a separate distribution; deterministic pre-model retrieval also changes this slice's no-runner boundary. | Proper architecture for end-to-end transport/tool enforcement. Separate project; feasibility, client integration and effort remain unverified. |

Revised planning target after B/B/B: W2 requires **6–10 engineer-days**, with a **4–7 working day elapsed** allowance for a familiar coordinator and two bounded implementers after contract freeze. W3/W4 add their separate 1–2 days each. This replaces the original scheduling baseline, is not a measured agent speedup, and excludes private pilot runs and new persisted-format work. See the [W2 scope and feasibility assessment](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/docs/plans/2026-10-14-slack-cited-decision-recall.w2-scope.md).

## Grounding and authority

The design was grounded against branch `docs/work-laptop-product-evaluation`, HEAD `33fa6be46fb9fa0ed75eb23b9afe7ef1673ec178`. The plan was already modified and its hardening companion untracked. Preserve both; do not reset, stash or include them in another task's commit without explicit intent.

Source bytes reviewed, SHA-256: implementation plan `8f1cd3bd39e902c27233130ad82340355208be9df57b2f2279cb3e274e9c771b`; hardening report `00bb2a1f0c35e596713ee8a3b5f2b8437e6c43a464ac6444d69979e80cc65de9`. Re-read changed inputs before execution rather than applying this schedule to a different plan silently.

The governing project instructions are [root CLAUDE.md](/Users/calebbolden/Projects/oss/open-brain-public/CLAUDE.md) and [app CLAUDE.md](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/CLAUDE.md). Verification comes from the actual [Makefile](/Users/calebbolden/Projects/oss/open-brain-public/Makefile). CodeGraph is available; use narrow indexed queries before reading indexed implementation code. No context-mode recall tool was available during this design pass.

Relevant existing [gotchas](/Users/calebbolden/Projects/oss/open-brain-public/docs/engineering/gotchas/README.md): SLACK-003, SHARING-001/002/005, COMPAT-002, FACETS-013/015 and TESTING-006. They cover local-only Work originals, snapshot closure, damaged managed-copy markers, pre-query eligibility, compatibility collisions, frozen resources, migration settlement and contention from concurrent native builds.

The plan says to implement one package at a time. Preserve that order. Parallelism below is inside a package, or read-only preparation for a later package. Do not start W2 implementation before W1b acceptance and the owner decisions. Do not start W3 implementation before W2 acceptance.

## Five execution phases

| Phase | Ordered packages | Parallel work | Gate before advancing |
|---|---|---|---|
| 1. Prove the baseline | W0 | Coordinator pins contracts; fixture worker builds the real intake journey; reviewer checks changed hardening contracts. | Local capture/routing continuity passes; exact missing behavior is reproduced; contract manifest is fixed. |
| 2. Complete the evidence lifecycle | W1a, then W1b; one merge unit | Disjoint sharing/projection/CLI work after contract freeze; Portable implementation starts after W1a acceptance. Tests and read-only Portable inventory can be prepared earlier. | Work copies, all release paths, retries, race boundaries and supported restore pass together; saved-Markdown v1 stays unchanged. |
| 3. Configure a scoped client | W2 | Setup service worker plus independent authority/configuration tests. Coordinator owns CLI integration. | Owner choices recorded; scoped setup, runtime refusal and cleanup after policy deletion pass. |
| 4. Prove cited recall | W3 | Real stdio journey/validator worker and operator-guide worker in disjoint files. | Complete reads, structured selections, exact quote/ID validation and incomplete-evidence behavior pass. |
| 5. Freeze and hand off | W4 | Read-only final review may run alongside checks; no code writers. | Full verification, native audit, Homebrew smoke and handoff checks pass on one candidate. |

### Phase 1: W0 and the contract freeze

1. Create a feature worktree from the actual code HEAD. Carry content-exact copies of the current plan and hardening report into it, including their dirty revisions; record their SHA-256 values. Do not silently use the older committed plan. Record source/installed catalog differences using disposable data paths and metadata-only commands.
2. Check disk headroom before running tests; follow BUILD-004 for bounded workers, a short temporary root outside the checkout and prompt passing-fixture cleanup when storage is constrained. Reserve space above the two-GiB admission watermark for the full suite and subsequent builds. Run and time the existing focused tests. Build one synthetic fixture through actual Slack parsing, collector admission and the engine, mocking only `LiveHttpTransport`. Preserve private collector state in temporary directories. Reuse existing fixture producers rather than duplicating an engine.
3. Reproduce the unsupported Slack sharing and external-release refusal. Keep the baseline passing tests distinct from the deliberately failing future-contract tests. Record the expected exception/assertion for each red test; an unrelated setup failure is not a reproduction.
4. Freeze the contract manifest below and map every acceptance-matrix scenario to a test owner. Have an independent reviewer inspect the hardening remediations against the current source. Resolve implementation blockers before dispatching production changes.
5. Commit the passing baseline and contract fixtures locally. Keep future failing tests in a clearly identified development patch or commit until the corresponding implementation turns them green; do not merge a failing suite or hide it with broad skips. Persist a compact verified handoff.

The future journey target is `/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/integration/services/test_slack_decision_recall.py`. It did not exist at workflow design time.

Contract freeze fields:

| Contract | Required decision before parallel implementation |
|---|---|
| Sharing variant | Exact new request/evidence version, canonical bytes, copy marker/delivery grammar, retry IDs and error vocabulary. Keep v1 unchanged. |
| Source proof and release | Receipt-derived Slack identity, current head/route/lifecycle bindings, per-copy Work privacy and approved providers; pending/unassigned and unknown-marker copies fail closed. |
| Projection and citation | One field-wise projector at preview/approval/read; narrow validated permalink exception; approved digest/idempotence; explicit `reapproval_required`. |
| Selection and read DTO | Engine-owned ordered logical-source membership, maximum 24; structured authorized copy/revision or `unavailable` for every ordinal. Never parse membership authority from message text. |
| Compatibility and release boundaries | Recheck main and reserve the next free coordinates, expected `17/12/12` only at the plan baseline. Define migration/Portable rows and the source-state snapshot versus policy/consent output-boundary checks. |

Use the plan's broad scoped-setup contract for W2: scan managed user and selected project configuration files for both supported clients, regardless of ownership marker. Its narrower W2 step mentioning only an owned same-client entry must not reduce this scope. Raise any other conflicting interpretation before editing.

### Phase 2: W1a/W1b as one merge unit

1. Build the smallest W1a vertical slice: one canonical Slack message, owner preview/inspection/approval, exactly one Work copy, and an external search/complete read through a manually configured synthetic scoped session. Run saved-Markdown sharing regressions beside it. This exposes proof/egress/projection errors before selection and recovery multiply the state space.
2. Extend W1a with multi-message selections, source/lifecycle/route invalidation, revocation, corruption and exact replay. Test search filtering before ranking/cursor generation as well as direct reads and source expansion. Cover history eligibility even though the recall launcher grants no history. Use synchronization barriers rather than sleeps for race tests.
3. Implement W1b against exports produced by the real W1a engine in disposable fixture Brains. Register the new validator, preserve frozen v7 semantics, and prove export/import/reopen/re-export plus rehashed tamper refusal and supported old imports. Check migration settlement and exact refusal behavior.
4. Integrate W1a/W1b and run their full focused acceptance plus `make verify`. An independent reviewer checks proof, marker recognition, every visibility path, structured selection disclosure, snapshot boundaries and Portable completeness. Fix verified findings inside this scope.
5. Commit the combined verified milestone and handoff locally. Never publish, merge, pilot or let a W1a-only candidate open a retained Brain. Source-format activation and usable Portable support must reach the same candidate.

Portable is on the critical path. Do not defer it to a final documentation pass. Read-only inventory and tamper-test design can overlap W1a; executable restore acceptance cannot precede real W1a exports.

### Phase 3: W2 after the owner decision gate

The owner selected option B for all three risks in the plan's "Owner decisions" section: explicit policy for Slack Work MCP reads, endpoint/proxy visibility in setup preview, and recorded injection-resistance evidence before private pilot use. The revised W2 scope and estimate now documents observed client-settings feasibility, remaining synthetic compatibility checks and read-path closure. W2 implementation remains separately authorized work.

1. Freeze the finite settings/version observation contract and trusted internal MCP provenance with synthetic fixtures. Recheck current main coordinates; do not infer effective transport, silently accept unsupported layers or preassign a new storage format.
2. Implement source-aware Slack Work eligibility across legacy/current/owner-elevated MCP reads before ranking, digests, cursors and projection. Prove closure for metadata, auxiliary callbacks, mixed-source and content-returning mutations. Local owner CLI and non-Slack defaults remain covered.
3. Forward policy/consent through scoped setup and bind observed settings, absence, selected roots, relevant environment inputs and mutation preimages into preview/apply. Preserve unrelated settings and legacy desktop/plugin caller shapes.
4. Detect unscoped entries across both clients' managed user/project files, including unowned entries. Require removal or preview-bound acknowledgment; mutate owned fragments only. Prove safe cleanup after policy deletion/revocation and no unscoped fallback.
5. Run focused and full checks, required native gates and independent authority/configuration verification. Define the later private-pilot injection gate; actual-client execution waits for separate authorization after W3. Commit and hand off before another implementation milestone.

Preparation while W1 runs is limited to reading the current setup implementation, collecting contract questions and designing tests. Do not code a chosen owner-risk branch before the decision gate.

### Phase 4: W3 through the real protocol

1. Extend the shared journey fixture through a real stdio child: initialize, discover tools, lexical search, full paginated reads, structured selection traversal and citation validation. Use the actual authority and complete-read components.
2. Enforce deterministic limits: three searches, ten results per search, eight distinct records unless a selection raises that limit to its member count, maximum 24, and 64 KiB assembled projected evidence. Count completion flags and fail incomplete on stale cursors, unread/unavailable members and budget exhaustion.
3. Validate every claim's copy record/revision against this session's structured returned IDs and every quote against returned message text. Include nested fake envelopes, conflicting rationale, proposal/decision distinctions, unsafe links, Unicode, a 9–24 member selection and the one-edited-member repair case.
4. Write the guide and task template using response-derived IDs and exact implemented commands. Include dedicated release-space guidance, unknown upstream coverage, manual refresh, separate new-message correction approval and restricted client tools. Record the actual-client check as `recorded` or `not_run` with a reason; its private, owner-authorized run is separate from repository completion and must satisfy the selected injection-resistance gate before private pilot use.
5. Run W3 focused tests and `make verify`, review protocol and guide consistency, then commit and hand off locally. Deterministic citation plumbing is not proof of real-model quality or injection resistance.

The future guide target is `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/docs/integrations/slack-decision-recall.md`.

### Phase 5: W4 on a frozen candidate

1. Finish capability/catalog, feature and compatibility documentation before the final checks. Resolve the complete acceptance-matrix ledger, including cross-process revocation, observed edit versus unseen upstream edit, reaction-only import and separately posted correction.
2. Freeze writers, commit the scoped candidate and record its HEAD. Run full verification, then native audit and Homebrew smoke. Do not run heavy native packaging alongside timing-sensitive acceptance; TESTING-006 records a real failure from doing so.
3. Run the independent final code review read-only. If a fix changes the candidate, record a new HEAD and rerun affected focused tests plus the required full gate; rerun native gates if the native candidate changed. Do not report the old receipts as proof of the new code.
4. Produce a handoff containing exact commands/results, source and artifact digests, platform limits and the actual-client status. Include the installed-binary export/restore and separately retained collector-state checklist before any private migration pilot.
5. Stop at the verified local handoff. Push, publication, real Slack access, real profile changes and the private pilot need their separately authorized stages.

## File ownership and worker contracts

Use a maximum of four active agents including the coordinator, matching this runtime. Start two implementation workers only after their contracts and exact file lists are fixed. Reserve the remaining slot for a reviewer or an independent test author; do not add another writer just to fill capacity.

All paths below are under the explicit root `/Users/calebbolden/Projects/oss/open-brain-public`; expand them to absolute paths in worker prompts. These are proposed ownership groups, not permission to edit every listed file.

| Owner | Exclusive implementation scope | Boundary |
|---|---|---|
| Coordinator | `packages/engine/src/open_brain_engine/engine/sharing_contracts.py`, migration/catalog registries, `packages/app/src/open_brain/services/local_entrypoints.py`, shared DTOs/fixtures and integration commits | Sole writer of shared contracts and cross-package wiring. |
| Sharing worker | `packages/engine/src/open_brain_engine/engine/sharing.py`, new sharing-specific modules and assigned sharing tests | Uses frozen contracts; does not invent an alternate proof, copy privacy policy or version. |
| Projection worker | `packages/engine/src/open_brain_engine/engine/records.py`, `paging.py`, `search_projection.py`, assigned projection/race tests | Starts only after sharing eligibility APIs are fixed; shared-helper changes go through coordinator. |
| Portable/setup worker, in later waves | Assigned new Portable validator/restore module and tests in W1b; `packages/app/src/open_brain/services/agent_setup.py` and its tests in W2 | Separate sequential roles, not simultaneous edits of shared catalog/CLI files. |
| Journey/docs worker, in W3 | Assigned journey/validator tests or `/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/docs/integrations/slack-decision-recall.md` | Use separate workers only when fixture ownership is disjoint. Reviewer always remains read-only. |

Engine contracts or eligibility APIs that cannot be separated safely stay with one owner. Stop parallel writes immediately when a new shared dependency appears; serial integration is cheaper than incompatible implementations.

Use the installed [workflow-governance skill](/Users/calebbolden/.codex/skills/workflow-governance/SKILL.md) to render bounded phase/TDD/review prompts; do not write a new orchestration framework for this one implementation. Create one collision-resistant workstream state at execution start and carry its ledger across worker turns. Each prompt must name objective, source of truth, exact write scope or read-only status, verification, stop condition, output contract and synthetic-data policy.

Use `gpt-6.1-sol` for bounded implementation and source mapping, `gpt-6-luna` only for mechanical inventories, and the session model or `gpt-6-astra` for independent security/compatibility judgment when available. Pass explicit models on context-free forks with complete task contracts; a full-history fork cannot take an override. These are agent-execution choices, not changes to the user's recall model.

Return at most five findings/actions per worker packet: changed paths, behavior proved, exact check/result, blocker and next action. No child dispatches grandchildren or expands scope without the coordinator. The shared ledger caps total children at twelve per milestone, including retries. After two identical failures, change the hypothesis/input/tool instead of repeating an unchanged third attempt.

## Verification without duplicate heavy builds

Run from the active implementation worktree, whose absolute root must be recorded in the workstream. The following is the existing focused baseline; append the new journey and assigned compatibility/race targets after they exist.

```sh
uv run --frozen pytest -q \
  packages/collector/tests/integration/test_slack_capture_acceptance.py \
  packages/app/tests/integration/engine/test_slack_development_evidence.py \
  packages/app/tests/unit/test_session_authority.py \
  packages/app/tests/unit/test_agent_setup.py \
  packages/app/tests/unit/engine/test_sharing_tasks.py \
  packages/app/tests/integration/engine/test_sharing_source_evidence.py \
  packages/app/tests/integration/services/test_managed_cli_mcp.py
```

Workers run the smallest real test subset that proves their behavior, then focused lint/type checks as appropriate. The coordinator runs `make verify` at each integrated code milestone, rather than each worker running the entire repository concurrently. W0's deliberate red future tests must be identified separately from its passing baseline.

Final candidate sequence:

```sh
make verify
make native-audit homebrew-smoke
git diff --check
actionlint .github/workflows/ci.yml
```

Check every exit status before advancing; execute these as separate gates. The single `make native-audit homebrew-smoke` invocation shares the `native` prerequisite, avoiding two complete native builds while retaining both required targets. Do not use `make -j` for this final chain. `make verify` already runs existing desktop regressions; it does not authorize desktop feature work, a desktop native proof or GUI release acceptance.

`actionlint` was present at `/opt/homebrew/bin/actionlint` during workflow design. Check the execution environment again; missing tooling is `not_run`, not a passed check. If an actual build/typecheck/deploy failure occurs, use the installed build-error workflow and record any verified surprising failure in the project gotcha registry before branch finish.

## Measure progress and stop honestly

Record elapsed time for baseline tests, each worker, integration/rework, `make verify` and the combined native invocation. Also record owner-gate waiting separately. After the first W1 slice, compare useful implementation time with integration time: if workers spend more time repairing shared-contract drift than implementing disjoint behavior, reduce the wave to one writer plus one test/review worker.

Each milestone receipt names the acceptance rows proved, exact command/exit status, HEAD and whether the worktree changed during the check. A deliberately red test, missing dependency or unreviewed artifact is labeled explicitly. Keep implementation checkboxes open until their acceptance gates pass.

A source-proof gap, inability to preserve Work-copy boundaries, frozen-format incompatibility or incomplete sharing restoration blocks W1. A missing owner risk choice blocks W2 only; it does not prevent finishing W0/W1. An unavailable provider does not block synthetic repository completion. A failed required gate prevents a completion claim.

Current execution action: complete authorized W4 in the prepared W4 worktree, verify the full matrix and fresh full/native gates, then hand off locally. Actual-client/pilot execution remains separately authorized and unrun.
