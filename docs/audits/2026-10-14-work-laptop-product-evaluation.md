# Open Brain work laptop product evaluation

Date: 2026-10-14

Reviewed baseline: `d5cf64e652366dc14a8cf18732f7cb1e82ae4187`

Status: Product recommendations, not an implementation plan or release certification.

## Recommendation

Make Open Brain a desktop work memory assistant for technically confident knowledge workers. Its daily value should be answering **why a decision was made**, showing **what customer feedback actually supports**, and recovering **what someone committed to do**. A short daily brief should bring important changes and unfinished work back into view.

The strongest next investment is turning the existing storage, evidence, capture and review machinery into these workflows. Open Brain already has useful foundations; it needs a more complete path from connecting a work source to getting a trustworthy answer. Adding more source descriptors or a larger graph would not close that gap.

Keep the local foreground core, optional collector, SQLite, Portable Brain, CLI/MCP and current LLM connection model. Make the desktop the ordinary entry point. Keep personal use available through optional, separately scoped capture, with work-only retrieval and briefs by default.

## Confirmed product direction

The interview established these constraints:

| Area | Direction |
|---|---|
| Audience | Technically confident knowledge workers, including technical PMs, program managers, product operations, analysts and technical leads. Everyday use should have a straightforward desktop experience. |
| Main outcomes | Recover decision rationale; connect feedback into traceable evidence; surface commitments and unresolved questions. |
| Sources and delivery | Prioritize Slack, email, meeting transcripts and Jira. Provide cited answers on demand and a short daily brief. |
| Personal use and models | Personal capture is optional and separately scoped. Default work searches and briefs exclude personal content. Preserve existing LLM connection choices. |
| Action authority | Read, connect evidence and draft suggestions first. Require approval before sending messages, changing Jira tickets or making commitments for the user. |

This direction is a product hypothesis from one owner interview. It is sufficient to focus the next pilot, but does not establish market demand or willingness to pay.

## What exists today

The repository has more implemented capability than its supported public onboarding offers. Treat implementation, installed packaging, permission grants, provider readiness and release acceptance as separate states.

| Capability | Observed baseline | Product implication |
|---|---|---|
| Desktop | Search, capture, source controls, review/publication and agent setup exist. Distribution remains a contributor build with public acceptance gates. | Improve and release the existing app rather than designing a replacement from scratch. [E1, E2] |
| Work sources | The optional live runtime handles Gmail, Drive, Slack and selected coding-agent sessions. Jira and meeting-transcript adapters convert host-supplied data, but are not live providers in that runtime. Public source onboarding remains gated. | The requested four-source experience is incomplete, rather than wholly unimplemented. [E3, E4] |
| Retrieval and evidence | Paged lexical retrieval, complete reads, retained revisions, duplicate/contradiction/supersession relationships, facets and source-bound proposals exist. Desktop search returns records rather than synthesized answers. | Build answer and workflow views over the existing evidence model. [E5, E6, E7] |
| Capture reliability | Durable retry, checkpointing, pause and recovery mechanisms exist. Health distinguishes completed runs from partial coverage, but the desktop does not expose the full health operation. | Surface and validate existing reliability data before adding another scheduler or sync engine. [E8] |
| Work and personal scope | Privacy tiers and enforced space scopes exist. Default read authority can include public, work and personal content; the desktop has no default Work scope. Selective forgetting remains deferred even though retirement and some custody-discard controls exist. | Safe defaults and understandable retention/removal are adoption gates. [E9, E10] |

One retrieval detail matters: the current versioned search path uses lexical relevance tiers followed by record type and ID. An older path uses BM25. Evaluate the actual desktop/MCP path rather than assuming older ranking guarantees carry over. `hybrid_required` currently returns `model_unavailable`; semantic graph refresh is a different capability. [E5]

## Assessment from five perspectives

