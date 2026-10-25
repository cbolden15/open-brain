# Ledger: daily brief execution

Milestone: execute `docs/superpowers/plans/2026-10-24-daily-brief.md` (17 tasks) to PR-ready on `feat/daily-brief`, no push. Base `f21dfb9`.
Caps: 6 active, 12 total. New milestone ledger; the planning milestone (3 Fable authors) is counted in `LEDGER.md`.
Planned: one task-tdd-implementer and one task-spec-compliance-reviewer per package (6); 6 reserved for remediation and the final whole-branch review. Review fixes go back to the same implementer (resumed, not a new child).

| # | Child | Package | Model | Status |
|---|---|---|---|---|
| 1 | task-tdd-implementer | P1 Tasks 1–5 | opus | DONE_WITH_CONCERNS; f85e7f9..f02d6d7 (5 commits); 36 package tests, broad suite 5838 passed; deviation: `sharing_eligible` applied to available heads only so unavailable captures are counted; ruff/mypy fixes to plan test code; commits carry the Opus trailer |
| 2 | task-spec-compliance-reviewer | P1 | opus | APPROVED_WITH_FIXES; spec met; Important I1 (unavailable heads skipped the historical fence and sharing approval/revocation checks for non-owners) sent to child 1 (resumed); fixed in 7ecdb5f (2 new tests, 228 covering tests pass); re-review RESOLVED. P1 complete at 7ecdb5f |
| 3 | task-tdd-implementer | P2 Tasks 6–13 | opus | DONE_WITH_CONCERNS; a51f63f..0e5032e (8 commits); unit+security 3131 passed, package files 382 passed; concerns: `brief_ack` adds `brief-read` for ack-only sessions, family-ref bound widened to 1–2200 non-control chars, Task 10–13 commits rebuilt to drop unrelated ruff reformatting, Opus trailer |
| 4 | task-spec-compliance-reviewer | P2 | opus | APPROVED_WITH_FIXES; I1 due items starved past the 500 scan; I2 ack-only sessions (capability lift, unreadable key, fingerprint leak); I3 client space IDs evicting owner key files; sent to child 3 (resumed); fixed in e5c0f35 (370 covering tests pass; decided scan keeps a documented 500-row bound, no ListFilter field for new evidence); re-review: I2, I3, Minor 1 RESOLVED; I1 due part RESOLVED, decided part NOT_RESOLVED (500 oldest decided rows starve new evidence). Second wave sent to child 3: engine `ListFilter.new_evidence` filter + decided scan uses it; owner key files exempt from the 16-key eviction (residual I3 gap with 5+ real spaces); fixed in ab0159b and 667577f (554 covering tests pass; owner marker follows `authority.owner`); re-review: all RESOLVED (no MCP session reaches the brief with `owner=True`; launcher-policy authority hard-codes `owner=False`; no wire, schema or Portable change). P2 APPROVED, complete at 667577f |
| 5 | task-tdd-implementer | P3 Tasks 15–18 | opus | DONE; `502db0f..a9ace6c` plus the review-fix docs commit; 8 acceptance tests; audit object budget raised to 600,000 (coordinator decision, f3ea292); gate green at `f3ea292`; review APPROVED_WITH_FIXES, I1 (ack scope sentence in `docs/daily-brief.md`) and three doc minors fixed in the review-fix commit |
| 6 | task-spec-compliance-reviewer | P3 | opus | APPROVED_WITH_FIXES; one Important (guide said acks follow the `--space` selection; they live under the unselected key) and 7 Minor; see `.superpowers/sdd/P3-review.md` |
| 7 | general-purpose (final whole-branch review) | 54110bc..0b47ba4 | fable | With fixes: 0 Critical, 0 Important, 10 Minor; 3 required before merge plus Minors 1-2 taken; fixed in 10a2adb and the `docs(brief): final review fixes` commit |
| 8 | general-purpose (final fix) | 0b47ba4..HEAD | opus | DONE; 10a2adb canonical `newest_recorded_at` (Minor 1) and the brief.py comment (Minor 3); docs commit: outage test calls `brief last` while follow-up reads fail (P3 Minor 3), UTC due-date sentence (Minor 2), lock wording and the 2026-10-16 gotcha tense (Minor 9), local paths in this directory replaced with `$SCRATCHPAD` and `$WORKTREE`; final gate in `$GATE/verify-final.log` |

Coordinator decisions on P2 findings that touched the plan (recorded here and in the spec, owner may override): I2 → `brain_brief_ack` requires both `brief-read` and `brief-ack`; ack without read is `unsupported_capability`; the grant flags stay independent. I3 → a selected space ID the session cannot see or that does not exist is `invalid_request` on every surface, before the visibility key is computed.

Minor findings carried to the final review (from `.superpowers/sdd/P1-review.md`): visibility-key tests do not cover every key dimension one at a time; `source_freshness` scan cost is unbounded by capture count; P1 commits carry the Opus trailer (six commits); `source_freshness` imports the private `_managed_copy_identity` from `sharing.py` (a public alias could follow); no external-provider test over an unavailable copy. From `.superpowers/sdd/P2-review.md`: `brief last` on a corrupt log prints "No saved brief" and hides that the log was unavailable; family-ref regex 9 characters narrower than the contract (being fixed with I1–I3); stale acks are never pruned; key-file eviction runs before the CAS create. Unverifiable from P2's diff, for P3's acceptance: owner `--space` filters follow-ups by evidence head capture spaces (verified by P3's acceptance test `test_owner_space_selection_filters_followups_by_evidence_capture_space`). From the P2 re-review: owner log files have no count cap (only the owner's own `--space` choices create them); log files written before 667577f lack the `owner` field and are set aside once (format unreleased).

