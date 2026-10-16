# Open Brain competitive-gap program

Date: 2026-10-15. Status: implementation program in progress; onboarding documentation and owner-authorized local W4 baseline integration verified. This document does not claim the missing features are implemented or all release gates are closed.

## 1. Summary

Close the remaining local retrieval, owner undo, consumer export, onboarding, client and optional-source delivery gaps identified in the reconciled assessment. Reuse existing engine authorities and completed W4 rather than creating parallel capture, retrieval or sharing systems. Keep the core local, foreground-only and usable without a provider account.

Owner decisions: keep the Open Brain name for now and plan renaming separately; assess a bundled local embedding model/runtime; include BOTH ChatGPT and Claude official consumer exports in the first import implementation; remove sync/mobile from this program. Active assessment gaps are **1,2,4,5,6,7,9**. Gap3 is deferred; gap8 removed; gaps10–11 remain outside scope.

## 2. Ownership

The owner chooses product/distribution tradeoffs and authorizes external acceptance/publication. The coordinator owns baseline integration, engine/app entrypoints, acceptance contracts and ledger. Bounded GPT-6.1 Sol implementers own disjoint files; independent GPT-6 Astra review checks source fidelity, authority, compatibility and recovery. Release/API/model documentation can be researched publicly; private accounts, exports, client profiles and Brain state are never implicitly available.

## 3. Background and baselines

The [reconciled audit](/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/competitive-gap-reconciliation/docs/audits/2026-10-15-competitive-gap-reconciliation.md) inspected main at `d5cf64e652366dc14a8cf18732f7cb1e82ae4187`. This feature worktree started from its documentation commit `8889181e0589ebe6e684634c9a001d6080077204` on `feat/competitive-gap-reconciliation`, under `/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/competitive-gap-reconciliation`. That starting source used engine/runtime/Portable16/11/11; the integrated W4 baseline uses17/12/12.

W4 is complete locally at tested candidate `7818f4f3138b887220c414119be9ac16e28b0a80`; its retained branch ends at `19f2f160e67a91623b5ec252d4002b8826b03830`, including committed continuity and W5 P0 specifications. W4's full/native receipts are historical exact-candidate evidence, not a fresh pass for this program. Its coordinates are17/12/12. W5 remains parked; this program does not authorize implementing its guided Slack workflow.

The owner explicitly authorized local integration of completed W4 into this feature branch on2026-10-15. The merge from19f2f16 onto `fd2147a96afe7a62e6f180552d73ed5e07c0dff8` resolved one conflict in `docs/engineering/gotchas/README.md` by preserving both documented sets. The initial resolved tree matched the prepared proposal `208a81305350bd370fa11f3e9d860dd3ef5a8498`; subsequent documentation records this authorization and verification phase. Runtime, packaging, tests and build inputs match completed W4. Current-only onboarding coordinates now read17/12/12; historical fixture bodies/receipts remain unchanged. Fresh integration verification is recorded separately from W4's historical acceptance. No merge into main, push, live checks or deployment is authorized.

The public v0.1.0 release and tap already exist. Gap6 means a new verified candidate and public proof, not inventing a first release. Semantic graph suggestions are not semantic search. Source withdrawal is not arbitrary capture/session rollback. Coding-session capture is not official consumer export import. Provider-copy approval is not multi-user collaboration.

## 4. Objective and completion tests

| Gap | Required user outcome | Evidence needed to close it |
|---|---|---|
| 1 | Paraphrased local queries find eligible current evidence without inference network calls or a separate model-install step | Bundled pinned helper/model/tokenizer; frozen relevance corpus; privacy/currentness/rebuild/failure and both-platform resource gates |
| 2 | Owner previews and retracts one capture or an exact session membership snapshot, retaining durable tombstones/history | Explicit authority/CAS, replay/partial-batch recovery, append-race refusal, dependent-page impact, reimport/restore and lexical/semantic exclusion tests |
| 4 | A newcomer understands the pitch and completes a short safe capture/search/setup journey | Source-matched current guide, synthetic CLI proof, honest published-vs-candidate wording and no broadened grants |
| 5 | Both ChatGPT and Claude consumer exports import as unverified provenance-bound records and rerun safely | Verified versioned format profiles for both; bounded archive/text/branch/attachment handling, item identity, receipts, edits/conflicts and interrupted retry |
| 6 | A frozen candidate installs on both supported platforms and has current public proof | Full/native receipts on exact commit/artifact digests, selected GUI/client/provider gates, changelog/demo, explicit publication approval |
| 7 | Supported Claude Desktop, Cursor and VS Code configurations plus a distributable Hermes host are usable | Synthetic protocol/config tests, explicit grants/consent, selected-version owner-run receipts, documented install/remove and retained current native clients |
| 9 | Ordinary users can install/configure the optional collector and understand its state/recovery | Separate distribution, current provider onboarding/authorization, no automatic source/service opt-in, source-specific current deployment proof |