| Perspective | What creates value | Main risk | Consequence for priorities |
|---|---|---|---|
| Daily work and product management | Recover context across tools without reconstructing a project by hand. | A plausible summary conceals missing evidence or an outdated decision. | Show rationale, source dates, later changes, uncertainty and linked evidence. |
| Desktop usability and adoption | Connect a source, ask a useful question and understand the answer without maintaining the system. | Raw IDs, developer OAuth setup and internal workflow vocabulary consume the time the product should save. | Guided onboarding and clear source health come before a wider feature menu. |
| Engineering and retrieval | Reuse immutable evidence, revisions, facets and scoped reads. | A parallel AI knowledge store drifts from the canonical records and their permissions. | Make generated answers, themes and briefs revision-bound derived views. |
| Employer IT and privacy | Explain exactly what is stored, read, sent to models and retained after disconnection. | Local installation is mistaken for permission to collect employer data or for encryption. | Provide a reviewable permissions/data-flow statement and enforce Work scope throughout the answer path. |
| Product focus and maintenance | Deliver a dependable cross-source memory loop with a small support surface. | Many adapters, publication tools and graphs create breadth without a recurring reason to return. | Measure useful work outcomes; defer breadth until the core loop earns repeat use. |

The product's useful distinction is continuity across conversations, customer evidence and execution records. A user should be able to move from a question to its evidence to an unfinished obligation. A second generic chat window is a weak reason to adopt another application.

## Top five changes

These are ordered by dependency and alignment with the interview, not a calculated RICE score. Reach, retention and delivery capacity have not been measured. Effort describes implementation breadth; provider approvals remain separate uncertainties.

| Rank | Change | Expected impact | Effort and principal risk |
|---|---|---|---|
| 1 | Deliver usable desktop onboarding and dependable Work-source intake. | Prerequisite for all three work outcomes. | High: packaging, production onboarding and live Jira/transcript paths; provider access may block adoption. |
| 2 | Add cited answers and a decision history view. | Directly delivers decision recall. | Medium to high: evidence selection, currentness and answer quality across sources. |
| 3 | Add a reviewable commitments and open questions view. | Helps users recover forgotten obligations. | Medium: ownership, dates, state reconciliation and false positives. |
| 4 | Add a customer feedback evidence explorer. | Turns scattered requests into useful product evidence. | Medium to high: customer identity, deduplication and misleading counts. |
| 5 | Add a short daily work brief. | Brings the other workflows into a repeatable habit. | Medium after earlier foundations; noise and stale inputs are the main risks. |

### 1. Desktop onboarding and dependable source intake

Guide the user through selecting an account, approving a narrow scope, previewing content and asking a first question. Replace raw resource IDs with recognizable account, channel, project and folder names. Show the difference between connected, collecting, current, incomplete and blocked.

Promote the existing Slack/Gmail implementation into a supported path. Complete live Jira and meeting-transcript intake using the existing adapters. Email provider and transcript platform remain pilot choices: Gmail is closest to the inspected runtime, while Microsoft 365 and specific meeting platforms need their own readiness evidence. Do not equate coding-agent session transcripts with workplace meeting transcripts.

Keep text/Markdown import as a useful fallback when a source API is unavailable or an employer allows exports but restricts integrations. Start with existing transcript exports; recording meetings or deploying a meeting bot is a separate feature with different permissions.

**Acceptance:** A new pilot user can install the desktop, select one permitted work source and retrieve its evidence without editing config files. A proposed onboarding target is ten minutes after required account permissions are available. The UI reports coverage, retry/backlog and last successful observation, including partial runs. Source enablement and background installation remain separate choices. [E1–E4, E8]

### 2. Cited answers and decision history

Answer “Why did we drop self-serve onboarding?” with the decision, stated rationale, alternatives, constraints, author or owner when known, source date, supporting excerpts and later changes. Distinguish a proposal from an accepted decision. Show disagreement rather than choosing a winner merely because one message was ingested later.

Reuse source identities, immutable revisions and explicit relationships. Existing harness decision-currentness logic is a useful limited precedent, not a universal truth detector. Keep ingestion time separate from source time and the date a decision took effect. [E6]

Improve candidate retrieval against realistic questions before selecting an embedding architecture. Query expansion through an already authorized model and complete source reads are reasonable first steps. The current graph adapters return graph-specific results; desktop question answering still needs an explicit task contract and execution path. Keeping provider choices unchanged does not make that feature already implemented.

