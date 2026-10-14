# Slack capture to cited decision recall implementation plan

Date: 2026-10-14

Status: Reviewed implementation plan. Implementation and live deployment have not started.

Planning baseline: `e444526`; application code baseline: `d5cf64e652366dc14a8cf18732f7cb1e82ae4187`.

Product direction: [/Users/calebbolden/Projects/oss/open-brain-public/docs/audits/2026-10-14-work-laptop-product-evaluation.md](/Users/calebbolden/Projects/oss/open-brain-public/docs/audits/2026-10-14-work-laptop-product-evaluation.md).

## Outcome

An owner selects a Slack channel, captures a bounded conversation, and asks an existing MCP client, “Why did we keep invite-only onboarding?” The client returns a limited answer with exact supporting quotes, record/revision references and approved Slack links. Edits or withdrawal of bound source records invalidate their old evidence copies. A correction posted as a new message needs separate selection and approval; the answer does not claim to include unseen later messages.

CLI operations perform setup, source selection, capture, routing and owner approval. Existing MCP search and complete-read tools deliver authorized evidence to the user's chosen client/model. The core does not become an LLM runner.

Desktop app development, onboarding, packaging and release are out of scope. Also exclude new sources, DMs, automatic channel discovery, scheduled briefs, vector search, a general decision database, automatic publication and outward Slack/Jira actions. Optional development extraction is a regression case, not a prerequisite: unlabeled conversation must remain usable evidence.

## Architecture decision

| Approach | What it proves | Decision |
|---|---|---|
| Existing local CLI capture/search/read with record references | Captured Slack evidence is retained and locally readable. Lowest effort, but does not authorize sending local-only evidence to a cloud-backed client. | Use as the first implementation checkpoint. It is not completion of provider-backed recall. |
| Owner-approved Work evidence copies plus a scoped MCP session | Selected evidence and citation metadata can reach the chosen provider under current consent; original Slack records stay local-only. Existing clients perform reasoning. | Recommended complete approach for this workflow. Extend the existing sharing architecture with a versioned Slack evidence kind and preserve its old formats. |

Use canonical source identities, source revision receipts, owner routing, sharing approvals and ordinary record reads. Do not introduce a parallel index, capture pipeline, publication requirement or new `brain_decision_recall` tool for this slice. A reusable client workflow is enough once the evidence and authority boundaries work.

## Verified baseline and actual gaps

The checkout's metadata-only catalog reports schema/runtime/Portable `16/11/11`, task contract `t03.v1`, and public acceptance `not_certified`. The product version `0.1.0` does not establish installed feature parity.

| Area | Existing behavior | Work required |
|---|---|---|
| Slack capture | Headless selected-channel connect, resource discovery, preview/import, managed revisions, retries and coverage health exist. Source options accept `date_floor` and collector `privacy_tier`. [E1] | Reuse these paths in an executable journey; repair only failures exposed by the new acceptance fixture. |
| Work organization | Owner `source route` checks the expected head and route version. New managed revisions inherit the current logical source route. A source option named `space_id` is not supported. [E2] | Route selected logical sources through the owner CLI. Keep the collector's existing restricted authority. Newly encountered logical sources remain unrouted until owner selection. |
| Retrieval and citations | Existing search/read returns record IDs, immutable revisions, projected text and provenance IDs. It deliberately withholds raw source references; structured Slack permalinks are not ordinary read fields. [E3] | Construct explicit, owner-inspected citation metadata in the approved evidence copy. Do not relax the global source-reference projection. |
| External release | Work-classified Slack still has `external_egress=false`. Existing sharing admits saved-Markdown managed deliveries and creates a public-tier v1 copy; it does not admit raw Slack source revisions. [E4] | Add a distinct Work-preserving sharing contract for Slack, including source proof, revocation and Portable validation. Changing a tier or adding Slack to a connector allowlist is insufficient. |
| Client setup | Manual MCP supports whole-session policy and durable provider consent. Generic `agent setup` does not forward those policy/consent paths. [E5] | Add preview-bound forwarding for scoped setup, preserving model selection, unrelated configuration and existing setup defaults. |

