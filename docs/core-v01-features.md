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
| Local state schema | `22` | Incompatible coordinates refuse; same-coordinate older binaries are not fenced |
| Runtime session | `16` | Stop old sessions and upgrade clients together |
| Task contract | `t03.v1` | Negotiated search/read/history/source-route DTOs |
| Catalog schema | `2` | Joined implementation, package, authorization and acceptance metadata |
| Plugin bridge | `1` | Existing bounded inherited-stdio envelope |
| Portable metadata | `17` | Versioned evidence; distinct from live database schema |

Supported Portable 1–3 imports remain implementation compatibility; standalone Portable 4 import
is refused, and standalone clean restore accepts Portable 5 through 17. Portable 7 clean restore preserves current identity, privacy, issuer, managed source and sharing
evidence. [Portable 9](portable-brain-v9.md) adds explicitly versioned retained import evidence
and mixed historical operations. [Portable 12](portable-brain-v12.md) retains owner-approved Slack Work sharing and immutable recall selections. Later formats add capture lifecycle ([13](portable-brain-v13.md)), owner retraction ([14](portable-brain-v14.md)), conversation intake ([15](portable-brain-v15.md)) the local embedding policy ([16](portable-brain-v16.md)) and the owner egress policy history (17). A catalog version is not a protocol or database upgrade instruction.

## Surface and claim boundaries

| Surface | Candidate implementation | Acceptance / limitation |
|---|---|---|
| Core CLI | Local capture, Markdown import, spaces/inbox, source routing, proposals and inspected decisions | Locally verified; [first-use examples](first-use.md); no release promotion |
| Capture admission | Engine-enforced envelope, body, and batch bounds, per-principal rate and concurrency gates, bounded writer waiters, absolute storage watermarks, owner explicit tiers, and destination-bound submission under a trusted launcher policy | Stable refusal results leave no partial state; defaults in the [capture contract](capture-contract.md); redaction boundary classifier off by default |
| Conversation intake | Internal ChatGPT/Claude normalized plans, owner `consumer-import` preview/body/commit/status/recover/halt, atomic conversations and Portable15 evidence; [intake](consumer-intake.md), [from coding agents](consumer-intake-agents.md) | Synthetic acceptance; both external parsers inactive, so new ChatGPT/Claude archives return `unsupported_profile`; no end-user importer claim |
| Offline semantic search | Opt-in local model in a separate embedding resource, `embedding status/enable/disable/rebuild`, `search-page --mode`, derived vector cache, `embedding_health`; [semantic search](semantic-search.md) | Not network egress; off until enabled; lexical fallback with a `model_unavailable` warning; cache is not part of Portable export |
| Owner retraction | `retract preview/commit/status` for a capture or trusted capture session; [owner retraction](owner-retraction.md), [impact](retraction-impact.md) | Owner-local only; no MCP, plugin or desktop command; no purge certification for backups or earlier exports |
| Owner-defined facets | `facet dimension/value/assignment/query/history/vault`; [design](design/owner-defined-facets-stage-one.md) | Owner CLI and Obsidian workflows; [Portable 10](portable-brain-v10.md) |
| Harness memory | Foreground `harness` bridge and optional lifecycle adapters for Codex, Claude Code and Pi; [setup](harness-setup.md), [memory](harness-memory.md) | Native acceptance in progress; synthetic events do not prove live capture or recall |
| Retrieval | Lexical search by default, optional local hybrid mode, paged filters, complete projected reads, retained history and explicit relationships | Independent content/history grants for MCP; cursors can become stale |
| Managed vault | `workspace setup`, `status`, `refresh`; approved canonical pages | Sibling `Open Brain Vault`; routing alone does not publish; no `vault` CLI |
| Agent memory | Claude Code and Codex preview/apply/remove through `agent setup` | Nine independent grants, off by default; synthetic setup proof is not live client/provider acceptance |
| MCP | Foreground stdio; `tools/list`, `brain_catalog`, grant-filtered callable tools; optional whole-session launcher policy and durable external-provider consent | No-grant startup refused; authority is the intersection of policy, selected flags, injected implementations, and the scoped-safe matrix; live revocation terminates the process |
| Obsidian | Packaged desktop plugin, explicit staging/activation, existing capture/routing/review/read workflow | Assets and bridge registration do not prove UI use; exact-candidate GUI gate remains open |
| Graphify | Separately packaged structural helper | Paired resource; rebuildable graph is not source evidence |
| Direct-provider graph refresh | Existing consent-gated adapters | Authorization/readiness unassessed; distinct from offline semantic search; no provider readiness claim |
| Optional connectors | Fifteen source descriptors plus YouTube conformance extension metadata | Source-only; not installed in the core artifact; no public onboarding claim |
| Slack cited recall | Optional selected-channel intake, owner-approved Work evidence copies, scoped existing stdio search/read and exact citation plumbing | Synthetic acceptance only; [operator and pilot guide](integrations/slack-decision-recall.md); actual-client/model/injection checks `not_run`; no live Slack or public integration claim |
| Optional collector recovery | Durable receipts/quarantine/retry implementation, T09 local evidence | Optional source functionality; not public connected-source continuity or service acceptance |
| Optional offline outbox | Bounded durable enqueue, one-cycle owner drain with an exclusive lease and batch bounds, receipt-verified body removal, quarantine with owner retry, confirmed discard, and lineage conversion | Optional connectors distribution, not in the core artifact; text payloads only; the synthetic transport is the only automated-test transport and real transports are private deployment acceptance |
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

T10–T18 and T22 remain deferred: continuously updating connected sources; correct (as opposed to
retract); Markdown-root rebind; public PDF/DOCX integration; Linux background service lifecycle;
production Google OAuth/provider onboarding. Owner retraction and optional offline semantic search
have since been implemented and are listed above. Slack public
integration and desktop release are also outside this candidate. See the
[source guide](integrations/core-sources.md). Existing source code or historical live receipts cannot
promote these features into supported public `0.1.0` claims.

T21 validates documentation locally. T23 exact-candidate acceptance starts only in a fresh thread;
no T23 acceptance is claimed here. Publication, live sources, OAuth registration and real service
installation remain separate actions. Searching never downloads a model.