**Acceptance:** Each factual answer claim points to supporting evidence. Changed decisions show their earlier state and later change. Missing or conflicting evidence produces an appropriately limited answer. Scope and source eligibility are revalidated before evidence is returned or sent to a model.

### 3. Commitments and open questions

Provide a small list of possible commitments and unresolved questions, each with a project, quoted evidence, proposed owner, due date when explicit, and state. Make “no owner identified” and “no date stated” valid results.

Separate detected suggestions from commitments the user has confirmed. Separate the user's commitments from someone else's. A request, hypothetical statement or Jira assignee is not automatically a promise. Let users dismiss, correct, snooze or link a suggestion to an existing ticket. Resolution should retain evidence, such as a later message or ticket transition.

The collector already extracts kinds including requested, proposed, committed, approved, decision and correction, and binds developments to supporting captures. Extend that evidence seam into a usable tracker rather than introducing another extractor and truth store. [E7]

**Acceptance:** Duplicate mentions produce one candidate with multiple sources. A correction or completed task updates the view without erasing history. Draft Jira updates and follow-up messages remain drafts until the user approves the exact action and destination.

### 4. Customer feedback evidence explorer

Group feedback by customer, project, topic and date. Show the original statement, what problem it describes, any requested solution and the linked evidence. Let a PM ask “Which customers struggled with onboarding, and what did they actually say?”

Use existing facets for grouping. Add reviewed identity/alias resolution where the same customer has different names across email, Slack and Jira. Facets alone do not establish identity. Show unknown identities instead of silently merging them. [E7]

Count distinct evidence and customers explicitly. A complaint copied into Slack, a transcript and a Jira ticket is not three independent customer requests. Expose duplicate handling and source coverage. Distinguish the observed sample from the whole customer base; a cluster of mentions is not a population-level demand estimate.

**Acceptance:** Every theme opens to its evidence. Users can split a mistaken cluster or correct a customer link. Repeated copies do not inflate independent-customer counts, and summaries preserve contrary feedback.

### 5. Daily work brief

Make Today a quiet entry point with up to five useful items: outstanding commitments, decisions awaiting resolution, meaningful decision changes or recurring feedback. Rank items by user relevance, urgency, explicit ownership and evidence. Let the user pin projects, dismiss irrelevant items and choose when to be notified.

Show what changed since the last brief and why each item appears. A brief should say when relevant sources are stale or only partially covered. “Nothing needs attention” is preferable to filling a quota with old summaries.

Generate on opening the app first. A closed-app scheduled brief can use the optional collector only after explicit background opt-in. A sleeping or offline laptop cannot promise an on-time brief; show the generation time and refresh when work resumes. Store the brief as a replaceable view with evidence bindings and source-health metadata.

Show known model usage and let the user bound background generation. Reuse unchanged summaries where their evidence and permissions remain valid, rather than resending an entire history every day. Keep storage growth and the selected history window visible alongside source health.

**Acceptance:** Users can inspect an item's sources and complete or dismiss it. Resolved items stop reappearing without new evidence. Unavailable model access preserves access to source records and the last clearly dated brief. Local viewing does not require publishing a canonical vault note or sending a message.

## Work laptop gates

These conditions apply across all five changes rather than becoming a separate backlog of optional features.

1. **Enforce Work scope throughout.** Keep work/personal domain selection independent of privacy tier. Use trusted tier and space constraints across discovery, evidence reads, facets, synthesis, brief caches and configured MCP policies. Existing owner-authority operations need an explicit scope audit. A filter hidden in the search form is insufficient. This is application scoping, not isolation from another process running as the same OS user. [E9]
2. **Explain and complete removal.** Distinguish pause, account disconnection, source retirement and selective removal. Current retirement preserves history; selective forgetting remains deferred. A removal design must cover source bodies, canonical derivatives, history, indexes, pending collector custody and generated summaries. Explain that prior exports, backups and provider-held copies have separate lifecycles. Do not describe retirement as erasure. [E10]
3. **Make operation bounded and visible.** Reuse existing recovery and coverage controls. Verify sleep/wake, prolonged offline operation, locked credentials, rate limits, disk pressure, restart and upgrade with representative selected data. Proposed pilot targets are an idle/offline app with no recurring failed-request loop and a brief a user can scan in two minutes; CPU, memory, battery and storage budgets need measured baselines before release promises.
4. **Make employer review practical.** Provide an exact release identity, storage locations, requested scopes, model egress explanation, background behavior and uninstall/retention instructions. Local storage remains protected by the OS account and disk; Open Brain does not currently claim application-level encryption. [E11]
5. **Preserve approval and source authority.** Read-only source access should suffice for initial value. Treat source content as untrusted data, not instructions. Require current evidence and explicit approval for outward changes. Work-scope changes must not silently change provider selection or consent.

