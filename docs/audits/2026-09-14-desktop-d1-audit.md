# Desktop D1 implementation audit

Date: 2026-09-14

Plan: [Desktop companion](../plans/2026-09-14-desktop-companion.md).

Scope: D1 only, standard audit. Branch `feat/desktop-companion-d1`, based on D0 commit `8ee64cd`.

## Readiness

READY for the local D1 milestone on macOS arm64. All seven requirements are complete, including
native UI operation and save/fresh-session recall through both actual clients with the desktop closed.
Linux clean-host acceptance and public desktop release remain separate gates. D2 through D4 are proposed.

## Requirements

| Requirement | Status | Evidence |
|---|---|---|
| R1: Five destinations with real local capture/search and useful empty/error states | Complete | `packages/desktop/src/App.tsx`, `src/client.ts`, and `src-tauri/src/runtime.rs`; component tests pass; actual native capture/search and all five destinations verified. |
| R2: Optional desktop, shared headless setup, same Brain and executable | Complete | `agent_setup.py` derives the selected Brain and exact executable; `local_entrypoints.py` exposes the same preview/apply service; the native host selects only matched bundled resources. |
| R3: Claude Code/Codex, project/user scopes, independent capture/search | Complete | `agent_setup.py:_build_plan`, `_client_roots`, `_instruction_path`; both grants begin off in the UI; permission and profile tests pass. |
| R4: Preview, preservation, idempotency, owned removal | Complete | `apply_agent_setup` binds preimages, `_atomic_apply` rechecks before replacement, JSON/TOML/Markdown ownership checks refuse edits; preservation, race, FIFO, partial-state, and removal tests pass. |
| R5: Desktop note found by both clients; each client's save found in a fresh session | Complete | Claude Code 2.1.271 and Codex 0.154.0 returned the desktop note; each fresh session retrieved the exact capture ID from its save session. |
| R6: Actual native app operation and cleanup | Complete | Packaged macOS app used for capture/search and agent setup; normal quit removed the app and its owned runtime. Packaged proof and all process-group tests pass. |
| R7: Enforced shared-runtime compatibility floor | Complete | Schema 4/session 1 handshake, registry-held migration admission, schema migration tests, and actual D0/D1 binary compatibility test. |

## Verification

`make verify` passed on macOS arm64:

- 1,064 Python tests passed; five filesystem-specific cases skipped on this Mac.
- Ruff passed; mypy passed for 197 files; all workspace wheels built.
- Obsidian plugin: 18 tests passed.
- Desktop frontend: seven tests passed; typecheck and production build passed.
- Desktop control fixture: one test passed. Rust: 13 tests passed; formatting passed.

`make native-audit homebrew-smoke desktop-native-proof` also passed. The Homebrew journey preserved
the existing product and verified capture, search, export, Graphify, MCP, and Obsidian behavior.
The packaged app passed local signature verification and its native capture/search/Graphify/cleanup proof.
`git diff --check` and `actionlint .github/workflows/ci.yml` passed.

Final logs are `.plan-runs/d1-verify-acceptance.log` and `.plan-runs/d1-native-acceptance.log`.
The matched macOS arm64 artifacts have these SHA-256 digests:

| Artifact | SHA-256 |
|---|---|
| Bundled core | `dced5d0c11d2cd669fc20a4270f769e52dd52db5bd10f7b6753f9be8ebeec53d` |
| Bundled Graphify helper | `54bf1efd4b064f56f328ebe2d3b1778813d23699daf66b6e146be72d90608de0` |

Actual Claude Code initially discovered tools but rejected every call because its request metadata
was not accepted. A wire-shape probe recorded only method and parameter keys, confirming `_meta`
with `claudecode/toolUseId` and `progressToken`. The transport now accepts metadata as transport
context, never as tool input or authority. The 45 focused MCP checks and actual source-runtime
retrieval through both clients pass.

The restricted implementation worker had two environment failures, including nested macOS sandbox
execution. The coordinator's full rerun cleared both; no test was weakened to hide those failures.

The actual old/new native test established that a live D0 schema-3 peer prevents migration without
changing the database, that peer can still capture afterward, exclusive D1 access upgrades to schema
4, and the D0 executable subsequently rejects access without changing the database dump.

## Integration and limitations

The renderer exposes named commands only. Native requests use the exact paired runtime, a 15-second
cold handshake and 10-second interactive deadline, explicit exhaustion, and process-group cleanup.
Uncertain saves retain their draft and operation ID. Codex server extensions outside the owned block
also require review before removal, preserving user-added per-tool policy rather than leaving an
incomplete server entry. Setup errors expose bounded categories, never unrelated configuration text.
Generated instruction files require changes through their generator.
Codex project configuration still requires native client trust; setup does not edit that trust.

File replacement cannot form a transaction with noncooperating editors. Preview preimages, a final
write-time check, conditional rollback, and repeatable partial-state recovery bound this behavior.
Activity is explicitly session-only. No account adapter, transcript capture, background collector,
service registration, real Brain migration, or personal client configuration was enabled by this work.

## Native and client acceptance

The native UI captured and searched the Copper Orchard note, previewed and applied Claude Code
project setup, and displayed actual Activity entries. Both capability grants started off. Settings
showed the selected synthetic Brain. Source cards identify future connections as planned. The final
package displayed the corrected Open Brain title, a UTF-8 capture counter, and the saved note:

> Final native desktop memory: Copper Orchard launches with the color amber-17.

After normal quit, the app PID and its owned runtime were absent. The desktop stayed closed for all
final client sessions. Claude Code used its generated project `.mcp.json`; Codex used the generated
server entry through CLI overrides so the synthetic project's trust did not need to be persisted in
personal configuration. Both entries pointed to the exact bundled executable and the same Brain.

| Client | Explicitly saved fact | Fresh-session result |
|---|---|---|
| Claude Code 2.1.271 | Synthetic claude memory: the Quartz Lantern code is violet-38. | Same capture ID retrieved; desktop amber-17 note also found. |
| Codex 0.154.0 | Synthetic codex memory: the Quartz Lantern code is silver-62. | Same capture ID retrieved; desktop amber-17 note also found. |

The proof parser checked actual successful MCP responses, distinct session IDs, exactly one save
per client, and capture-ID equality across sessions. No shell, file, web, or delegated work supplied
the answers. Results are in `.plan-runs/d1-client-proof.json`; the four final traces are
`.plan-runs/d1-{claude,codex}-{save,recall}.jsonl`.

Initial Codex attempts claimed the tools were unavailable and made no calls, so they failed acceptance
despite exiting zero. The final test required MCP initialization and explicitly allowed tool discovery.
Test-only per-tool approval overrides authorized the two synthetic tools. No personal settings or
global approval policy changed. These test controls use the documented
[Codex MCP settings](https://learn.chatgpt.com/docs/config-file/config-reference).
This establishes actual packaged MCP execution, not automatic project trust or unattended tool approval.

Headless user-scope setup passed for both clients: search-only permissions, unchanged repeated apply,
owned removal, and no Brain initialization. The test used isolated profile directories.

Evidence uses synthetic data only. Runtime compatibility and user-scope CLI fixtures live outside
the app bundle. Private execution logs remain ignored under
`.plan-runs`; they are not release artifacts.
