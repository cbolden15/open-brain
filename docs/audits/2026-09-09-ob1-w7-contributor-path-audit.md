# Codebase vs plan audit: OB1-W7 contributor path

Date: 2026-09-09. Mode: standard.

Plan: `docs/plans/2026-09-08-ob1-product-completion.md`, OB1-W7 implementation and gate.
Scope: W7 only on `docs/ob1-w7-contributor-path`, based on merged W6 commit `c1a415d`.
Paths below are relative to the repository root.

## Assessment

All W7 implementation requirements are complete and locally verified. The exact documented frozen
setup and contributor target passed in a fresh single-branch clone on macOS arm64. W7 merge readiness
remains NEEDS WORK until both supported-platform CI jobs pass at the exact goal-branch head.
No public release or tap has been created.

## Requirement evidence

| Requirement | Evidence |
| --- | --- |
| One contributor command, ordered composition | `Makefile` declares `contributor-check` phony and invokes recursive `make verify` then `make homebrew-smoke` as sequential recipe lines. |
| Contributor prerequisites, commands, outputs, failures, no private access | `README.md` and `CONTRIBUTING.md` document macOS arm64/Linux x86_64 tools, frozen uv setup, the shared target, success markers, supported failure recovery, and owner-only private audits. Targeted `CLAUDE.md` updates preserve the existing instruction structure. |
| Canonical shipped metadata | All three shipped package projects declare repository, README, issue, and changelog URLs under `cbolden15/open-brain`. `CHANGELOG.md` exists and remains unreleased. The formula renderer's repository default uses the same owner. |
| Two existing CI jobs share contributor target | `.github/workflows/ci.yml` retains only Linux x86_64 and macOS arm64. Each invokes `make contributor-check`. The goal-branch push trigger enables the required post-merge exact-head gate. No new job, matrix, VM, Docker, private access, or release credential is introduced. |
| Isolated Homebrew formula and preserved product | `tools/homebrew-smoke.sh` renders a keg-only `OpenBrainSmoke` formula and invokes its unlinked binary by absolute prefix. `tools/open_brain_dev/homebrew_smoke.py` snapshots formula identity, versions, prefix, resolved prefix, global link, and executable digest. An absent product must remain absent. |
| Cleanup ownership and recovery | Cleanup requires the reserved tap's exact regular-file marker, rejects repository/marker symlinks and foreign installed formulas, uninstalls only the qualified smoke formula, and untaps without force. Startup removes marked residue. EXIT owns teardown; INT/TERM exit with conventional status. |
| Production-shell preservation and interruption tests | `tests/release/test_homebrew_smoke.py` runs the production shell with a command-recording fake brew and real formula renderer/guard. Tests cover installed/absent product, install/journey failures, foreign state, stale owned tap/formula, digest/link/version mutation, INT/TERM, and SIGKILL followed by successful retry. |
| Roadmap and acceptance status after exact documented command | Updated after the documented commands passed; both documents retain pending CI and public release gates. |

## Review and interpretation

One coordinator owns edits and git state; one parallel read-only reviewer mapped the requirements,
reviewed the implementation, and rereviewed the final correction. The added symlink regression exposed
that `Path.exists()` follows links and treats a dangling tap link as absent. The guard now rejects tap
symlinks before the existence test. Both existing and dangling links are tested through the shell.
The final rereview found no remaining implementation blocker.

The real host has no installed `open-brain` formula. Installed-product preservation is demonstrated
with the modeled existing product, while the live Homebrew run verifies the absent-product case.
No product-named stand-in was installed. The conditional live preservation check will execute on any
contributor host that already has the real formula. Concurrent smoke checks against one Homebrew
installation are unsupported and documented. SIGKILL cannot run a trap; a subsequent run recovers
marked Homebrew residue, while abandoned operating-system temporary files may await normal cleanup.

## Verification record

The code candidate is `d593b54b855005d7084299aede35e334e8383689`. Subsequent changes record this evidence and update status documents.

| Check | Result |
| --- | --- |
| Documented `uv sync --frozen --group dev --group native-build` then `make contributor-check` | PASS in a fresh single-branch clone on macOS arm64, exit 0. |
| Repository checks within contributor target | PASS: Ruff; MyPy across 583 source files; 3605 passed, 5 skipped in 110.84s (0:01:50); engine/app/connector source and wheel builds. |
| Focused release/smoke regression tests | PASS: 35 tests, including production-shell failure, preservation, foreign-state, and signal/recovery coverage. |
| Native and Homebrew journeys within contributor target | PASS: signature/dependency audit, CLI capture/search/export/status/doctor, Markdown import, and local MCP. Homebrew confirms keg-only installation; final guard reports `existing_product: absent`; smoke formula/tap removed. |
| Built wheel metadata | PASS: all three shipped wheels contain all five canonical Project-URL entries. |
| `git diff --check`, `shellcheck tools/homebrew-smoke.sh`, `actionlint .github/workflows/ci.yml` | PASS. |
| Independent review and final rereview | PASS; no remaining implementation blocker. |
| Canonical owner tree/history audits | PASS at the code candidate; repeated at the final documentation commit with results recorded in the external handoff. |
| macOS arm64 and Linux x86_64 CI | Pending owner-authorized PR publication, then exact goal-head verification after merge. |

The first focused run reached three metadata assertions before the new changelog file existed.
Adding that required file resolved them. The original 31-case rerun passed, and all 35 final focused
cases passed after additional mutation and symlink coverage. The first complete fresh-clone workflow
also passed; the corrected candidate received a second complete fresh-clone workflow.

Owner audits use a disposable single-branch clone to exclude pre-existing ignored execution logs
and unrelated refs. The canonical denylist and the previously approved history-only exceptions
are unchanged. Private release auditing remains outside the contributor target and CI.