## End-to-end behavior

1. **Select and capture locally.** The owner selects one permitted channel and a bounded date floor, sets source privacy to Work, inspects a preview and imports it. No schedule is installed. The collector retains raw evidence and current coverage status.
2. **Select the Work evidence.** The owner resolves the returned capture IDs to logical sources, inspects each source and routes the selected conversation records into one existing or explicitly created Work space. Repeated intake uses the logical source route. Additional new messages need their own owner routing in this first slice.
3. **Inspect and approve provider release.** A local sharing preview freezes selected raw revisions, exact projected text, safe citation metadata, Work space and allowed provider IDs. The owner inspects and approves it. Separate Work-tier copies become eligible only under a matching external-provider session and current consent. No collection or model client can grant that approval.
4. **Recall through existing tools.** A client configured with search/content-read grants and the Work policy finds the approved copies and reads their complete text. It treats their contents as untrusted evidence, distinguishes proposals from stated decisions and cites every factual answer claim. The CLI supplies equivalent search/read commands for inspection.
5. **Refresh and recover.** An edit, withdrawal or route change to a bound record, sharing revocation or provider-consent change prevents further release of its stale evidence copy. A changed raw revision requires a new approval; it is never automatically shared. A newly posted correction has a separate source identity and does not automatically invalidate an older message. The owner must select/approve the new evidence and revoke an obsolete approval when replacing it. Every answer states that Slack coverage beyond selected approved messages is unknown.

The Work space is the domain boundary; privacy tier and external-processing consent are separate controls. Owner CLI access remains trusted local access, not a confinement claim. A model using a network-backed client must never be configured as `owner_local` to bypass an external-release refusal.

## Contracts to implement

### Slack sharing and citation envelope

Extend sharing with a new explicitly versioned request/evidence variant. Preserve saved-Markdown v1 bytes, public-copy semantics, validators and retry behavior. Do not change the existing `_copy_privacy()` behavior globally.

The new Slack preview must bind these groups:

| Group | Required binding |
|---|---|
| Source proof | Brain/issuer identity; Slack connector/account/channel/message identity proven by canonical intake receipts; logical source ID; raw capture/revision ID; current head, lifecycle and route versions; source, payload, privacy and provenance digests. |
| Selected content | Exact current projected message text, projection version and digest. The first slice shares complete bounded message projections rather than arbitrary owner-edited summaries. Select several records explicitly for thread context. |
| Citation metadata | Original record/revision identifiers; source timestamp when proven; observation time separately; validated Slack permalink or explicit unavailability; third-party/unverified label; fixed scope `selected_owner_approved_messages` and Slack coverage `unknown`. Freeze the exact metadata the owner approves. |
| Destination authority | Exact Work space, copy tier `work`, `external_egress=true` only for the approved copy, named permitted providers, preview expiry and inspection/decision digest. Source privacy remains unchanged. |
| Retry and visibility | Frozen operation IDs, approval/copy linkage, replay receipts, current-source checks and revocation. No copy becomes externally visible before the complete evidence/linkage reservation is valid. |

Use a versioned text envelope, provisionally `slack-recall-evidence.v1`, inside the new approved copy. Existing complete-read tools can return it as untrusted text; a new generic permalink-read tool is unnecessary. Validate reserved fields before retaining it. Project envelope fields separately before serializing valid JSON, following the existing structured harness-citation precedent. If projection invalidates a citation field, withhold that field with an explicit reason; do not bypass redaction to preserve framing. A message that contains fake citation JSON remains quoted message content, never envelope authority. Each selected raw message has its own revision-bound approval; a new batch-approval protocol is unnecessary.

The permalink must come from validated Slack connector identity/evidence, use an allowed HTTPS Slack message-link shape, and contain no credentials or arbitrary note-supplied destination. Approval must cover the permalink because workspace/channel identity is metadata disclosure too. Do not pass raw `source_reference`, provider responses, local paths or tokens through the envelope.

The client cites the **authorized copy's** record/revision and the quote it actually read. The original record/revision and permalink provide provenance; neither is authority to read the local-only original. Message time, ingestion time and decision effective time must remain distinct. An absent effective date or identified decision owner stays unknown.

