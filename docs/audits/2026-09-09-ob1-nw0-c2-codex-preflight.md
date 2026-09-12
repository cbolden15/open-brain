# NW0-C2 offline Codex catalog and policy probe

- Result: **isolation not established; Codex subscription deferred**.
- Baseline: `243b913` on `docs/ob1-native-workspace-plan`.
- Target: installed Codex CLI `0.153.4`, macOS `26.3.0`, arm64.
- Scope: the existing 45-minute NW0-C2 limit; zero inference calls, no real note input,
  login inspection, credential changes, product implementation, or Linux execution.

During this checkpoint the user authorized leaving Codex subscription for a later implementation
if its isolation work proved too costly. The coordinator selected that option after the offline
results below. The [current plan](../plans/2026-09-09-ob1-native-workspace.md) now requires four
launch paths: OpenAI API, Anthropic API, Claude subscription, and Gemini API. OpenAI API access
remains in scope. Codex is a deferred adapter, not an enabled but unverified launch option.

## What ran

The unmodified native client ran through private stdio with a disposable client-state directory,
a schema-validated configuration, and synthetic user/project instruction canaries. The child-only
configuration-directory override used the client's normal configuration mechanism; it did not
change the parent environment or the user's real configuration. No token was supplied or copied.

A test sandbox denied network access, writes outside the case directory, execution of other
programs, and selected real configuration, skill, credential, and managed-policy paths. The thread
case also denied the preference-service lookups and preference files identified for local managed
configuration. These controls constrain the experiment; they are not a verified shipping sandbox.
In particular, an empty requirements response in this environment cannot certify an authenticated
account's effective policy.

| Retained case | Result |
|---|---|
| Incorrect plan-tool setting | Client exited 1 before initialization: a boolean was supplied where `UpdatePlanToolConfig` was required. |
| Incorrect user-input-tool setting | Client exited 1 before initialization: a boolean was supplied where `ExperimentalRequestUserInput` was required. Both settings were then corrected to nested `enabled = false`, and the whole fixture was validated against the pinned JSON schema before another launch. |
| Initialize and read configuration/requirements | Completed in 0.075 seconds; 26,370 stdout bytes, no stderr. Explicit model/provider, ChatGPT login restriction, disabled web search, zero project-document bytes, and requested feature values appeared in configuration evidence. Requirements were `null` in the isolated environment. |
| Ephemeral thread and control diagnostics | Completed in 0.073 seconds; 30,095 stdout bytes and 211 stderr bytes. Returned an idle `gpt-5.6-sol`/`openai` thread with no turns, empty runtime workspace roots, and a read-only/network-disabled sandbox. Hook and MCP inventories were empty. **The synthetic user-level `AGENTS.md` remained an instruction source.** A real host-skill discovery attempt was denied by the test sandbox. |
| Conflicting request and unsupported history read | The thread case rejected combined `permissions` and `sandbox` fields with `-32600`. It also rejected `includeTurns` for the ephemeral thread. Neither response is a managed-policy-conflict test or a complete tool catalog. |

The two successful processes terminated with SIGTERM in 0.004 seconds each. These are idle-process
observations, not active-turn cancellation evidence. An earlier launch hit a probe-harness teardown
error before saving its output; that failure is recorded separately. The harness was changed to
persist output first, retain the owned PID, and check process exit before signaling. Its initial
unsaved output is not counted as a verified case.

The disposable client state also contained log, memory, goal, queue, and state SQLite files with
sidecars despite ephemeral thread mode and the selected disabled features. File presence does not
prove those features were active or that prompts were retained. It does establish that the future
retention test must inspect runtime stores beyond the thread transcript directory.

Every retained case stayed below the 20-second process and 64 KiB metadata-output limits. Requests
were limited to initialization, configuration/requirements reads, thread creation, hook/MCP
inventory, and thread reads. No `turn/start`, review, command, tool, or model request was sent.
Successful startup does not establish subscription availability or model-response attribution.

Private evidence includes scripts, complete synthetic configurations, raw receipts, installed
binary hash, pinned source, generated-protocol inventory, and verification results. The binary
SHA-256 is `b973d440acac501fd2594a43e7ca9ce41e0a65b9dfb28d0d7a7837c99e1261e3`.

## Why the isolation gate did not pass

| Requirement | Finding and disposition |
|---|---|
| Complete pre-dispatch tool inventory | The installed generated protocol and documented API expose component inventories, but no complete model-visible tool-catalog request was identified. `thread/start` returned no such catalog. Empty hooks/MCP and disabled flags are insufficient. **Unproven.** |
| No ambient instruction loading | The explicit empty environment/root lists, replacement base/developer instructions, and `project_doc_max_bytes = 0` did not suppress the synthetic user-level instruction source. The pinned home-instruction provider reads its own `AGENTS.md` independently of that project-byte setting. **The tested recipe failed.** |
| No host-skill discovery | The process attempted a host skill-directory walk despite `skip_host_skill_discovery = true`, disabled skill instructions, and disabled bundled skills. The sandbox denied access. This proves an attempted scan, not that private skill content was loaded or sent. **The flags alone did not establish the boundary.** |
| Deterministic incompatible-managed-policy rejection | The release CLI supplies default loader paths. The separate app-server source has debug-only policy-path hooks; these are not supported release-client controls. Real system/account policy was not modified and no synthetic managed requirement was injected. The malformed-config and conflicting-parameter cases do not substitute for this test. **Unproven.** |

Source anchors: [home-instruction loading](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/codex-home/src/instructions/mod.rs),
[CLI app-server launch](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/cli/src/main.rs), and
[debug-only app-server hooks](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/app-server/src/main.rs).
The [official protocol reference](https://learn.chatgpt.com/docs/app-server) describes configuration
and component inventories. Source inspection is tied to `rust-v0.153.4`; it is not a reproducible-build
attestation for the installed binary. No claim of impossibility is made about other client versions.

## Deferred implementation boundary

The best interface remains an official completion mode with an explicit empty capability set and
effective policy/tool metadata available before note submission. Its availability was not established
for this client. Preserve that option for a future upstream-supported integration.

If a staged executor is needed, keep the engine's existing readable-asset, allowed-host, deadline,
and output contract. A concrete native executor must isolate model-readable assets from runtime
and authentication files; apply platform-level read/write/process restrictions; constrain provider
and auth egress; reject incompatible managed policy; and bound retention and descendant cleanup.
It must pass those cases on both supported platforms. A portable interface declaration is not
that implementation.

The unresolved credential constraint is specific: a disposable client-state directory avoids the
user's ambient instructions but does not automatically reuse the user's official login. Granting
access to the ordinary client-state directory can reintroduce instructions and skills. Open Brain
must not solve that by copying or relaying tokens. A future design must prove client-owned
authentication within a separate runtime boundary, or use a supported auth-only separation exposed
by the official client. No sandbox, credential broker, runtime dependency, or client fork is selected
by this audit.

Codex implementation, installation/login discovery, and timed acceptance are deferred together.
The existing common provider contract remains the future integration seam; no placeholder Codex
adapter or premature abstraction is required now. Its spent NW0-C effort remains charged. The
original 80-attempt outer ceiling and 16-per-path limits remain; four launch paths allow at most
64 active attempts, with 32 reserved for initial/incremental platform samples. Codex's unused
allowance is not transferred to other paths.

Next action: continue Claude's bounded policy/retention preflight from C1. Its remaining isolation
requirements still block note dispatch; Codex deferral does not pass Claude, the three direct API
proofs, frozen Graphify packaging, or desktop acceptance. C1 remains the historical five-path audit;
this user-authorized scope decision supersedes its launch-count statements.
