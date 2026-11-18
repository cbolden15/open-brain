# Browser capture and recall implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans after the owner selects execution. Track each step with its checkbox.

**Goal:** From Chrome, save a page with a reason, recognize it later, keep several passages, file it, import bookmarks and revisit it on a chosen date. Preserve the results through Portable export and restore.

**Architecture:** Chrome talks on demand to a separately installed native host. The host applies installation-owned grants and targets one Brain. Both local and optional remote transports invoke the same operations against canonical engine state.

**Tech stack:** Existing Python 3.14 workspace, SQLite, Portable Brain, Chrome Manifest V3 and a small extension in plain JavaScript modules. Use Node's built-in test runner for pure extension logic. Use a real Chrome installation for browser acceptance.

**Spec:** [Approved browser capture and recall design](../specs/2026-11-18-browser-saved-items-design.md). Plan baseline is `2dfb8f5`. Paths and commands below are relative to its worktree root.

## Global constraints

- Default to same-computer use. The remote destination is explicitly selected and shown. A disconnected remote session stays bound to that destination.
- Capture only on an owner gesture on a supported HTTPS top-level page. Browser data cannot supply authority, a filesystem path, an executable or a transport destination. Apply grants to reads, counts and writes at the Brain.
- Preserve source-scoped saved-item identity, fragment privacy, redaction, unverified provenance, terminal acknowledgement and replay. A restricted lookup returns the same miss for hidden and absent pages. Partial-domain grants permit reads only.
- Keep core foreground-only. Browser setup is separate from core installation. Use synthetic Brains and disposable profiles for proof. Production credentials, publishing and live host configuration require separate authorization.
- Acceptance order is Chrome on macOS first, real two-machine proof for remote, then clean-install Chrome on Linux x86_64. Track each platform and transport separately. A failing remote gate blocks remote claims while local work can proceed.

## Review focus

1. An old fragment-sensitive bookmark remains identifiable after a fresh restore with no source folder or collector cache. Own this in task 1.
2. A grant change or a tab navigation while a popup is open invalidates the affected operation. Own this in tasks 2–3.
3. A browser restart after a lost reply or an unavailable remote never duplicates an annotation or redirects the save. Own this in tasks 3–5.
4. A changed page repeating the same phrase twice never highlights a speculative match. Own this in task 5.
5. A revisit due on a local date around a daylight-saving transition stays due on that date after restore. Own this in task 7.

## Work sequence and file map

Use one plan because the browser operations share a source identity, authorization boundary and replay contract. Tasks 1–3 make one locally testable vertical slice. Task 4 adds remote proof. Tasks 5–8 are conditional on a passing local slice. Retain evidence per task and use the repository's commit-message-file convention for each scoped commit.