Collector coverage remains an owner-local diagnostic in this slice. Do not import private collector state into the core or infer Slack completeness from an MCP completion flag. The client always reports “Based on selected approved messages; Slack coverage unknown.” A successful complete read only means those returned bytes are complete.

### Scoped client setup

Add proposed `--session-policy` and `--consent-state` options to `agent setup` preview/apply/remove, forwarding the existing MCP options. These setup flags do not exist at the planning baseline. Reuse `launcher-policy.v1` and the durable consent store; do not add a competing policy file.

For this workflow, the policy permits search/content-read, allows only the selected Work space and `work` read tier, has `egress_mode=external_provider`, and names the existing selected provider plus current consent. Grant no capture, organize, inbox, review, publication or history authority. Ordinary request filters can only narrow that authority.

For configuration, validate policy/consent identity and paths, bind their relevant preimages and effective authority to the preview, and revalidate them at configuration apply and every runtime operation. Refuse incompatible grants and missing/stale policy; never fall back to unscoped setup. Removal validates owned-fragment identity and configuration preimages without requiring active grants or readable policy/consent files, so revocation or deleted policy cannot prevent cleanup. Preserve unrelated client settings and model/provider selection. Policy content and credentials stay outside the repository and generated instruction text.

### Recall workflow and bounds

The initial client workflow is a documented task template, not a new model adapter or core inference API. It searches lexical variants of the question, reads complete candidate evidence and produces a cited answer only from returned text.

Proposed per-recall limits: at most three search variants, ten results per search, eight distinct approved evidence records and 64 KiB of assembled projected evidence. These limits stay inside the existing per-call/session budgets. Enforce limits in the deterministic acceptance driver; client instructions request the same limits but do not claim to constrain a hostile same-user client.

Record every traversed page/read completion flag. On truncation, stale cursor, revision change or budget exhaustion, return an incomplete-evidence result rather than “no decision.” Currentness means current retained evidence under current authority, not exhaustive or live Slack coverage. When sources conflict or fail to establish acceptance, the answer must retain that uncertainty.

## Ordered work packages

Implement one package at a time. Later packages depend on the earlier acceptance checks passing. Estimates assume one engineer familiar with this repository: approximately **5–10 working days**, excluding Slack app approval, private deployment and external-provider availability.

### W0 Baseline and failing journey fixture

Estimate: 0.5–1 day. No prerequisite.

1. Pin the code and candidate catalog; collect metadata-only CLI help for source intake, routing, sharing, scoped MCP and agent setup. Keep installed/source mismatch explicit. Do not open the owner's Brain.
2. Add a synthetic fixture using the actual `PriorityRuntime`, `SlackSourceClient`, `LiveSourceManager`, engine and private collector state with an injected `LiveHttpTransport`. Include an unlabeled proposal, a rationale-bearing decision, an edit of that message and a separately posted correction, plus Personal and unrelated Work-space decoys.
3. Drive preview/import, owner source inspection/routing and ordinary CLI/MCP search/read. Establish existing duplicate/restart/routed-revision behavior. Capture the current external-release refusal and unsupported Slack sharing as expected baseline results.
4. Define the v2 sharing bytes, copy envelope and compatibility fixtures before production code changes. Make the new approved-copy/scoped-setup journey fail on the actual missing behavior, not a fabricated test-only shortcut.

**Acceptance:** Real Slack-shaped intake produces locally readable evidence; source updates preserve its route; external release remains refused. New requirements fail for the identified missing contracts. No provider or real account is used.

**Targets:** Add the future end-to-end test at [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/integration/services/test_slack_decision_recall.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/integration/services/test_slack_decision_recall.py). Reuse the existing fixtures listed in E1–E5 rather than copying their implementation into a fake engine.

### W1 Versioned Slack Work sharing

Estimate: 2–4 days. Depends on W0.

