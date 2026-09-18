# Core v0.1 T19 capability catalog

Status: bounded contract frozen for implementation from T09 `87b2805`. Catalog schema 2 is a new, additive app contract. It does not replace connector descriptor schema 1, bridge framing 1, MCP transport negotiation, or `t03.v1`.

## Inventory and authority

The Core v0.1 user decision narrows T19/A14 to core CLI/MCP and the existing Obsidian workflow. T10–T18/T22 and desktop release remain deferred. T20/T21/T23 exact-artifact and public acceptance are later gates.

Existing signals are independent:

- `local_entrypoints.py` registers installed core commands and explicit MCP grant flags; current version is 0.1.0.
- `LocalMcpAdapter.list_tools()` advertises only injected capabilities; `T03AppAdapter.available_operations()` further filters negotiated operations by grants.
- `plugin_bridge.py` owns base, negotiated and optional collector operation registries. Handshake framing stays unchanged. Negotiated operations use `contract.describe`.
- `obsidian_plugin.py` validates matching, bounded assets from the installed executable prefix. Workspace source files alone do not prove an installed plugin, and bundled assets do not prove enablement in Obsidian.
- The optional connector CLI emits 15 schema-1 descriptors with `public_onboarding` booleans. They describe provisional adapters, not joined public certification. Preserve their format and implementation.
- Optional collector and connectors have separate 0.1.0 distributions and entry points. Desktop is a separate contributor build. None is a default core dependency.
- Schema 7, runtime session 2, task contract `t03.v1`, Portable metadata 4, bridge protocol 1 and product 0.1.0 are separate compatibility coordinates, not interchangeable versions.

## Bounded contract

1. Add one app-owned pure catalog builder with deterministic JSON-safe output and strict schema version 2 requests. Expose `open-brain catalog --json` before root selection/bootstrap. Inspection must not initialize/read a Brain, query credentials, import optional distributions, execute helpers, connect to providers, or start/install services. No mutation/promotion/action executor is added.
2. Add read-only `brain_catalog` to an already authorized MCP session and `catalog.describe` to the existing plugin bridge. These use the same builder. Keep existing no-grant MCP refusal and all old tool/response/grant semantics. Catalog disclosure contains public implementation metadata only; it grants no operations.
3. Separate implementation (`implemented`, `planned`), scope (`core`, `source_only`, `deferred`), acceptance (`source_only`, `developer_accepted`, `public`), package installation/version compatibility, authorization, operational readiness and public acceptance. Unknown/not-assessed states are first-class. A registered operation is not necessarily ready. This candidate has **no public acceptance certification**; package version 0.1.0 does not make M2/T09/T19 source features part of the historical public artifact.
4. Describe core CLI commands from actual parser registration (including positional action choices), MCP callables from the actual session tool registry and bridge callables from its actual operation registries. CLI inspection may list MCP grant requirements/registered implementation, but must not claim a synthetic all-grants session is authorized. Each surface must label discovery context. Plugin bridge dispatch is not proof of an Obsidian UI affordance; preserve and label that distinction.
5. Report core/engine installed metadata and exact required versions, task/storage/transport compatibility, declared supported artifact platforms (`macos-arm64`, `linux-x86_64`) separately from current platform observation. Optional package metadata may be inspected without importing those packages; entry-point registration and matching versions are distinct from executable availability/auth/readiness. Missing, incompatible and unknown metadata fail closed. No paths, environment values or credentials appear in output. Represent lifecycle explicitly: one-shot commands/import, foreground MCP/plugin sessions, and optional scheduled collector implementation. A lifecycle label grants no service installation or public continuity claim.
6. Explicitly enumerate source-only optional connector descriptors: GitHub, GitLab, Gmail, Google Drive, iMessage, agent sessions, Jira, Microsoft 365 Mail, Slack, local documents, calendars, Confluence, Notion, web clips and meeting transcripts. Slack, connected-source continuity, local PDF/DOCX public integration, Google OAuth/provider readiness, Linux service lifecycle, semantic recall, retire/correct/selective forget, Markdown-root rebind and desktop release remain unavailable/deferred. Separately classify the registered YouTube conformance extension as source-only metadata, without loading it or claiming public onboarding. Existing direct-provider graph refresh is distinct from deferred semantic recall: preserve its implementation but mark consent/auth/readiness unassessed, never public-provider-ready. Collector recovery is locally verified optional source functionality, not continuous-source public acceptance.
7. Acceptance evidence is bounded, repository-relative, and scope-labeled. Historical M2/T09 receipts may explain developer verification but cannot certify a new artifact. No environment flag, arbitrary receipt string, legacy onboarding boolean, package installation or descriptor can promote an entry to public. The current trusted certification set is empty; future promotion requires separate exact-artifact, platform and applicable authorization evidence and is outside T19.
8. Unsupported catalog versions/arguments and unavailable tool/action calls fail with existing bounded error mechanisms; no fallback to an invented supported action. Serialize deterministically, without dataclass/enums/paths leaking. Keep existing strict DTOs and frozen compatibility contracts untouched.

## Verification boundary

Focused tests must cover deterministic serialization, strict request versions/keys, parser and actual MCP/bridge registry parity, grant isolation, missing/mismatched package metadata and entry points, plugin asset absence, no bootstrap/optional imports/provider access, and refusal of unknown/deferred actions. Prove legacy descriptor v1, CLI/MCP search and bridge compatibility with existing focused suites. Use only synthetic fixtures. Independent Astra/high review follows Sol/medium implementation. Coordinator owns `git diff --check`, `actionlint .github/workflows/ci.yml`, and final serial `make verify`.

A14 closure is scoped catalog truth only. No exact-artifact, provider, GUI, desktop release, deferred task or full program acceptance is claimed.

## Consumer semantics

The CLI inspection entry point is `open-brain catalog --json`. MCP clients in an
already authorized stdio session discover `brain_catalog` through `tools/list`
and pass `{"schema_version": 2}`. Existing plugin bridge clients discover
`catalog.describe` in the handshake and pass the same arguments in the unchanged
`open-brain-client` v1 envelope. No catalog operation grants capture, search,
content, history, organization or publication authority.

Consumers must retain the distinction between registered implementation and the
current session's callable tools. A package entry point describes distribution
metadata; it does not establish executable discovery or provider authorization.
Obsidian packaged assets describe the executable prefix; they do not establish
plugin enablement or an observed GUI journey. Declared artifact platforms are
release targets, not exact-candidate acceptance receipts. Unknown/unassessed
states should stay visibly unresolved.

The optional source descriptors and deferred capability list are explicit
classification records. They are not new source dispatch commands. Existing
connector v1 `public_onboarding` values remain legacy descriptor data and cannot
promote any schema-2 entry. A consumer must not treat catalog metadata as an
execution or certification API.
