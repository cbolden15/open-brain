# W5 guided Slack workflow and source health

Date: 2026-10-15. Status: implemented on branch `feat/w5-guided-slack-workflow`, pending final gates (P1–P4 code head `67fad50`, 2026-10-22). Operator guide: [guided Slack workflow](../integrations/slack-guided-workflow.md).

P0 specification is complete in the [five-artifact contract packet](2026-10-15-guided-slack-workflow-w5-p0/ACCEPTANCE.md). P1–P4 are implemented on the branch above; re-anchoring decisions and deviations are recorded in the packet's `REANCHOR-P1.md`, `REANCHOR-P2.md` and `REANCHOR-P3.md`. The packet freezes per-store health with global consistency unknown, exact replay-or-halt rules and an inherited operation-lifetime guard. It also discloses a narrow create-only owner helper for the existing launcher-policy.v1 format; no existing persisted format or engine authority changes.

## 1. Summary

Give an owner one foreground CLI journey from selecting a Slack channel to inspecting imported messages, explicitly releasing selected Work evidence and configuring an existing scoped MCP client. Show the selected source's actual capture state, coverage limits and recovery action without requiring the owner to copy internal IDs between commands.

W5 improves the usability of the W0–W4 evidence path. It adds no model runner, unattended recall, provider adapter, new recall tool, daemon or desktop workflow. Actual-client/model checks, real Slack access, private pilot, push, merge and deployment remain separate owner-authorized stages.

## 2. Ownership and decision boundaries

The owner chooses the account, channel, date floor, Work destination, individual messages, providers and client context. The coordinator owns contracts, cross-package integration, CLI entrypoints and the execution ledger. Bounded implementers own disjoint files; an independent reviewer checks authority, restart behavior and truthful health reporting before heavy acceptance gates.

This proposal chooses the architecture and scope below so implementation does not begin with an open-ended feature menu. Package P0 must verify the assumptions before code writes. A new engine/Brain/Portable persisted format, changes to existing account/custody formats, a stronger old-binary fence, a new distribution or a different privilege boundary requires a concrete scope/estimate revision before implementation. No checkbox in this proposal grants those changes.

## 3. Background and grounded baseline

The product assessment prioritizes dependable Work-source intake and headless setup before commitments, feedback and a daily brief. Its ten-minute onboarding target is a pilot hypothesis after account permissions are available, not a measured W5 guarantee. W4 completed synthetic Slack intake, approved Work copies, scoped stdio citation plumbing and current-platform native acceptance at `7818f4f3138b887220c414119be9ac16e28b0a80`. Actual-model usefulness and injection resistance remain `not_run`.

Planning source HEAD: `328e55bc6e6eb8551ad6057029e8f70617880651`, which only preserves W4's final continuity beyond its tested candidate. Local `origin/main` was `d5cf64e652366dc14a8cf18732f7cb1e82ae4187`; no fetch was performed. Schema/runtime/Portable remain `17/12/12`. Historical W4 receipts keep their original tested HEAD and exact bytes.

| Existing seam | Verified behavior | W5 consequence |
|---|---|---|
| Optional collector source CLI | Accounts, paged resources, configure, foreground preview/import, status and custody inspection already exist. Existing output is JSON. | Add a guided owner interface; preserve old commands and output contracts. |
| Collector import identity | Import returns collector `source_id`, counts and custody `receipt_ids`. Custody inspection returns `capture_id`; it does not return an engine logical source ID. | Follow exact receipts into owner-local record reads. Never use lexical search, guessed UUIDs or permalink parsing as identity resolution. |
| Engine record/source operations | A literal capture ID can be read at its expected revision; structured record metadata resolves its logical source. Source inspection supplies current head/lifecycle/route versions. | Revalidate before any owner mutation. A newer head is a conflict requiring renewed inspection, not permission to silently substitute text. |
| Foreground capture health | Source `last_run` retains import counts, receipt IDs, completion time, `has_more` and notices. Aggregate health receipts are recorded by runner paths, not each direct foreground import. | Build a selected-source report from the correct evidence; do not call aggregate runner health the result of this manual import. |
| Existing owner workflows | Source routing, per-message sharing, selections, consent and scoped client setup already enforce distinct authority and preview contracts. | Guide those application services under owner authority; do not grant them to collector automation. |

