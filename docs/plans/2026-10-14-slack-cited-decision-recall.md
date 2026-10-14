# Slack capture to cited decision recall implementation plan

Date: 2026-10-14

Status: Reviewed implementation plan, hardened 2026-10-14 (see the adjacent `.hardening.md` report). Implementation and live deployment have not started.

Planning baseline: `e444526`; application code baseline: `d5cf64e652366dc14a8cf18732f7cb1e82ae4187`.

Product direction: [/Users/calebbolden/Projects/oss/open-brain-public/docs/audits/2026-10-14-work-laptop-product-evaluation.md](/Users/calebbolden/Projects/oss/open-brain-public/docs/audits/2026-10-14-work-laptop-product-evaluation.md).

## Outcome

An owner selects a Slack channel, captures a bounded conversation, and asks an existing MCP client, “Why did we keep invite-only onboarding?” The client returns a limited answer with exact supporting quotes, record/revision references and approved Slack links. Edits or withdrawals of bound source records that the Brain has observed, through an owner-run re-import, invalidate their old evidence copies. Without a re-import, Slack changes after each quote's observation time are unknown, and the answer says so. A correction posted as a new message needs separate selection and approval; the answer does not claim to include unseen later messages.

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
2. **Select the Work evidence.** The owner resolves the returned capture IDs to logical sources, inspects each source and routes the selected conversation records into one existing or explicitly created Work space. Repeated intake uses the logical source route. Additional new messages need their own owner routing in this first slice. When several messages form one context, the owner creates a selection listing their logical sources.
3. **Inspect and approve provider release.** A local sharing preview freezes selected raw revisions, exact projected text, safe citation metadata, Work space and allowed provider IDs. The owner inspects and approves it. Separate Work-tier copies become eligible only under a matching external-provider session and current consent. No collection or model client can grant that approval.
4. **Recall through existing tools.** A client configured with search/content-read grants and the Work policy finds the approved copies and reads their complete text. It treats their contents as untrusted evidence, distinguishes proposals from stated decisions and cites every factual answer claim. The CLI supplies equivalent search/read commands for inspection.
5. **Refresh and recover.** An edit, withdrawal or route change to a bound record, sharing revocation or provider-consent change prevents further release of its stale evidence copy. Upstream Slack edits and deletions become local events only when an owner-run preview/import observes them; this slice adds no polling. A changed raw revision requires a new approval; it is never automatically shared. A newly posted correction has a separate source identity and does not automatically invalidate an older message. The owner must select/approve the new evidence and revoke an obsolete approval when replacing it. Every answer states that Slack coverage beyond selected approved messages is unknown.

The Work space is the domain boundary; privacy tier and external-processing consent are separate controls. Owner CLI access remains trusted local access, not a confinement claim. A model using a network-backed client must never be configured as `owner_local` to bypass an external-release refusal. At this baseline an MCP session started without `--session-policy` is `owner_local`, reads `public`, `work` and `personal` tiers and has no space restriction ([t03_contracts.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/t03_contracts.py:57)). A pre-existing unscoped `agent setup` fragment for a network-backed client can therefore read imported raw Slack records. This slice does not change those defaults; W2 and the operator guide make their removal a precondition of the workflow. Raw originals stay local-only only while no unscoped MCP session for a network-backed client is configured. W2 detects this in the configuration files `agent setup` manages; other clients and project directories remain the owner's responsibility.

Provider identity is the owner-declared `provider_id` in the launcher policy and consent store. The Brain checks it at configuration and on every operation, but it does not control the client's network endpoint. A client launched against a proxy or another endpoint under the same declared provider is outside the Brain's enforcement. Provider-mismatch tests prove declared-identity refusal only.

## Contracts to implement

### Slack sharing and citation envelope

Extend sharing with a new explicitly versioned request/evidence variant. Preserve saved-Markdown v1 bytes, public-copy semantics, validators and retry behavior. Do not change the existing `_copy_privacy()` behavior globally.

The new Slack preview must bind these groups:

