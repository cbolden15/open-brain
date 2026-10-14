# Codebase vs plan audit: public Slack capture

Date: 2026-10-14. Mode: standard, with execution probes for critical paths.
Plan: `docs/plans/2026-10-14-slack-capture-implementation.md`.
Commits reviewed: `eb3961e..daf3602`.

## Audit result

Completion at the reviewed commit: 21 of 32 public requirement groups complete
(65.6%); 10 partial and one documented deviation. Partial work is not counted as
complete.

Ship readiness: NOT READY at the reviewed commit. The successful nominal test
suite does not cover the reproduced stale-evidence, edited-source backlog and
missed-schedule sequences below. Earlier completion checkboxes overstate readiness.
The private pilot remains outside this public audit.

Three independent, read-only reviews covered engine evidence/portability,
collector processing/review, and discovery/scheduling. Each used synthetic
disposable state; no real accounts, private Brain, credentials or schedules were
accessed. The coordinator checked the candidate-name boundary independently.

## Requirement audit

The table groups related plan bullets into testable public requirements. Partial
requirements remain incomplete. The label extractor is a documented deviation;
the seven-day private pilot is excluded from public completion.

| Requirement | Status at daf3602 | Primary evidence |
| --- | --- | --- |
| R1 Installed/source version and CLI/MCP boundary inventory | COMPLETE | `docs/audits/2026-10-14-slack-capture-baseline.md:5` |
| R2 Reproduce privacy interface mismatch without widening MCP | COMPLETE | `docs/audits/2026-10-14-slack-capture-baseline.md:9` |
| R3 Versioned observations, developments, candidates and run receipts | COMPLETE | `capture_contracts.py`, `capture_review.py`, `capture_health.py` in the collector |
| R4 Privacy validation before capture/retrieval; unknown staging | COMPLETE | Collector `capture_policy.py:18-68`, `live_capture.py:750-751` |
| R5 Processing/exclusion checks before extraction and egress | COMPLETE | Collector `capture_processing.py:123-128`, `capture_workflow.py:255-261` |
| R6 Restricted automation, existing-facet assignment and owner decisions | COMPLETE | Collector `capture_workflow.py:25-40`, engine `review.py:1284-1300` |
| R7 Shared source identity across discovery and admission | COMPLETE | Connector `slack_live.py:257-297,481-484`, collector `capture_discovery.py:177-207` |
| R8 Ingestion/processing checkpoints and recoverable queue | PARTIAL | Collector `live_capture.py:844-878`; F3 blocks later observations |
| R9 Identical observation dedup before extraction | COMPLETE | Collector `capture_processing.py:86-94,119-122` |
| R10 Changed material/context and immutable source revisions | PARTIAL | Collector `capture_processing.py:147-179`; F4 loses current evidence |
| R11 Visible empty/excluded/failed/partial/unsupported coverage | PARTIAL | Collector `capture_health.py:108-158`; F8 drops coverage codes |
| R12 Edit/deletion handling and incomplete-scan absence safety | COMPLETE | Connector `slack_live.py:299-327`; collector revision acceptance tests |
| R13 Generic development fields and distinct semantic kinds | COMPLETE | Collector `capture_contracts.py`, `capture_processing.py:37-52` |
| R14 Source-linked development relationships and closure | PARTIAL | Engine `developments.py:79-99`; F1 bypasses an ancestor |
| R15 One supported development per derived capture | COMPLETE for labeled statements | Collector `capture_workflow.py:181-220` |
| R16 Conservative cross-post handling and broader extraction | DEVIATED | Identical source revisions deduplicate; cross-post claims are not automatically merged. Default extraction handles labels only |
| R17 Separate attention with defer/resurface/dismiss and terminal preservation | COMPLETE | Collector `capture_review.py:309-343` |
| R18 Bounded proposals for explicit target page/revision | COMPLETE | Collector `capture_workflow.py:289-304`, engine append reservations |
| R19 Approval revalidation of current/transitive evidence and page | PARTIAL | Engine `review.py:953-981`; F1 and F2 bypass supporting evidence |
| R20 Persistent candidates outside authoritative vocabulary | COMPLETE | Collector `capture_review.py:97-140` |
| R21 Owner map/create/reject/defer with decision-before-backfill | COMPLETE | Collector `capture_review.py:159-248`, `capture_candidates.py:133-170` |
| R22 Candidate ranking and compatible input boundary | PARTIAL | Commitment priority exists at `capture_workflow.py:271`; F5 exceeds the name limit. Retrieval-need ranking is not implemented |
| R23 Opt-in owner/mention/DM authorization and source admission | COMPLETE | Connector `slack_auth.py:32-54`, `slack_discovery.py:63-71` |
| R24 Current exclusions and revocation fencing | COMPLETE | Collector `capture_discovery.py:212-238`, `live_manager.py:593-620` |
| R25 Permitted DM end-to-end acceptance | PARTIAL | Connector request tests exist; actual discovery-to-development sequence is not covered |
| R26 Headless commands, validated config and one shared lease | COMPLETE | Collector `runner.py:102-124`, `sources_cli.py:99-144` |
| R27 Bounded retries and catch-up after missed runs | PARTIAL | Collector `capture_discovery.py:168-173`, `runner.py:103-110`; F6/F7 |
| R28 Inactive supervisor templates | COMPLETE | `docs/integrations/examples/slack-capture.*` |
| R29 Operation health and coverage metrics | PARTIAL | Collector `capture_health.py:92-165`, `live_manager.py:482-509`; F8 |
| R30 Schema migration, runtime floor and historical formats | COMPLETE by inspection | Engine `development_schema.py:69-108`, `local_schema.py:157-188` |
| R31 Portable evidence preservation and replay integrity | PARTIAL | Engine `portable_v11_evidence.py:23-47`, `portable_v11_restore.py:23-47`; F2 |
| R32 Collector backup, migration and rollback instructions | COMPLETE | `docs/portable-brain-v11.md`, public operator guide |

