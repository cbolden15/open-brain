# NW3 Obsidian plugin implementation audit

Status: **NEEDS WORK**

Mode: standard

Plan: `docs/plans/2026-09-09-ob1-native-workspace.md`, NW3

Range: `origin/main..f7d13a4`

NW3 has a working desktop plugin, local client protocol, managed-vault installer, direct-provider custody flow, and a verified macOS structural journey. It cannot exit under the plan because the required provider journeys, Linux GUI journey, and much of the renderer and activation matrix have not run.

## Requirement assessment

| Plan requirement | Status | Evidence | Missing proof |
|---|---|---|---|
| 1. Desktop package, discovery/protocol, provider onboarding, credentials, consent, and exclusions | **PARTIAL** | `packages/obsidian-plugin/manifest.json` marks the plugin desktop-only. `package.json` and its lockfile pin the TypeScript, Obsidian, esbuild, and Vitest toolchain. `src/bridge.ts` and `services/plugin_bridge.py` use the bounded `open-brain-client` protocol with a separate version. `services/provider_credentials.py` keeps keys out of plugin settings and supports macOS Keychain, Linux Secret Service, and session custody. Provider and exclusion commands are present in `src/main.ts`. | Real OpenAI, Anthropic, and Gemini setup and calls have not run through Obsidian. Linux OS-store behavior has not run. Claude subscription remains deliberately unavailable with `subscription_isolation_unproven`; it must stay closed until an unprivileged design proves credential and policy isolation. Fresh and reused login journeys are absent. |
| 2. Capture, search, refresh, navigation, conflicts, suggestions, pause, retry, and unload | **PARTIAL** | `src/main.ts` implements the required commands and review modals. `src/refresh-scheduler.ts` owns batching, persistent pause, and manual refresh. The Python bridge keeps one engine session for the plugin lifetime. Unit and integration coverage exercises bridge reuse, request bounds, provider setup/removal, suggestion review, conflict resolution, exclusions, scheduler behavior, and process termination. The macOS GUI journey activated the plugin, captured and found synthetic text, opened an exact managed source note from search, refreshed one existing Canvas tab to a visible `fresh` result, and terminated the full child process group on unload. | Actual GUI suggestion acceptance, conflict resolution, pause/reload, retry, and edit-burst behavior remain unrun. |
| 3. Managed vault and plugin install, upgrade, removal, and preservation | **COMPLETE** | `services/obsidian_plugin.py` owns staged assets through a marker and rejects unowned replacement/removal. CLI install/status/remove coverage is in `test_obsidian_plugin.py` and `test_local_entrypoints.py`. The Homebrew smoke installed, checked, and removed the plugin without displacing an existing product. Explicit Obsidian activation was required in the GUI journey. | A second-platform GUI activation/removal journey is still required by requirement 5. |
| 4. Shared fixtures plus adapter, process, trust, renderer, activation, lifecycle, and conflict matrix | **PARTIAL** | The full suite passed 1,011 tests with five expected filesystem skips. Plugin tests passed 18 cases. Existing engine suites cover durable conflicts, stale suggestion rejection, generated-output exclusion, privacy, export/import, and bounded provider adapters. Focused bridge tests cover the client protocol, missing or wrong versions, one-session operation, provider state, and Claude refusal. Focused Canvas tests cover existing-leaf reuse and first-open behavior. | The plan's complete matrix is not represented by actual renderer tests or GUI runs. Missing proof includes changes during inference, pause across reload, manual refresh while paused, repeated acceptance after rebuild/import, both conflict versions after restart, concurrent GUI conflict resolution, credential expiry, quota failure, provider switching in Obsidian, and OS-store-unavailable session entry. |
| 5. Actual Obsidian journeys on both desktops, fresh/reused, all four launch paths | **PARTIAL** | Obsidian 1.13.7 was exercised on macOS 26.3 arm64 with an isolated synthetic Brain and vault. The plugin was explicitly enabled, spawned the packaged foreground bridge, captured and searched text, opened a managed note, visibly refreshed the generated Canvas without adding a second tab, and removed its process group on disable. The original Obsidian vault registry was restored afterward and the fixture was deleted. | No Linux GUI journey ran. The macOS journey was structural and untimed; it did not run real provider onboarding or suggestion acceptance. Fresh and reused onboarding for the three direct providers is absent. Claude cannot pass while the approved safety boundary remains unproved. |

## macOS GUI evidence

The journey used only a disposable root and synthetic text. It did not open or read a personal Brain or real vault.

1. The packaged plugin was staged into an isolated managed vault. Obsidian required an explicit trust and enable action, and the command palette exposed the Open Brain commands.
2. **Open Brain: Capture text** accepted `NW3 GUI proof: cedar atlas links to amber orbit.` and displayed the capture notice. **Open Brain: Search** returned that capture for `cedar atlas`.
3. A synthetic canonical note was materialized through the local engine contract so source navigation had a managed file target. Searching `juniper compass` in the plugin opened `spaces/<synthetic-space>/notes/page_<synthetic-id>.md`; Obsidian exposed the exact title and body.
4. The structural projection returned `fresh`. `Open Brain Graph.canvas` contained a summary node and a file node for that managed note. A second isolated run showed exactly one graph tab before and after **Open Brain: Refresh graph now**, and that same leaf visibly rendered `Status: fresh` with the managed note. Commit `f7d13a4` reuses and reveals the existing generated-Canvas leaf instead of opening a new tab on every refresh.
5. Before disable, the renderer owned one foreground bridge process group containing the packaged parent and engine child. Disabling the plugin emptied the enabled-plugin list and left no matching process. The current-user Obsidian process then stopped, the exact registry backup was restored, and the disposable fixture was removed. An unrelated `ob1nw0` Obsidian process was preserved.

The initial empty-vault refresh also exposed a protocol-contract defect: the backend can return Canvas status `failed`, but the plugin parser accepted only `fresh`, `missing`, or `stale`. Commit `cfe188e` adds `failed` to the bounded union and a regression test.

## Verification

- `make verify`: passed. Ruff passed; strict MyPy passed over 192 source files; pytest reported 1,011 passed and five expected filesystem skips; plugin typecheck and build passed; all Python distributions built; Vitest reported 18 passed.
- `make homebrew-smoke`: passed. The installed journey reported `capture`, `doctor`, `export`, `graph_projection`, `local_mcp`, `markdown_import`, `obsidian_plugin`, `search`, and `status` passed, with `self_check: passed` and `existing_product: absent`.
- `actionlint .github/workflows/ci.yml`: passed.
- `shellcheck tools/homebrew-smoke.sh`: passed.
- `git diff --check`: passed.

## Safety boundary

The shipped runtime remains a foreground normal-user application. NW3 adds no root staging, namespaces, Linux capabilities, fixture-owner topology, daemon, network device, clipboard bridge, host-folder share, or real-vault dependency. The disposable C59 Linux CI topology is not part of the product.

## Readiness decision

**NEEDS WORK.** The local implementation is reviewable and its automated checks pass, but NW3's explicit exit condition is unmet. The remaining acceptance work is:

1. Run real fresh and reused Obsidian onboarding for OpenAI, Anthropic, and Gemini on macOS, including semantic suggestion preview, acceptance, source navigation, and verified export.
2. Run the equivalent actual Obsidian journeys on Linux without adding privileged product mechanisms.
3. Keep Claude subscription unavailable unless a separate unprivileged design proves its credential, ambient-context, tool, MCP, and retention boundaries.