| Group | Required binding |
|---|---|
| Source proof | Brain/issuer identity; Slack connector/account/channel/message identity proven by canonical intake receipts; logical source ID; raw capture/revision ID; current head, lifecycle and route versions; source, payload, privacy and provenance digests. |
| Selected content | Exact current projected message text, projection version and digest. The first slice shares complete bounded message projections rather than arbitrary owner-edited summaries. Select several records explicitly for thread context. When several records form one context, the owner first creates an engine-owned selection: a selection ID and an ordered list of member logical source IDs, at most 24. Each member's preview references that selection; approvals remain per message and bind each member's own revision. Membership is by logical source, not revision, so re-approving one edited member restores the selection without touching the others. A selection is grouping metadata, not a batch approval. |
| Citation metadata | Original record/revision identifiers; source timestamp when proven; observation time separately; upstream currency `unknown_since_observation`; validated Slack permalink or explicit unavailability; third-party/unverified label; fixed scope `selected_owner_approved_messages` and Slack coverage `unknown`. Freeze the exact metadata the owner approves. |
| Destination authority | Exact Work space, copy tier `work`, `external_egress=true` only for the approved copy, named permitted providers, preview expiry and inspection/decision digest. Source privacy remains unchanged. The copy capture carries its own `PrivacyDecision` (tier `work`, reason `policy_work`, `external_egress=true`, a new copy policy version), which the existing model accepts ([models.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/core/models.py:180)) and the search projection stores per record as `effective_external_egress`. Work tier itself is not made egressable; originals keep their own local-only decision. |
| Retry and visibility | Frozen operation IDs, approval/copy linkage, replay receipts, current-source checks and revocation. No copy becomes externally visible before the complete evidence/linkage reservation is valid. Source, route, lifecycle, approval and revocation state are checked in the same read snapshot (`read_snapshot` in `paging.py`) that produces the returned bytes. A change committed before that snapshot refuses the read; one committed after it does not retract that response. Launcher policy and provider consent are revalidated again at the MCP output boundary ([local_mcp.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/local_mcp.py:888)), so a consent or policy change committed before the response crosses that boundary still refuses it. |

Use a versioned text envelope, provisionally `slack-recall-evidence.v1`, inside the new approved copy. Existing complete-read tools can return it as untrusted text; a new generic permalink-read tool is unnecessary. Validate reserved fields before retaining it. Project envelope fields separately before serializing valid JSON, following the existing structured harness-citation precedent. If projection invalidates a citation field, withhold that field with an explicit reason; do not bypass redaction to preserve framing. Read-time projection uses the same field-wise projector, and the permalink exception lives inside it as a field-level allowance keyed to this envelope variant and its citation field, applied identically at preview, approval and read. Projecting the approved envelope bytes again must reproduce the approved digest; a test proves that idempotence. If read-time output still differs from the approved digest, for example after a projection-version change or a new protected value, withhold the copy with an explicit `reapproval_required` reason. Never return the stored bytes unprojected. A message that contains fake citation JSON remains quoted message content, never envelope authority. The copy record/revision a client may cite, and any selection membership, come from the read response's structured, engine-generated fields, never from envelope text. Each selected raw message has its own revision-bound approval; a new batch-approval protocol is unnecessary. The engine-owned selection lets recall detect a partial context without one.

For a copy whose original belongs to a selection, the existing `brain_read` response adds structured fields computed inside the same read snapshot under the session's authority: selection ID, member count, this member's ordinal, and for each ordinal either the currently visible approved copy record/revision or `unavailable`. This extends an existing response; it is not a new tool, and it uses no lexical search over selection tokens.

The permalink must come from validated Slack connector identity/evidence, use an allowed HTTPS Slack message-link shape, and contain no credentials or arbitrary note-supplied destination. Parse it; the connector's existing `"slack.com" in permalink` substring check is not sufficient. At this baseline a Slack capture's `source_reference` is its permalink ([source_intake.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/connectors/src/open_brain_connectors/runtime/source_intake.py:121)). Releasing it is therefore a deliberate exception to the source-reference projection, limited to the owner-approved citation field of this envelope variant. The message-text field keeps the ordinary protected-value removal, and the global projection stays unchanged. Approval must cover the permalink because workspace/channel identity is metadata disclosure too. Do not pass other source references, provider responses, local paths or tokens through the envelope.

