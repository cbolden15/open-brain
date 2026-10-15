# W2.0 contract freeze

Status: frozen for W2 implementation, 2026-10-14.

## Authority and compatibility

Trusted internal EffectiveAuthority.session_origin is SessionOrigin.LOCAL_OWNER (default direct owner operations), MCP_IMPLICIT (every policy-omitted MCP session including owner elevation), or MCP_POLICY (only after trusted startup policy validation). No wire DTO accepts provenance. Include origin in cursor authority commitments. Shared engine eligibility classifies retained Slack namespace/envelope proof and recognized copy commitments, never message text. Refuse implicit MCP Work Slack originals/copies and derived evidence before ranking, digest, paging, projection, source/selection expansion, auxiliary metadata and content-returning mutations. Canonical/member provenance and graph note IDs preserve lineage. Unresolved recognizable protected lineage refuses. Explicit policy still obeys all existing restrictions.

Current candidate schema/runtime/Portable: 17/12/12. Remote main remains d5cf64e652366dc14a8cf18732f7cb1e82ae4187. No new persistence or older-binary fence required for upgraded-runtime enforcement. Older binaries do not gain this gate. Stop for any implementation-discovered persisted-format requirement.

## Settings matrix and inputs

Supported versions: standalone Codex CLI 0.162.1; standalone Claude Code 2.1.300. Version/help rechecked with cleared environment and synthetic homes only. No real configuration/authentication or provider/model invocation. Ordinary declared project root equals declared launch cwd. Unknown versions, hosted/cloud/remote contexts, linked worktrees/subdirectories, nondefault setting-source lists, additional settings files, nonempty launch overrides and unresolved managed settings are explicitly unsupported initially.

Optional setup inputs: session_policy, consent_state, client_version, launch_context, acknowledge_unscoped. launch_context is closed: surface, cwd, profile, setting_sources, settings_file, overrides, managed_state. Require explicit standalone context and owner-declared absence of unobservable MDM/server/host policy; label those unobserved, never verified absent. Known managed presence refuses. Profile is Codex v2 <safe-name>.config.toml; legacy [profiles] refuses. Claude default user/project/local settings only.

Observe only bounded settings inputs and route/provider/proxy key allowlists. Inventory both clients' managed user and selected-project MCP files regardless of marker/server name. Codex: config.toml, selected v2 profile, trusted project config, /etc/codex/config.toml, /etc/codex/requirements.toml, /etc/codex/managed_config.toml. Claude: user settings.json, project settings.json/settings.local.json, managed-settings.json, managed-settings.d presence and managed-mcp.json under /Library/Application Support/ClaudeCode (macOS) or /etc/claude-code (Linux). Do not enumerate managed fragments or execute helpers/includes. Unsupported OS refuses scoped configure. Custom declared config roots are preview-bound. Test roots are injected internally, never client wire authority.

Bind file absence, identity, bounded byte preimages, environment allowlist, selected profile and entire declaration privately into preview. Apply rebuilds and compares before any write; output-boundary policy/consent revalidation remains required. Public endpoint/proxy metadata is authority-only: remove userinfo, path, query, fragment; redact no_proxy values and arbitrary selector identifiers. Separate observed, owner-declared, not-observed and unsupported. No claim of effective inference routing. Setup environment is only setup environment. Missing observations never imply a default.

Policy scoped setup grants search/content-read only, requires selected Work space/provider/current consent; rejects writes/history/owner. Preserve default caller shapes when optional inputs absent. Only owned MCP/instruction fragments may change; preserve unrelated bytes. Removal depends only on owned identity/preimages, never policy/consent/settings availability. Cross-client unscoped coexistence requires removal or preview-bound explicit acknowledgment.

## Evidence and ownership

Coordinator re-ran test_w2_settings_feasibility.py: 20 passed in 0.02s. Engine worker ran test_slack_sharing_surfaces.py: 8 passed in 18.90s. Independent Astra source review confirms transient lineage enforcement feasible, with callback closure work required.

Engine Sol owns shared eligibility and engine path/test implementation except t03_contracts.py/contracts.py. Settings Sol owns agent_setup.py, new settings observer module and setup tests. Coordinator owns shared authority/protocol declarations, app CLI/DTO/callback integration, documentation and ledger. No overlapping writes. Final independent Astra verifies integrated candidate.

Official sources checked: https://learn.chatgpt.com/docs/config-file/config-reference ; https://learn.chatgpt.com/docs/config-file/config-basic ; https://learn.chatgpt.com/docs/enterprise/managed-configuration ; https://code.claude.com/docs/en/settings ; https://code.claude.com/docs/en/env-vars ; https://code.claude.com/docs/en/network-config ; https://code.claude.com/docs/en/managed-settings . These establish documented candidates and precedence, not empirical effective client transport.

## Integration clarification

The whole-workspace structural graph cache has no per-source extraction coverage proof. Implicit MCP refuses that cache before reading; authorized engine suggestions remain available. Global workspace policy counters are suppressed for implicit views. Proper per-source extraction provenance or extraction from an independently authorized snapshot is outside W2; no persisted change is required for this safe refusal. See READ-PATH-INVENTORY.md.