1. Add an engine-owned Slack source-proof branch for the new sharing contract. Validate actual source-intake receipts, current canonical bytes and Slack identity; do not fabricate saved-Markdown receipts or weaken its existing proof checks.
2. Add frozen owner preview/inspection/approval/revocation and the Work-preserving copy envelope. Reuse existing sharing lifecycle and exact retry principles. Route visibility of a pending or unassigned copy must fail closed until its approved Work membership is established.
3. Make all visibility and source-expansion paths enforce the approved source head/lifecycle/route, provider list and current consent. Changed or withdrawn originals and revoked approvals hide copies from new retrieval, including retained-history requests. Do not promise to retract content already delivered to a provider.
4. Append the required canonical compatibility boundary and portable evidence support. Reserve the next free schema/runtime/Portable coordinates after rechecking main; expected successors at this baseline are `17/12/12`. Preserve earlier migrations and frozen formats. Do not silently reinterpret `16/11/11` sharing history.
5. Prove source-proof, retry, corruption and revocation behavior before adding client setup. Verify export/import/reopen/re-export, including refusal of removed or substituted sharing/citation evidence after manifest rehashing.

**Acceptance:** Explicit approval creates exactly one current Work copy for selected providers, with unchanged local-only originals. A crash or lost acknowledgement resumes the same copy. Unsupported sources, stale versions, provider mismatch and malformed evidence create no readable substitute. Saved-Markdown v1 tests and supported old-format imports still pass.

**Targets:** Engine sharing/contracts, canonical source proof, source eligibility/projection, compatibility and Portable registration/validation/restoration; owner CLI sharing dispatch. Exact existing paths are in E2–E4. Keep the existing source-intake protocol and collector authority unchanged unless W0 proves a real defect.

### W2 Preview-bound Work client setup

Estimate: 0.5–1 day. Depends on W1.

1. Extend the setup request/service and owner CLI parser with the two policy/consent options. Forward them to the existing MCP invocation without adding a new transport.
2. Include selected Work scope, read grants, egress/provider identity and relevant file preimages in preview validation. Refuse stale apply, unsafe/missing files, another Brain's consent and attempts to combine this workflow with owner-authority write grants.
3. Generate concise evidence-use guidance for Claude Code and Codex without changing their selected model or unrelated configuration. Keep generic MCP use available through the existing manual invocation for other compatible clients.
4. Test preview/apply/remove, untouched defaults, changed policy/consent, revocation during a running session and ownership-preserving removal in synthetic client profiles. Removal must also succeed after policy deletion or consent revocation without restoring read access.

**Acceptance:** Installed fragments launch only the intended scoped session. Personal and unrelated Work-space records cannot be discovered, affect returned evidence, or be fetched by guessing IDs. Policy changes terminate the old session; reconnection does not reuse revoked consent. No owner grant or credential is introduced.

**Targets:** [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/agent_setup.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/agent_setup.py:422), CLI parser, setup DTOs/callers and existing setup/session tests in E5. Keep desktop setup callers on their old defaults; no desktop feature or release work is included.

### W3 Cited recall journey and operator guide

Estimate: 1–2 days. Depends on W2.

1. Add a headless operator guide covering selected-channel connection, bounded preview/import, Work source routing, local sharing inspection, approved copy discovery and scoped client setup. Derive IDs/versions from actual responses; label new request variants explicitly.
2. Add a reusable decision-recall task template: stated decision, stated rationale, alternatives only when evidenced, source dates, later changes, unresolved disagreement, claim-level citations and the selected-message/unknown-Slack-coverage qualifier. Document separate owner approval of a new-message correction and explicit revocation when replacing an obsolete release. Do not label an unapproved proposal or unverified derived record as accepted truth.
3. Extend the journey fixture through a real stdio MCP subprocess: initialize, discover tools, search, read complete approved envelopes and follow their record/revision citations. Keep core/collector network calls mocked at the Slack transport boundary only.
4. Validate a deterministic answer fixture against exact returned quotes and allowed claim/evidence IDs. Report it as protocol/citation plumbing, not proof of LLM reasoning quality. Add a separately invoked, owner-authorized actual-client check using synthetic data and the user's existing chosen model.