The client cites the **authorized copy's** record/revision and the quote it actually read. The original record/revision and permalink provide provenance; neither is authority to read the local-only original. Message time, ingestion time and decision effective time must remain distinct. An absent effective date or identified decision owner stays unknown.

Collector coverage remains an owner-local diagnostic in this slice. Do not import private collector state into the core or infer Slack completeness from an MCP completion flag. The client always reports “Based on selected approved messages as observed at their capture times; later Slack edits, deletions and messages are unknown.” A successful complete read only means those returned bytes are complete.

### Scoped client setup

Add proposed `--session-policy` and `--consent-state` options to `agent setup` preview/apply/remove, forwarding the existing MCP options. These setup flags do not exist at the planning baseline. Reuse `launcher-policy.v1` and the durable consent store; do not add a competing policy file.

For this workflow, the policy permits search/content-read, allows only the selected Work space and `work` read tier, has `egress_mode=external_provider`, and names the existing selected provider plus current consent. Grant no capture, organize, inbox, review, publication or history authority. Ordinary request filters can only narrow that authority.

The session can read every currently approved Work copy in that space for that provider, not only one recall's selection. Text inside an approved message can steer a tool-calling client to read any of those copies or to use the client's own non-Brain tools. The Brain guarantees only that nothing outside session authority is returned: raw originals, Personal records, other spaces, unapproved, revoked or stale copies, and copies for other providers. The guide recommends a dedicated release space when recall should be limited to one channel or topic. It also requires running recall in a client invocation where only the scoped Open Brain tools run without per-call approval, and shell, file-write and network tools are disabled or approval-gated by the client's own controls. That restriction is a client-side control the Brain cannot verify. This slice adds no per-recall allowlist or broker.

Preview scans the user-scope and selected project-scope configuration files `agent setup` manages for both supported clients. It reports every MCP entry that invokes the Open Brain runtime without a session policy, whether or not it carries the ownership marker. Scoped apply refuses while such an entry remains unless the owner removes it first or explicitly acknowledges it in the preview. Setup removes only owned fragments; it never edits an unowned entry.

For configuration, validate policy/consent identity and paths, bind their relevant preimages and effective authority to the preview, and revalidate them at configuration apply and every runtime operation. Refuse incompatible grants and missing/stale policy; never fall back to unscoped setup. Removal validates owned-fragment identity and configuration preimages without requiring active grants or readable policy/consent files, so revocation or deleted policy cannot prevent cleanup. Preserve unrelated client settings and model/provider selection. Policy content and credentials stay outside the repository and generated instruction text.

### Recall workflow and bounds

The initial client workflow is a documented task template, not a new model adapter or core inference API. It searches lexical variants of the question, reads complete candidate evidence and produces a cited answer only from returned text.

Proposed per-recall limits: at most three search variants, ten results per search, eight distinct approved evidence records and 64 KiB of assembled projected evidence. Reading a selection raises the record limit to its member count, at most 24, inside the same 64 KiB limit. These limits stay inside the existing per-call/session budgets. Enforce limits in the deterministic acceptance driver; client instructions request the same limits but do not claim to constrain a hostile same-user client.

Record every traversed page/read completion flag. When a read response reports selection membership, read every listed member copy. On truncation, stale cursor, revision change, budget exhaustion, or any member reported `unavailable` or left unread, return an incomplete-evidence result rather than “no decision.” Currentness means current retained evidence under current authority, not exhaustive or live Slack coverage. When sources conflict or fail to establish acceptance, the answer must retain that uncertainty.

## Ordered work packages

Implement one package at a time. Later packages depend on the earlier acceptance checks passing. Estimates assume one engineer familiar with this repository: approximately **8–14 working days**, excluding Slack app approval, private deployment and external-provider availability. W1a/W1b dominate because together they are a storage-format revision as well as a sharing contract.

