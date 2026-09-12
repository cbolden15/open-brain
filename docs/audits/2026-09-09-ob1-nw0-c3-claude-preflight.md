# NW0-C3 Claude policy and retention preflight

- Result: **offline preflight complete; subscription isolation is not established**.
- Baseline: `51ee523` on `docs/ob1-native-workspace-plan`, macOS arm64.
- Client: unmodified Claude Code `2.1.265`. Separate policy-resolver evidence uses the existing
  TypeScript Agent SDK `0.3.251`, whose package identifies Claude Code `2.1.251`.
- Scope: at most 45 minutes from remaining NW0-C effort, zero model calls, synthetic fixtures.
  No real notes, account inspection, credential changes, product code, dependency installation,
  Linux execution, or publication.

Claude subscription remains required alongside OpenAI API, Anthropic API, and Gemini API. Codex
subscription remains deferred. This checkpoint supplies narrower evidence and identifies the next
enforcement work; it does not pass the subscription gate or change the four-path launch scope.

## What ran

Nine native-client processes used C1's no-schema recipe, private stdio, empty built-in/MCP tool
selection, safe/restricted modes, disabled session persistence and system-prompt snapshots, and
disposable configuration/temp locations. A synthetic system-context marker replaced the default
system prompt. Only metadata/control requests were sent. No user message, completion request,
credential-status command, or login operation was submitted.

The marker was a synthetic launch argument for this retention experiment. This does not verify the
production requirement to send selected note context only through private stdio. Notes and credentials
must never be placed in process arguments.

The OS test sandbox denied network connections and writes outside each case's runtime directory.
It denied selected real configuration, credential, and managed-policy locations, including Keychain
and preference-service access. The first seven native cases allowed execution of the exact Claude
and Node binaries; the final two allowed only Claude. Other executables were denied. The later
cases also explicitly denied the documented Anthropic profile directory after documentation review
identified that additional credential source. Earlier cases do not prove that particular read denial.
All initialization responses reported token source `none`; one deliberate negative case additionally
reported the nonfunctional synthetic API key described below. No real credential was supplied or copied.

This is a test sandbox with selected read denials, not a complete filesystem-read allowlist or a
shipping containment mechanism. Denying real policy and credential access deliberately prevents
these runs from certifying the user's authenticated policy or login compatibility.

| Native case | Elapsed time | stdout / stderr bytes | Observation |
|---|---:|---:|---|
| Baseline metadata | 0.917 s | 28,748 / 0 | `get_settings` returned effective settings, their sources, and the applied model. Context diagnostics contained the 12-token synthetic system prompt and no system-tool category. |
| Parent policy with invalid cleanup value | 0.317 s | 28,851 / 185 | Kept permission denials; warned that `cleanupPeriodDays: 0` was invalid. No claim that this was a valid whole-policy fixture. |
| Synthetic remote cache | 0.704 s | 28,748 / 0 | Cached synthetic command/HTTP hooks and routing did not appear in effective settings in this unauthenticated run. This did not exercise managed hooks. |
| In-memory flag change and EOF | 0.284 s | 29,499 / 0 | A second settings read reflected the changed model and endpoint setting. EOF exited 0 without escalation. No provider request established the endpoint actually used. |
| Idle SIGKILL | 0.265 s | 28,748 / 0 | Exited -9. Left a session-discovery metadata file; no context marker was present in runtime files. |
| Unsupported control request | 0.734 s | 28,897 / 0 | Rejected the unknown subtype, then acknowledged interrupt. Did not echo the extra synthetic context field. This is a control-validation failure, not model-output validation. |
| Controlled idle deadline | 0.515 s | 28,748 / 0 | A 250 ms idle hold ended with host SIGTERM. This is not an active inference timeout. |
| Valid parent policy | 0.956 s | 28,851 / 0 | Kept `permissions.deny`; dropped parent hook suppression, hooks, routing, and cleanup settings without the earlier validation warning. |
| Deliberate synthetic API-key override | 18.521 s | 28,783 / 0 | Initialized with `apiKeySource: ANTHROPIC_API_KEY` despite effective `forceLoginMethod: claudeai`. No model request or network connection was permitted. |

Each native case completed its control responses within the 20-second process cap and 64 KiB
metadata-output limit. Teardown took 0.009–0.019 seconds; none needed escalation. SIGTERM cases
exited 143. These are individual
probe timings, not cold/warm startup benchmarks or five-minute installation measurements.

The separate SDK resolver process completed six synthetic policy cases in 0.492 seconds, producing
4,862 stdout bytes and no stderr. A 0.244-second sandbox self-test allowed an owned runtime write
and returned `EPERM` for a denied synthetic read, outside write, other-program execution, and
loopback connection. It used Node as its sole permitted executable. Those negative controls verify
the tested sandbox mechanisms, not every path or operation an authenticated Claude runtime needs.

## Policy and routing findings

The installed client's `get_settings` control request is useful: it returns `effective`, per-source
settings, and applied model information before a user turn. The tested response does not attest
the complete model-visible tool catalog, current authenticated remote-policy freshness, or the
effective network destination. An observed in-memory settings change also means a snapshot cannot
authorize arbitrary later work without a policy-change strategy.

The installed SDK's `resolveSettings()` supplied a separate, observable policy-merging experiment:

| Synthetic policy | Resolver result |
|---|---|
| Empty | Empty effective settings and source list. |
| Parent-only restrictions plus other settings | Retained permission denials; dropped `disableAllHooks`, model, routing environment, and cleanup settings. Origin was `parent`. |
| Server-managed command and HTTP hooks | Retained both hook configurations and `disableAllHooks: false`; parent restrictions were absent. Origin was `remote`. No hook executed in this resolver process. |
| Server-managed routing and login | Retained endpoint, Bedrock selection, retry-watchdog settings, Console login selection, model, and cleanup period. This was policy data, not an executed provider route. |
| Explicit admin opt-in to parent merging | Added the parent's permission denials while retaining admin hooks and `disableAllHooks: false`. Parent hook suppression still did not apply. |
| Policy helper | Returned the helper declaration. The resolver did not execute it or produce its effective output. |

These results agree with the official [parent-policy precedence rules](https://code.claude.com/docs/en/managed-settings#let-an-embedding-host-add-policy).
Parent settings cannot stand in for controlled admin policy. The resolver's installed type contract
explicitly calls its result a raw settings cascade and excludes policy-helper execution. Its older
bundled client version also prevents treating it as an attestation for Claude Code `2.1.265`.
No new SDK dependency was added to Open Brain.

The deliberate API-key case establishes a distinct failure: requesting `forceLoginMethod: claudeai`
does not establish subscription-only runtime selection. The child environment normally contained
only explicit allowed names; this negative case deliberately added one nonfunctional synthetic key.
The client then reported the API-key source. A future adapter must reject an API-key/profile/gateway
or otherwise indeterminate source when subscription was selected, even if `apiProvider` says
`firstParty`. The official [authentication precedence](https://code.claude.com/docs/en/authentication#authentication-precedence)
explains why provider identity alone does not identify the billing/access path.

No authenticated managed-hook execution, live policy refresh, policy-helper failure, or complete
pre-dispatch incompatible-policy rejection was established. A missing source in a no-auth sandbox
is not a clean-policy verdict. Do not suppress organizational policy to make a test pass.

## Retention and authentication boundary

Every native case's writable runtime tree was inventoried before and after execution, including
file hashes and a context-marker scan. The client wrote configuration and a backup. The SIGKILL
case also left a session-discovery record with PID, session identity, working directory, and client
metadata. It was not a conversation transcript. No inspected runtime file contained the supplied
system-context marker. No transcript or system-prompt snapshot was observed.

That is evidence for the tested startup/control paths only. Success, schema-invalid model output,
active interruption, inference timeout, crash during a turn, automatic resumption, and provider-side
retention remain untested. The private harness intentionally retains synthetic request/response
receipts outside the product runtime; those evidence files are not product retention claims.
Stale session-discovery files must be included in cleanup/resumption checks even with persistence off.

`CLAUDE_CODE_TMPDIR` was set alongside the ordinary temporary-directory variable, because the
client documents its own [temporary-file location](https://code.claude.com/docs/en/env-vars).
A disposable `CLAUDE_CONFIG_DIR` also selects a different credential location, including a distinct
macOS Keychain entry. It therefore does not prove reuse of an existing official login. See the
[credential-storage contract](https://code.claude.com/docs/en/authentication#credential-management).

Read-only inspection of agent-config confirmed useful patterns for abort/close handling, explicit
access modes, and denying tools. Its current adapter cannot be copied wholesale: it requests schema
mode, its environment helper preserves an OAuth-token variable, and its credential-name scrub does
not remove every provider-routing/retry variable. Open Brain still requires client-owned login,
no token relay, a full environment allowlist, independent attempt limits, and its shared JSON validator.

## Architecture consequence and smallest next proof

| Approach | Disposition |
|---|---|
| Official completion interface with a complete pre-dispatch capability/policy/auth snapshot and isolated transient state | Best interface. Availability and supported login reuse remain unproven. Keep it as the preferred upstream-supported option. |
| Version-matched configuration with external supervision | Native settings observability is now demonstrated. Configuration alone still fails the subscription-selection test and cannot override managed hooks. It needs independent policy rejection, credential-source validation, and runtime containment before receiving notes. |
| Native staged executor using the existing engine port | Next containment candidate. Separate selected content from runtime/auth assets, constrain filesystem/process/network authority, and own deadlines/output/cleanup. The engine port is an interface, not an OS sandbox. Packaging and both-platform proof remain required. |

The next bounded NW0-C step should produce a concrete Claude supervisor design and a zero-inference
prototype of its pre-dispatch rejection path. Cap it at 45 minutes from the same cumulative allocation.
Use a version-matched official policy interface where available. Require observable handling of admin
hooks, routing conflicts, policy helpers, missing/stale policy, and changes after the snapshot. Define
how the official client can reuse its own login while transient state stays inside the owned runtime;
do not copy credentials into that runtime or adopt an undocumented storage override as a solution.

The content pipe stays closed until the policy, access mode, capabilities, and runtime controls are
established. A conflicting or indeterminate source returns a bounded setup/policy error before any
note bytes. A policy change must invalidate authorization; a one-time snapshot alone is insufficient.
This recommends the next experiment, not a shipping sandbox, new dependency, or passed architecture
gate. Authenticated egress, actual request accounting, and active retention/cancellation proofs follow
only after concrete enforcement exists. Do not repeat these metadata cases without a relevant change.

Private evidence contains scripts, exact sandbox/configuration fixtures, raw receipts, SDK type/source
hashes, runtime inventories, and verification assertions. Claude's binary SHA-256 is
`164b09eb800dedb9bb06304129fbf07743ab9db972ae07333ef4f18cde0cb8d5`.
The cumulative ledger preserves prior C1/C2 effort and all attempt counters. Four active paths retain
the 64-attempt maximum, 32 mandatory initial/incremental samples, and unchanged per-path limits.
