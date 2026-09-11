# NW0-C6 SDK lifecycle and supervisor interface check

- Result: **the existing SDK lifecycle can withhold input while the supervisor checks native
  metadata and rejects the request. The evidence required to permit native dispatch is incomplete.**
- Baseline: `d6c7766`, branch `docs/ob1-native-workspace-plan`, native macOS arm64.
- Limit: 30 minutes from remaining NW0-C, synthetic fixtures, zero model calls.
- Versions: installed TypeScript Agent SDK `0.3.251`, explicit native Claude Code `2.1.265`,
  Node `26.8.1`. The native executable retained C4/C5's pinned SHA-256.
- Scope: disposable private interface probes and public documentation. No product adapter,
  dependency, installed SDK/client, router, credential, or account configuration was changed.

This check follows the [C4 supervisor contract](2026-09-09-ob1-nw0-c4-claude-supervisor.md)
and [C5 Mac runtime boundary](2026-09-09-ob1-nw0-c5-native-runtime-layout.md). It focuses on
integrating the existing agent-config SDK lifecycle, rather than replacing that lifecycle.
Claude subscription remains required alongside OpenAI API, Anthropic API, and Gemini API.
Codex subscription remains deferred.

## Interface decision

Keep the official SDK's structured-output iterator, AbortController handling, and idempotent
stream cleanup as reuse candidates. Change the point where request content enters that lifecycle.
The existing adapter calls `query` with the prompt string immediately. Its readiness check is a
cached result from a separate `auth status` process; that result cannot authorize this process.

The installed SDK accepts an asynchronous iterable of user messages. Holding that iterable pending
allows initialization and metadata requests without yielding a note. The official documentation
describes this input form and waiting between messages. See
[streaming input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode).

```text
Engine selection, consent, exclusions, revisions, request limits
  -> supervisor retains selected content
  -> SDK query starts with a held AsyncIterable
       -> documented custom-process interface
       -> supervisor launches the pinned native client
       <- public initialization / account / context / MCP observations
  -> supervisor evaluates auth, policy, capabilities, and runtime evidence
       reject or unknown: stop input, close SDK stream, wait for owned processes
       permit: future single-use release bound to the current request and process
```

The native probe implements only the reject branch. It cannot yield a native user message.
The release branch runs only against an in-memory fake process with no model transport.
The Engine selection and consent boundary remains C4's existing contract; C6 did not run the
capture-to-export vertical slice or a real accepted request.

## Public surface and remaining evidence

| Interface | Observed behavior | Integration consequence |
|---|---|---|
| `query` with `AsyncIterable<SDKUserMessage>` | Native initialization, context usage, and MCP status arrived before any user frame. | The SDK lifecycle supports a pre-input gate. Widen the existing string-only QueryFactory seam. |
| `initializationResult()` and `accountInfo()` | Return the initial account snapshot. A fake account change did not update `accountInfo()`. | Treat them as observations of initialization, not a fresh authorization check. |
| `reinitialize()` | A fake process's changed account appeared in the new initialize response. | A supported refresh candidate exists, but real auth freshness, policy coverage, and the check/send race remain unproven. |
| `spawnClaudeCodeProcess` and executable override | The real SDK used the explicitly selected native binary through a supervisor-owned process bridge. | Retain the official SDK while moving launch and lifetime ownership to the supervisor. |
| Live effective settings | Runtime JavaScript contains `getSettings()`, but installed public `Query` declarations expose no corresponding method or generic request method. | Do not build a shipping adapter around an untyped cast or assume initialization is a complete policy snapshot. |