Relevant code currently also records schedule metadata when configuring a source. A configured source begins disabled. W5 must distinguish stored interval metadata, source enablement, running collection and installed background service; none implies another.

## 4. Objective and success criteria

A technically confident owner can complete one permitted-channel workflow through readable choices and exact response-derived IDs. The first implementation release must satisfy these checks:

1. The primary guided path requires zero manual copying of internal account, channel, capture, source, approval or preview IDs. Advanced flags and receipts retain exact IDs for inspection and scripts.
2. Every local import, route, provider-consent decision, sharing approval and client-configuration apply has its own explicit owner checkpoint. The guide never treats a previous approval as permission for changed intent.
3. Restart after any boundary either reconciles an existing operation or stops with a precise recovery action. It creates no duplicate capture, approval, copy or client fragment and never automatically advances an unconfirmed write.
4. Source status distinguishes completed execution from partial/unknown coverage, current local state from unknown upstream changes, and selected-source metrics from global collector metrics.
5. Synthetic acceptance has zero Work/Personal scope leaks and no collector routing/release authority. Existing W4 citation, revocation, projection, Portable and setup behavior remains covered.

The usability measure is successful completion and reduced manual ID handling. Measure elapsed operator time separately in a later authorized pilot; do not turn a scripted test duration into an onboarding claim.

## 5. Audience and scope

The first user is an owner on a supported host who already has an allowed Slack account/app configuration and an existing compatible client/model choice. Core-only users must receive an actionable optional-collector-unavailable result. Core installation must remain sufficient for local capture/search and must not install the collector.

Included: one account, one explicitly selected channel, a bounded date window, owner-selected messages, one dedicated Work release space and one existing client context. Show names alongside stable identities; ambiguous names require an explicit row/identity choice. Existing Slack accounts currently all have the display label `Slack`; allow an owner-local display alias in the workflow journal, with the actual account ID always visible. An alias never changes identity or selects an account automatically. Slack names and message content are untrusted display data.

Deferred: new OAuth scopes, DMs, automatic channel discovery, additional sources, live Jira/transcript providers, commitments, feedback clustering, briefs, semantic retrieval, background activation, publication and outward messages. No new MCP tools expose the privileged guide or collector health. No new read access to raw originals is granted to an external client.

## 6. Architecture and user journey

### Architecture choice

| Option | Benefit and cost | Decision |
|---|---|---|
| CLI shortcuts and a read-only report | Smallest delivery; still leaves the owner to coordinate several commands and recover interrupted work. | Useful fallback, but does not meet the zero-ID-copy/resume outcome alone. |
| Shared owner application coordinator with a typed optional-collector adapter | A complete modular approach: one foreground owner flow, existing engine authority, reusable phase logic for interactive and JSON CLI, and explicit process/distribution separation. | Recommended W5 architecture; verify the child protocol and executable compatibility in P0. |
| A separately distributed owner workflow frontend | Strongest separation if core must never launch optional source tooling. Requires another release/upgrade surface. | Architecturally valid larger option. Pending distribution and compatibility assessment; outside this W5 estimate. Stop and re-scope if core policy rules out the recommended adapter. |

The recommended coordinator belongs in the app, behind thin CLI parsing. It calls existing owner application/engine services for Brain operations and an explicit optional collector executable for source operations. Core must not import collector/connectors or add them as dependencies. The collector keeps its existing restricted ingestion principal. This is foreground command composition, not a broker, model runner or new service.

The adapter uses an absolute executable, argument arrays without a shell, bounded JSON responses, a fixed operation allowlist and explicit state/Brain paths. Never accept an executable, command, path or operation from Slack text. Verify the intended collector package/capability before use; product version `0.1.0` alone cannot prove interface parity. Any new capability handshake is inert metadata and must not open a Brain, credentials or network. P0 defines its exact compatibility rule and error shape rather than assuming an existing handshake proves every source operation.

New account authorization remains the existing separately invoked `connect` flow in the first slice; the guide can explain and resume after it, but does not implement a new OAuth experience or extra scopes. Start the first vertical slice with one already connected account, one preview/import batch (currently at most 25 records), exact record resolution and one owner-approved message. Extend to the bounded multi-message path only after that slice passes.

Do not hold an engine/database lease while a child process runs or an owner reads a prompt. Use existing per-operation leases/CAS plus a short workflow-journal lock. Two guide processes cannot mutate one journal concurrently; different sessions still rely on authoritative engine/collector concurrency controls.

