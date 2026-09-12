# NW0-C5 native runtime-layout proof

- Result: **the tested macOS file/process boundary works; Claude subscription dispatch remains closed**.
- Baseline: `131e9d9`, branch `docs/ob1-native-workspace-plan`, macOS 26.3 (25D125), arm64.
- Limit: 45 minutes from remaining NW0-C, synthetic fixtures, zero model calls.
- Changes: private disposable C/Python probes and public documentation. No product runtime,
  dependency, installed-client configuration, real login, or Keychain changes.

This proof advances the [C4 supervisor design](2026-09-09-ob1-nw0-c4-claude-supervisor.md).
Claude subscription remains required alongside OpenAI API, Anthropic API, and Gemini API.
Codex subscription remains deferred. No result here authorizes native note dispatch.

## Pass/fail contract

| Question | Pass criterion | Result |
|---|---|---|
| Can auth data be separated from unrelated files? | Read selected synthetic credential/bootstrap files while denying adjacent history/settings and unrelated note contents. | Passed for the tested file-backed fixture. Real Keychain access is unproven. |
| Can required writes be narrow? | Allow a selected synthetic credential replacement and owned temporary state, while denying unrelated writes and escape paths. | Passed with a C helper and one explicitly allowed temporary filename. Native token refresh/rotation is unproven. |
| Does the official client start inside that boundary? | Pinned native client returns bounded metadata with network, real user-file data, Keychain lookup, forks, and other executables denied. | Passed after identifying required loader and timezone reads. Two earlier startup failures are retained. |
| Do unavailable or changed policies reject? | Required synthetic policy failures stop launch; an observed native settings change prevents authorization. | Passed for denied/missing/malformed host policy and the observed routing change. Complete native policy discovery and atomic change handling are unproven. |
| Is the workload bounded? | Deny tested fork/spawn paths; terminate and wait for the owned process, including escalation, without terminating a separate canary. | Passed for the synthetic workload. Native metadata processes were also waited for; active inference and exhaustive descendant confinement remain unproven. |

## Tested layout

```text
supervisor: owns request content and rejection decision
  |
  +-- selected synthetic auth namespace
  |     .credentials.json       readable by client; read-only in native runs
  |     .claude.json            readable bootstrap fixture; read-only in native runs
  |     history / settings      file-data access denied
  |
  +-- required synthetic host policy
  |     managed-settings.json   readable, not writable by the workload
  |
  +-- per-run private runtime
  |     cwd / tmp               permitted data reads and writes
  |
  +-- unrelated note fixture    data reads and writes denied
```

The private profile denies file reads by default, then permits system library/share data, timezone
data, the pinned executable, selected fixture files, and the owned runtime. It also permits filesystem
metadata and opening the root directory. This is a file-data allowlist, not a claim that filesystem
names or all OS metadata are hidden. System read grants are broader than individual library files;
this is a tested Mac candidate, not a minimal shipping policy or a cross-platform sandbox.

Network operations and Mach service lookup are denied. Process execution permits the designated
binary only; process creation is separately denied. The C helper's `fork`, same-binary `posix_spawn`,
and other-binary `posix_spawn` checks all failed with permission errors. File tests rejected reads and
writes through an escaping symlink and creation of a hard link to an unrelated fixture. They do not
establish resistance to a hostile same-user process replacing inputs outside the supervisor's control.

Native cases retained the C4 metadata-only reader, binary hash check, fixed control-message list,
16-second read window with teardown reserve inside the 20-second cap, and 64 KiB combined-output
ceiling. There is no completion writer. All native network was denied, including during the synthetic
status check. No user/model turn, login, logout, or explicit refresh command was submitted.

## Native startup findings

The first restrictive profile stopped the C helper in the macOS loader. Controlled comparisons and
the helper's crash stack narrowed the issue to loader startup; permitting a data read of the root
directory allowed the helper to run. Broad user-file access was not added.

Claude then exited before metadata with a separate failure. Exact-PID sandbox diagnostics recorded a
denied ICU timezone-data read. Adding read-only access to the system timezone-data directory allowed
the same pinned client to reach metadata. These failures matter: a profile that prevents execution
cannot serve as positive evidence for a working integration.

The client also attempted bootstrap locks/temporary writes and other startup operations outside the
allowed runtime. Denied bootstrap writes did not prevent the successful metadata cases, and their
stderr was empty. Successful metadata therefore does not establish that all expected runtime writes
or authentication operations succeeded. OS crash reports and sandbox diagnostics exist outside the
owned runtime for failed startup cases; no inference-retention claim is drawn from these experiments.

## Authentication evidence and limits

A nonfunctional synthetic credential file produced a `Claude Max` account label. Two subsequent
metadata processes reused that same synthetic namespace with separate runtime directories. The
auth/bootstrap/history fixtures remained byte-for-byte unchanged.