### W0 Baseline and failing journey fixture

Estimate: 0.5–1 day. No prerequisite.

1. Pin the code and candidate catalog; collect metadata-only CLI help for source intake, routing, sharing, scoped MCP and agent setup. Keep installed/source mismatch explicit. Do not open the owner's Brain.
2. Add a synthetic fixture using the actual `PriorityRuntime`, `SlackSourceClient`, `LiveSourceManager`, engine and private collector state with an injected `LiveHttpTransport`. Include an unlabeled proposal, a rationale-bearing decision, an edit of that message and a separately posted correction, plus Personal and unrelated Work-space decoys.
3. Drive preview/import, owner source inspection/routing and ordinary CLI/MCP search/read. Establish existing duplicate/restart/routed-revision behavior. Capture the current external-release refusal and unsupported Slack sharing as expected baseline results.
4. Define the v2 sharing bytes, copy envelope and compatibility fixtures before production code changes. Make the new approved-copy/scoped-setup journey fail on the actual missing behavior, not a fabricated test-only shortcut.
**Acceptance:** Real Slack-shaped intake produces locally readable evidence; source updates preserve its route; external release remains refused. New requirements fail for the identified missing contracts. No provider or real account is used.

**Targets:** Add the future end-to-end test at [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/integration/services/test_slack_decision_recall.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/tests/integration/services/test_slack_decision_recall.py). Reuse the existing fixtures listed in E1–E5 rather than copying their implementation into a fake engine.

### W1a Versioned Slack Work sharing contract and visibility

Estimate: 3–5 days. Depends on W0.

1. Add an engine-owned Slack source-proof branch for the new sharing contract. Validate actual source-intake receipts, current canonical bytes and Slack identity; do not fabricate saved-Markdown receipts or weaken its existing proof checks.
2. Add frozen owner preview/inspection/approval/revocation and the Work-preserving copy envelope, including engine-owned selections, their structured read fields and the parsed-permalink exception. Reuse existing sharing lifecycle and exact retry principles. Route visibility of a pending or unassigned copy must fail closed until its approved Work membership is established.
3. Make all visibility and source-expansion paths enforce the approved source head/lifecycle/route, provider list and current consent. Changed or withdrawn originals and revoked approvals hide copies from new retrieval, including retained-history requests. Do not promise to retract content already delivered to a provider. Copy identity is currently keyed to the v1 marker and delivery prefix: `MARKER_PREFIX`, `_managed_copy_identity`, `sharing_eligible` and the `LIKE 'urn:open-brain:sharing-copy:v1:%'` predicate in `external_candidate_clause` ([sharing.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/sharing.py:950)), the paging callers and `portable/v7.py`. Extend each one for the new variant, and treat any unrecognized `urn:open-brain:sharing-copy:` marker as an ineligible managed copy. Otherwise a new-marker copy would pass the external candidate filter without its approval, revocation and source checks.
4. Append the required canonical compatibility boundary for the new persisted sharing state. Reserve the next free schema/runtime/Portable coordinates after rechecking main; expected successors at this baseline are `17/12/12`. Preserve earlier migrations and frozen formats. Do not silently reinterpret `16/11/11` sharing history.
5. Prove source-proof, retry, corruption and revocation behavior before adding client setup. Include barrier tests that commit a revocation or source edit just before and just after the read snapshot, and a consent revocation between the snapshot and the MCP output boundary.

W1a and W1b land as one merge unit. No W1a-only build may open, migrate or export a retained Brain, because the new persisted state has no Portable validator until W1b.

**Acceptance:** Explicit approval creates exactly one current Work copy for selected providers, with unchanged local-only originals. A crash or lost acknowledgement resumes the same copy. Unsupported sources, stale versions, provider mismatch, unrecognized copy markers and malformed evidence create no readable or searchable substitute. Saved-Markdown v1 sharing tests still pass.