Provider onboarding is a product dependency. Gmail classifies `gmail.readonly` as restricted, with verification requirements and applicable exceptions; “read only” does not eliminate production onboarding work. [Google Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes). Jira scopes do not override the user's underlying project permissions, and read/write scopes are separate. [Atlassian Jira scopes](https://developer.atlassian.com/cloud/jira/platform/scopes-for-oauth-2-3LO-and-forge-apps/).

Slack history throughput depends on the app's distribution category: the stricter documented limits apply to new commercially distributed non-Marketplace applications, while Marketplace and internal customer-built apps retain Tier 3 treatment. Confirm the intended installation category before promising a history backfill time. [Slack conversations history](https://docs.slack.dev/reference/methods/conversations.history/).

Platform fit is an early pilot gate. Current native targets are macOS arm64 and Linux x86_64; the reviewed product does not offer a supported Windows distribution. Start with users on supported hosts rather than assuming work-laptop reach. A Windows port is a valid larger investment if the target cohort needs it, pending verification of native packaging, credential storage and the collector's Unix-specific control transport. [E1, E8]

## What to remove or defer

These are proposed product and prioritization changes. They do not authorize deleting existing code or weakening safety checks.

| Change | Recommendation | Reason |
|---|---|---|
| Remove technical setup from ordinary use | Move raw Space IDs, credential references, fixture-style collector configuration and second-based schedules into advanced/operator controls. | Technically confident users should not need to understand internal contracts to recover a decision. |
| Remove unavailable choices and misleading success signals | Hide unavailable hybrid modes from the ordinary UI; expose them with clear readiness in advanced settings. Replace “connected/completed means healthy” messaging with source-specific coverage. | An attractive control that cannot work or implies complete evidence reduces trust. |
| Remove publication as a prerequisite for value | Keep evidence searchable after authorized capture. Reserve canonical-note review for publishing curated knowledge; keep external-action approval separate. | Everyday recall should not require maintaining an Obsidian vault or approving every captured record. |
| Defer source and visualization breadth | Keep graph/Canvas and Obsidian optional. Pause expansion beyond the four work-source priorities, except dependencies such as Drive-hosted transcripts. Retain existing agent-memory integration without making coding-session capture the main onboarding journey. | Focus development and support on the selected work outcomes. |
| Defer larger product expansions | Autonomous outward actions, team-wide hosted knowledge, cross-device synchronization, meeting recording bots and a new encrypted-custody product should remain separate decisions. | They introduce operational and authority boundaries that the immediate work-memory product does not need. |

The graph is already an optional capability, not the current desktop's main navigation. Keep it optional; do not manufacture a graph-first problem or remove useful structural links. Similarly, retain provenance, scoped grants, conflict handling and portability even when their UI becomes simpler.

## Architecture choices

| Option | What it delivers | Trade-off |
|---|---|---|
| Existing MCP clients plus work-scoped views | Use a compatible client and reusable prompts for cited questions; expose saved Work selections and evidence reads. | Fastest way to test answer usefulness while preserving model choice. It does not alone deliver a self-contained desktop brief or complete onboarding. |
| Complete target architecture, recommended incrementally | Keep the engine as canonical evidence. Add revision-bound decision, commitment, feedback and brief projections; one scoped evidence-selection layer; desktop task execution; optional collector scheduling; and existing credential/consent boundaries. | More application work, but answers, briefs and action drafts share currentness and authority rules. Avoids separate stores disagreeing about what is true. |