### Proposed CLI surface

The following command family is proposed, not implemented:

- `open-brain source guide slack`: interactive foreground flow, with explicit absolute Brain, collector-state and workflow-state paths and optional collector executable selection.
- `open-brain source guide status`: owner-local read-only progress/source-health view, with human and versioned JSON output.
- `open-brain source guide advance`: one explicitly chosen phase using a named owner decision and expected workflow revision. In JSON/noninteractive mode it never prompts or advances to another write automatically.

P0 freezes exact parser spellings and closed DTOs. Preserve all existing source, sharing, consent and setup caller shapes. Avoid a global `--yes`, arbitrary operation passthrough or an “approve everything” action. JSON mode emits one bounded result object; human prompts and diagnostics must not corrupt stdout JSON. Escape terminal control characters in untrusted names and error text.

### Owner checkpoints

| Checkpoint | Owner-visible behavior | Authority and stop rule |
|---|---|---|
| Prerequisites | Choose a disposable Brain for synthetic use. For a retained real Brain, complete the existing installed-binary export, separate collector backup and same-installed-binary restore checklist before the candidate opens it. Show binary identities and optional-package availability. | The guide does not certify backups through a checkbox. Real use still requires separate approval. Require explicit paths; never silently select the real default Brain. |
| Client safety and source selection | Scan the managed client configurations using the W2 finite-inventory rules before the first Work Slack import. Resolve account/channel names from actual returned resource pages and show the exact channel/date floor/Work classification. Connect only through the existing separately confirmed authorization flow when needed. | Refuse unresolved unscoped client setups. Explain that other projects/wrappers, older sessions and real endpoint/tool enforcement remain owner responsibilities. No implicit capability expansion or model launch. |
| Local capture | Preview one bounded source page, show count/coverage, then explicitly import that preview. Resolve exact custody receipts to captured records. Read selected bodies completely before offering routing/release. | Collection and routing remain separate. Metadata-only collector previews are not full-body inspection. Unknown privacy, stale generation, cancellation or incomplete receipt resolution stops advancement. |
| Work routing and release | Offer a dedicated release space; explicitly route owner-selected current logical sources. Create an engine selection where needed, then preview, inspect and approve each message for explicitly chosen providers. | No bulk substitute for per-message inspection. A reused broad space allows the client to read all eligible copies in that space, not only this guide's selection. Keep originals local-only; no publication prerequisite. |
| Scoped client handoff | Reuse current consent and W2 setup preview/apply with search/content-read only. Preserve model/provider choices and unrelated settings. Show approved copy IDs, selection completeness and a reusable recall task. | Setup changes require its own owner apply. Do not launch the actual client/model, infer destination/TLS enforcement or claim a passing private-pilot gate. |

### Identity and currentness contract

For each import page, retain its exact collector selection, preview identity and returned custody receipt IDs. Inspect each receipt, require it belongs to the selected collector source, and accept a capture identity only for a supported terminal Slack observation result. Generic collector receipts may carry an ingestion ID; do not reinterpret them as engine captures. Unavailable, quarantined, expired, malformed or mismatched receipts remain unresolved.

Pass the literal returned `capture_id` to the owner-local complete read with that same expected revision. Use structured `record.source_id` and revision metadata, then current source inspection. Require the head/revision to match what the owner inspects. Never strip/add prefixes, resolve identities from message text, follow arbitrary embedded URLs or silently select the newest record after a conflict. P0 must verify the complete binding chain available through current DTOs; add an owner-only read helper only if a genuine gap is demonstrated, with no direct UI SQL or authority broadening.

Historical/duplicate intake can legitimately identify a noncurrent revision. Display it as retained historical evidence only when the existing history path permits that exact read, and stop its release path. Hidden, retired or otherwise unavailable history remains unresolved; do not infer identity/content from a refusal. Offer an explicitly selected current-head inspection as new intent only when ordinary visibility permits it. Newly encountered messages remain unrouted until the owner chooses them. Edits need a new release preview; separately posted corrections need their own selection/approval and explicit revocation when replacing an older release.

### Resume and bounded execution