Collector paths are under `packages/collector/src/open_brain_collector/`; engine
paths are under `packages/engine/src/open_brain_engine/engine/`; connector paths are
under `packages/connectors/src/open_brain_connectors/runtime/`.

## Critical gaps and recommended fixes

1. **F1, P1: Derived supporting captures bypass transitive validation.** Bind A to
   raw R, bind B with `supporting_capture_ids=(A,)`, propose B, then correct R.
   Approval succeeds. `engine/developments.py:99` traverses only the separate
   `supports_development_ids` list. Traverse all bound supporting captures, with
   consistent cycle/dependency limits in live and Portable validation.
2. **F2, P1: Portable evidence can be removed without invalidating owner review.**
   After correcting raw evidence for a pending development, replace
   `history/developments/evidence-v1.json` operations with an empty list and recompute
   manifest hashes. Full validation, import and approval with the unchanged review
   digest succeed. `portable/v11.py:78-99` validates sidecars independently;
   `engine/developments.py:79-84` treats a missing receipt as ordinary content.
   Commit receipt identities/hashes and their closure into the review binding and
   digest. Validate and restore those commitments; absence must fail closed.
3. **F3, P1: An edit before processing permanently blocks the queue.** Capture r1
   and changed r2, then process. Both retries raise `T03Error revision_changed`;
   both observations stay pending. Collector `capture_processing.py:164-169` and
   `capture_workflow.py:208-220` attempt stale evidence and stop unrelated work.
   Reconcile current source revisions before derived acceptance, retain explicit
   superseded/conflict dispositions and isolate independent observations.
4. **F4, P2: Same claim on newer evidence has no approvable review path.** Process
   `Decision: Keep local` in r1, then r2 retaining that line with new context.
   Processing reports one processed, zero captures; the only proposal permanently
   conflicts. `capture_processing.py:147-179` removes evidence IDs from identity and
   retains the original operation. Separate semantic identity from evidence
   revisions and produce a current revision/replacement with historical continuity.
