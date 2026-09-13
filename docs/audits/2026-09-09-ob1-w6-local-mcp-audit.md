# Codebase vs plan audit: OB1-W6 local MCP

Date: 2026-09-09. Mode: standard.

Plan: `docs/plans/2026-09-08-ob1-product-completion.md`, fixed MCP decisions and OB1-W6.
Scope: W6 only on `feat/ob1-w6-local-mcp`, based on merged W5 commit `14489a2`.
Paths below are relative to the repository root.

## Assessment

All ten implementation requirements are complete and locally verified. Repository verification and
macOS arm64 native/Homebrew smoke passed. The branch is ready for PR review; merge readiness remains
NEEDS WORK until owner safety audits and both supported-platform CI jobs pass at the committed
candidate. W7 and public release work have not started.

## Requirement evidence

| Requirement | Status | Evidence |
| --- | --- | --- |
| Shared CLI/MCP operations without adapter-to-adapter calls or concrete SQLite access | COMPLETE | `packages/app/src/open_brain/services/local_operations.py` calls injected engine capture, reconciliation, and retrieval tasks and projects public receipt/result fields. `local_entrypoints.py` and `local_mcp.py` use those functions. |
| Separate capture-only write and whole-Brain read authority | COMPLETE | `local_operations.mcp_capture_sink` constructs a profile-bound non-owner identity with only `capture.accept`. `LocalMcpAdapter` receives that sink separately from an optional search callable. It has no EngineTaskSet, owner capture task, or publication task. |
| Explicit flags, two tools, bounded stdio, clean EOF | COMPLETE | `local_entrypoints.py` rejects neither flag and JSON mode before bootstrap. `local_mcp.py` both omits and rejects unselected tools. `mcp_protocol.py` contains the extracted line-bounded transport; `mcp_stdio.py` retains the work-scope admission check. |
| Idempotency namespace, exact text binding, bounded conflict | COMPLETE | `local_mcp.py` hashes keys into `delivery.mcp.key.` and generates random keyless IDs. `local_operations.py` binds exact UTF-8 input through a source digest alongside the normalized engine submission. Repeated keys preserve capture IDs across sessions; distinct text, including canonically equivalent Unicode, conflicts. |
| Exact session limits before engine work | COMPLETE | The adapter validates arguments, charges valid attempts, and enforces 500 capture calls, 16 MiB capture bytes, and 2,000 search calls. Duplicates, conflicts, and backend failures count. A byte-bound refusal consumes a call attempt without submitting or charging rejected bytes. Protocol tests assert public limit codes. |
| CLI/MCP contention completes or returns database_busy | COMPLETE | `local_bootstrap.py` distinguishes transient shared-writer contention. `storage/sqlite.py` adds a narrow SchemaError subtype for busy/locked errors; `engine/local_schema.py` preserves it through classification and migration. Existing WAL and five-second timeout remain. Live subprocess tests hold both the application lease and a real SQLite write transaction, check bounded responses, inspect integrity, and retry. |
| Trust/provenance projection and export preservation | COMPLETE | Both representations use `local_operations.search_result`, including trust and public source origin without raw provenance. The shared semantic dataset is materialized through CLI capture, Markdown import, and MCP. Contract tests compare exact CLI/MCP results and verify Portable v1 export with unknown origin, automation-absent context, and unverified trust. |
| Retained scoped MCP remains deny-by-default | COMPLETE | `phase1_application.mcp_adapter` and `EngineMcpAdapter` authority are unchanged. New tests verify empty and nonmatching grants return no result; matching grants still work. |
| Disclosures at configuration and CLI help choice points | COMPLETE | `README.md` contains capture-only, search-only, and combined examples beside whole-Brain/egress and durable-capture/no-selective-delete disclosures. CLI help repeats both choices. `docs/privacy-model.md` and `docs/threat-model.md` describe poisoning, prompt injection, client permissions, same-user authority, session limits, and excluded capabilities. |
| Installed binary and unchanged native exclusions | COMPLETE | `tools/open_brain_dev/base_native.py` requires the three new local modules and keeps every forbidden module prefix. Its smoke runs capture-only replay and search-only exchange after the existing CLI journey and compares MCP results with CLI search. The PyInstaller exclusions and runtime dependencies are unchanged. |

## Review and interpretation

One coordinator owns all edits and git state. A parallel read-only reviewer mapped the existing
architecture and reviewed the implemented diff. The review identified transient writer
misclassification and loss of busy error identity; both are corrected. A follow-up identified a
valid attempt that crossed the byte limit without consuming a call count. The counter now advances
before that refusal, with a regression test. The final bounded rereview verified that correction,
native smoke, authority separation, busy handling, residue coverage, and documentation and found no
remaining blocker. Review does not replace the pending exact-head CI gate.

The engine's existing idempotency conflict path writes bounded quarantine evidence but no new
capture. The requirement to reject before work applies to session-bound refusals. No new deletion,
rollback, or purge behavior is introduced. Restarting a process resets session counters but retains
successful captures and idempotency history.

The local protocol uses the existing transport's supported maximum of 1 MiB per message, including
the newline, so a maximum-sized text remains representable after JSON Unicode escaping. The
retained work adapter keeps its prior 64 KiB default. Invalid initialize requests do not initialize
a session; nested non-request arrays, invalid UTF-8, and overlong JSON numbers receive bounded
request or parse errors.

## Verification record

| Check | Result |
| --- | --- |
| Focused MCP and migration tests | PASS: 99 tests, including all 38 new W6 cases. |
| `make verify` | PASS: Ruff, MyPy across 581 source files, 3,583 tests passed with 5 filesystem-dependent skips, and engine/app/connector source and wheel builds. |
| `make homebrew-smoke` (includes `make native`) | PASS on macOS arm64: native build and signature/dependency audit, CLI journey, Markdown import, verified export, capture-only MCP replay, search-only MCP exchange, and the same journey from the Homebrew-installed executable. Exit 0 after cleanup. |
| `git diff --check` and `actionlint .github/workflows/ci.yml` | PASS. |
| Independent read-only review and final rereview | PASS; no remaining blocker. |
| Canonical owner tree/history audits | Required at the committed candidate in a disposable single-branch clone; results belong in the final handoff and PR evidence. |
| macOS arm64 and Linux x86_64 CI | Pending publication of the focused W6 PR. |

The first full run found the old generic migration-error expectation after busy errors gained a
specific subtype. The test now asserts `DatabaseBusyError` while retaining its unchanged-data and
safe-retry checks. Subsequent assertion corrections preserved the generic callback-failure test and
accepted Python 3.14's bounded invalid-request result for a parsed nested array. The final full run
passed with no skipped implementation work.

A tree audit of the working directory also encountered pre-existing ignored execution logs. Those
logs are outside the candidate and were not removed or exempted. As in W5, the committed source and
history must be audited in an isolated single-branch clone so unrelated worktree files and refs
cannot contaminate the release candidate's inventory. The canonical denylist and approved historical
exceptions are unchanged.