Use a private, versioned workflow journal outside the Brain/Portable payload. It stores destination/issuer identity, collector binding, selection IDs, bounded canonical non-content request preimages with their digests, explicit owner confirmation state, phase outcomes and returned receipt IDs. Persist exact operation/preview IDs, expected head/route/lifecycle versions, destinations and provider choices needed by a replayable request. Never reconstruct a pre-crash request from current mutable versions. No message bodies, credentials, tokens, provider answers or new source authority belong in it. The one explicitly proposed new persisted surface is this app-local journal v1; it is not a change to engine admission or existing source/account/custody formats. Bounded owner aliases may be retained only as display hints. Parent directory mode 0700, file mode 0600, owned regular files, atomic writes and symlink rejection are required. P0 checks retention/removal and old-journal compatibility before freezing this app-local format. It must not silently become a second truth store.

Journal each intended exact operation before execution. After a crash, reconcile authoritative receipts before creating a new operation ID. Reuse an existing ID only with the same request. Where a current public API cannot reconcile a committed operation safely, stop and present the exact recovery path; do not promise unconditional automatic resume or bypass CAS. P0 freezes a transition table: import and route use their exact existing replay requests when eligible; selections/sharing use their exact frozen replay contracts; any transition without a usable receipt/replay API is halt-only until the owner chooses a new inspected intent. Visibility checks can prevent route replay even after an operation committed. An ambiguous setup apply is halt-and-repreview against current configuration, not automatic replay of a stale setup preview. Never retain client config bodies, policy contents or credentials to reconstruct setup. Exact canonical request-byte preservation is part of lost-ack acceptance. Partial per-message routing/approval remains visible and retained; cancellation stops future work without erasing completed captures or releases.

Proposed new guide bounds: one child at a time; at most five read-only resource pages per inventory advance; one capture preview page per preview advance; and exactly one previously inspected, explicitly confirmed preview per import advance. One interactive invocation may cover at most five capture pages, with a distinct owner import checkpoint for each page and explicit continuation afterward. JSON import advancement must never fetch, preview or import the next page. Other bounds are at most 1,000 retained inventory receipt references, a 256 KiB journal and at most 24 explicitly selected release members. P0 checks these limits against existing producer bounds and freezes lower limits if necessary. Hitting a limit returns partial progress plus an explicit continuation; it never infers absence or completeness. Child response size/deadline limits must accommodate current legitimate DTOs and existing operation deadlines; invalid/oversized output or timeout stops with unknown-outcome reconciliation. Never shorten or weaken production provider/helper deadlines to make tests pass. Existing Slack custody retains observation replay evidence even after import, with a current quota of 256 retained items / 2 MiB; generic resolved receipts have a separate 512-receipt pruning bound. These admission limits take precedence over guide inventory limits and are not expanded by W5. Show capacity refusal and retained recovery evidence; never delete custody or clear queues to continue. No sleep/retry loop waits through a rate limit; show the existing retry deadline and exit.

### Source health contract

Return separate fields rather than a single misleading green “healthy” flag:

| Dimension | Evidence and presentation |
|---|---|
| Connection and credentials | Account registration is distinct from current token availability. A no-network status read reports last observed access failure and its timestamp, or credential status unknown. Do not test credentials merely to render status. |
| Activity | Show manual idle, awaiting inspection, blocked/retry-wait or paused from actual source/workflow state. “Collecting” requires evidence of this foreground operation; stored interval metadata is not a running schedule. |
| Execution and coverage | Show last import outcome/counts separately from `has_more` and supported coverage notices. Completed selected-window execution is not exhaustive Slack coverage or upstream currentness. Legacy/absent evidence is unknown. |
| Time and recovery | Label last successful import completion from `last_run.finished_epoch` only when that retained run has a successful outcome. If the latest run failed and no earlier successful timestamp is provable from retained receipts, show last successful completion as unknown; label the latest failed attempt separately; keep source time and per-message observation time distinct. Show only proven retry deadlines. Do not describe this timestamp as live Slack currency or invent an overdue schedule for a manual source. |
| Backlog and release | Separate collector custody/quarantine, development-processing backlog and owner approval/selection completeness. Use selected-source metrics where supported; global values must say global, otherwise return unavailable with a reason. A ready recall selection says nothing about collector completeness. |