Statuses remain implementation, synthetic acceptance, installed proof, live proof and published availability separately. No placeholder passing JSON can promote catalog certification. Real acceptance waits for separate authorization and the existing backup/restore prerequisites.

## 5. Audience and constraints

The first supported hosts remain macOS arm64 and Linux x86_64. First semantic quality acceptance is an English corpus; multilingual behavior is not promised. Existing local notes, official export archives and explicitly configured source selections are the input boundaries. No Windows, phone client, remote endpoint, sync, team membership, hard purge, model runner, background service in core, direct-provider fallback or renamed distribution is included.

The app continues to depend on the engine only. The engine owns records, authority, source lifecycle, derived retrieval and Portable contracts. Optional parsers/collector and the proposed embedding executable are separate resource/dependency boundaries. No UI/parser/helper writes live SQLite directly. Source text, archive metadata, titles and model outputs are untrusted data, never authority.

## 6. Value and smaller alternatives

The program makes existing memory useful across wording differences, recoverable after bad intake and populated from both consumer export ecosystems. It makes source and client onboarding match actual delivered capabilities.

The proper design uses one engine source/session lifecycle and one authorized retrieval path. Smaller alternatives remain visible: lexical relevance tuning cannot close semantic recall; an existing-source withdrawal wrapper cannot promise arbitrary session undo; manual Markdown conversion cannot claim official-export support. An optional separately installed embedding resource keeps the base smaller but misses the requested no-extra-install outcome. Bundling a fixed local helper/model is the recommended assessment direction; production runtime/model selection still needs measured feasibility. These alternatives are not silently substituted for the full completion tests above.

## 7. Solution and ordered packages

### G0: onboarding correction and contract preparation

The immediate code-independent slice rewrites the README first screen, uses a disposable synthetic Brain, distinguishes released CLI commands from candidate-only setup, and states current source coordinates. The existing marked10/5/5 first-use journey and all eleven example-body hashes stay byte-exact; its pinned historical artifact evidence is not repurposed as16/11/11 proof. Current guide additions are separately labeled and use canonical physical temporary paths, preserving no-symlink admission. Source inspection found one pre-existing mcp-grant-isolation body/pin mismatch: all eleven current bodies remain unchanged versus8889181, but the strict historical hash gate currently fails. Preserve and report that failure; do not equate the ten focused documentation tests with a passed pinned-artifact journey. The10/5/5 label describes assertion literals, not newly verified compatibility with the historical executable. New production runtime features remain unimplemented.

Before G1, freeze the exact baseline, compatibility ADR and supported export profile evidence. No guessed migration number or unversioned extension of a frozen receipt/export contract. A new tombstone/membership/import-ledger format or resource-manifest role receives a concrete scope/version/fencing design before implementation.

### G1: integrate completed W4 and freeze persistence/distribution contracts

The local W4 integration passed fresh full and native gates at `7b511c292a83b9f6d5aab7efbda3c28435fee996`, with all prior packets retained. Exact receipts are in [INTEGRATION-ACCEPTANCE.json](/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/competitive-gap-reconciliation/docs/ai/workstreams/20261015-open-brain-public-competitive-gap-program-6ccf0f/INTEGRATION-ACCEPTANCE.json). Verify W4 source ancestry and17/12/12 boundaries; do not claim old same-coordinate binaries are newly fenced. The remaining G1 contract work is not completed by this integration. Only then reserve any new coordinates required by capture/session tombstones, membership or import receipts. Specify old-reader refusal, migration retry/recovery, predecessor fixtures and Portable round trips. A derived embedding cache can be excluded from Portable, but cannot bypass currentness/authority or silently become authoritative state.

Freeze new public CLI/DTO shapes for owner preview/commit/receipt operations, per-surface grants, operation-ID semantics, bounded sessions/batches and failure outputs. No `allow everything`, automatic reset or publication step. No child process or owner prompt while holding an engine writer lease.

### G2: both consumer importers and shared intake receipts

Implement two adapters together over one normal source-intake/application service, not independent truth stores. Owner chooses a stable local import namespace; archives do not authenticate account identity. Item IDs, revision hashes, branch topology and segmenter/parser versions are distinct. Missing items in a later archive do not prove deletion. Unordered changed revisions conflict or require explicit owner resolution; timestamps do not invent newest-wins authority. Exact replay returns retained engine receipts; changed intent with the same operation ID refuses.

