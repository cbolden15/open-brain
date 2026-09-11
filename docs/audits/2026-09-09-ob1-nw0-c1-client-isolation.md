# NW0-C1 official-client isolation control audit

- Result: control mapping complete; **not ready for note dispatch**.
- Baseline: `a38e8fa`, branch `docs/ob1-native-workspace-plan`, macOS arm64.
- Installed clients: Codex CLI `0.153.4`; Claude Code `2.1.265`.
- Scope: the existing 45-minute NW0-C1 reservation. Zero inference calls, no account inspection,
  credential changes, product implementation, or Linux execution.
- Authority: [NW0 decision record](../plans/2026-09-09-ob1-native-workspace-nw0.md) and
  [subscription isolation contract](../plans/2026-09-09-ob1-native-workspace.md#subscription-completion-isolation).

This checkpoint establishes which controls exist and what the offline experiments actually show.
It does not pass either subscription adapter. All five launch access paths remain required.

## Observed evidence

Installed help and versions were captured afresh. The installed Codex app-server generated 416
experimental JSON schemas in 0.344 seconds. Inspection also used the official source tag
`rust-v0.153.4`, including its configuration schema and tool-registry construction. Source findings
are evidence about that tag, not a reproducible-build attestation for the installed binary.

Three Claude processes used private stdio, a disposable configuration directory, an empty MCP
configuration, and the combined launch controls listed below. The test sandbox denied network
access, writes outside the test directory, execution of other programs, and access to selected real
configuration/credential locations. It also denied the local managed-settings directory. No user
message or model turn was submitted. The environment was built from an allowlist, without inherited
provider credentials, routing variables, debug flags, or retry watchdogs.

| Offline case | Observed result |
|---|---|
| Initialize with combined controls | Success in 0.583 seconds; 3,188 stdout bytes, no stderr. Reported token source `none`, first-party provider, analytics disabled, and permission mode `dontAsk`. |
| Initialize, MCP status, context usage, idle interrupt; schema enabled | Success in 0.292 seconds; 28,372 stdout bytes. MCP servers/tools, memory files, context agents, and commands were empty. Context diagnostics reported **687 system-tool tokens**. |
| Same diagnostics without `--json-schema` | Success in 0.265 seconds; 28,304 stdout bytes. The same empty ambient catalogs; the system-tool category was absent. Both diagnostic runs reported null API usage and zero user-message/tool-call tokens. |
| Idle shutdown | SIGTERM exit 143 in all three cases, taking 0.011–0.021 seconds. Idle interrupt acknowledged an empty queue in both diagnostic cases. This does not prove cancellation of an active request or detached descendants. |
| Test-sandbox negative controls | An allowed scratch write succeeded. An outside write, explicitly denied synthetic-canary read, subprocess execution, and loopback network connection each failed with a permission error. |

The diagnostic cases staged synthetic `CLAUDE.md`, agent, skill, and MCP canaries. Their reported
catalogs were empty. The initialization response still listed built-in agent descriptions; a list
of available agent definitions is not evidence of an invocable agent tool. Context diagnostics are
also not an authoritative serialized model request. These distinctions prevent a stronger claim
than the experiment supports.

Each Claude startup wrote a configuration file and backup under its disposable configuration
directory. No transcript was observed there, but no prompt was submitted either. Persistence flags
therefore have not passed a prompt-retention test. No real subscription login was exercised.

The sandbox self-test first exposed an interpreter-launcher indirection, then an incorrect test
assumption: creating a socket can succeed while connecting is denied. Both failed receipts were
retained. The corrected test used the resolved interpreter and tested a connection. It passed.
This sandbox is a test harness, not a selected cross-platform product mechanism or a complete
filesystem-read allowlist.

Private evidence retains launch scripts, synthetic fixtures, help, schemas, source hashes, raw
receipts, and a 64-assertion metadata/integrity verification result. Those assertions do not count
as 64 subscription tests. Metadata processes were capped at 20 seconds and 64 KiB output; the Codex
schema-generation process had a 30-second cap. Real inference retains the agreed 16 KiB input/output,
60-second attempt, and two-attempt/90-second probe limits.

## Control-to-test matrix

“Candidate” means a supported control exists but its full enforcement has not been demonstrated.
“Open” means a concrete pre-dispatch boundary still needs proof. Every open row prevents the
affected client from receiving note content.

| Boundary | Codex `0.153.4` | Claude `2.1.265` | Required pass test |
|---|---|---|---|
| Model tools | **Open.** Feature switches cover shell and many capabilities; `dynamicTools: []` only omits added tools. Source can add clock and asynchronous user-input tools from model metadata. Environment removal also matters for patch/image tools. | **Partial offline evidence.** Empty tools plus wildcard denial and empty MCP produced no system-tool diagnostic category only when schema mode was absent. | Establish the complete effective catalog before dispatch, including hosted, extension, dynamic, discovery, subagent, and formatting tools. Then test hostile input and deny any forbidden action. |
| Ambient instructions and extensions | **Candidate.** Zero project-document bytes, disabled skill instructions/bundled skills, disabled plugins/apps/hooks, and no environments are relevant. `exec` exposes user-config/rules suppression; app-server has a different launch surface. | **Partial offline evidence.** Safe mode, restricted mode, empty setting sources, disabled slash commands, and strict empty MCP suppressed the staged catalogs. | Synthetic user/project/parent instructions, skills, plugins, hooks, and MCP canaries must be absent from effective context and effects. Test the exact authenticated launch recipe. |
| Managed policy | **Open.** Effective requirements/configuration need inspection, including enforced hooks, providers, and capabilities. | **Open.** Safe mode retains managed hooks; local/CLI hook suppression cannot override them. Local files are only one managed-policy source. | Resolve effective policy before notes; reject conflicting or indeterminate policy. Include controlled managed command/HTTP-hook cases and policy changes. Do not bypass organizational policy. |
| Subscription selection and attribution | **Candidate.** Explicit ChatGPT login restriction and disabled provider-model fallback exist. Authentication and actual response-model attribution were not tested. | **Open.** Offline startup had no credentials. Safe mode preserves normal auth; bare mode is unsuitable. Inherited API credentials and managed routing must not override the chosen access path. | Client-owned login, explicit selected subscription, verified effective provider/model, missing/expired-login rejection, and zero silent API/model fallback. Never copy tokens into Open Brain. |
| Egress | **Open.** Disabling web tools or sandboxed shell networking does not establish confinement of the entire official runtime. | **Open.** Nonessential-traffic controls were accepted and analytics reported disabled. Managed HTTP hooks and runtime traffic still require enforcement. | Allow only reviewed provider/auth traffic, deny redirects/other endpoints as applicable, and observe hostile-input effects. The offline deny-all sandbox proves no authenticated allowlist. |
| Retention and resumption | **Candidate.** Ephemeral threads, disabled history, and telemetry controls exist. Configuration, caches, diagnostics, and shutdown behavior remain untested. | **Open.** Session persistence and system-prompt snapshots can be disabled. Disposable startup still wrote configuration/backups. | Inspect controlled writable locations after success, validation failure, interruption, timeout, and crash. No retained prompt/context, resumed session, or diagnostic echo; preserve client-owned authentication. |
| Cancellation and output | **Candidate.** App-server interrupt exists; no active process was tested. | **Partial offline evidence.** Idle interrupt and SIGTERM worked; active requests/descendants remain unproven. | Host deadline and streaming byte counter terminate the whole owned workload, drain/close pipes, and reject late output. Test hanging, flooding, and detached-child cases. |
| Attempts and schema | **Open.** Verify internal retries/reconnections and actual model requests, not just adapter invocations. | **Open.** Schema mode changed tool diagnostics. Client retries and structured-output repair can spend additional attempts. | Reserve/count every actual request. Disable or account for hidden retries/repair/fallback. Independently validate bounded JSON, IDs, evidence, and attribution through the common contract. |

## Version-specific findings

Codex's source registers `request_user_input_async` when the model catalog advertises it and
registers the clock when either the feature or model metadata enables it. Turning off the shell
does not affect those branches. `environments: []` is a supported app-server thread control; an
empty dynamic-tool list is not a global deny list. The pinned configuration schema uses
`features.view_image`; it does not expose the `tools.view_image` key seen in the live configuration
reference. Bind a recipe to the installed version and verify its effective values.
See the [pinned tool construction](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/tools/spec_plan.rs)
and [pinned configuration schema](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/config.schema.json).

Codex exposes a startup model-catalog override and optional per-turn tool metadata. These are
candidates for a version-pinned experiment, not proof of an empty catalog before network dispatch.
A catalog override must preserve truthful model capabilities and attribution, reject drift, and
cover every tool source. Do not substitute a fabricated model identity or enable permissive execution
to obtain diagnostics. The [app-server contract](https://learn.chatgpt.com/docs/app-server) distinguishes
thread setup, generation, and interruption; C1 generated schemas but did not start an authenticated thread.

Claude's offline recipe combined `--safe-mode`, `--restricted`, `--tools ""`,
`--disallowedTools "*"`, `--strict-mcp-config` with an empty file, `--setting-sources ""`,
`--disable-slash-commands`, `--no-session-persistence`, `--system-prompt-snapshot off`,
`--permission-mode dontAsk`, and `--permission-prompts none`. Settings requested
`disableAllHooks: true`. Diagnostic cases also used `--no-chrome` and a zero-retry environment
setting. This is an **offline test recipe**, not an approved subscription launch command.

Managed hooks survive safe mode, and only managed-level hook suppression can disable managed hooks.
The absence of a local policy file cannot establish absence of MDM or account-delivered policy.
See [safe-mode behavior](https://code.claude.com/docs/en/cli-reference),
[hook suppression](https://code.claude.com/docs/en/hooks#disable-or-remove-hooks), and
[settings precedence](https://code.claude.com/docs/en/settings#settings-precedence).

For the strict tool-free candidate, omit Claude's schema flag, request JSON as text, and validate it
at the shared engine boundary. This keeps schema-valid accepted results as a requirement; it trades
provider-assisted formatting for rejection of invalid text. Measure that tradeoff in the existing
quality matrix. The observed 687-token diagnostic does not identify or prove execution of a specific
tool. It is enough to reject an unverified empty-tool claim. Structured-output repair also requires
attempt accounting; [the SDK documentation](https://code.claude.com/docs/en/agent-sdk/structured-outputs)
describes re-prompting on validation failure.

Claude's ordinary idle-timeout setting has a five-minute minimum when explicitly set, and its retry
watchdog can continue retrying. Use a host wall-clock deadline, remove inherited watchdog settings,
and verify zero internal retries rather than equating one process with one model attempt.
Environment filtering must also remove API credentials and provider-routing overrides from the
subscription path. [Environment controls](https://code.claude.com/docs/en/env-vars) document these
interactions; C1 did not exercise failures against a live provider.

## Architecture assessment and next experiment

| Approach | Assessment |
|---|---|
| Official completion interface with explicit empty capabilities and pre-dispatch attestation | Best interface: keep client-owned authentication while making tools, effective policy, retention, and routing observable before content. Availability is unproven for the installed clients. Upstream support may be required; no client fork is selected. |
| Version-pinned official-client configuration plus an independent process boundary | Smallest next candidate. Prove Codex's complete catalog and effective configuration offline; retain Claude's no-schema variant. It needs authenticated policy/egress/retention proof and drift rejection before any note dispatch. |
| Staged-asset executor with platform confinement | Stronger containment candidate if configuration alone cannot enforce the contract. Existing engine ports describe readable assets, allowed network hosts, time, and output limits. They do not implement an OS sandbox. A native executor needs explicit runtime/auth allowances, protected selected assets, independent network enforcement, descendant cleanup, and both-platform verification. This adds packaging and five-minute setup work; do not import the full agent-config orchestration runtime. |

The staged contract is in `core/ports.py` (`StagedExecutionRequest`, `StagedAssetExecutor`) and its
authority gate is `core/policy.py::invoke_staged_executor`. Existing security tests reject arbitrary
executor injection; they do not establish a runnable isolated subscription client. Any executor
must stay behind those privacy controls and the product-neutral engine boundary. This assessment
selects no sandbox architecture and grants no exception to the launch contract.

Next experiment: **NW0-C2, zero-inference Codex catalog and policy preflight**, capped at 45 minutes
from the remaining NW0-C allocation. Attempt a complete pre-dispatch tool inventory with explicit
model/configuration inputs and controlled ambient/managed canaries. Pass only with an empty effective
catalog, no loaded ambient content/effects, and deterministic rejection of incompatible policy.
If that interface cannot be established, stop and record the concrete staged-executor design and
credential-access constraint for review. Claude's policy, authenticated egress, retention, and active
cancellation rows remain mandatory follow-ups; C2 does not authorize a live call by itself.