**Targets:** Engine sharing/contracts, canonical source proof, source eligibility/projection, paging candidate filters, schema migration and owner CLI sharing dispatch. Exact existing paths are in E2–E4. Keep the existing source-intake protocol and collector authority unchanged unless W0 proves a real defect.

### W1b Compatibility and Portable evidence

Estimate: 2–3 days. Depends on W1a.

1. Add Portable registration, validation and restoration for the new sharing, selection and citation evidence at the coordinates reserved in W1a. Keep the existing v7 sharing validator strictly v1 ([v7.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/portable/v7.py:504)); add a validator gated on the new Portable version that accepts both variants. A rehashed older-version archive containing the new marker must be refused.
2. Verify export/import/reopen/re-export, including refusal of removed or substituted sharing/citation evidence after manifest rehashing.
3. Confirm old clients refuse the new state safely and supported old-format imports still pass.

**Acceptance:** New evidence survives supported restore unchanged, tampered evidence is refused, and every supported old format still imports.

**Targets:** Compatibility and Portable registration/validation/restoration listed in E4.

### W2 Preview-bound Work client setup

Estimate: 0.5–1.5 days. Depends on W1b.

1. Extend the setup request/service and owner CLI parser with the two policy/consent options. Forward them to the existing MCP invocation without adding a new transport.
2. Include selected Work scope, read grants, egress/provider identity and relevant file preimages in preview validation. Refuse stale apply, unsafe/missing files, another Brain's consent and attempts to combine this workflow with owner-authority write grants. Report and refuse a coexisting unscoped owned fragment for the same client as described under scoped client setup.
3. Generate concise evidence-use guidance for Claude Code and Codex without changing their selected model or unrelated configuration. Keep generic MCP use available through the existing manual invocation for other compatible clients.
4. Test preview/apply/remove, untouched defaults, changed policy/consent, revocation during a running session and ownership-preserving removal in synthetic client profiles. Removal must also succeed after policy deletion or consent revocation without restoring read access.

**Acceptance:** Installed fragments launch only the intended scoped session. Personal records, unrelated Work-space records and same-space local-only originals cannot be discovered, affect returned evidence, or be fetched by guessing IDs. Policy changes terminate the old session; reconnection does not reuse revoked consent. A coexisting unscoped fragment is reported and blocks scoped apply until removed or acknowledged. No owner grant or credential is introduced.

**Targets:** [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/agent_setup.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/agent_setup.py:422), CLI parser, setup DTOs/callers and existing setup/session tests in E5. Keep desktop setup callers on their old defaults; no desktop feature or release work is included.

### W3 Cited recall journey and operator guide

Estimate: 1–2 days. Depends on W2.

1. Add a headless operator guide covering selected-channel connection, bounded preview/import, Work source routing, local sharing inspection, approved copy discovery and scoped client setup. Derive IDs/versions from actual responses; label new request variants explicitly. Before the first Work Slack import, the guide has the owner remove or confirm the absence of unscoped Open Brain setups for network-backed clients. It explains that refreshing upstream edits requires an owner-run re-import, and that a dedicated release space limits what a scoped client can read.
2. Add a reusable decision-recall task template: stated decision, stated rationale, alternatives only when evidenced, source dates, later changes, unresolved disagreement, claim-level citations and the selected-message/unknown-Slack-coverage qualifier. Document separate owner approval of a new-message correction and explicit revocation when replacing an obsolete release. Do not label an unapproved proposal or unverified derived record as accepted truth.
3. Extend the journey fixture through a real stdio MCP subprocess: initialize, discover tools, search, read complete approved envelopes and follow their record/revision citations. Keep core/collector network calls mocked at the Slack transport boundary only.
4. Validate a deterministic answer fixture against exact returned quotes and allowed claim/evidence IDs. The validator resolves every cited copy record/revision against the IDs the engine returned in that session, and every quote as an exact substring of a returned message-text field. It refuses citations that resolve only to envelope-shaped text inside a message. Report it as protocol/citation plumbing, not proof of LLM reasoning quality. Add a separately invoked, owner-authorized actual-client check using synthetic data and the user's existing chosen model. The synthetic corpus includes a message instructing the model to run shell or network tools. The check runs under the guide's restricted client invocation and records whether any non-Brain tool was attempted. It records the client, the model as configured, the fixture digest, the answer and the citation-validator result privately outside the repository, as `recorded` or `not_run` with a reason. It does not gate repository completion. Real-client citation fidelity and injection resistance against a hostile message corpus are not proven by this slice.

