# New-user brain functionality assessment

Date: 2026-09-16 (America/Chicago).

Source baseline: `main`, commit `5cf081aa3d591e14b89245db290d4a186ff8156a`.

Public distribution baseline: Homebrew `v0.1.0`, tag commit `a374e4d806bcebe396c63eab916e005813f07bda`. Current source is 54 commits beyond that tag. Availability was checked against the public release, tap formula, and installed executable help. This was not a clean-machine Homebrew installation test.

Audience: a new user comfortable with AI coding tools, IDEs, and terminals. The assessment covers both documented promises and the broader functionality needed for a useful personal brain. It does not assume that this user should write Python against internal engine APIs to finish normal product workflows.

## Assessment

Open Brain can capture and find knowledge, and current source can organize captures and publish reviewed notes. It is not yet a complete daily knowledge workflow. Three composed workflows fail: the released capture-to-vault journey, current-source multi-source publication followed by search, and recurring source updates after organization. Full-record retrieval, end-user Portable restore, and selective forgetting also remain incomplete.

Several previous gaps have been closed in source. Counting agent setup, inbox organization, or reviewed publication as wholly absent would misstate the current implementation. However, the public install still lacks those newer interfaces. The top-level documentation mixes stable, source-only, and contributor-only capabilities.

The most urgent finding is a compatibility defect between two implemented features in current source. Publishing a canonical note supported by two captures succeeds, but subsequent CLI, MCP, and client searches fail. This occurred even though `make test` passed **1,654 tests, with 5 filesystem-specific skips**.

## What a new user can already do

| Workflow | Verified state |
|---|---|
| Create a Brain, capture text, search by known words | Works on fresh synthetic data without manual database setup. |
| Import a Markdown directory repeatedly | Existing tests verify duplicate suppression, changed/missing-file handling, reactivation, and retained export history. |
| Organize captures and connect an AI client | Current source has spaces/inbox CLI and MCP operations and owned Claude Code/Codex setup. Synthetic Codex project setup succeeded. These are not in the public v0.1.0 CLI. |
| Review and publish one source into a managed Markdown note | Current-source propose, inspect, approve, workspace setup/refresh, and subsequent search succeeded. Multiple sources expose F1 below. |
| Create a complete Portable Brain export | Verified Portable export works, including review history. Engine-level clean import also has passing round-trip tests; the end-user restore surface is the missing piece. |

Gmail, Drive, selected Claude Code projects, and selected Codex projects have source-selection and recurring-collection implementations. Existing integration evidence records live acceptance; this assessment did not reconnect accounts or read private content.

## Highest-priority gaps

| ID | Priority | User-visible gap | Classification |
|---|---|---|---|
| R1 | High, released product | Fresh captures never become managed notes in the documented Obsidian graph journey | Reproduced missing publication step in the public release |
| F1 | Release blocker for current source | Combining sources into a published note breaks CLI/MCP/client search across the Brain | Reproduced violation of the documented workflow |
| F2 | High | Search finds a long memory, but CLI/MCP cannot retrieve the complete record or narrow/page the result set | Missing product surface over existing engine capabilities |
| F3 | High | A verified export has no supported end-user identity-preserving restore command | Incomplete portability workflow; engine support exists |
| F4 | High | Organizing a changing automated source blocks its next revision and later items in the same collection batch | Disclosed revision freeze plus independently reproduced batch stall |

Selective forgetting (F5 below) is an additional high-impact owner-control gap, explicitly excluded by the current release rather than a broken implemented workflow.

### R1. The released capture-to-vault journey has no publication step

Using the installed v0.1.0 executable against a fresh synthetic Brain, capture, workspace setup, and workspace refresh all succeeded. Workspace status still reported `active_notes: 0`. Both the coordinator and onboarding reviewer reproduced this. The reviewer's graph projection check reported missing graph state.

The Obsidian capture command submits a quick inbox capture and then refreshes the workspace. Workspace refresh only materializes canonical pages. The stable executable has no `review`, `space`, or `inbox` command, and its plugin has no publication operation that closes this gap. Its ordinary capture command cannot produce the accepted notes required by the documented fresh three-note graph journey. Reading/searching the raw captures still works.