A separate official `auth status` process reported `loggedIn: true`, `authMethod: claude.ai`, and
`subscriptionType: max` from the synthetic file while network was denied. The documented command
returns local authentication status as JSON. Its success here is evidence of local source selection,
not validation of a subscription or of the fake credential against a server. See the official
[CLI reference](https://code.claude.com/docs/en/cli-usage).

The initialization response did not contain C4's required `tokenSource` field. C4 therefore continued
to emit its conservative `access_missing` reason even when the synthetic Max label was present.
Interpret that reason as missing required source evidence, not proof that no local credential was
read. The native content path remained closed in every case. A status result from another process
cannot authorize the process that would receive notes.

The C helper demonstrated a temporary-file write and atomic replacement within explicitly granted
paths. Native runs kept credentials read-only. The helper's filename and synthetic bytes do not
establish Claude's actual refresh locks, temporary filenames, write ordering, or recovery behavior.

For real login reuse, the adapter must retain the user's existing official namespace and map the
credential and bootstrap locations separately. Default bootstrap storage and a custom config
directory are not interchangeable. A fresh `CLAUDE_CONFIG_DIR` changes both file storage and the
macOS Keychain entry; it cannot serve as an unnoticed substitute for the existing login. These are
documented in [authentication](https://code.claude.com/docs/en/authentication#credential-management)
and [settings locations](https://code.claude.com/docs/en/settings#find-or-create-your-settings-files).

The no-fork/no-Mach-lookup candidate has not demonstrated client-owned Keychain access. No live
Keychain entry or search list was read or changed. Do not copy tokens into the tested file layout,
force a second login, silently switch to API billing, or claim that file fallback proves Keychain reuse.

## Policy and termination evidence

The host-policy preflight used a sandboxed helper to test readability, decoded the synthetic policy,
and supplied the resulting host policy through the official client's existing parent-policy input.
Denied, missing, and malformed fixture cases rejected before a native process was created. The
readable case appeared in native settings. This checks a designated host input; it does not enumerate
organizational sources or let the parent override higher-priority policy. The official
[managed-settings contract](https://code.claude.com/docs/en/managed-settings) remains authoritative.

A separate live metadata case changed routing between settings reads. C4 detected the change and
rejected while the client was alive. The host-file readability check, subsequent read, and native
observation are not one atomic operation. Neither this result nor a timestamp closes the interval
in which an external policy source could change before dispatch.

Two synthetic deadline cases exercised normal termination and SIGKILL escalation after SIGTERM was
ignored. Both completed within two seconds, were waited for, and left an unrelated canary process
alive. The coordinator then terminated and waited for its own canary. These are active synthetic
process tests, not cancellation tests during a model completion or remote-policy helper execution.

## Executed checks

- The completed OS fixture run passed 25 permission/operation checks, including a normal owned
  workload stop. Earlier startup diagnostics and the first 23-check run are retained separately.
- Three host-policy failure fixtures stopped native launch. Two additional deadline cases passed.
- Nine official metadata processes were retained: two startup failures and seven successful live
  rejection cases. One additional official process ran synthetic `auth status`.
- Every metadata process stayed within 20 seconds and 64 KiB combined output. The slowest successful
  case was denied auth access at 6.797 seconds. These native Mac timings are not emulated Linux results.
- No selected note bytes were sent and no model calls were made. Private receipts retain fixture,
  profile, output, timing, PID and source evidence. Public documentation contains no raw private logs.

No shipping code, dependency, packaging, or public command was added. Linux, active inference,
complete tool-catalog proof, enforced provider egress, attempt accounting, and active retention remain
separate launch gates. Prior C1–C5 effort remains charged; no attempt allowance was reset or transferred.

## Decision and next action

Keep the file-data allowlist as a feasible Mac prototype. It has demonstrated more than startup flags,
but it has not established the full authentication/runtime boundary required by C4.

| Architecture | Decision |
|---|---|
| Official completion interface with client-owned auth, complete policy/capability identity, and isolated transient state | Best architecture; supported availability remains unverified. Check this interface before investing in a shipping wrapper. |
| Supervised native client preserving the existing login | File-backed primitives and metadata startup now have evidence. Keychain access, actual rotation, complete same-process auth/policy reporting, and their containment still need a concrete mechanism. |
| Dedicated login or token-copy bridge | Not selected. Neither substitutes for the agreed existing-login and client-owned-auth requirements. |

Next, perform a bounded supported-interface check for client-owned Keychain access and complete
same-process auth/policy evidence: at most 30 minutes from remaining NW0-C, zero model calls, and
no live credential access. Bring forward a concrete mechanism or record its absence before
another authentication experiment. An authenticated no-note probe comes only after that boundary is
defined; native note dispatch remains closed. This result does not authorize reducing launch scope.