**Acceptance:** The question about invite-only onboarding yields sufficient authorized evidence for the expected rationale with exact quote citations and approved links. Paraphrases exercise bounded query variants; ambiguous or insufficient evidence remains visibly unresolved. No message sending, publication or direct-provider inference is added.

**Targets:** The new end-to-end test, scoped setup guidance and a new guide at [/Users/calebbolden/Projects/oss/open-brain-public/docs/integrations/slack-decision-recall.md](/Users/calebbolden/Projects/oss/open-brain-public/docs/integrations/slack-decision-recall.md). Neither future path exists at this planning baseline.

### W4 Regression, native proof and handoff

Estimate: 1–2 days. Depends on W3.

1. Run the acceptance matrix below through real engine/source/sharing/MCP components, including same-process and cross-process revocation and correction boundaries. Fix failures within the agreed slice.
2. Run the repository's full verification and native gates on the final code candidate. Record exact commit/digests and platform limits. Core and optional collector remain separate distributions; do not make the collector a core dependency.
3. Update the feature matrix, catalog capability description, source/operator docs and compatibility notes for the actual resulting candidate. Clarify capture-versus-logical-source routing guidance without changing old receipt semantics. Keep live Slack and public-promotion claims separate from synthetic acceptance.
4. Provide the owner-run pilot checklist. First, before the candidate binary ever opens the real Brain: export a full Portable Brain with the currently installed binary, retain a copy of collector state, record both digests privately, and confirm the export restores into a disposable data directory with that same installed binary. Prefer running the pilot against such a restore. Then select one permitted small channel, inspect the captured evidence and every release preview, use the existing chosen client/provider, and record useful-answer/citation failures privately. No schedule or account is activated by implementation tests.

**Acceptance:** All required automated/native checks pass, old clients refuse unsupported state safely, and the guide exercises the exact candidate. Provider-backed workflow acceptance requires the approved-copy path; local-only recall alone cannot close this package. Actual Slack/provider access remains an explicit private deployment step.

## Acceptance matrix

| Group | Required scenarios and oracle |
|---|---|
| Capture and continuity | Real connector parsing and collector admission; duplicate import; lost acknowledgement; restart; routed source edit; new source unrouted; partial scan; rate limit; deletion only after complete coverage. Exactly one current source per identity and no collector routing authority. |
| Scope and release | Work space separate from tier; Personal/unrelated Work decoys; same-space local-only originals and copies approved only for another provider; no policy; missing/revoked consent; wrong declared provider; raw Slack read refused externally; approved copy allowed only under current bindings; message text attempting tool-use injection. No restricted text, identifiers or citation metadata returned outside session authority. |
| Cited evidence | Unlabeled rationale; multi-message context; proposal versus stated decision; fake citation payload, including a complete nested envelope naming fabricated IDs and a foreign permalink; unsafe URL; Unicode projection/quote integrity; incomplete read and byte/call limits. Quotes and citation IDs resolve to exactly returned authorized evidence. A selection with a withheld, revoked, stale or never-approved member yields incomplete evidence, and no claim rests only on the missing member. One member edited and re-approved restores the selection without re-approving the others. A selection larger than eight and up to 24 members is fully read. An approved message embedding a foreign selection ID or member count does not change the engine-reported membership. Complete MCP reads retain the unknown-Slack-coverage qualifier. |
| Changes and retries | Bound-record edit/withdrawal/reroute between preview and approval, search and read, and before and after the release snapshot; consent revocation between the snapshot and the MCP output boundary; revoked approval; failed capture-link acknowledgement; historical reads; projection-version change after approval (`reapproval_required`) and projector idempotence over approved bytes; Portable tampering and restore. Stale copies are withheld. A reaction-only re-import leaves the approved copy current. An upstream edit with no re-import still returns the old quote with its observation time and the upstream-unknown qualifier; after an owner re-import the stale copy is withheld. A new-message correction stays unavailable until separately selected/approved and does not masquerade as an edit of the original. |
| Client and answer quality | Real stdio negotiation/tools/grants; setup preservation and removal after policy deletion/revocation; coexisting unscoped fragment detection; lexical paraphrases; contradictory decisions; unknown owner/effective date; source time versus observed time; unknown Slack coverage. Deterministic protocol checks and actual-model quality checks are reported separately. |

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

