# Open Brain integrations assessment

Date: 2026-10-25

Reviewed baseline: `43baec2` (main, after follow-ups #119, the daily brief #120 and the path cleanup #121).

Status: product recommendations, not an implementation plan or release certification. Nothing here authorizes a new dependency, a listener, a scheduler in the core, or an outward write.

## Recommendation

Add integrations in the order the owner's three work outcomes need them, and reuse seams that already exist before building a new provider.

1. **Meeting notes through Drive.** Gemini's Meet notes are Google Docs in Drive. Drive capture already exports Docs as text, so the gap is folder selection and long-document handling, not a new provider. This turns meetings into a live source without a Meet or Zoom client.
2. **Slack back into the active milestone.** Slack threads are where the owner loses the most context, and the Slack source is fully built: OAuth, channel selection, replies, deletions, guided workflow, cited recall. The remaining work is provider and employer onboarding, not code.
3. **Jira and Confluence as live Atlassian sources.** Both have adapters for host-supplied payloads and no client. Follow-ups already link tickets by key; decisions live on Confluence pages. One Atlassian OAuth flow serves both.
4. **GitHub completeness and GitLab.** GitHub capture is live but fetches only the issues endpoint and stamps every capture `public`. Fix the tier before any private repository is selected, add comments, then give GitLab the same client its adapter is waiting for.
5. **VS Code as a configured client.** `agent setup` writes Claude Code and Codex configuration only. The owner also works in VS Code; its MCP configuration is a small writer, not a new transport.

Outward actions stay out until the connector contract's action-approval receipt exists. The one action the owner named, pushing a branch and opening a pull request, already belongs to the coding agent. Open Brain's part is to capture the resulting pull request as resolution evidence for the follow-up it closes and, later, to draft the pull-request text from that evidence. Drafts, not sends.

## What the interview established

| Area | Answer | Consequence |
|---|---|---|
| Chat and meetings | Slack and Google Meet. Meet conversations end up as Gemini notes in Docs and Drive. | Slack is the primary source. Meetings are reachable through Drive. |
| Tracking and documents | Jira, Confluence, Google Docs and Drive, GitHub and GitLab issues. | Atlassian is the second source family. Both code hosts matter. |
| Agents and editors | Claude Code and VS Code. Not Codex day to day, not Cursor. | Keep Codex support; add a VS Code writer; skip Cursor. |
| Delivery | Terminal agent session and Slack. | MCP already covers the first. Slack delivery needs the first outward write path. |
| Actions | Push and pull request. | The coding agent performs it; Open Brain records and drafts. Jira and Slack drafts rank below. |
| Most-lost context | Slack threads. | Confirms Slack over meetings as the first reactivation. |
| Personal sources | Not answered; assumed out of scope. | Work-only ranking. Personal capture stays as it is. |

This is one owner interview. It is enough to order the next pilot. It says nothing about other users' tool stacks.

## What exists today

Every source is optional and source-only; none ships in the core artifact, and the public onboarding claim covers Markdown import only (`docs/integrations/core-sources.md`). The states below are from the code on `43baec2`.

| State | Integrations |
|---|---|
| Live, fetches from the service | Gmail (one label, plain text), Google Drive (one file per selection, 60,000-character cap), Google Calendar (foreground CLI only, not in the collector), GitHub (selected repository, issues endpoint only, stamped `public`), agent sessions for Claude Code and Codex, Markdown import, saved Markdown roots, YouTube polling (library with no entry point). |
| Built, deferred | Slack (full source, excluded from the active milestone), local DOCX and text PDF. |
| Adapter only, no service client | Meet and Zoom transcripts, Jira, GitLab, Confluence, Notion, Microsoft 365 Mail, iMessage, web clips, the Hermes memory host, the outbox. |
| Absent | Cursor and VS Code session capture, Pi session capture, any delivery of briefs or answers to Slack or email, webhooks, every outward write. |

Delivery is the stdio MCP server with fourteen off-by-default grants, the Obsidian plugin, and the harness memory bridge for Codex, Claude Code and Pi. `agent setup` configures Claude Code and Codex.

No code writes to a third-party service. Every provider scope is read-only, and the only HTTP POSTs are OAuth token exchanges. The connector manifest reserves an `action_authorities` list, default empty, and the product contract requires an action-specific approval receipt for any outward action (`docs/v0-product-contract.md`, rule V0-REVIEW-09). The nearest existing approval model is review publication: propose, inspect, owner decision, under separate grants, writing only the local vault.

Two facts shape the engineering cost of any new live source. The collector's provider set is hard-coded to Gmail, Drive and Slack in `live_manager.py`, so adding a live source means editing the collector. And a live source must produce `SourceRecordIntake` values with a stable record key, a privacy tier that admission can only narrow, redaction through the engine, and a checkpoint that advances only after every item has a receipt (`docs/capture-contract.md`, `docs/integrations/collector-recovery.md`).

## Ranked candidates

Ranking weighs the owner's stated pain, which of the three outcomes the source feeds (decision recall, commitments, feedback evidence), how much of the seam already exists, and provider or employer onboarding risk. Effort describes implementation breadth. Nothing here has been measured against users.

### Capture sources

| Rank | Integration | Owner need | Existing seam | Effort | Principal risk |
|---|---|---|---|---|---|
| 1 | Gemini Meet notes via Drive folder selection | Meetings leave text only in Drive today. Feeds decisions and commitments. | Drive client, Docs export, revision identity. Missing: folder or "new files" selection, splitting documents over 60,000 characters. | Medium | Unconfirmed that Meet always saves notes as a Google Doc. Account-wide `drive.readonly` scope with selection narrowing capture. |
| 2 | Slack, reactivated | The most-lost context. Feeds all three outcomes. | Complete: OAuth, selection, replies, deletions, Work copies, cited recall. | Low in code | Slack app distribution class sets history rate limits; employer approval of a user-token app. Live acceptance has never run against a real workspace. |
| 3 | Jira live client | Tickets are where commitments and customer asks get recorded. Follow-ups link ticket keys already. | `JiraSourceAdapter` for issues and comments; descriptor. Missing: Jira Cloud REST client, OAuth 2.0 (3LO) or API token, checkpointing. | Medium | Atlassian app approval for an OAuth app; API-token fallback is per user and leaks nothing but needs a Keychain entry. |
| 4 | GitHub: private tier fix, comments, review comments | Code-host evidence for decisions and resolved follow-ups. | Live polling, device flow, `comment_from_rest` exists but is never called. | Low to medium | Captures are stamped `public` with external egress true. A private repository selected today would be misclassified. Fix before widening. |
| 5 | Confluence live client | Decision pages and specs for decision recall. | `WorkspaceContentSourceAdapter`; PKCE scopes documented, no flow. Shares the Atlassian OAuth app with Jira. | Medium | Page bodies are large; the 65,536-character intake limit needs a splitting rule shared with Drive. |
| 6 | GitLab live client | Second code host. | `GitLabSourceAdapter`; device-flow auth declared in the descriptor. | Medium | Self-hosted instances need a per-connection base URL; redirect and origin rules must stay fixed per connection. |
| 7 | Google Calendar in the collector | "Today" context for the brief. | Live CLI with sync tokens. Missing: `live_manager` wiring. | Low | None beyond the provider-set edit. |

Not recommended now: Notion (blocked on a confidential token exchange the repo forbids hosting), Microsoft 365 Mail and Outlook Calendar (removed from scope 2026-09-15; the owner does not use them), iMessage (unproven against the real schema; personal), web clips (no extension exists), YouTube wiring (no work outcome), Pi session capture (owner does not use Pi), Cursor (owner does not use it).

### Delivery and clients

| Rank | Integration | Owner need | Existing seam | Effort | Principal risk |
|---|---|---|---|---|---|
| 1 | VS Code MCP configuration in `agent setup` | Daily editor. | `agent_setup.py` writes preview-bound owned fragments for two clients. | Low | VS Code's MCP configuration format differs between the editor and extensions; pick one, verify with a synthetic profile. |
| 2 | Brief and answer delivery to Slack | Second delivery surface the owner wants. | None outward. The brief is on request and returned only to the asking client. | Medium to high | This is the first outward write. It needs the action-approval receipt, a posting scope the Slack app does not request today, and a decision about who runs it (the core has no scheduler). |
| 3 | Obsidian plugin | Keep as is. | Live. | None | GUI acceptance gate still open. |

### Action targets

| Rank | Action | Owner need | Existing seam | Effort | Principal risk |
|---|---|---|---|---|---|
| 1 | Pull request as follow-up resolution evidence | Named by the owner. | GitHub captures pull requests; follow-up decisions retain evidence. Missing: linking a captured PR to the follow-up it closes. | Low | None; read-only. |
| 2 | Draft pull-request text from follow-up evidence | Same. | Follow-up items carry quoted evidence. | Low | A draft returned to the client is not an action; the coding agent decides whether to use it. |
| 3 | Jira comment or transition draft | Not ranked by the owner. | `ticket_key` link only. | Medium after the receipt design | First write to the ticket system. |
| 4 | Slack reply draft | Not ranked by the owner. | None. | Medium after the receipt design | First write to Slack. |

The action-approval receipt is a single design, not four. Specify it once: what the receipt binds (action kind, destination, exact payload hash, evidence revisions, approving session), where it is stored, how it expires, and how an MCP client presents a draft without being able to send it. Every outward action then reuses it.

## Gates that apply to all of them

1. **Live sources belong to the collector, never the core.** The core has no listener, scheduler or background service. Make the collector's provider set extensible before adding a fourth live source, so each addition is a registration rather than an edit to `live_manager.py`.
2. **Read-only scopes first.** Every source above can deliver its outcome with read scopes. Posting or write scopes arrive only with the action-approval receipt.
3. **Fix the GitHub tier before widening GitHub.** A capture's tier is set by the source and can only be narrowed. Stamping `public` on a selected private repository is a scope defect, not a feature gap.
4. **Employer review stays practical.** A Slack user-token app and an Atlassian OAuth app are both visible to workspace administrators. Provide the scope list, storage location and uninstall path before asking anyone to approve them.
5. **Document splitting is one rule.** Drive, Confluence and long transcripts all hit the same character caps. Decide once how a long document becomes several revisions under one identity, and apply it to all three.

## What to drop or defer

| Change | Recommendation | Reason |
|---|---|---|
| Notion, Microsoft 365, iMessage, web clips | Leave the adapters; do not build clients. | Not in the owner's stack, or excluded earlier for the same reason. |
| YouTube connector wiring | Leave as the conformance reference. | No work outcome depends on it. |
| Pi and Cursor session capture | Do not add. | The owner uses neither. |
| Meet or Zoom API clients | Do not build. | Drive already receives the notes; a meeting-bot or recording integration was deferred in the 2026-10-14 evaluation. |
| Push and pull request as an Open Brain action | Do not build. | The coding agent already has it. Capture the result instead. |

## Proposed sequence

1. **I1, meeting notes via Drive.** Folder selection, long-document splitting, synthetic acceptance with a Doc shaped like a Gemini note, then one owner-authorized real note to confirm the format.
2. **I2, Slack reactivation.** Move `slack_public_integration` out of the deferred list, run live acceptance against a disposable workspace, write the employer-facing scope and retention page.
3. **I3, Atlassian.** One OAuth app, Jira client first, Confluence second, shared checkpointing and splitting. Follow-ups gain ticket-state evidence; decision recall gains page revisions.
4. **I4, code hosts and editor.** GitHub tier fix and comments, VS Code writer in `agent setup`, GitLab client, pull-request-to-follow-up linking.
5. **I5, the action-approval receipt, then Slack delivery.** Design the receipt as its own spec. The first consumer is posting an approved brief to a private channel; the second is a pull-request draft.

Each step is a brainstorm, spec and plan of its own, with the same package execution and review used for #3 and #5.

## Open questions for the owner

- Does Meet save Gemini notes as a Google Doc every time, and into a predictable folder? One real note answers it.
- Is a Slack user-token app approvable at the employer, or does Slack need to wait for an admin-installed app?
- Jira Cloud OAuth app or per-user API token for the pilot?
- VS Code: Copilot agent mode's MCP configuration, or another extension?

## Documentation drift noticed

- `docs/product-family.md` still describes the collector as planned and not implemented. The code and `docs/plans/2026-09-16-priority-capture.md` say otherwise.
- `docs/google-calendar.md` lists collector wiring as an open gate; that matches the code and should stay until I1 or the Calendar wiring lands.

## Evidence

- E1. Source states and file references: `packages/connectors/src/open_brain_connectors/runtime/source_registry.py` (fifteen descriptors), `runtime/google_sources.py` (Gmail, Drive export and the 60,000-character refusal), `runtime/meeting_transcript.py` (host-supplied transcripts), `runtime/gitlab_jira.py` (Jira and GitLab adapters), `runtime/workspace_content.py` (Confluence and Notion adapters), `packages/collector/src/open_brain_collector/live_manager.py` (hard-coded provider set), `runner.py` (GitHub issues endpoint and `public` tier stamp).
- E2. Deferrals: repo `CLAUDE.md` (Slack deferred, product boundary), `packages/app/src/open_brain/services/catalog.py` (deferred capabilities list), `docs/plans/2026-09-16-priority-capture.md` (priority milestone and Microsoft exclusion).
- E3. Intake and approval contracts: `docs/capture-contract.md`, `docs/integrations/collector-recovery.md`, `docs/v0-product-contract.md` (V0-REVIEW-09, default-profile connector bar), `runtime/connectors.py` (`action_authorities`).
- E4. Clients and delivery: `packages/app/src/open_brain/services/agent_setup.py` (two clients), `docs/agent-setup.md`, `docs/harness-setup.md`, `docs/daily-brief.md` (on request, returned only to the asking client).
- E5. Owner interview, 2026-10-25, recorded in the table above.
- E6. Prior evaluation: `docs/audits/2026-10-14-work-laptop-product-evaluation.md` (source priorities, deferral of meeting bots and autonomous outward actions).