5. **F5, P2: Candidate-name limits stall supported development inputs.** A valid
   513-character labeled claim becomes a subject name; extractor limit is 4,000 at
   `capture_processing.py:41-48`, candidate limit is 512 at `capture_review.py:25-27`.
   `_postprocess` passes it through at `capture_workflow.py:263-271`. Preserve the
   development, skip/record unsuitable candidates visibly and continue unrelated
   work instead of retrying an impossible candidate forever.
6. **F6, P1: Optional discovery loses the gap after long downtime.** Finish a scan,
   resume seven days later: `capture_discovery.py:168-173` sets oldest to four days
   after the last completion. Retain successful coverage and use its overlap when
   catching up. Freeze intervals across pagination and advance only after completion.
7. **F7, P1: Daily one-shot scheduling does not catch up.** Actual `run_once()` with
   three pages processes one and exits completed, leaving two due one second later.
   The timer runs again the next day. `runner.py:103-110`, `live_capture.py:875-876`
   and `slack_live.py:207` make ordinary backlogs accumulate. Provide bounded drain
   under the same lease, honor deadlines and report partial/deferred exhaustion.
8. **F8, P2: Health hides incomplete/unsupported coverage.** A completed final batch
   with `partial_thread_coverage` and `incomplete_search_coverage` is persisted as
   completed with neither notice. `capture_health.py:108-158` ignores notices.
   Retain bounded public coverage codes and distinguish execution completion from
   source coverage in health.

## Integration and quality concerns

The public guide says discovery returns source IDs, but coordinator
`capture_discovery.py:285-291` returns counts/continuation/notices only. Return the
IDs or direct operators to source status. Add a synthetic permitted-DM path through
actual discovery, owner classification, admission and development processing.

The default label extractor and missing private pilot are already disclosed.
Conservative extraction is not general conversational recall; no live precision,
recall, cost or provider coverage claim is supported. Relevance/ranking beyond
commitment priority remains a private extension rather than a verified feature.

## Validation and uncertainty

The engine reviewer reproduced F1 and F2 against actual engines and full Portable
validation/import. The collector reviewer reproduced F3/F4 against actual live
admission and processing. The discovery reviewer reproduced F6/F7/F8 using the
actual coordinator, runner/live scheduler and health store with synthetic source
callbacks. F5 is established independently by the exact incompatible boundaries.
Scratch paths were resolved to avoid macOS no-follow refusal of `/var` symlinks.

No full suite or native build was repeated for this read-only audit. The prior
7,142 passing Python tests and client/native gates remain nominal evidence, not
refutations of the reproduced findings. Group-DM provider behavior and real search
recall were not verified. All findings above concern public behavior, not the
excluded private pilot.

## Repair disposition

The owner selected fixing blockers before opening a draft PR. This report records
the read-only audit at `daf3602`; subsequent fixes and exact verification will be
recorded separately. No branch was pushed and no PR was opened during this audit.

### Regression repairs

The subsequent repair uses immutable review binding 6, supported development
source revisions, retained item-level queue outcomes, frozen discovery coverage
intervals, bounded scheduled catch-up and explicit public health coverage codes.
Focused tests cover all eight findings; repository-wide and native gates are
recorded below.

| Finding | Repair and primary regression |
| --- | --- |
| F1 | Traverse every capture/development relationship and reject cycles; `test_supporting_capture_traverses_derived_ancestor`, `test_supporting_capture_cycle_is_rejected_atomically` |
| F2 | Commit receipt rows and absent leaves in the owner digest; reject sidecar removal/substitution on full validation/restore; `test_portable_cannot_change_confirmed_development_closure` |
| F3 | Preflight current evidence, retain superseded/recovery dispositions, isolate item failures; `test_superseded_pending_evidence_does_not_write_or_block_independent_work` |
| F4 | Separate semantic identity from evidence operations, retain current and historical development revisions; `test_same_claim_new_evidence_has_one_current_development_and_approvable_proposal` |
| F5 | Preserve development and record `candidate_name_too_long`; `test_long_candidate_name_retains_development_and_does_not_block_next_item` |
| F6 | Retain completed watermark and freeze pagination bounds; `test_completed_coverage_catches_up_after_seven_days_and_freezes_pages` |
| F7 | Drain actual live pages/retry deadlines under one lease with explicit deferred bounds; `test_headless_drains_actual_live_capture_with_retry_and_no_duplicate_writes` |
| F8 | Retain allowlisted coverage codes separately from execution outcome; `test_health_preserves_public_coverage_separately_from_execution` |