`actionlint` is a repository handoff requirement that no Make target provisions; it must be on `PATH`. If it is missing, report the check as not run rather than claiming it passed.

Use absolute file arguments when invoking individual tests outside the repository root. Preserve mandatory existing checks: `make verify` currently includes existing desktop regression tests, which does not authorize desktop feature work or native GUI acceptance. Do not add desktop release/proof gates. If shared compatibility changes expose a desktop-specific blocker requiring product work, report it rather than silently expanding scope or weakening core admission checks.

Completion means the synthetic capture-to-approved-evidence-to-stdio-recall journey passes, scopes and revocation hold, and compatibility evidence survives supported restore. The actual-client check is recorded separately as `recorded` or `not_run` and does not block completion. Public release, real Slack quality and private pilot acceptance remain distinct. Checkboxes for implementation stay open until their package acceptance passes:

- [ ] W0: Baseline and failing journey fixture.
- [ ] W1a: Versioned Slack Work sharing contract and visibility.
- [ ] W1b: Compatibility and Portable evidence.
- [ ] W2: Scoped client setup.
- [ ] W3: Cited recall journey and guide.
- [ ] W4: Regression/native proof and handoff.

## Recovery and stop conditions

On failure, stop new approved-copy creation, revoke the affected sharing approval/provider session as appropriate and pause optional collection while retaining evidence and retry receipts. Removal or revocation does not erase messages, exports, backups or text already sent to a provider. Never edit raw database files or delete queues to force progress.

Do not run an older binary on migrated state. Verify a matching disposable Brain restore and separately retained collector state before private rollout; the pre-migration export from the W4 pilot checklist is the only route back to the installed release. A missing source-proof path, inability to keep copies Work-scoped, or inability to preserve existing sharing history is a contract blocker for W1a/W1b; it must be resolved before claiming external-client delivery.

If this plan is executed after other schema work merges, rebase its compatibility reservation and rerun the relevant old-format tests before proceeding. No observed fact here authorizes reading real Slack data, changing real client profiles, installing services, pushing or publishing.

## Unresolved owner decisions

Hardening left these risks as accepted limitations of using existing clients. The owner decides each one before W2 starts; the slice as written implements option A.

| Risk | A: current plan | B: stronger within this slice | C: proper architecture |
|---|---|---|---|
| Declared provider does not bind the real network endpoint | Documented limitation; provider checks prove declared identity only. | Bind the client's visible endpoint and proxy settings into setup preview and report overrides. Pending: which settings each client exposes. | A controlled egress broker with endpoint and TLS-host enforcement. It conflicts with the core's no-listener, no-daemon boundary, so it needs a separate distribution. |
| Untrusted Slack text can steer the client's own shell or network tools | Guide-mandated restricted client invocation; outcome recorded by the non-gating actual-client check. | Make a recorded injection-resistance check a gate for private pilot use, not for repository completion. | A deterministic broker that retrieves bounded evidence before model invocation. It conflicts with this slice's no-new-runner and no-recall-tool limits. |
| Unscoped `owner_local` sessions can read Work-tier Slack originals | Detection and refusal in managed configuration files plus an operator precondition. | Require an explicit session policy before any MCP session reads Slack-sourced Work records. It changes existing defaults and needs its own compatibility note. | Same as B, extended to every network-backed session class. |

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
