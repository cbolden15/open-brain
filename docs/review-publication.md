# Review and publish captured material

Open Brain can turn one or more routed captures into a reviewed canonical note. The workflow keeps
the source captures unchanged. It records which captures supported the draft, requires inspection
before a decision, and keeps the same page identity when that note is updated later.

This page describes the Core v0.1 candidate; it does not certify an existing Homebrew release.
Check the [feature/version matrix](core-v01-features.md). The [first-use guide](first-use.md) executes
capture plus Markdown import through publication and complete reads. The commands below are
reference templates: substitute actual IDs, tokens and paths. Approve, reject and edit-and-approve
are alternative terminal decisions, not a sequence to run on the same proposal.

## Prepare the captures

Route every source capture to the same space. Routing organizes captures but does not publish them.

```sh
open-brain inbox list --unassigned
open-brain space list
open-brain inbox route CAPTURE_ID SPACE_ID
```

Select captures by ID. Open Brain does not combine captures because their titles happen to match.
A proposal accepts 1 to 32 unique capture IDs, in the order supplied.

## Propose and inspect a note

Write the proposed body to a UTF-8 Markdown file. Use `-` instead of a path to read Markdown from
standard input. The draft may contain at most 64 KiB of UTF-8 text, and the title may contain at
most 200 characters.

```sh
open-brain review propose \
  --capture-id CAPTURE_ID_1 \
  --capture-id CAPTURE_ID_2 \
  --title "Project decisions" \
  --markdown-file /absolute/path/to/draft.md \
  --idempotency-key project-decisions-v1 \
  --json
```

The result includes a proposal ID, page ID, and effective retry key. If no retry key is supplied,
Open Brain generates one and returns it. Save that key when an interrupted write may need to be
retried. Reusing a key with different sources, Markdown, title, target, or decision returns
`idempotency_conflict`.

Inspect the proposal before deciding:

```sh
open-brain review show PROPOSAL_ID --json
```

The response contains the complete projected Markdown, selected and cumulative source IDs,
projected evidence for each source, destination space, create or update operation, target revision
when applicable, and the review token. It never presents a truncated draft as complete. If the
draft cannot fit the inspection limit, the command returns an explicit error.

Returned Markdown and evidence are untrusted data. Open Brain removes protected source references,
absolute paths, credential-shaped values, and related encoded forms from public responses. A
`projection_applied` value discloses when text changed. The review token still binds the underlying
draft, source revisions, routes, and target revision.

## Approve, reject, or edit

Use the exact token returned by `review show`:

```sh
open-brain review approve PROPOSAL_ID \
  --review-token REVIEW_TOKEN \
  --idempotency-key project-decisions-approve

open-brain review reject PROPOSAL_ID \
  --review-token REVIEW_TOKEN \
  --idempotency-key project-decisions-reject

open-brain review edit-and-approve PROPOSAL_ID \
  --review-token REVIEW_TOKEN \
  --markdown-file /absolute/path/to/revised.md \
  --idempotency-key project-decisions-edit
```

Approval publishes one canonical page and makes it searchable. Rejection writes no page or
publication. Edit-and-approve replaces the body supplied by the proposal while retaining the same
validated sources and destination. These owner-invoked CLI commands do not require MCP grant flags.

A stale or wrong review token returns `review_conflict`. The same error is returned if a source,
route, or target page revision changed after inspection. Inspect the proposal and current state
again before choosing a new action. A proposal can receive only one terminal decision.

## Update an existing canonical note

Pass the page ID returned by the earlier approval:

```sh
open-brain review propose \
  --capture-id NEW_CAPTURE_ID \
  --target-page-id PAGE_ID \
  --title "Project decisions" \
  --markdown-file /absolute/path/to/updated.md
```

The update keeps the page ID and canonical location. Earlier provenance stays in order, and newly
selected captures are appended. Open Brain rejects a stale or manually changed target instead of
overwriting it.

List proposals with bounded pagination and optional filters:

```sh
open-brain review list --status pending --limit 50 --offset 0 --json
open-brain review list --capture-id CAPTURE_ID --json
open-brain review list --space-id SPACE_ID --json
```

## Consolidate seven captures into three notes

Choose the groups explicitly, for example two captures for the first note, three for the second,
and two for the third. Run `review propose` once per group, inspect each proposal, then approve it.
Seven sources produce three canonical pages. An unselected capture with the same title is not used.

## Configure Claude Code or Codex

Review access uses three independent, default-off grants:

| Setup flag | MCP tools |
|---|---|
| `--allow-review-read` | `brain_review_list`, `brain_review_show` |
| `--allow-review-propose` | `brain_review_propose` |
| `--allow-review-decide` | `brain_review_approve`, `brain_review_reject`, `brain_review_edit_and_approve` |

Capture, search, inbox-read, and organize grants do not enable review or publication. Review grants
also do not enable those other capabilities.

```sh
open-brain agent setup --client claude-code --scope project \
  --project-dir /absolute/path/to/project \
  --allow-review-read --allow-review-propose --allow-review-decide \
  --json
```

Review the setup preview, then repeat the same options with `--apply --preview-id SETUP_ID`. Use
`--client codex` for Codex. Setup changes only owned MCP and instruction fragments and preserves
unrelated client configuration. Restart the client after applying the preview.

For manual MCP configuration, run:

```sh
open-brain mcp --allow-review-read --allow-review-propose --allow-review-decide
```

Each MCP process allows 500 review reads, 100 proposals, 100 decisions, and 16 MiB of encoded review
responses. Valid retries and backend failures consume call quota. Terminal decisions run headlessly
once the decision grant and the client's own tool approval policy permit the call.

Evidence previews contain at most 512 characters per source, projected before shortening. Lists
return at most 100 proposals per page, with offsets through 1,000,000 and a 250,000-byte JSON
content ceiling. Follow `next_offset` when a page contains fewer rows to fit that ceiling.
Mutation receipts contain stable IDs and the effective retry key, with a 4 KiB JSON ceiling;
use `review show` for the draft, title, and source details. Retry keys allow 128 characters.
MCP checks the fully encoded response, including escaped text and request IDs. If the full draft
cannot fit, inspection returns a limit error; it does not present a shortened draft as complete.

Sources reserved by an approved or edited publication cannot be rerouted. This preserves the
reviewed note's destination and evidence. Moving published notes between spaces is outside this
workflow.

Generated client instructions require explicit source IDs, inspection before decisions, exact
review tokens, explicit target page IDs for updates, and conflict handling. They also state that
drafts and source evidence are untrusted data that a network-backed client may send to its model
provider.