Current source adds the missing review/publication route; a single-source publication successfully reaches the managed vault. This is therefore partly implemented but unreleased. The primary guide still needs to describe that step, and F1 must be fixed before multi-source publication is shipped as the solution.

Evidence: `packages/obsidian-plugin/src/main.ts:321`; `packages/app/src/open_brain/services/plugin_bridge.py:320`; `packages/engine/src/open_brain_engine/engine/managed_workspace.py:1277`; `docs/acceptance/five-minute-install.md:50`. The capture and workspace paths were checked against the installed release as well as current source. The acceptance document itself says exact-candidate GUI/provider gates remain open, so this finding does not claim a previously certified GUI test regressed.

The complete fix is a supported, visible capture-to-canonical-publication-to-vault journey in the installed product. Preserve the distinction between unreviewed source captures and published knowledge, and make the required transition usable through the chosen UI/CLI. Test it from an empty Brain without pre-seeding canonical pages through internal engine APIs.

### F1. Multi-source publication disables current-source CLI/MCP/client search

A fresh Brain was populated using ordinary CLI commands. Two captures were routed to one space, combined into one proposal, inspected, and approved. Approval succeeded. The next search returned `local_operation_failed`, exit 78. No workspace, provider, background service, or external integration was necessary. Two independent reviewers reproduced this minimal case. The coordinator separately reproduced the same failure when an existing single-source page was updated using an additional capture.

Publication correctly writes every contributing capture ID into the page's provenance. Search first reconciles canonical Markdown, and reconciliation still requires that provenance equal a singleton list containing the search row's representative capture ID. The new publication model and the older validation rule disagree.

One such page blocks the entire reconciliation scan before the query executes, including searches for unrelated records. CLI, MCP, Obsidian, and the desktop share this operation in current source. Direct engine search still works. `doctor --check search-index` returns `ok`, and a verified export succeeds, so neither result proves that the app's search is usable.

Evidence:

1. Incorrect singleton comparison: `packages/engine/src/open_brain_engine/engine/reconciliation.py:183`.
2. Correct accumulation of source membership: `packages/engine/src/open_brain_engine/engine/review_bound.py:97`.
3. Shared pre-search reconciliation: `packages/app/src/open_brain/services/local_operations.py:96`; plugin/desktop binding at `packages/app/src/open_brain/services/plugin_bridge.py:324`.
4. Promised multi-source consolidation and search: `docs/review-publication.md:21`, `:80`, and `:112`.
5. Existing cumulative-source update test stops before the affected search/reconciliation step: `packages/app/tests/integration/engine/test_review_publication_engine.py:778`.

The sound repair is to validate the complete ordered membership from durable publication state, using the existing `canonical_source_rows` resolver. Do not remove provenance validation or silently bypass reconciliation. Add product-level regression checks for initial multi-source publication and a later page update, each followed by CLI/MCP search. This defect affects the assessed source candidate, not the older v0.1.0 release that lacks these publication commands.

### F2. Finding a memory does not complete the reading workflow

A synthetic capture contained roughly 3,300 characters, including a decision in its middle. Searching its opening title returned only an opening excerpt. Searching a distinctive word from the middle returned that passage, proving the text was indexed. But no CLI or MCP read-by-ID tool can return the whole record after discovery. A user must already know which words to search for, inspect raw stored/exported files, or use an original document outside Open Brain.

Inbox previews are bounded to 320 characters; review source evidence is bounded to 512. `review show` does return the complete proposed draft, but that is not a general source-content reader. Obsidian can open materialized canonical Markdown; an unmaterialized capture has no corresponding managed note to open.

The engine already supports complete projected content through `read_page`. Its separate `fetch` method still returns an excerpt and should not be mistaken for a full-content primitive. Engine search also supports space, payload-family, and record-type filters. The public CLI and MCP expose only query and limit, with no continuation. Limits are 100 results for CLI and 10 for MCP. Users can create spaces but cannot ask the public search tool to restrict results to one space.

Evidence:

1. Full-content primitive: `packages/engine/src/open_brain_engine/engine/retrieval.py:274`.
2. Public MCP query/limit-only schema: `packages/app/src/open_brain/services/local_mcp.py:186`.
3. CLI query/limit-only parser: `packages/app/src/open_brain/services/local_entrypoints.py:347`.
4. Public search response fields: `packages/app/src/open_brain/services/local_operations.py:113`.
5. Engine filters and bounded retrieval: `packages/engine/src/open_brain_engine/engine/retrieval.py:120` and `:236`.