The complete target does not require a universal entity graph, vector server or a replacement storage engine. Reuse facets and development evidence first. Add stable customer/project aliases where real cross-source identity problems demand them.

Hybrid passage retrieval remains an architectural option, pending representative recall, latency, model availability and laptop resource measurements. If justified, bind chunks to source revisions, apply authority before model input and returned evidence, invalidate changed or removed sources and preserve lexical fallback. Benchmark the current paged path first.

Preserve the two existing model relationships: compatible local-stdio MCP clients choose their own model, while Open Brain's managed graph inference uses explicit supported API adapters and consent. New answer/brief tasks should extend that model, with explicit content and task bounds, rather than implying any raw LLM endpoint is already a supported direct adapter. No subscription-transport workaround is part of this recommendation. [E11]

## Personal use worth preserving

Quick capture, private project decisions, household follow-ups, personal research and portable export can reuse the same foundation. Personal capture should be opt-in, have a recognizable destination and appear only after an explicit personal-context switch.

Keep work-only defaults in mixed installations, including derived summaries and connected clients. Personal use does not imply that an employer-managed laptop is a private device. Do not make personal-account connection, iMessage, health or finance tracking prerequisites to work value.

## Delivery sequence and evidence of value

1. **Validate a narrow usable loop.** Start with a signed desktop candidate, one permitted source and synthetic then authorized pilot data. Test account selection, evidence recovery, Work scope, source-health display and documented retention. Settle actual mailbox and meeting platforms before committing to four production connectors.
2. **Prove decision recall.** Exercise the same desktop/MCP retrieval path users will invoke. Build synthetic scenarios spanning the four source types, including paraphrases, changed decisions, duplicates, contradictory evidence and source revocation. Run them in every answer-path release check.
3. **Add commitments and feedback views.** Reuse developments, relationships and facets. Pilot with roughly five technically confident knowledge workers over two working weeks; this is a proposed discovery sample and duration, not statistical validation.
4. **Add Today after the evidence is useful.** Compare on-demand use with brief-assisted use. Validate resume/offline behavior before closed-app scheduling, and establish retention/removal acceptance before broad source rollout.
5. **Expand only where observed failures justify it.** Choose additional email/transcript providers, semantic retrieval or wider personal features from pilot evidence rather than source-count targets.

Track successful work outcomes: a decision recovered with correct evidence, a genuinely useful feedback connection, or an obligation recovered in time. Measure baseline reconstruction time and repeat the same task with Open Brain. Also record citation correctness, missed evidence, false commitments, duplicate inflation and recurring dismissed brief items.

Proposed hard release gates are zero work/personal scope leaks in the synthetic acceptance set and supporting evidence for every factual generated claim. Treat these as gates to test, not guarantees already achieved. A brief opening or message count alone is not evidence of value.

## Evidence and limits

This assessment combines the confirmed interview direction with source and contract inspection from desktop, source/operations and retrieval perspectives. It does not claim a native GUI acceptance run, live employer-source validation, an installed-artifact audit, or user research beyond the interview. Provider documentation was checked on 2026-10-14. Code presence and synthetic test cases establish implementation intent and coverage, not current production readiness.

First-use documentation also drifts from current code: some guides still name schema/session `10/5`, while the feature matrix and native runtime name `16/11`. Reconcile those documents before using onboarding completion as a release criterion. [E1, E2]

The highest-priority open questions are which mailbox/transcript systems pilot users have, whether their organizations permit selected-source collection and existing model use, which installation platforms they need, and what removal/retention conditions they require. These are pilot eligibility questions; the interview's agreed product direction remains intact.

### Local evidence index

Paths below are absolute and refer to the reviewed checkout. Line anchors identify the relevant region; the reviewed commit above fixes the baseline.