Completed discovery also returns source IDs. A new synthetic permitted-DM test
uses actual discovery, protected staging, owner privacy classification and
development acceptance. This closes the missing public integration case without
claiming real-provider or group-DM acceptance.

### Independent integration review

Two reviewers checked the combined repairs and reproduced three additional
boundary defects before the full gate:

- F9, P1: late binding to an absent receipt leaf frozen in binding 6 invalidated
  export. Binding admission must cover every frozen closure capture, including
  null receipt leaves, while preserving exact existing replay.
- F10, P2: scheduled catch-up reset the processing budget on every pass. A queue
  configured for one call performed three. Budget exhaustion must defer the whole
  activation before another extraction pass. Repeated pages smaller than the
  budget have the same defect; drain ingestion separately and process once per
  activation.
- F11, P2: final health used only the last pagination pass and lost an earlier
  source's known partial coverage. The final activation receipt must retain every
  source's latest coverage and cumulative capture/known usage totals.

All three were repaired before the full gate. The absent-leaf regression covers
pending and approved confirmations; the runner regressions cover repeated-page
budget reuse, deferred work, every source's retained coverage and mixed
known/unknown usage. Independent source review also corrected the earlier
reject/recreate recovery advice: previous unreleased schema-16 development
histories are unsupported upgrade/export sources, including terminal proposals.
No automatic conversion is provided. Schema-15 migration and Portable-10 import
remain covered by the supported compatibility tests.

### Final verification

The combined code candidate was frozen before verification. `make verify` exited
0: Ruff, strict mypy (644 files), package builds, 7,179 Python tests, 673 plugin
tests, 259 desktop UI tests, 30 desktop Python tests and 26 Rust tests passed.
Five Python cases were skipped because this macOS filesystem refuses non-UTF-8
names or collapses distinct NFC/NFD names. One pre-existing fork deprecation
warning remains; no product deadline or admission check was relaxed.

`make native-audit homebrew-smoke` exited 0 on macOS arm64. The required native
module inventory includes `portable.development_binding`. The guarded Homebrew
journey passed capture, search, verified export, Markdown import, local MCP,
graph projection and Obsidian operations; the existing product was preserved.

An additional synthetic probe used the frozen executable for verified binding-6
export, full restore, approval with the original owner review token and verified
re-export. It passed. The first probe attempt searched only the top-level binding
directory and raised `StopIteration`; correcting the probe to traverse the dated
subdirectories resolved that tooling error without changing product code.

Public readiness after repairs: ready for draft review. Thirty of 32 public
requirement groups are complete; R22 remains partial and R16 remains a disclosed
deviation. All eleven reproduced audit/integration findings are resolved by
regressions. This does not establish private pilot quality or live group-DM
behavior.

### Remaining public limitations

R16 remains a disclosed deviation: the default local extractor requires labels
and does not merge conversational cross-post claims. R22 remains partial for
retrieval-need ranking beyond commitment priority. General extraction and private
pilot quality are not established by synthetic tests. The public implementation
does not promise exhaustive Slack search or discovery of untracked old roots.

Capture acceptance and evidence binding remain separate engine transactions.
Already stale evidence is screened before capture; a correction after uncertain
acceptance can require explicit owner recovery, retained as
`processing_evidence_changed_pending_recovery`. This limit is documented and tested;
the queue never reports that operation complete or silently evicts it.