The complete product design is a shared read API for CLI/MCP/client use: search with filters and stable continuation, then read a result by ID with explicit content limits and the same privacy projection. Space filters improve relevance; they do not by themselves establish privacy isolation or narrower MCP authorization. Keep those promises distinct.

### F3. Portable export lacks an end-user restore path

The coordinator exported a synthetic Brain containing two captures, a canonical note, route/review/publication history, and managed-workspace state. Export reported schema 3 and successful verification. Giving that export to the only public `import` command created new captures for `_space.md` and the canonical Markdown file, while skipping source JSON, history, identity, and the manifest. Searching the new Brain for the unpublished source's distinctive text returned nothing.

This is not a defect in the Markdown importer: it is documented as a Markdown importer. It demonstrates that its name does not fill the missing restore workflow. The original export remained intact. The engine has a separate validated `import_clean` that preserves identities and bytes, but no active CLI/MCP/desktop restore operation exposes it. Workspace `restore` reactivates a managed note; it does not restore a Portable Brain.

The current product authority includes byte-preserving export and import in `OB-PORTABLE-01`. Lower-level documentation separates app-owned restore orchestration from the engine contract. The implementation satisfies the engine boundary but leaves a practical product-level promise incomplete.

Evidence:

1. Product portability contract: `docs/product-family.md:109` and `:157`.
2. Public import parser/dispatch: `packages/app/src/open_brain/services/local_entrypoints.py:327` and `:899`.
3. Engine clean import: `packages/engine/src/open_brain_engine/engine/portability.py:501`.
4. Passing engine round-trip coverage: `packages/app/tests/integration/engine/test_portability.py:263`.

Expose a separate owner-invoked Portable restore command backed by the existing engine operation. A first supported version should restore into a new empty destination, verify before reporting completion, and document how to select the restored Brain. A complete recovery journey should test restore, search, review-history preservation, and re-export through product commands. Retaining verified export directories is a useful current mitigation, but it is not a supported recovery procedure.

### F4. Source freshness conflicts with organizing the inbox

A user connects a changing source, imports a record, and routes it into a project space. The automated source-replacement predicate requires the old capture's `space_id` to be empty. Once routed, the next revision is rejected. The documentation explicitly tells users to leave changing source items unassigned if automatic revisions must continue.

The refusal protects the owner's organized record, so simply removing the check is not a sound fix. The user nevertheless has to choose between organization and freshness. Published evidence also needs a preserved snapshot, but that should not prevent receiving later upstream revisions as new knowledge.

The live collector applies a pending batch sequentially. Its generic exception handler records `source_capture_failed`, retains pending work, and retries. An independent synthetic reproduction imported item A, routed it, then staged revised A followed by new item B. Two apply attempts both failed with `conflicting delivery`. The checkpoint stayed at the prior cursor, the same preview remained pending, and neither revised A nor new B became searchable. The batch-stall consequence is reproduced using synthetic provider inputs, not a real connected account. No public unroute operation was found to undo the triggering assignment.

Evidence:

1. Replacement predicate: `packages/engine/src/open_brain_engine/engine/capture.py:577`.
2. Explicit user-facing limitation: `docs/spaces-inbox.md:93`.
3. Passing refusal test: `packages/app/tests/unit/engine/test_foundation_contracts.py:284`.
4. Sequential intake and failure handling: `packages/collector/src/open_brain_collector/live_capture.py:420` and `:524`.

The architectural fix is to separate stable source identity and its current revision from the immutable revisions cited by a published page. Organization should follow the logical source, while publication can remain pinned to reviewed evidence. A changed routed source should become an explicit new revision or reviewable update without blocking unrelated collection. Item-level quarantine or an explicit frozen-item acknowledgement can contain the batch failure sooner, but must preserve unresolved changes visibly rather than silently dropping them. Verify the composed workflow with a second item after the changed source in the same batch.

### F5. There is no selective forgetting workflow

