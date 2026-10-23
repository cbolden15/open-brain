# Guided Slack workflow

`open-brain source guide` takes one owner from choosing a Slack channel to a scoped MCP client that can read selected, approved Work messages. It drives the existing collector, sharing, consent and agent setup services. You never copy internal account, channel, receipt, source or preview IDs between commands. Each write still has its own owner checkpoint.

This is a contributor candidate at schema/runtime/Portable `22/16/17` on branch `feat/w5-guided-slack-workflow`. It has only been run against synthetic fixtures. Real Slack access, a real client or model, and a private pilot are separate owner-authorized steps. The lower-level commands in the [Slack capture guide](slack-capture-workflow.md) and the [cited-recall guide](slack-decision-recall.md) still work and remain the reference for each step.

Slack coverage beyond the selected approved messages is unknown. The guide never reports a complete channel history.

## Before you start

For synthetic use, create a disposable Brain and a disposable collector state, and point every path below at them.

Before the guide touches a real Brain:

1. Export a full Portable Brain and keep it outside the Brain directory.
2. Back up the Brain directory, the collector state root and the consent state file with the Brain closed.
3. Restore that backup into a scratch location once and confirm `open-brain status --json --data-dir <scratch>` reads it.
4. Stop network-backed client sessions and remove or confirm the absence of every unscoped Open Brain MCP setup, as described in [closing unscoped access](slack-decision-recall.md#1-close-unscoped-access-before-importing).
5. Connect the Slack account with the optional collector as described in [Slack authorization](slack.md). The guide never connects accounts or reads credentials.

The optional collector is a separate install. Core installation does not install or enable it, and the guide does not schedule collection.

## Commands and common flags

Every command needs the same four absolute paths, each given once:

| Flag | Meaning |
|---|---|
| `--data-dir` | The Brain directory. The guide never selects a default Brain and never bootstraps one. |
| `--collector-state` | The collector state file. Its parent directory is the collector state root; `live` sits beside it. |
| `--workflow-state` | The private workflow journal, outside the Brain and collector stores. |
| `--collector-executable` | The installed `open-brain-collector`. It must be owner- or root-owned and not group- or world-writable. |

The three commands:

- `open-brain source guide status [--json] COMMON` reads the journal and the selected source's capture state without writing anything, creating files or taking locks.
- `open-brain source guide advance COMMON --decision-file FILE --json` performs one named decision and prints one JSON result.
- `open-brain source guide slack COMMON` runs the same decisions from terminal prompts. It needs an interactive terminal.

On macOS, a path that starts with `/var`, `/tmp` or `/etc` is rewritten to `/private/...` only when the operating system link points there. Any other symlink in a path refuses as unsafe.

A decision file is a JSON object, 0600 under a 0700 directory and at most 65,536 bytes:

```json
{
  "schema_version": 1,
  "workflow_id": "guide:example-1",
  "expected_revision": 0,
  "action": "start",
  "confirmation": null,
  "arguments": {}
}
```

A write first returns `needs_confirmation` with the exact intent. Read it, then send the same action and arguments again with `"confirmation": {"intent_sha256": "<from the intent>", "decision": "approve"}` and the new `expected_revision`. A changed intent needs a new confirmation.

Exit codes: 0 for completed or cancelled, 2 for an invalid command or decision, 3 for `needs_confirmation` or `partial`, 78 for a refusal or unknown outcome. The interactive `slack` command also exits 3 when it stops at its five-page limit. A `completed` journal refuses every action, including `cancel`, with exit 2 and `invalid_request`.

## The journey and its checkpoints

| Step | Owner checkpoint | What the write does |
|---|---|---|
| `start` | Running it | Creates the private journal bound to this Brain, collector state root and executable. |
| `preflight` | None | Records the client kind, scope, project, consent, policy and runtime paths, provider and launch context. Reads the client settings inventory only. |
| `accounts`, `resources`, `choose` | None | Lists connected accounts and channels and records the chosen channel and date floor. Journal only. |
| `configure` | Confirm | Creates or updates the collector source for that channel as Work. It stays disabled; nothing is scheduled. |
| `preview` | Confirm | Fetches one Slack history page and keeps it pending in the collector. |
| `import` | Confirm | Imports exactly that pending page as local Work captures. It never fetches the next page. |
| `inspect` | None | Shows the complete current text of one imported message. |
| `acknowledge-inspection` | Confirm | Records that you saw that exact text. Nothing else can use a message you have not acknowledged. |
| `create-destination` | Confirm | Creates a new Work space for this workflow. v1 refuses every existing space. |
| `route` | Confirm | Routes one acknowledged message into that space. |
| `select` | Confirm | Creates the selection of 1 to 24 messages that may be released. |
| `consent` | Confirm | Grants Work-tier consent for the one chosen provider. It never widens an existing consent. |
| `release-preview` | Confirm | Builds the external payload for one message. |
| `approve` | Confirm, after reading the full payload | Approves that one release and creates one separately captured copy. |
| `policy-preview` | None | Shows the complete scoped launcher policy: search and content-read only, one space, Work tier, one provider. |
| `policy-apply` | Confirm | Creates the policy file. It never overwrites one. |
| `setup-preview` | Confirm | Computes the client setup for that policy. Every grant except search and content-read stays off. |
| `setup-apply` | Confirm | Writes only the Open Brain owned fragments. Unrelated settings are kept. The client and model are not launched. |

The interactive `slack` command fetches at most five pages per run and asks before each page. When setup is applied it prints the approved copy IDs, how many selected messages have a current approved copy, and a reusable recall task to give the client.

The journal becomes `completed` once setup is applied and every selected message has a current approved copy. Until then it stays `active`, so you can release the rest or repair an edited message in the same journal. A completed journal refuses every further action. If the `completed` marker itself could not be written after a successful `setup-apply` or `approve`, that step still reports its real outcome and the journal stays `active`; the next action records the marker and refuses with `invalid_request`. Repair a message edited later with `open-brain sharing preview` and `approve`, as in the [cited-recall guide](slack-decision-recall.md).

## Recovery

The guide never retries on its own. A lost acknowledgement or crash leaves the operation `unknown`, and every later action refuses until you resolve it.

- Reconcile: `reconcile` with the operation's `operation_sequence` replays the exact saved request for `create-destination`, `route`, `consent`, `select`, `release-preview`, `approve` and `import`. The authority returns its existing receipt or refuses. No new operation ID is created.
- Repreview: an unknown `policy-apply`, `setup-preview` or `setup-apply` is never replayed. Inspect the current policy file or client configuration, then start again from `policy-preview` or `setup-preview`. An unknown `configure` or `preview` is also halt-only; inspect the collector state and choose a new intent.
- Halt: a refused replay, or a refusal that cannot prove the earlier write did not commit, marks the journal `halted`. Only `cancel` is accepted after that.
- Cancel: `cancel` marks the journal cancelled. It keeps every receipt and capture, and it never resets the source, revokes an approval, removes configuration or deletes custody.

`journal_busy` means another guide operation holds this workflow's lifetime guard. Wait for it to finish. A stale guard is never cleared by elapsed time. To start over, remove the journal file yourself after every guide process has stopped. Receipts, captures and approvals stay; the safe resume evidence is lost.

The journal is bound to the device and inode of the collector state root as well as its path. Moving another collector state into the same path makes every command refuse with `binding_mismatch`. Restoring the root from a backup, copying it or remounting it also gives it a new identity, so the guide treats it as a swap: `status` and every `advance` report `binding_mismatch`, and the guide cannot resume that journal. `cancel` stays available in that one case. It is journal-only, keeps every receipt and unresolved operation, and makes no collector or engine call. Start a new journal afterwards. A different Brain, collector state path or executable still refuses `cancel` too.

## Limits

| Limit | Value |
|---|---|
| Slack messages per preview page | 1 |
| Pages per interactive run | 5 |
| Journal size | 256 KiB |
| Journal operations | 1,000 |
| Receipts retained by one journal | 1,000, refused with `capacity_reached` when a page could exceed it |
| Collector retained custody items, shared by all sources | 256, refused with `capacity_reached`; nothing is deleted to make room |
| Messages in a selection | 1 to 24 |
| Providers per workflow | 1 |
| Inspected message body | 3,500,000 bytes |
| Decision file | 65,536 bytes |

Native collector and owner-service failure codes are mapped to the closed guide codes. The journal keeps the mapped state only, never the native code or any provider text.

## What this does not establish

The scoped client can read only the dedicated space. Inside it, Slack originals stay local, and only approved copies leave. If you also turn on the owner egress policy (`open-brain egress-policy`) for the Work tier, other Work captures you later route into that space become readable by the scoped provider too. Keep the space for this workflow alone.

Synthetic tests do not prove live Slack access, real client configuration, model answers or injection resistance. Slack coverage beyond the selected approved messages is unknown: edits, deletions and messages outside the fetched pages are not reflected until you import and inspect them again.