**Acceptance:** The question about invite-only onboarding yields sufficient authorized evidence for the expected rationale with exact quote citations and approved links. Paraphrases exercise bounded query variants; ambiguous or insufficient evidence remains visibly unresolved. No message sending, publication or direct-provider inference is added.

**Targets:** The new end-to-end test, scoped setup guidance and a new guide at [/Users/calebbolden/Projects/oss/open-brain-public/docs/integrations/slack-decision-recall.md](/Users/calebbolden/Projects/oss/open-brain-public/docs/integrations/slack-decision-recall.md). Neither future path exists at this planning baseline.

### W4 Regression, native proof and handoff

Estimate: 1–2 days. Depends on W3.

1. Run the acceptance matrix below through real engine/source/sharing/MCP components, including same-process and cross-process revocation and correction boundaries. Fix failures within the agreed slice.
2. Run the repository's full verification and native gates on the final code candidate. Record exact commit/digests and platform limits. Core and optional collector remain separate distributions; do not make the collector a core dependency.
3. Update the feature matrix, catalog capability description, source/operator docs and compatibility notes for the actual resulting candidate. Clarify capture-versus-logical-source routing guidance without changing old receipt semantics. Keep live Slack and public-promotion claims separate from synthetic acceptance.
4. Provide the owner-run pilot checklist: select one permitted small channel, inspect the captured evidence and every release preview, use the existing chosen client/provider, and record useful-answer/citation failures privately. No schedule or account is activated by implementation tests.

**Acceptance:** All required automated/native checks pass, old clients refuse unsupported state safely, and the guide exercises the exact candidate. Provider-backed workflow acceptance requires the approved-copy path; local-only recall alone cannot close this package. Actual Slack/provider access remains an explicit private deployment step.

## Acceptance matrix

| Group | Required scenarios and oracle |
|---|---|
| Capture and continuity | Real connector parsing and collector admission; duplicate import; lost acknowledgement; restart; routed source edit; new source unrouted; partial scan; rate limit; deletion only after complete coverage. Exactly one current source per identity and no collector routing authority. |
| Scope and release | Work space separate from tier; Personal/unrelated Work decoys; no policy; missing/revoked consent; wrong provider; raw Slack read refused externally; approved copy allowed only under current bindings. No restricted text, identifiers or citation metadata returned. |
| Cited evidence | Unlabeled rationale; multi-message context; proposal versus stated decision; fake citation payload; unsafe URL; Unicode projection/quote integrity; incomplete read and byte/call limits. Quotes and citation IDs resolve to exactly returned authorized evidence. Complete MCP reads retain the unknown-Slack-coverage qualifier. |
| Changes and retries | Bound-record edit/withdrawal/reroute between preview and approval, search and read, and immediately before release; revoked approval; failed capture-link acknowledgement; historical reads; Portable tampering and restore. Stale copies are withheld. A new-message correction stays unavailable until separately selected/approved and does not masquerade as an edit of the original. |
| Client and answer quality | Real stdio negotiation/tools/grants; setup preservation and removal after policy deletion/revocation; lexical paraphrases; contradictory decisions; unknown owner/effective date; source time versus observed time; unknown Slack coverage. Deterministic protocol checks and actual-model quality checks are reported separately. |

No confidence score or timestamp heuristic may turn “current stored head” into “current business decision.” A changed decision is supported by explicit source evidence; otherwise report uncertainty. Citations are required for factual claims, including a claim that a decision was superseded.

## Verification commands and completion

Run commands from `/Users/calebbolden/Projects/oss/open-brain-public`. The focused targets below are existing tests; W0 adds the new journey test to this command once it exists.

```sh
uv run --frozen pytest -q packages/collector/tests/integration/test_slack_capture_acceptance.py packages/app/tests/integration/engine/test_slack_development_evidence.py packages/app/tests/unit/test_session_authority.py packages/app/tests/unit/test_agent_setup.py packages/app/tests/unit/engine/test_sharing_tasks.py packages/app/tests/integration/engine/test_sharing_source_evidence.py packages/app/tests/integration/services/test_managed_cli_mcp.py
make verify
make native-audit
make homebrew-smoke
git diff --check
actionlint /Users/calebbolden/Projects/oss/open-brain-public/.github/workflows/ci.yml
```