- **E1, product readiness:** [/Users/calebbolden/Projects/oss/open-brain-public/docs/core-v01-features.md](/Users/calebbolden/Projects/oss/open-brain-public/docs/core-v01-features.md:9) and [/Users/calebbolden/Projects/oss/open-brain-public/packages/desktop/README.md](/Users/calebbolden/Projects/oss/open-brain-public/packages/desktop/README.md:69).
- **E2, actual desktop experience:** [/Users/calebbolden/Projects/oss/open-brain-public/packages/desktop/src/App.tsx](/Users/calebbolden/Projects/oss/open-brain-public/packages/desktop/src/App.tsx:9), [/Users/calebbolden/Projects/oss/open-brain-public/packages/desktop/src/SearchPanel.tsx](/Users/calebbolden/Projects/oss/open-brain-public/packages/desktop/src/SearchPanel.tsx:128), [/Users/calebbolden/Projects/oss/open-brain-public/docs/first-use.md](/Users/calebbolden/Projects/oss/open-brain-public/docs/first-use.md:25) and [/Users/calebbolden/Projects/oss/open-brain-public/packages/desktop/src-tauri/src/runtime.rs](/Users/calebbolden/Projects/oss/open-brain-public/packages/desktop/src-tauri/src/runtime.rs:14).
- **E3, live sources and public boundary:** [/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/src/open_brain_collector/live_manager.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/src/open_brain_collector/live_manager.py:87) and [/Users/calebbolden/Projects/oss/open-brain-public/docs/integrations/core-sources.md](/Users/calebbolden/Projects/oss/open-brain-public/docs/integrations/core-sources.md:17).
- **E4, existing adapters and source UI:** [/Users/calebbolden/Projects/oss/open-brain-public/packages/connectors/src/open_brain_connectors/runtime/gitlab_jira.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/connectors/src/open_brain_connectors/runtime/gitlab_jira.py:574), [/Users/calebbolden/Projects/oss/open-brain-public/packages/connectors/src/open_brain_connectors/runtime/meeting_transcript.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/connectors/src/open_brain_connectors/runtime/meeting_transcript.py:310) and [/Users/calebbolden/Projects/oss/open-brain-public/packages/desktop/src/SourceCapturePanel.tsx](/Users/calebbolden/Projects/oss/open-brain-public/packages/desktop/src/SourceCapturePanel.tsx:194).
- **E5, actual paged retrieval and graph distinction:** [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/paging.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/paging.py:284) and [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/catalog.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/catalog.py:269).

Further evidence:

- **E6, source and decision continuity:** [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/records.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/records.py:91), [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/relationships.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/relationships.py:88) and [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/harness_retrieval.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/harness_retrieval.py:174).
- **E7, grouping and development evidence:** [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/facet_queries.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/facet_queries.py:176), [/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/src/open_brain_collector/capture_contracts.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/src/open_brain_collector/capture_contracts.py:12) and [/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/src/open_brain_collector/capture_workflow.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/src/open_brain_collector/capture_workflow.py:368).
- **E8, coverage and desktop boundary:** [/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/tests/integration/test_capture_health.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/tests/integration/test_capture_health.py:150), [/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/src/open_brain_collector/live_manager.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/collector/src/open_brain_collector/live_manager.py:287) and [/Users/calebbolden/Projects/oss/open-brain-public/packages/desktop/src-tauri/src/collector.rs](/Users/calebbolden/Projects/oss/open-brain-public/packages/desktop/src-tauri/src/collector.rs:14).
- **E9, default authority and enforced scopes:** [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/t03_contracts.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/t03_contracts.py:49), [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/paging.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/paging.py:93) and [/Users/calebbolden/Projects/oss/open-brain-public/docs/privacy-model.md](/Users/calebbolden/Projects/oss/open-brain-public/docs/privacy-model.md:65).
- **E10, retirement and deferred forgetting:** [/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/source_lifecycle.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/engine/src/open_brain_engine/engine/source_lifecycle.py:128) and [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/catalog.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/catalog.py:51).

Model and storage boundary:

- **E11, preserved product and provider contract:** [/Users/calebbolden/Projects/oss/open-brain-public/docs/product-family.md](/Users/calebbolden/Projects/oss/open-brain-public/docs/product-family.md:70) and [/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/managed_providers.py](/Users/calebbolden/Projects/oss/open-brain-public/packages/app/src/open_brain/services/managed_providers.py:188).