| Area | Existing seams | Proposed ownership |
|---|---|---|
| Source and capture | `packages/connectors/src/open_brain_connectors/runtime/saved_item.py`, `source_intake.py` in that directory, `packages/collector/src/open_brain_collector/lifecycle.py` (`EngineCaptureSink.submit`) | Keep URL normalization, item key, provenance and custody in existing code. Browser operations live in the separately installed helper. |
| Authority and canonical data | `packages/engine/src/open_brain_engine/engine/` (called **engine/** below), including `capture.py`, `records.py`, `spaces.py`, `local_schema.py`, `owner_retraction.py`, `search_projection.py` | New `browser_pages.py`, `browser_annotations.py`, `browser_revisits.py` and a versioned schema in that directory. Share existing query and write authority checks. |
| Host and extension | Root `pyproject.toml`, `uv.lock`, and `Makefile` enumerate workspace and verification surfaces | New optional `packages/browser-host/` with `src/open_brain_browser/` (**host/**) and `tests/`; new `packages/browser-extension/` (**extension/**). Register the host workspace, tests and build without adding a core dependency. |
| Export and brief | `engine/portable_v18_evidence.py`, `engine/portable_v18_restore.py`, `packages/engine/src/open_brain_engine/portable/v18.py`, `packages/app/src/open_brain/services/brief.py` and sibling `brief_contracts.py` | Extend version dispatch, validation and fixtures together. Add scoped due-record references to the brief. |
| Installed proof | `tools/open_brain_dev/`, `tests/release/`, `docs/integrations/saved-items.md` | New `browser_acceptance.py` harness, `test_browser_distribution.py`, and browser setup/diagnostics guide. |

### 1. Prove the existing source identity survives a restore

- [ ] Extend `packages/collector/tests/integration/test_saved_item_capture.py` with `test_saved_page_identity_round_trip`. Use the actual select → preview → import workflow for two URLs differing only in fragment, a tracking variant and an identical URL in another source. Record expected capture IDs; export through the supported entry point, restore to a new root, and reopen with fresh client state and the original source binding.
- [ ] Run `uv run --frozen pytest -q packages/collector/tests/integration/test_saved_item_capture.py -k identity_round_trip`. The missing resolver or unresolved identity is the initial failure. Check the actual restored delivery evidence before choosing an index.
- [ ] Add `engine/browser_pages.py` with a scoped lookup accepting the canonical delivery ID and authority, returning a visible capture ID or a miss. The caller derives the ID with `identity_url`, `item_key` and `SourceRecordKey.delivery_id()`. Reuse existing delivery evidence where available. Scope before emitting an ID or metadata. Keep fragments out of stored display URLs.
- [ ] Run the same test and the full saved-item contract and integration files. Assert tracking variants resolve to one ID, sources and fragments remain distinct, unseen and hidden both miss, and all results agree after restore. If v18 lacks sufficient identity, record exactly which evidence is missing and resolve the compatibility design before building dependent features. Never match by title or displayed URL.
- [ ] Commit the verified resolver and its fixture. Evidence is an actual restored lookup, independent of the browser.

### 2. Implement the smallest authorized native host

- [ ] Create `packages/browser-host/tests/test_browser_protocol.py` for one native message through stdin/stdout to a synthetic Brain. Cases include allowed origin, incorrect origin, malformed frame, wrong version, oversized input, timeout, a partial-domain writer, and revocation between lookup and save. Test that hidden and absent lookups produce the same public response.
- [ ] Run `uv run --frozen pytest -q packages/browser-host/tests/test_browser_protocol.py` and observe the missing entry point. Add the optional package and its workspace/test wiring with the existing pinned local packages. Keep native framing and installation policy outside the engine.
- [ ] Implement `host/native.py`, `host/policy.py` and `host/operations.py`. The proposed browser.v1 envelope has version, operation, operation_id on mutations, and operation-specific arguments. Define exact shapes for `page.lookup`, `page.save` and `operation.status`. Use one native-endian length-prefixed UTF-8 JSON frame per invocation; choose a 256 KiB application limit per frame and 10 s foreground deadline. Reject unknown fields and operations. Load source, destination, callable operations and tiers from owner-local setup. Apply current authorization inside the lookup or mutation transaction. Use a default-deny policy for missing/corrupt setup.
- [ ] Implement `page.save` through the saved-item intake/capture path. Return pending, accepted, or a bounded refusal. Match a replay to its original request and destination. A simultaneous save from the folder and browser must yield one page, retaining the first reason. Status must revalidate visibility. Verify that per-operation conflict and pending states survive a fresh host process.
- [ ] Rerun the protocol file, plus task 1 tests. Commit only after a real host subprocess returns an accepted synthetic capture that ordinary search can find by reason.

### 3. Prove the browser round trip on macOS

- [ ] Create `extension/manifest.json`, `worker.js`, `popup.html`, `popup.js` and `tests/capture.test.mjs`. First pin the UI contract in tests: gesture on tab A → tab changes → save canceled; queued → pending; lost reply → retry with the same operation ID; accepted → saved. Check missing host, rejected scheme, empty reason, and scoped miss separately.
- [ ] Run `node --test packages/browser-extension/tests/capture.test.mjs`. Implement popup/context-menu capture with active-tab permissions, scoped top-frame selection and a native registration restricted to the installed extension ID. Re-check the tab and document identity before mutation. Display the fragment-free URL, title, destination and reason. Retain only the minimum local retry state, scoped by destination, source and operation; expire it and clear it after acknowledgement.
- [ ] Create `tools/open_brain_dev/browser_acceptance.py` with a `local` scenario. Register the real extension and host only for a disposable OS account or equivalent isolated user configuration, targeting a synthetic Brain. Exercise an actual toolbar/context-menu save and reason search, discard one accepted reply, restart the extension and retry. Open a second tab with a different URL. Verify one accepted capture and zero writes for the second tab.
- [ ] Rerun JS and host tests. Record OS, Chrome version, artifact IDs, acceptance counts and results, with synthetic inputs only. Unit tests or a mocked native host do not satisfy this gate. Stop local expansion if genuine Chrome cannot complete it.
- [ ] Commit the local slice and evidence. The installed release remains unqualified until task 8.

### 4. Prove the optional remote path

- [ ] Add `packages/browser-host/tests/test_remote.py` and a `remote` scenario in the acceptance harness. Freeze the same request/reply contract. Pin unknown host identity, wrong Brain binding, refused connection, timeout before write, lost terminal reply and reconnect. Both before and after retry, assert the laptop Brain has zero records for the synthetic input.
- [ ] Run `uv run --frozen pytest -q packages/browser-host/tests/test_remote.py`. Implement `host/remote.py` against a fixed, owner-configured destination and restricted remote handler. Use the SSH candidate only after verifying its availability and host identity checks on the target pair. Start the process with a fixed argument vector; operation contents travel on stdin. Enforce policy on the canonical host. Show the configured destination on failure and keep any pending operation bound to it.
- [ ] On an explicitly authorized disposable two-host setup, save to the remote Brain from real Chrome, interrupt the reply, reconnect and replay the original ID. Verify exact count and destination on both machines. Check the same scoped miss, unauthorized write, and changed-grant cases as local. Record the two independent host identities as synthetic labels.
- [ ] Commit the transport and results. With only a loopback fixture or unavailable second host, retain remote status as unqualified, with local qualification independent.

### 5. Persist, search and restore multiple highlights

- [ ] Create `packages/engine/tests/integration/test_browser_annotations.py` and `extension/tests/anchors.test.mjs`. Pin two independent quotes on one page, intentional repeats with distinct annotation IDs, a replayed mutation, a conflicting reused ID, stale parent, masked quote, repeated text, and an ambiguous match after DOM edits. Test query/search, retraction, history, archive validation and import into a fresh Brain.
- [ ] Run those files with `uv run --frozen pytest -q` and `node --test` respectively. Implement `engine/browser_annotations.py` and versioned browser schema. Store parent, annotation ID, mutation identity/digest, bounded quote, prefix, suffix and optional note. Save with parent visibility and write authorization checked atomically. Index admitted searchable passages with the same parent visibility. Child status and replay remain separately identifiable from parent capture.
- [ ] Extend migration, export validation, restore, retraction and projection together. At this baseline reserve local schema 24 and Portable 19 **only if still free at execution**. Mirror the evidence/restore dispatch used for v18, validate all child references before any mutation, support older archives with empty child families, and rebuild indexes on restore. Include revisit persistence from task 7 before freezing these formats; a format already released requires a new version. Never export redacted/retracted quote text through an anchor, index or receipt.
- [ ] Wire `annotation.add` and scoped paged annotation reads into the host. Implement `extension/anchors.js` for top-document exact-quote matching with bounded context. Restore on owner gesture; unresolved or ambiguous quotes remain readable in the extension. Keep notes and space names out of page DOM. Verify two real highlights through save → close → revisit → export → restore → revisit, including after a lost acknowledgement. Test partial success and retry without a second parent.
- [ ] Commit after the engine tests, JS tests and installed `highlights` acceptance scenario pass for the local path. Reuse that scenario for qualified remote targets.

### 6. File saves and import bookmarks

- [ ] Add `packages/browser-host/tests/test_browser_filing.py` and `test_browser_import.py` beside it. Test skip-space, routed success, save success followed by filing failure, grant revocation after preview, imported tracking duplicates, HTML entities, non-HTTPS URLs, cancellation, rejected oversize input, partial completion, and replay.
- [ ] Implement `space.list` and `page.file` in the host via `engine/spaces.py`; recheck subject and destination. Return a separately retryable filing result. The popup uses allowed space IDs and can leave a capture unassigned. Assert original trust and evidence survive. Show a current space only if its metadata is visible.
- [ ] Implement `host/bookmark_import.py` and an extension file picker. Parse bookmark HTML as inert data or URL-list text with bounded standard-library parsing. Suggested first-release limits are 1 MiB per file, 1,000 entries per import, and transport pages within the task 2 frame bound. Pass file content in bounded chunks, never a client-supplied host path. Preview a snapshot and bind confirmation to its immutable digest, batch reason, source and destination. Bind stable row operation IDs to that batch. Enforce limits on the complete upload as well as chunks; expire abandoned previews.
- [ ] Run `uv run --frozen pytest -q packages/browser-host/tests/test_browser_filing.py packages/browser-host/tests/test_browser_import.py`. Exercise the real extension import UI: cancellation creates zero records, interruption and replay preserve exact per-row outcomes, and importing HTML makes zero requests to embedded URLs. Open a filed item after a Portable round trip and verify its visible space.
- [ ] Commit filing and import as separately reviewable changes, each with its passing test.

### 7. Surface due revisits and finish accessible interactions

- [ ] Add `packages/engine/tests/integration/test_browser_revisits.py` and extend `packages/app/tests/integration/services/test_daily_brief_acceptance.py`. Assert set/change/dismiss/clear are replay-safe, due state follows the stored IANA zone across midnight and DST, a read or filing preserves the date, and export/restore preserves the due decision.
- [ ] Implement `engine/browser_revisits.py` plus host `revisit.set`, `revisit.dismiss`, `revisit.clear` and bounded `revisit.due`. Tie each state to the page capture. Include revisit evidence in the migration and Portable version selected in task 5, validate time zones explicitly, and enforce the parent's visibility on restore and read.
- [ ] Extend `brief.py` and `brief_contracts.py` with scoped due-record references. Add the relevant kind, reason, ordering and versioned consumer fixtures. Due work precedes revisits; revisits precede undated work. Keep the five-item limit, existing ordering of other items and existing UTC work-follow-up semantics. Test a full brief, a hidden due item and retraction.
- [ ] Run both test files and the existing brief scope tests. Extend installed acceptance with a keyboard-only journey covering all capture, import, filing, highlight, due and dismiss operations; check focus, screen-reader status, disconnected and empty states. Verify reopening a restored Brain shows the same due view.
- [ ] Commit durable revisit behavior and the accessible user journey with results.

### 8. Qualify install, upgrade and removal

- [ ] Create `tests/release/test_browser_distribution.py` and `docs/integrations/browser-saved-items.md`. Specify optional host installation/registration, update and removal for Chrome on macOS ARM64 and Linux x86_64. Preserve the extension identity across upgrades. Setup must show exactly which registration and destination it changes. Removal drops only owned registration and temporary client state; verify Brain records remain available.
- [ ] Integrate host and JS suites with the existing repository checks. Test a clean core-only install, opt-in host installation, registration diagnostics, authorized local or remote setup, upgrade from the previous supported core and removal. Use the release's real installable artifacts. A development-only unpacked extension test supplies development evidence only.
- [ ] Run `make verify`, `make native-audit` and `make native-integration-smoke` on the relevant release targets. Use the repo-supported Ruby/toolchain and keep the tree stable while acceptance tests bind the source. Include the licensed distribution, version, changelog and install instructions with the candidate.
- [ ] For each advertised platform and topology record the installed artifact identity, browser version, real result and recovery/removal proof. Keep untested cells pending. Confirm that no background service, account or listener was introduced in the default core install.
- [ ] Commit qualification evidence. Stop at a locally verified release candidate; public release and live configuration are separate actions.

## Execution handoff

Start with task 1 in the approved design worktree. Run the existing baseline first: `uv run --frozen pytest -q packages/collector/tests/integration/test_saved_item_capture.py packages/connectors/tests/contract/test_saved_item.py`. On the plan baseline it passes 130 tests. Only the pre-existing saved-item path has been demonstrated. Track installed local, installed remote and Linux qualification independently throughout.