Status must make no Slack/provider requests, process developments, run imports or mutate scheduling. A new collector-owned, closed, non-bootstrapping metadata read entrypoint is required. Existing `sources.status` dispatch constructs `LiveSourceManager`/`LiveCaptureService`, which can initialize `brain.json` and lock files; `PrivateJsonStore` construction and reads create absent directories. The new read path must bypass those mutating constructors and locks, engine bootstrap/migrations and credential access. Use no-follow existing-file reads with current ownership/permission/size validation and no creation or repair. Missing directories, partial/unbound state, absent or mismatched Brain bindings and unsafe files return explicit unavailable/uninitialized results without creation. Snapshot tokens/generations must detect concurrent state replacement and return an inconsistent/retryable read rather than merging it into a healthy report. Pending-preview metadata uses this same read path: never call existing `preview` as a status fallback because it drains cleanup, fetches and persists. The additive helper has a versioned DTO; existing mutating commands/receipt formats remain unchanged. Raw provider error text, message bodies, credentials and source references must not leak into the health response. These owner-local names/IDs must not be placed in a general MCP catalog or telemetry.

## 7. Implementation packages and acceptance

Run packages in order. P0 is a prerequisite contract check, not permission to redesign persistence mid-implementation.

| Package | Bounded deliverable | Acceptance gate | Estimate |
|---|---|---|---|
| P0 Contract freeze (specification complete) | Exact CLI/DTOs, optional executable handshake, receipt/source binding, journal/reconciliation rules, selected-source health mapping and fixtures. Recheck main coordinates and nested instructions. | Every transition has an existing authority API or a narrowly specified gap. Confirm no new engine/Brain/Portable persisted format; identify any missing reconciliation before P1. | 1–2 engineer-days |
| P1 Read-only foundation | Optional collector adapter, required non-bootstrapping metadata read path, resource inventory, exact captured-record resolution and source-health report. | Core-only refusal is actionable; status performs zero network/writes; duplicates/ambiguity/partial pages and source mismatches fail closed. | 1–2 days |
| P2 Foreground capture guide | Interactive and one-phase JSON flow through preflight, selection, explicit configure/preview/import and complete owner inspection, with bounded journal recovery. | Real intake/collector/engine components with only Slack transport mocked; no authority changes; interruption matrix passes. | 2–4 days |
| P3 Owner release and setup | Explicit routing, per-message release/selection inspection and scoped setup handoff using existing services. | No implicit consent/approval; stale sources/policies refuse; two-session races and exact retries preserve W4 scope/currentness; no client launch. | 3.5–6 days after P0 |
| P4 Regression and handoff | Operator/source/capability docs, synthetic usability transcript, complete compatibility/regression and current-platform native receipts on one candidate. | Independent review resolved; required gates pass; exact source/artifact digests and remaining live/platform limitations recorded. | 1–2 days |

Total after P0 scope clarification: 8.5–16 engineer-days for a familiar implementer, excluding provider approval, actual-client checks, real pilot, another distribution or new engine/Brain/Portable persistence/fencing. P0 adds0.5–1day to P3 for explicit policy preview/create-only apply using the unchanged launcher-policy.v1 format; existing code has no owner authoring receipt. The original proposal was8–15days. These are planning ranges, not measured agent elapsed time. P0 must re-estimate if safe receipt reconciliation, capability negotiation or journal retention is materially larger than assumed. No desktop feature work is included.

### Required scenario matrix

| Group | Required cases and oracle |
|---|---|
| Prerequisites and boundaries | Missing/wrong collector executable or capabilities; incompatible Brain identity/coordinates; unsupported host; retained-Brain backup precondition; unscoped setup found before any import; no default Brain fallback. No optional imports/dependencies enter core native closure. |
| Names and intake | Duplicate account/channel labels, rename, untrusted terminal/shell text, resource pagination, source reset during preview, Work/unknown/Personal classification, multi-page JSON import advances that never fetch/import a subsequent page, partial-thread coverage, rate limit/offline/locked credentials, empty and duplicate-only imports. No new scopes, source enablement or service installation. |
| Identity and inspection | Collector selection ID versus custody receipt versus capture versus logical source; generic ingestion-ID receipt rejected; missing/pruned/quarantined receipt; unrelated source/capture/Brain; current and historical duplicate; changed head during complete read; metadata preview versus full projected body. No search-based guess or automatic revision substitution. |
| Restart and owner writes | Crash before/after each committed import/route/selection/approval/setup action and before receipt journal write; lost acknowledgements; byte-exact saved request replay versus changed-intent ID reuse; ambiguous setup apply halts for repreview; concurrent guide sessions; expired preview; policy/consent removal; partial multi-message progress. Reconcile or halt, never double-apply or auto-approve. |
| Health and regression | Absent/partly initialized/read-only/unsafe state with zero created/modified files; no creating lock/engine bootstrap/credential calls; pending-preview metadata without calling preview; concurrent mutation yields inconsistent; direct foreground import with no runner receipt; completed but partial/unknown coverage; stale global health; manual disabled source with stored schedule; global versus selected backlog; old missing fields; no-network/read-only status; forged provider notices; W4 scope/revocation/projection/citation/selection repair and Portable goldens remain green. |

