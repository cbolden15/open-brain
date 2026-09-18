# Core v0.1 sources

The supported local candidate path is explicit text capture and Markdown directory import.
Follow the [first-use guide](../first-use.md) to capture synthetic text, import a synthetic `.md`
file, route both IDs, propose a canonical note, inspect it and approve it. A route changes
assignment/search metadata without changing source trust or publishing a note.

Markdown import recursively reads lowercase `.md` files, skips Obsidian metadata, and leaves the
source tree unchanged. The first root requires confirmation (`--yes` for deliberate noninteractive
use). Imported content is unverified; links, frontmatter, HTML and embeds are inert text. Retained
source revisions remain evidence. This does not promise continuously updating connected sources.
Changed ordered connector intake, confirmed root rebind and public PDF/DOCX import are deferred.
Do not import the managed vault back into the same Brain.

## Optional source implementations

The catalog inventories GitHub, GitLab, Gmail, Google Drive, iMessage, agent sessions, Jira,
Microsoft 365 Mail, Slack, local documents, calendars, Confluence, Notion, web clips and meeting
transcripts. The YouTube conformance extension is separate metadata. These are source-only
implementations in optional distributions, not core-installed public onboarding choices.

The [Google source guide](google-sources.md) and [agent session capture guide](agent-session-capture.md)
retain implementation details and historical acceptance. Their old receipts do not certify this
candidate. Production OAuth/provider readiness, continuous source updates and real service setup
remain deferred or separately gated. Ordinary [agent memory setup](../agent-setup.md) only grants
explicit MCP operations; it does not install hooks or collect transcripts.

The optional collector owns scheduling and recovery. Core capture/import/search is foreground-only
and does not install, enable or require it. Installing an optional package would not prove that its
service, credentials or sources are ready. Keep catalog installation, authorization, operational
readiness and acceptance distinct; see the [feature/version matrix](../core-v01-features.md).