First release is bounded text-only, with explicit omissions for attachments, artifacts, tool/media blocks and unsupported fields. Never silently truncate; segment through a frozen bounded policy. Reject path traversal, links, duplicate ZIP entries, recursive extraction, oversized expansion, malformed/unknown profiles and source URLs that try to trigger network fetches. Parsed user/assistant assertions remain unverified third-party evidence. Reimports cannot silently resurrect retracted evidence.

The inspected [OpenAI export help](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data) and [Claude export help](https://support.claude.com/en/articles/9450526-export-your-claude-data) describe export availability, not full field/branch/ID/order schemas. This is not proof no schema exists elsewhere. Obtain primary schema evidence or an explicitly authorized, minimized representative specimen for each supported profile before promising official-format compatibility. Synthetic proposed profiles may be implemented/tested as such; neither API message DTOs nor coding-session logs count as consumer export evidence. Private exports are not authorized by this program preparation.

### G3: proper capture/session retraction

Build on existing source withdrawal and its owner CAS/replay receipts. Add authoritative capture tombstones and durable trusted session membership only after the G1 compatibility contract. Old captures without proved session membership are explicitly ungrouped; never infer a session from timestamps or text. Preview shows the exact current capture/session membership, newer heads, approved-copy/dependent-page impact and irreversible-history boundary. A later session append stales that preview. A historical member does not authorize retiring a newer unrelated head.

Freeze bounded atomicity versus durable per-item partial outcomes. Cancellation stops future work and retains completed receipts; no automatic edits to reviewed pages, provider deliveries or backups. Tombstones/membership/replay evidence survive Portable restore; current lexical/semantic/graph candidates exclude withdrawn evidence immediately even if derived cleanup lags. Existing historical/source evidence remains readable only through its ordinary authority.

### G4: bundled offline semantic retrieval

Extend existing paged search and its `hybrid_required`/model_unavailable seam in `packages/engine/src/open_brain_engine/engine/paging.py`, not a new recall tool. Inject an engine-owned embedding backend protocol; app/resource code resolves a pinned CPU native helper. Engine owns projection, chunk/revision identity, eligibility, scoring/fusion, cursor binding and final current projection. Helper receives bounded projected text, model identity and purpose only, never a Brain path, database, provider key or network/download permission. No ML Python dependency enters the audited core closure.

Assessment candidates are CPU ONNX Runtime plus [all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2), with [bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) as the larger comparison. Publisher listings show roughly90.4MB and133MB FP32 ONNX weights; these are not total installed sizes or measured performance. Runtime/tokenizer/redistribution licenses, pinned versions, exact native platform availability and CPU requirements remain feasibility checks. No weights, runtime binaries or model inference ran during preparation.

Add an explicitly recognized model/helper resource to the native manifest/formula contract; current allowed base+Graphify(+harness) sets cannot accept an arbitrary fourth role. Pin model/tokenizer/preprocessing digests and notices. Model upgrades use a new derived generation and rebuild. Derive only from eligible current search projections; inference can run outside a writer lock, then commit against exact revalidated revision/privacy/projection preimages. Incomplete builds cannot replace the last complete generation. Restore rebuilds derived vectors, never copies live SQLite.

At query time, filter eligible records before scoring/fusion/limits, collapse chunks by record, and revalidate current projections before output. Initially prefer bounded exact scan of eligible vectors to avoid authorization overfetch. Bind cursors to model/index/fusion and authority versions. `hybrid_required` fails explicitly on unavailable/corrupt resources; optional fallback reports lexical mode. Never use cloud fallback or an automatic download. CLI/MCP/plugin/desktop/harness share these semantics.

Proposed acceptance targets, frozen before tuning:200-record corpus,40 independently judged paraphrase queries,20 exact-identifier queries, paraphrase hit@5 at least90% with no regression on the fixed lexical set; zero unauthorized scored/returned records under instrumented scope/tier/provider tests; source and canonical edit/route/privacy/withdraw/rebuild/restore cases. On declared reference hardware with10,000chunks/384dimensions: warm-query p95<=500ms, cold p95<=3s, rebuild<=10min, incremental RSS<=600MB and additional installed helper/model/tokenizer<=250MB per platform. These are proposed product budgets, not observed results or edits to existing production deadlines. A failed quality/runtime spike requires a new scope estimate, not silently reduced acceptance.

Actual local model execution/download requires an explicit bounded acceptance authorization because prior work stopped before actual-model checks. Prepare exact digests, sizes, licenses, commands, synthetic corpus and a disposable target first. No retained real Brain is used for a feasibility spike.

### G5: supported clients and optional collector delivery

Add version-bounded Claude Desktop/Cursor/VS Code templates and fixture-tested configuration ownership/preview/apply/remove where the existing app service supports safe observation. Different clients have different JSON keys and lifecycle contracts; generic stdio launch is not selected-version client proof. Preserve Claude Code/Codex/Pi integrations and the Hermes foreground adapter. A turnkey Hermes host must pin identity/provider routing, lifecycle and receipt handling, not add a core listener or private-host dependency.

External-provider sessions require current scoped policy and explicit provider/tier consent; search grant alone does not make private content safe to release. Keep all per-capability grants and authority intersections. Current official [Claude Desktop guidance](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop), [Cursor guidance](https://prod.cursor.com/help/customization/mcp) and [VS Code guidance](https://code.visualstudio.com/docs/agent-customization/mcp-servers) inform adapters; pin supported versions before implementation acceptance. No actual client is launched here. ChatGPT remote connectivity stays excluded with gap8.

Prepare a separately distributed collector with pinned core/engine compatibility, clear optional installation and account/source/service distinctions. Use the existing foreground service/receipt path; do not require new core daemons or automatically enable a source/service. Preserve historical Google/client proof and locally completed W4 while renewing exact-candidate provider/platform evidence only under separate authorization. W5's guided flow is not a prerequisite silently resumed by this package; its accepted contracts can be referenced as design evidence without implementing W5.

### G6: frozen local candidate, proof and publication boundary

Finish docs and independent review before committing/freezing all checkout inputs. Run focused component and integration tests, then `make verify`; native/packaging changes require combined `make native-audit homebrew-smoke`, plus `git diff --check` and `actionlint .github/workflows/ci.yml`. Use a fresh current-worktree environment, actual ignored `.venv` directory/bin alias for plugin fixtures, two Python workers, two Cargo jobs, short external temp paths, failed-fixture retention1, complete logs and preserved failed fixtures. Do not overlap native packaging with timing-sensitive Python tests or change production deadlines/admission to pass.

Record exact tested HEAD, source/artifact/model digests, commands, results/skips and both supported platforms. Local source/synthetic passes do not certify GUI/provider behavior, Linux on a macOS machine, public onboarding or public acceptance. Prepare a deterministic synthetic demo; a real Hermes write-up must use expressly approved private material and redact it before review. Before any later real Brain candidate access: installed-binary Portable export, separately retained collector state/private digests, and disposable restore verified using that installed binary; prefer acceptance against the restore. Rollback never points an older binary at incompatible state.

Publishing releases, tap changes, pushing, merging into main, deployment, real Slack/provider access, actual client/model checks and private pilot execution remain separate owner-authorized boundaries. A release checklist is prepared locally before asking for those actions. Existing v0.1.0 publication is not replaced until its full candidate requirements pass.

## 8. Release phases, estimates and current state

| Package | Planning range, one familiar engineer | Exit |
|---|---|---|
| G0 current onboarding/source preparation | 1–3working days | Correct claims and source-CLI smoke, unchanged historical proof, reviewed program |
| G1 baseline/contracts | 2–4days plus decisions/evidence wait | Explicit W4 integration authorization, persistence/resource formats frozen, no guessed compatibility |
| G2+G3 shared import/retraction | 24–41days before shared-work adjustment | Both verified export profiles plus capture/session lifecycle/Portable acceptance |
| G4 semantic helper/index/integration | 14–25days | Offline relevance/privacy/failure and two-platform native/resource gates |
| G5+G6 client/source delivery and candidate proof | 6–12days plus external access/platform work | Synthetic local candidate first; live/publish gates separately authorized |

These are conditional engineer-day estimates, not agent elapsed-time promises. Shared schema, CLI, withdrawal/projection and release verification overlap; a reconciled initial envelope is roughly45–80working days,9–16weeks for one engineer. Export evidence, platform availability, provider approval and owner-run gates can extend elapsed delivery and are not bounded by this estimate. Re-estimate after G1 and the model/export feasibility slices. Do not claim all active gaps fixed when only G0 or a narrower wrapper passes.

Current preparation: README/first-use correction implemented; focused documentation tests and source-CLI smoke passed. Owner-authorized local W4 integration passed fresh full and native verification. G1 design preparation is resumed in [the contract checkpoint](/Users/calebbolden/Projects/oss/open-brain-public/.worktrees/competitive-gap-reconciliation/docs/plans/2026-10-15-competitive-gaps-g1/README.md): shared persistence/session/retraction, bounded both-provider import and native embedding-resource designs. The shared compatibility design reserves18/13/13; current implemented coordinates remain17/12/12. Concrete SQL/DTO/Portable schema fixtures and external export-profile evidence are still pending, so the executable G1 freeze is not complete. No importer, vector index, capture/session migration, new client adapter, collector release or native resource is implemented by these documents. Next coding slice: shared compatibility/lifecycle foundation and synthetic staging/replay/restore acceptance before provider parsers or model inference. Model execution and external acceptance/publication remain separately gated. Renaming and sync/mobile stay outside this program.