A user or agent can accidentally capture irrelevant, incorrect, or sensitive content. The current product has no supported operation to remove one durable capture from the active Brain and future exports. This limitation is explicitly disclosed in CLI/MCP help. Stopping a client prevents future captures but leaves completed writes intact.

Managed-note deactivation, upstream-source unavailability, and removal from a search projection are different operations. None supplies a general owner-requested forget operation over a capture and its dependent records. Existing exports and backups must also be distinguished from the live Brain.

Evidence: `packages/app/src/open_brain/services/local_mcp.py:156`, `packages/app/src/open_brain/services/local_entrypoints.py:353`, and `README.md:91`.

This is an intentional limitation, not an undisclosed encryption guarantee failure. It remains a high-impact gap for a personal brain. A complete design should distinguish retiring knowledge from active retrieval, correcting/superseding it while retaining history, and deleting selected content from the live store and future exports. Dependency handling must account for published pages, review evidence, blobs, and projections. Physical erasure of old external backups is a separate promise.

## Wider functionality gaps

These should remain visible without being confused with broken advertised behavior.

| Area | Practical effect | Current mitigation / next direction |
|---|---|---|
| Meaning-based recall | Search is lexical. `launch due date` did not retrieve a capture saying `release deadline`, although queries containing its project name worked. Semantic graph suggestions do not add semantic search. | AI clients can reformulate queries. Evaluate local hybrid retrieval against named paraphrase failures before changing dependencies or privacy behavior. Embeddings are explicitly deferred. |
| History, duplicates, and changed facts | Revisions are retained, but there is no general history list/show/revert interface. Equal independent captures create separate records; idempotency handles retries, not semantic duplicates. Canonical/source pairs intentionally remain separate results. | Review supports explicit consolidation and stable page updates once F1 is fixed. Add readable revision history and explicit duplicate/supersedes relationships before automatic merging. |
| Local-source continuity | Markdown root moves are refused with `import_root_changed`; no rebind command exists. Selected PDF/DOCX import is one-shot and does not deactivate a captured document when the original file disappears. | Keep paths stable and rerun supported imports. Add owner-controlled rebind and source-availability operations with preserved identity/history. |
| Background collection on Linux | The optional managed background-service path is launchd-only; non-macOS setup is rejected. Core Linux support does not imply collector lifecycle support. | Operators can run the collector foreground under their own supervision. A supported Linux lifecycle belongs in the optional collector package. |
| Capability discovery | The provisional source CLI catalog labels planned or host-mediated adapters `public_onboarding: true`; primary docs omit the joined capture-to-publication journey and disagree about whether collection exists. This catalog is not used by stable core or the desktop live-manager interface. | Give each integration a verified availability state and actual supported commands. Publish one version-labelled daily-use guide. |

Supporting references: `docs/retrieval.md:247`; `docs/import.md:71` and `:518`; `packages/collector/src/open_brain_collector/sources_cli.py:30`; `packages/connectors/src/open_brain_connectors/runtime/source_registry.py:346`.

## Release and onboarding boundary