## Task 18 frozen-tree gate

Frozen HEAD `6693b5a`, merge-base with `main` `fc5ef95`. Logs are under `$GATE` =
`$SCRATCHPAD/brief-gate`, where `$SCRATCHPAD` is the coordinator session's scratchpad
directory and `$WORKTREE` (in the package briefs and `STATE.md`) is this branch's worktree.

| Date | Step | Command | Result | Log |
|---|---|---|---|---|
| 2026-10-24 | gate | `make verify` | exit=2 (pytest 9529 passed, 9 skipped; plugin 701 passed, 1 failed: `bridge.test.ts` process-group ready timeout) | $GATE/verify.log |
| 2026-10-24 | gate | `make plugin-test` alone (retry) | exit=0 (702 passed) | $GATE/plugin-test-rerun.log |
| 2026-10-24 | gate | `make native-audit` | exit=2 (`base archive failed the strict content audit`: 550,577 objects against `MAX_OBJECTS` 550,000; BUILD-021) | $GATE/native-audit.log |
| 2026-10-24 | gate | `make homebrew-smoke` | exit=2 (same audit failure in its native build) | $GATE/homebrew-smoke.log |
| 2026-10-24 | gate | `git diff --check fc5ef95..HEAD` | ok | $GATE/diff-check.log |
| 2026-10-24 | gate | `actionlint .github/workflows/ci.yml` | ok | $GATE/actionlint.log |
| 2026-10-24 | gate | brief test set (19 files), umask 022, /private/tmp basetemp | exit=0 (429 passed) | $GATE/basetemp-umask022.log |
| 2026-10-24 | gate | acceptance + docs tests, umask 077, /private/tmp basetemp | exit=0 (21 passed) | $GATE/basetemp-umask077.log |
| 2026-10-24 | gate | frozen HEAD | 6693b5a | $GATE/HEAD |
| 2026-10-24 | gate | `make verify` after the gotcha commit, HEAD 1f6718b | exit=2 (9502 passed, 1 failed, 26 errors: nested-pytest `TimeoutExpired` at 60 s in `test_harness_acceptance.py` and `test_pytest_timing.py` under host load average up to 11; no `source_changed_during_run`) | $GATE/verify-2.log |
| 2026-10-24 | gate | those two files alone (retry) | exit=0 (45 passed) | $GATE/verify-2-timeouts-rerun.log |
| 2026-10-24 | gate | `make verify` again, HEAD 1f6718b | exit=0 (pytest 9529 passed, 9 skipped; plugin 702 passed) | $GATE/verify-3.log |
| 2026-10-24 | gate | HEAD after ledger | 1f6718b | $GATE/HEAD-after-ledger |

New gotchas: BUILD-021 (audit object budget), TESTING-017 (plugin process-group test under load).

Coordinator decision on the native audit: option (a), mirroring de4a6dd (#88). f3ea292 raises
`MAX_OBJECTS` in `tools/open_brain_dev/artifact_audit.py` from 550,000 to 600,000. `MAX_DEPTH`, the
30-second worker CPU limit and every content rule are unchanged. BUILD-021 was rewritten to record
the raise. The measured scan is 6.82 s CPU, with 550,577 objects and no findings. The gate was
rerun on the frozen HEAD `f3ea292`:

| Date | Step | Command | Result | Log |
|---|---|---|---|---|
| 2026-10-24 | gate | `make verify` | exit=0 (pytest 9529 passed, 9 skipped; plugin 702 passed) | $GATE/verify-4.log |
| 2026-10-24 | gate | `make native-audit` | exit=0 | $GATE/native-audit-2.log |
| 2026-10-24 | gate | `make homebrew-smoke` | exit=0 (every journey step passed) | $GATE/homebrew-smoke-2.log |
| 2026-10-24 | gate | `git diff --check fc5ef95..HEAD` | ok | $GATE/diff-check-2.log |
| 2026-10-24 | gate | `actionlint .github/workflows/ci.yml` | ok | $GATE/actionlint-2.log |
| 2026-10-24 | gate | brief test set (19 files) + `test_native_artifact_audit.py`, umask 022, /private/tmp basetemp | exit=0 (548 passed) | $GATE/basetemp-umask022-2.log |
| 2026-10-24 | gate | acceptance + docs tests, umask 077, /private/tmp basetemp | exit=0 (21 passed) | $GATE/basetemp-umask077-2.log |
| 2026-10-24 | gate | frozen HEAD | f3ea292 | $GATE/HEAD-budget |

## After merge

Deferred from the final review's triage, in suggested order: push a `space_id` predicate into the
`source_freshness` SQL and give `brief_ack` a single-family path; let `brief last` report a log
that was set aside instead of printing "No saved brief"; export a public alias for
`_managed_copy_identity` from `sharing.py`; prune acks whose family no longer appears at generate
time; move key-file eviction after the CAS create; export one family-ref pattern from
`brief_contracts` and build the other three copies from it. Whether due dates should follow the
local calendar instead of UTC is a follow-ups question, not a brief one. `CLAUDE.md:77` ("Artifact
audit limits ... remain fixed") now conflicts with the `MAX_OBJECTS` raise and is left untouched
as an owner decision.