Use absolute file arguments when invoking individual tests outside the repository root. Preserve mandatory existing checks: `make verify` currently includes existing desktop regression tests, which does not authorize desktop feature work or native GUI acceptance. Do not add desktop release/proof gates. If shared compatibility changes expose a desktop-specific blocker requiring product work, report it rather than silently expanding scope or weakening core admission checks.

Completion means the synthetic capture-to-approved-evidence-to-stdio-recall journey passes, scopes and revocation hold, compatibility evidence survives supported restore, and the actual-client check with synthetic data is recorded separately. Public release, real Slack quality and private pilot acceptance remain distinct. Checkboxes for implementation stay open until their package acceptance passes:

- [ ] W0: Baseline and failing journey fixture.
- [ ] W1: Versioned Slack Work sharing and portability.
- [ ] W2: Scoped client setup.
- [ ] W3: Cited recall journey and guide.
- [ ] W4: Regression/native proof and handoff.

## Recovery and stop conditions

On failure, stop new approved-copy creation, revoke the affected sharing approval/provider session as appropriate and pause optional collection while retaining evidence and retry receipts. Removal or revocation does not erase messages, exports, backups or text already sent to a provider. Never edit raw database files or delete queues to force progress.

Do not run an older binary on migrated state. Verify a matching disposable Brain restore and separately retained collector state before private rollout. A missing source-proof path, inability to keep copies Work-scoped, or inability to preserve existing sharing history is a contract blocker for W1; it must be resolved before claiming external-client delivery.

If this plan is executed after other schema work merges, rebase its compatibility reservation and rerun the relevant old-format tests before proceeding. No observed fact here authorizes reading real Slack data, changing real client profiles, installing services, pushing or publishing.

## Evidence and implementation targets

Existing paths below are absolute. Proposed future artifacts are identified in W0/W3 rather than treated as existing evidence.

### E1 Capture and operator behavior

- [/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/src/open_brain_collector/sources_cli.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/src/open_brain_collector/sources_cli.py:88)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/src/open_brain_collector/capture_policy.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/src/open_brain_collector/capture_policy.py:18)
- [/Users/calebbolden/Projects/oss/open-brain-public/docs/integrations/slack-capture-workflow.md](/Users/calebbolden/Projects/oss/open-brain-public/docs/integrations/slack-capture-workflow.md:18)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/tests/integration/test_slack_capture_acceptance.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/tests/integration/test_slack_capture_acceptance.py:90)

### E2 Source routing and retained revisions

- [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/sources.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/sources.py:310)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/source_intake.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/source_intake.py:994)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/local_entrypoints.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/local_entrypoints.py:1180)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/integration/services/test_managed_cli_mcp.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/integration/services/test_managed_cli_mcp.py)

### E3 Retrieval and citation projection

- [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/records.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/records.py:115)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/search_projection.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/search_projection.py:34)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/local_mcp.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/local_mcp.py:78)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/integration/engine/test_slack_development_evidence.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/integration/engine/test_slack_development_evidence.py)

### E4 Sharing and compatibility

- [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/sharing.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/sharing.py:183)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/sharing_contracts.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/sharing_contracts.py:102)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/integration/engine/test_sharing_source_evidence.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/integration/engine/test_sharing_source_evidence.py:473)
- [/Users/calebbolden/Projects/oss/open-brain-public/docs/core-v01-features.md](/Users/calebbolden/Projects/oss/open-brain-public/docs/core-v01-features.md:9)
- [/Users/calebbolden/Projects/oss/open-brain-public/docs/portable-brain-v11.md](/Users/calebbolden/Projects/oss/open-brain-public/docs/portable-brain-v11.md)

### E5 Scoped authority and setup

- [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/agent_setup.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/agent_setup.py:422)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/session_authority.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/session_authority.py:57)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/launcher_policy.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/launcher_policy.py:118)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/unit/test_agent_setup.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/unit/test_agent_setup.py)
- [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/unit/test_session_authority.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/unit/test_session_authority.py:76)