The [public v0.1.0 release](https://github.com/cbolden15/open-brain/releases/tag/v0.1.0) and [Homebrew formula](https://github.com/cbolden15/homebrew-tap/blob/main/Formula/open-brain.rb) install the core executable, Obsidian assets, and Graphify helper. Current-source work is not automatically delivered to those users.

| Capability | Public v0.1.0 | Assessed current source |
|---|---|---|
| Core capture/search/Markdown import/export and Obsidian assets | Available | Available |
| Spaces/inbox, owned agent setup, source-bound review/publication | Absent from installed CLI | Implemented; publication/search composition has F1 |
| Dedicated desktop and optional collector | No public matching desktop/collector release | Contributor/source workflows; separate collector installation |
| Priority Google source onboarding | Not part of the public core install | Developer-client acceptance exists; public app registration/verification remains a release gate |

The README's first-use examples now include `space` and `inbox`, while the public executable does not. Feature-specific guides disclose source-only status, but the main install journey does not consistently do so. This is a concrete first-user failure even before broader feature design: a documented command can be unavailable immediately after the advertised installation.

The primary onboarding path also needs to connect capture/import, inbox routing, proposal drafting, inspection/approval, and managed-vault materialization. Graph-suggestion review is a separate workflow and should not substitute for explaining canonical publication. The CLI reference omits several new command families. A smaller reproducible documentation error is `open-brain doctor --json`: the actual parser requires `--check`; `doctor --check search-index --json` works.

Evidence: `README.md:42`; `docs/agent-setup.md:5`; `docs/review-publication.md:7`; `docs/cli.md:7`; `docs/audits/2026-09-16-priority-capture-audit.md:97`.

## What prior development already addressed

The development history shows a progression from local capture/search to client setup, source ingestion, organization, and reviewed publication. Those changes should be credited when planning the next work:

1. The daemonless local runtime and native release replaced the earlier appliance-heavy starting point (`551ee80`, released in v0.1.0).
2. Dedicated desktop capture/search and shared headless Claude Code/Codex setup reduced client-configuration friction (`9bc2776`, integrated through #34).
3. Selected document/calendar/session adapters and priority Gmail/Drive/agent collection added real ingestion and freshness paths (`7a4cfec`, `11d0fe4`, #34).
4. Spaces/inbox CLI and MCP made organization accessible without internal API use (#38).
5. Source-bound proposal, inspection, approval, and stable page updates exposed curation through CLI/MCP (#39), with F1 still preventing the complete multi-source journey.

Commit history and the recorded audits establish the capabilities added. They do not establish the frequency of each real-user complaint; no usage telemetry or fresh user interviews were available. Older archived product plans were not treated as current requirements.

## Recommended order and acceptance

1. Fix F1, then close the released R1 journey with a coherent publication flow. Pass multi-source publication and cumulative update through CLI/MCP search and managed-vault use on a fresh Brain.
2. Complete discovery-to-reading: expose full projected result reads, filters, and continuation through the shared product layer. Demonstrate an AI client reading a complete long source after search.
3. Finish the owner lifecycle: identity-preserving Portable restore and explicit retire/correct/forget operations. Verify restored use and retained/excluded history according to the selected action.
4. Make organization and collection compose. Update a routed source, preserve publication evidence, and prove unrelated batch items still progress.
5. Release a coherent supported feature set with matching docs and installation artifacts. Re-run the fresh-user journey against those exact artifacts, including the capture-to-vault path.

The recommended acceptance unit is the complete user journey. Module tests remain useful, but a release check should join capture, full retrieval, organization, publication, source updates, recovery, and owner-controlled removal. The two strongest new findings are interactions between individually implemented capabilities.

## Verification and limits

`make test` passed: **1,654 passed, 5 skipped in 153.15 seconds**. Skips concerned unsupported filename/Unicode-normalization behavior on this filesystem. Worker checks also passed four retrieval/portability tests and two source/import tests. The search regression was found through additional synthetic product journeys, not through a failing existing test.

Three grounding workers covered onboarding, retrieval/lifecycle, and history/source collection. Reviewers independently challenged the main findings. Codegraph was used for initial code grounding; direct source and actual commands resolved the call paths. Knowledge-base recall returned older brain-system context rather than current public-product evidence, so it was not used to establish findings.

All new captures, imports, publication operations, and client-setup probes used disposable synthetic roots. No real Brain content, credentials, client configurations, or connected provider accounts were changed. The installed core was exercised against a separate synthetic root, not reinstalled. No application code changed. This assessment document is the only repository addition.

Full `make verify`, native rebuilds, a factory-clean installation, real-provider reauthorization, native GUI timing, and a real-account routed-source batch reproduction were not run. The routed-source batch failure was reproduced with synthetic inputs. Existing acceptance evidence was read but is not represented as newly executed. The assessment is not an exhaustive security audit, scale benchmark, or guarantee that no other gaps remain.

Scratch evidence retained for this run:

- `/tmp/open-brain-assessment.XW9uvC/coordinator-evidence.md`
- `/tmp/open-brain-assessment.XW9uvC/onboarding-report.md`
- `/tmp/open-brain-assessment.XW9uvC/retrieval-report.md`
- `/tmp/open-brain-assessment.XW9uvC/history-report.md`
- Independent challenge reports: `/tmp/open-brain-assessment.XW9uvC/candidate-verification.md` and `/tmp/open-brain-assessment.XW9uvC/lifecycle-verification.md`

These temporary files support this assessment; the durable findings and essential reproduction details are recorded above.
