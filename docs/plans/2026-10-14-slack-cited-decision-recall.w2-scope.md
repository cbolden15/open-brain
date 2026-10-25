# Revised W2 scope and estimate

Date: 2026-10-14. Status: planning only; implementation is not authorized by this document.

The owner selected B for explicit Slack Work session policy, endpoint/proxy visibility, and recorded injection-resistance evidence before private pilot use. W1a/W1b is complete in `7a19321`; the selections are recorded in `c1d2c52`. This revision replaces the original W2 estimate of 0.5–1.5 days with **6–10 engineer-days** for a familiar engineer. W2 is now an authorization-boundary change as well as setup work.

## Client-settings feasibility first

**Feasible: a bounded snapshot of observed configuration for the supported standalone Claude Code and Codex CLI contexts.** Complete discovery of another running client's effective route is not established. Setup cannot see future launch flags or assume its environment equals the client's environment. The snapshot must describe that boundary rather than report an inferred endpoint as verified.

The planning pass used official documentation, repository source, and installed CLI help/version output. Installed versions were Codex CLI `0.162.1` and Claude Code `2.1.300`. An offline Codex schema export also confirmed `config/read` parameters `cwd`/`includeLayers` and response fields `config`/`layers`/`origins`. No RPC, model session, real client configuration, credential, or retained Brain was read or invoked. This is evidence of available surfaces, not a completed client-resolution integration test.

| Client | Observable settings surface | Important limit |
|---|---|---|
| Codex CLI | User configuration, explicitly selected profile inputs, trusted project configuration layers; provider selector, provider base URL and built-in base-URL override. | Current documentation excludes provider/profile-routing overrides from project configuration. CLI overrides take priority. Verify installed-version profile layouts with synthetic fixtures before freezing an adapter. |
| Claude Code CLI | User, shared-project, local-project and available managed settings; explicit `--settings`/`--setting-sources` context; endpoint/provider environment keys and proxy variables. | Managed values and per-key environment rules affect precedence. App-hosted provider routing can ignore settings that standalone CLI uses. |