These public methods and the custom-process seam are documented in the
[TypeScript SDK reference](https://code.claude.com/docs/en/agent-sdk/typescript). The installed
declarations also contain an internal `get_settings` request type. A wire type inside that file
does not supply a public `Query` method or establish policy completeness, identity, or change handling.
C4's raw metadata experiment remains useful evidence; it is not upgraded to a supported SDK contract.

The successful native run reported no token source and first-party provider identity. It did not
exercise a real subscription. Neither its public initialization response nor the exercised context/MCP
methods supplied complete effective policy, policy generation, or a complete tool-catalog witness.
An empty MCP list does not establish an empty built-in tool catalog.

The supervisor maps the received public metadata into the existing C4 `Observation` and calls
C4's unchanged `inspect` function while the native client is alive. Missing settings are represented
as an empty object without effective settings, sources, or applied policy. C4 returns `access_missing`
and `metadata_shape`; the bridge adds explicit missing
policy/auth/runtime/catalog reasons and sends a closed decision back to the SDK host. This proves
the lifecycle-to-validator wiring and rejection ordering. It does not exercise C4's full policy
inspection on complete SDK-supplied settings or provide a native positive authorization witness.
The observation's local generation value is bookkeeping, not a client policy generation.

## Existing code to reuse and adapt

1. **Input and result lifecycle.** Preserve the existing adapter's explicit model/schema options,
   async result iteration, model attribution checks, and cleanup. Replace immediate string input
   with a held stream whose release is owned by the supervisor. Never put selected content in
   startup arguments, settings, or the system prompt.
2. **Authentication environment.** Reuse the authentication helper's separation of access modes,
   but build an explicit child environment for this adapter. Its subscription helper can retain an
   OAuth-token environment variable and unrelated provider/proxy selectors. That behavior does not
   meet Open Brain's client-owned-auth boundary. The router is not part of this integration.
3. **Authorization.** Supply same-process observations to the shared supervisor contract. Preserve
   organizational restrictions and reject absent or incompatible evidence. Cached readiness,
   a requested login mode, and a successful local status process are insufficient permits.
4. **Cancellation and ownership.** Stop the held input on rejection/abort and retain idempotent
   `close()` cleanup. The supervisor owns process deadlines, output bounds, termination, and waiting.
   SDK spawn-signal behavior includes a graceful shutdown window; it is not an immediate kill guarantee.

These are extraction boundaries, not a dependency on the workflow orchestration runtime.
The existing tool-denial policy is useful configuration, but its tool-free branch does not itself
install an OS sandbox. C5's independently enforced boundary remains necessary evidence.

## Mac launch experiment

Applying the native sandbox from an already sandboxed SDK host failed with
`sandbox_apply: Operation not permitted` and exit code 71. That is a failed architecture experiment,
not a successful native launch. The correction kept both processes confined: a trusted disposable
coordinator launched the SDK host and native client as sibling processes under separate profiles.
The SDK's public custom-process interface attached to the native process through bounded pipes.

The native profile retained C5's denial of network, real user-file data, Mach service lookup, and
fork/spawn. The SDK host received read access to its installed package and synthetic probe assets;
it also had network and process creation denied. This is a Mac interface candidate, not a minimal
shipping sandbox or a Node packaging decision. No containment grant for real credentials was added.

The coordinator checked the native binary hash, owned working directory, required startup flags,
and fixed metadata message list. Both the SDK-side writer and coordinator rejected native input
other than `initialize`, `get_context_usage`, and `mcp_status` control requests. The native combined
output cap was 64 KiB; the separate SDK bridge framing cap was 128 KiB. The coordinator reserved
teardown within a 20-second process budget. No user frame or selected note reached the native client.

## Executed checks

- The final real-SDK/fake-process suite passed 15 assertions across five scenarios: immediate string
  input, held rejection, fake-only structured result and single release, abort while held, and cached
  versus refreshed account metadata. The fake has no subprocess, network, or inference capability.
- Three sibling bridge iterations passed seven, nine, and finally ten native checks. The final
  iteration checks C4's missing-auth and missing-policy rejection together. SDK and native process
  exits were both zero, and stream close ran once. The final native journey took about 0.67 seconds
  on this Mac.
- A first harness run failed because its receipt write was outside the permitted runtime directory.
  That run and the nested-sandbox failure remain in private evidence. Successful reruns are identified
  separately; repeated fake passes do not increase the unique 15-assertion count.
- Native receipts contain only metadata exchanges, with zero model calls and no native user input.
  These timings are native Mac measurements, not Linux or emulated desktop results.

The receipt/source verifier passed 56 checks, including four offline synthetic auth variants through
C4: missing credentials, an API source, another provider, and an unverified subscription source.
Each also rejected the absent policy snapshot. Source hashes remained unchanged and both final
owned processes were absent after teardown. This verifier is local verification, not an independent
agent review. Documentation whitespace and workflow lint checks passed.

The full product suite, native build, and Homebrew smoke were not run for these documentation and
private-prototype changes. Active-inference cancellation, native retention, token refresh, Keychain reuse,
provider egress, Linux containment, and final runtime packaging remain unproven.

## Architecture choice and next action

| Approach | Decision |
|---|---|
| Small shared provider component using an official completion interface with client-owned auth and complete policy/capability evidence | Best architecture. The existing SDK supplies a usable lifecycle seam; the missing evidence contract and cross-project/runtime distribution remain pending. |
| Existing TypeScript SDK behind a supervisor-owned custom-process bridge | Pre-input metadata and live rejection now work on this Mac. Node distribution and the remaining native auth/policy controls must be measured before selecting it for Open Brain. |
| Python supervisor using raw client control messages, or a cast to runtime-only SDK methods | Prior metadata experiments show mechanisms, but supported-interface stability and complete policy evidence remain unproven. Not selected as a shortcut around those gaps. |

Next, write the concrete provider/supervisor interface contract around this proven held-input seam:
inputs owned by the Engine, supported versus missing observations, release/revocation authority,
and client-owned authentication requirements. Specify the missing upstream API requirements locally
before any further authentication experiment; do not repeat lifecycle discovery or silently weaken
the gate. This interface check did not identify a supported, demonstrated mechanism for complete
same-process policy evidence or isolated existing-Keychain reuse. Native dispatch remains closed.
