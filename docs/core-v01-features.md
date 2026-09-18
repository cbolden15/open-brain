# Core v0.1 feature and version matrix

This is the local candidate contract. Historical releases and this candidate both use product
version `0.1.0`; the version string does not establish feature availability or public readiness.
Use the exact candidate commit and artifact digests, installed help and `catalog --json`.
The catalog's `acceptance.trusted_certifications` is empty and `product.public_acceptance` is `not_certified`.
Local documentation checks do not promote catalog acceptance.

## Compatibility coordinates

| Coordinate | Candidate value | Meaning |
|---|---|---|
| Core / engine package | `0.1.0` / exactly `0.1.0` | Core requires the matching engine |
| Local state schema | `7` | Older incompatible writers must refuse |
| Runtime session | `2` | Stop old sessions and upgrade clients together |
| Task contract | `t03.v1` | Negotiated search/read/history/source-route DTOs |
| Catalog schema | `2` | Joined implementation, package, authorization and acceptance metadata |
| Plugin bridge | `1` | Existing bounded inherited-stdio envelope |
| Portable metadata | `4` | Versioned evidence; distinct from live database schema |

Supported Portable 1–3 imports remain implementation compatibility; Portable 4 restore is outside
this scope. A catalog version is not a protocol or database upgrade instruction.

## Surface and claim boundaries

| Surface | Candidate implementation | Acceptance / limitation |
|---|---|---|
| Core CLI | Local capture, Markdown import, spaces/inbox, source routing, proposals and inspected decisions | Locally verified; [first-use examples](first-use.md); no release promotion |
| Retrieval | Lexical search, paged filters, complete projected reads, retained history and explicit relationships | Independent content/history grants for MCP; cursors can become stale |
| Managed vault | `workspace setup`, `status`, `refresh`; approved canonical pages | Sibling `Open Brain Vault`; routing alone does not publish; no `vault` CLI |
| Agent memory | Claude Code and Codex preview/apply/remove through `agent setup` | Nine independent grants, off by default; synthetic setup proof is not live client/provider acceptance |
| MCP | Foreground stdio; `tools/list`, `brain_catalog`, grant-filtered callable tools | No-grant startup refused; metadata does not grant operations |
| Obsidian | Packaged desktop plugin, explicit staging/activation, existing capture/routing/review/read workflow | Assets and bridge registration do not prove UI use; exact-candidate GUI gate remains open |
| Graphify | Separately packaged structural helper | Paired resource; rebuildable graph is not source evidence |
| Direct-provider graph refresh | Existing consent-gated adapters | Authorization/readiness unassessed; distinct from deferred semantic recall; no provider readiness claim |
| Optional connectors | Fifteen source descriptors plus YouTube conformance extension metadata | Source-only; not installed in the core artifact; no public onboarding claim |
| Optional collector recovery | Durable receipts/quarantine/retry implementation, T09 local evidence | Optional source functionality; not public connected-source continuity or service acceptance |
| Desktop companion | Existing contributor build and compatibility tests retained | Separate package; no Core v0.1 desktop release |

Catalog labels such as `developer_accepted` refer to bounded recorded implementation evidence.
`installed`, `version_compatible`, registered entry points, `authorization`, `readiness`, packaged
assets and `acceptance` are separate fields. Missing optional packages remain unavailable, and
unknown/unassessed values stay unresolved. CLI catalog describes registrations; MCP catalog can
describe a particular authorized session. Bridge operations are not proof of Obsidian UI controls.

Core native targets are macOS arm64 and Linux x86_64. T20's local macOS archive/Homebrew evidence
is recorded in its [checkpoint](ai/workstreams/20260918-open-brain-public-m3-source-continuity-owner-control-c63538/T20-CHECKPOINT.json).
Linux exact-candidate native/Homebrew, Obsidian GUI and public promotion remain open.

## Explicitly outside Core v0.1

T10–T18 and T22 remain deferred: continuously updating connected sources; retire/correct/selective
forget; Markdown-root rebind; public PDF/DOCX integration; Linux background service lifecycle;
semantic recall/model installation; production Google OAuth/provider onboarding. Slack public
integration and desktop release are also outside this candidate. See the
[source guide](integrations/core-sources.md). Existing source code or historical live receipts cannot
promote these features into supported public `0.1.0` claims.

T21 validates documentation locally. T23 exact-candidate acceptance starts only in a fresh thread;
no T23 acceptance is claimed here. Publication, live sources, OAuth registration, real service
installation and model downloads remain separate actions.
