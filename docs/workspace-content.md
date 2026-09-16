# Notion and Confluence capture

The optional workspace-content adapter covers D5.3 readiness for selected Notion
pages/data sources and Confluence Cloud spaces/pages. It currently provides the
bounded selection, metadata-only preview, revision identity, checkpoint, and import
surface used by synthetic tests. Real provider acceptance still requires explicit
owner consent for selected test resources.

Use `uv run --frozen open-brain-source` from a contributor checkout, or install the
optional connector package once packaged.

## Auth architecture

Inspect the supported authentication boundary before any live setup:

```sh
open-brain-source workspace-content auth-architecture
```

The command prints provider profiles without tokens, client secrets, or account
content.

Notion is intentionally marked as `confidential_oauth`. Notion public OAuth requires
a confidential authorization-code token exchange. A desktop bundle must not contain a
Notion client secret, and this repository does not silently introduce a hosted relay.
Live public onboarding is blocked until the owner authorizes a reviewed confidential
exchange component and records where that component runs. The current implementation
may process owner-provided synthetic payloads for selected pages/data sources, but
that is not evidence of public Notion OAuth acceptance.

Confluence is scoped to Atlassian Confluence Cloud using authorization code with
PKCE. Confluence Data Center is a separate adapter and is not claimed by this
profile. The first Cloud scope is selected spaces/pages with comments and original
links.

## Preview selected content

Synthetic or provider-normalized input must already be selected by the host. Preview
does not print body text.

```sh
open-brain-source workspace-content select-resource \
  --connection-id 'account:notion-fixture' \
  --connector notion \
  --resource-id 'notion:page/weekly-plan' \
  --resource-type page

open-brain-source workspace-content preview-content \
  --connection-id 'account:notion-fixture' \
  --connector notion \
  --resource-id 'notion:page/weekly-plan' \
  --resource-type page \
  --input /absolute/path/to/workspace-content.json \
  --selected-content-id 'notion:page/weekly-plan'
```

The input shape is an array of normalized records with `connector_name`,
`content_id`, `revision_id`, `content_type`, `title`, `body`, `source_kind`, and
`content_secret_scan: "clean"`. `parent_id` and `source_link` are optional.
Nested Notion blocks and Confluence comments are included only when their parent is
the selected page. Data-source and space selections include selected child pages
whose parent is the selected resource.

## Acceptance still open

Before D5.3 can be marked accepted, use owner-designated test resources to prove:

1. Reviewed Notion confidential OAuth architecture, with no desktop-bundled secret
   and no unapproved hosted relay.
2. Confluence Cloud OAuth consent for a selected test site, space/page, and comments.
3. Preview, import, retrieval through a fresh client session, update, replay without
   duplicates, permission loss, token invalidation, pagination recovery, and export.
4. Redacted receipts that identify commands and revisions without exposing private
   workspace bodies, credentials, browser cookies, or personal account content.

Synthetic fixtures remain implementation evidence only.