Use existing source/custody/intake/authority tests as evidence before adding new cases. Proposed new test homes are a dedicated app guide unit/integration suite and focused collector health/read-helper tests; exact filenames/owners are frozen in P0. Do not mock engine/sharing/routing/setup in the acceptance journey. Mock network only at the Slack transport boundary; unit protocol/failure tests may use narrow process fixtures and must be labeled as such.

## 8. Verification and release boundary

Implementation preparation must preserve W4 and this plan packet before creating a new execution workstream. Work only in the authorized checkout; if a new implementation worktree is requested, follow current policy under the repository's `.worktrees/`, not a sibling or `/tmp`. No worktree cleanup is part of W5 planning. Existing same-coordinate older binaries remain unfenced; this proposal adds no claim otherwise. No CodeGraph index is present; optional initialization requires separate authorization, and targeted source reads suffice for this plan.

Run focused acceptance and independent source/authority review before heavy gates. Use a fresh task environment with current-worktree imports, two Python workers, two Cargo jobs, short external temporary paths, failed-fixture retention count one and complete logs. Preserve the actual ignored `.venv` directory/bin alias required by plugin transport tests when using an external environment. Do not overlap native packaging with timing-sensitive Python acceptance.

Finish implementation docs, commit the candidate and freeze all checkout inputs, including docs/continuity, during fingerprint-bound checks:

```sh
make verify
make native-audit homebrew-smoke
git diff --check
actionlint .github/workflows/ci.yml
```

The combined native invocation shares its build prerequisite. Record tested HEAD, commands, complete logs, skips, source/artifact digests and platform limits. Resolve independent findings and rerun affected gates after changes. Planning itself changes documentation only: validate local references, plan consistency, diff and workflow syntax; do not report W4's full/native receipts as fresh W5 acceptance.

Before any later actual-client/real-source use, retain all W4 pilot gates: installed-binary Portable export, separately retained collector state with private digests, disposable restore verified by that installed binary, restricted client tools and separately recorded hostile-corpus citation/injection evidence. Prefer a pilot against a verified restore. Revocation does not retract prior provider deliveries or backups. Rollback uses the untouched installed-binary export, never an older binary against migrated state.

### Source index

- [Product assessment](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/docs/audits/2026-10-14-work-laptop-product-evaluation.md)
- [W4 acceptance](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/docs/ai/workstreams/20261015-open-brain-public-slack-recall-w4-execution-4f6ce3/ACCEPTANCE.json)
- [W4 operator and private pilot guide](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/docs/integrations/slack-decision-recall.md)
- [Collector source CLI](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/collector/src/open_brain_collector/sources_cli.py)
- [Collector dispatch and health integration](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/collector/src/open_brain_collector/live_manager.py)
- [Source status and import receipts](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/collector/src/open_brain_collector/live_capture.py)
- [Custody inspection](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/collector/src/open_brain_collector/custody.py)
- [Engine capture and source metadata](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/engine/src/open_brain_engine/engine/records.py)
- [Source route authority and CAS](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/engine/src/open_brain_engine/engine/sources.py)
- [Owner CLI entrypoints](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/src/open_brain/services/local_entrypoints.py)
- [Scoped client setup](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/src/open_brain/services/agent_setup.py)
- [Health coverage regression tests](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/collector/tests/integration/test_capture_health.py)
- [App dependency boundary](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/app/pyproject.toml)
- [Collector dependency boundary](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/collector/pyproject.toml)

- [Private storage creation and read semantics](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/connectors/src/open_brain_connectors/runtime/live_storage.py).
- [Existing source profile reads](/Users/calebbolden/Projects/oss/open-brain-public-worktrees/slack-cited-recall-w4-20261015-6d407b/packages/connectors/src/open_brain_connectors/runtime/source_host.py).