Codex keys and project restrictions come from the [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference). Layering is documented in [configuration basics](https://learn.chatgpt.com/docs/config-file/config-basic). The [app-server reference](https://learn.chatgpt.com/docs/app-server) documents effective on-disk configuration inspection; it does not establish visibility into an unrelated running invocation. W2 will not add an app-server dependency or launch a client to resolve settings.

Claude's [settings precedence](https://code.claude.com/docs/en/settings) and [environment-variable reference](https://code.claude.com/docs/en/env-vars) expose the relevant inputs, including `ANTHROPIC_BASE_URL` and provider-family overrides. Its [network documentation](https://code.claude.com/docs/en/network-config) documents `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, lowercase variants and app-hosted scope exceptions. Authentication values and helper commands are not needed for this observation contract.

### Finite observation contract

1. Identify client version, project/user scope, selected profile and declared launch context. Read only the documented, bounded input set for that adapter. Inventory both clients' managed user and selected-project MCP locations for unscoped coexistence. Do not recursively scan homes or execute shell startup files, authentication helpers, plugins or arbitrary includes.
2. Observe endpoint/provider selectors and proxy candidates through an explicit key allowlist. Distinguish `observed`, `owner-declared`, `not-observed` and `unsupported` inputs. List candidate values and provenance when precedence is unresolved. Never equate missing observation with a default, or infer consent/provider authority from a URL.
3. Bind observed file identity, presence/absence, preimages, selected profile, declared overrides and allowlisted setup-process environment inputs into the preview. Rebuild and compare at apply. File replacement, newly present settings or changed policy/consent must stale the preview. Do not use checks performed only after configuration writes.
4. Preview only sanitized endpoint/proxy metadata. Remove URL userinfo, queries and fragments; never show credentials, header values, helper output or full configuration/environment contents. Keep any additional private commitments out of generated guidance and public receipts. Preserve model/provider settings byte-for-byte outside owned MCP fragments.
5. Refuse scoped configure for unsafe, unreadable or known unsupported relevant layers and unresolved configured overrides. Unobserved future flags, app-hosted/cloud routing and a different launch environment remain explicit operator preconditions. Generic manual MCP is outside this setup claim; the Slack Work read gate still applies to this runtime regardless of client.

For Codex, report observed proxy candidates separately from any verified model-provider behavior. Sandbox-command proxy settings do not prove routing of inference traffic. For both clients, the adapter must name every unobserved layer instead of claiming complete effective configuration.

This implements the selected visibility option. Endpoint and TLS-host enforcement require the separate controlled-egress architecture already listed in the parent plan. No such broker, listener, daemon or client launcher belongs in W2.

## Bounded implementation scope

### W2.0: Freeze the observation and authority contracts

Timebox the initial implementation spike to 0.5–1 day, included in the setup estimate below. Use synthetic client homes and the observed client versions to prove file/profile precedence, ignored project keys, override reporting and redaction. Freeze the supported version/context matrix and exact input inventory. Unknown versions and hosted contexts must have an explicit unsupported outcome.

Freeze a trusted internal distinction between local owner operations, implicit MCP sessions and explicitly policy-authorized MCP sessions. Do not add a client-supplied authority/provenance flag to frozen T03 requests. Confirm the compatibility consequences before coding the gate. Failure to bound a client adapter or an engine read path is a stop-and-re-scope condition, not permission to weaken the contract.

### W2.1: Require policy before Slack Work MCP reads

Carry trusted MCP provenance from the verified policy loader into a shared engine eligibility rule. A session with omitted policy cannot read Slack-sourced Work records, including approved Work copies. An explicit policy is necessary; existing capabilities, space, privacy, provider, consent and sharing checks still determine what it can read. Explicit external-provider policy cannot authorize local-only originals.

Apply the rule before candidate ranking, visible-generation digests, paging/cursor commitments, content projection and canonical-member expansion. Include current and retained revisions, guessed-ID reads, source expansion, selection disclosure and owner-elevated MCP sessions. Classify sources through retained engine evidence and recognized copy commitments; message text and source-reference substrings cannot establish authority.

The legacy `brain_search` callback currently lacks the newer authority path. Route it through the same eligibility boundary. Audit auxiliary MCP callbacks for inbox, review, workspace, facets and other content-bearing returns; each needs the shared predicate or a proven refusal before protected content is accessed. Include titles, previews, counts, graph quotes, mixed-source publications and mutation responses that return content. Existing space previews read before limit/offset, so metadata closure is required work. Prove that every route is gated or cannot reach protected Slack-derived content, and record that tool-to-read-path inventory as acceptance evidence. A success-only test of the new setup flags cannot close this package.

Keep direct owner CLI access and ordinary non-Slack implicit MCP reads working. A blanket policy requirement for all MCP content would be broader than selected B. Per-response filtering would leave ranking and cursor disclosures. Trusted app provenance plus shared engine filtering is the recommended architecture within the selected scope.

### W2.2: Preview-bound scoped setup

Extend setup service, owner CLI and closed setup DTO forwarding with policy/consent inputs, reusing `launcher-policy.v1` and the existing durable consent store. This workflow grants only search/content-read in the selected Work space and provider context. Reject owner/write/history grants for this scoped recall setup. Use existing manual invocation for other supported generic MCP uses.

Add read-only observed inputs to preview commitments alongside mutation preimages. Validate policy/consent identity, authority and current bytes at configure/apply and reuse runtime revalidation. Report unscoped Open Brain fragments across the managed files for both clients, regardless of marker/server name; require removal or the existing preview-bound explicit acknowledgment. Only owned fragments may be mutated.

Removal must validate owned-fragment identity and configuration preimages without needing readable or active policy/consent/settings evidence. Deleted policy, revoked consent and an unavailable endpoint cannot block cleanup or restore access. Preserve existing rollback and unrelated-byte guarantees. Keep desktop/plugin setup callers on their existing default request/response behavior; add no desktop controls or release work.

### W2.3: Acceptance and handoff

| Boundary | Required synthetic evidence |
|---|---|
| Omitted versus explicit policy | Legacy and negotiated MCP paths, implicit owner elevation, direct/history/canonical/selection and auxiliary reads; no ranking, counts, cursor or content influence from refused Slack Work records. Local CLI and unrelated non-Slack behavior remain covered. |
| Snapshot freshness | All supported settings layers, absent-to-present files, selected profiles, declared overrides, allowlisted environment changes, unsafe paths, conflicting/unsupported settings, redaction, stale apply and rollback. |
| Authority and cleanup | Wrong Brain/provider, expired/deleted policy, revoked consent, session change before output, no fallback, cross-client unscoped entries and removal after deletion/revocation. |
| Compatibility | Frozen W0/T03 and v1 sharing resources; old setup callers; schema/runtime admission checks; unchanged Portable evidence unless a separately approved format change is required. |
| Combined candidate | Focused setup/session/engine/real-stdio tests, independent security verification, `make verify`, diff check and workflow lint. Run native audit and Homebrew smoke when shared runtime/compatibility/native changes require them. |

An internal transient policy context does not by itself require a new Portable format. The estimate assumes no new persisted evidence format. Main coordinates must be rechecked before implementation; decide and document any required runtime-floor change then. New-runtime enforcement does not retroactively change older binaries or confine a hostile same-user process. If cross-binary fencing requires an appended migration or format revision, stop and estimate that separately before implementing it.

## Injection check and pilot boundary

W2 defines the pilot acceptance contract and passes it to W3/W4; it does not run a provider or start a pilot. W3 supplies the real-stdio recall fixture and guide. Before private pilot use, an owner-authorized actual-client run must use a synthetic hostile-message corpus and restricted tool configuration, and retain a private record of client/model/version, fixture digest, tool configuration, attempted non-Brain tool use, outcome and citation validation.

A refusal enforced by permissions is distinct from a model choosing not to attempt an unsafe tool call; record both attempts and execution. Define the acceptance threshold before the run. A `not_run` or failed check cannot satisfy the pilot gate. Bind the receipt to the tested client/model/configuration/corpus; relevant changes require a new check. This is bounded evidence, not proof against every prompt injection. It does not gate repository completion or authorize real Slack intake, migration, scheduling or deployment.

## Estimate and execution shape

| Work | Engineer-days | Includes |
|---|---:|---|
| Scoped setup and settings observation | 3–5 | Initial version/precedence spike, finite discovery, service/CLI/DTO forwarding, preview/apply/remove and synthetic tests. |
| Slack Work policy boundary | 2–4 | Trusted internal provenance, legacy/current/owner paths, pre-ranking filtering, auxiliary callback closure and regression tests. |
| Combined verification and review | 0.5–1 | Integration, focused/full checks, independent findings and required native checks. |
| Rounded W2 total | **6–10** | One familiar engineer; no new persisted format, broker or client distribution. |

With two implementers, plan **4–7 working days elapsed** after the contract freeze. This is a scheduling estimate, not a measured agent speedup. Setup observation and engine eligibility can overlap only in disjoint files. The coordinator owns shared CLI/adapter integration; serialize changes to shared entrypoints. Use GPT-6.1 Sol implementers and independent GPT-6 Astra verification, with one combined acceptance gate before W3.

Allow a separate 0.5–1 engineer-day per client/model/configuration combination for pilot-check preparation and evidence after W3, excluding provider availability and remediation. W3 and W4 retain their separate 1–2 day estimates each. Remaining W2–W4 repository work is therefore approximately **8–14 engineer-days**, excluding private pilot runs and any separately scoped compatibility migration.

Re-estimate if client settings or derived-content lineage cannot be bounded, any content callback lacks a safe shared enforcement path, cross-binary fencing requires storage changes, or passing checks require a new client runner or desktop feature. None of those changes is silently included here.

## Repository evidence

Source anchors were checked against `c1d2c52`:

- [Setup inputs, preview commitments and client roots]($WORKTREES/slack-cited-recall-w4-20261015-6d407b/packages/app/src/open_brain/services/agent_setup.py:119).
- [Closed setup DTO forwarding]($WORKTREES/slack-cited-recall-w4-20261015-6d407b/packages/app/src/open_brain/services/plugin_bridge.py:1265).
- [Legacy MCP search and implicit authority construction]($WORKTREES/slack-cited-recall-w4-20261015-6d407b/packages/app/src/open_brain/services/local_entrypoints.py:2232).
- [Trusted policy loading and revalidation]($WORKTREES/slack-cited-recall-w4-20261015-6d407b/packages/app/src/open_brain/services/session_authority.py:57).
- [Record/canonical eligibility]($WORKTREES/slack-cited-recall-w4-20261015-6d407b/packages/engine/src/open_brain_engine/engine/records.py:115) and [paging authority commitments]($WORKTREES/slack-cited-recall-w4-20261015-6d407b/packages/engine/src/open_brain_engine/engine/paging.py:48).

No production source, client settings or model/provider selection was changed during this planning pass. Client precedence and live routing remain implementation/pilot verification work, with the bounded observation claim above.
