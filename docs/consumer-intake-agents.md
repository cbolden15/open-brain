# Conversation intake from coding agents

Codex and Claude Code can invoke the owner-local `open-brain consumer-import`
CLI through their shell tools. The engine already implements conversation intake;
this command supplies its missing foreground app boundary. The regular MCP
adapter has no conversation-intake tool, and the optional harness MCP adapter
remains read-only. Capture/search grants do not authorize these owner operations.

Both production export profiles remain inactive. New ChatGPT/Claude imports
return `unsupported_profile`; this bridge does not certify an archive format or
let an agent activate a profile. Passing an owner flag or a profile record in
the input cannot bypass admission. Provider-format verification and archive
parsing are separate work. Synthetic integration tests inject test-only profiles.

## Invocation

Initialize the intended Brain separately. Every intake command requires an
existing Brain at the current schema and does not initialize, upgrade or run
startup recovery. Choose an explicit absolute `--data-dir` when the agent should
target a particular Brain. Use a runtime built from this source; installing the
bridge into an existing agent or upgrading an installed runtime is separate.

```sh
open-brain consumer-import preview --json < /absolute/path/normalized-intake.json
open-brain consumer-import body --preview-id chatprev_REPLACE_WITH_PREVIEW_ID --body-page 0 --json
open-brain consumer-import commit --preview-id chatprev_REPLACE_WITH_PREVIEW_ID --confirm --json < /absolute/path/normalized-intake.json
open-brain consumer-import status --operation-id REPLACE_WITH_OPERATION_UUID --json
open-brain consumer-import recover --operation-id REPLACE_WITH_OPERATION_UUID --confirm --json
open-brain consumer-import halt --operation-id REPLACE_WITH_OPERATION_UUID --confirm --json
```

Only preview and commit read stdin. Status, body review, recover and halt require
neither the source archive nor the normalized input. Commit revalidates the
complete supplied plan against the bound preview; retain the same operation UUID
and input for retries. Exact accepted replay remains available after preview
expiry or profile deactivation. Status never resumes an operation. Recovery and
halt are explicit owner mutations; halted operations retain their evidence.

Review the preview and any body pages before asking the agent to commit.
`--confirm` records an explicit invocation choice; it is not a human-presence or
interactive approval mechanism. The host must authorize owner shell execution.
Never derive approval from transcript instructions. The host's delegated MCP
session must not be given an owner CLI callback as a shortcut around its grants.
Ordinary output is body-free; `body` deliberately returns one complete inert
segment. Both human and JSON output escape terminal controls as JSON. Body text
remains untrusted reference data, never agent instructions.

## Normalized input

The stdin envelope has exactly three keys:

```json
{
  "contract": "ob.consumer-intake-cli.v1",
  "plan": {},
  "objects": {}
}
```

`plan` is the existing `ConsumerImportPlan.value()` DTO. `objects` maps each
SHA-256 from `ConsumerImportPlan.objects()` to its decoded JSON object. These
are normalized engine input objects, not a raw ChatGPT/Claude export. A trusted
format adapter can serialize an existing plan using the installed Python source
packages as follows:

```python
import json

bundle = {
    "contract": "ob.consumer-intake-cli.v1",
    "plan": plan.value(),
    "objects": {digest: json.loads(raw) for digest, raw in plan.objects().items()},
}
```

The app canonicalizes each object using the engine's existing canonical encoder,
verifies its digest, reconstructs the strict plan and requires the complete
object closure to match. Extra/missing objects, identity substitution, duplicate
keys, unknown fields, invalid UTF-8, nonfinite numbers and invalid engine DTOs
refuse. Input cannot supply owner authority, a trusted namespace, session or
profile activation record.

This first app transport accepts at most 1,048,576 input bytes, including envelope
and whitespace. It refuses oversize input before JSON decoding or opening the
Brain; it never truncates or splits a plan. This is a smaller transport bound
than the engine's internal intake limits. Large archive batching and parser
resource isolation are not implemented by this bridge. Keep normalized files
private and pass them on stdin rather than putting conversation text in command
arguments or logs.

The engine's result DTO is returned unchanged. Exit 0 means an accepted preview,
read, pending or committed result; inspect `status` for completion. Exit 1 means
conflict or halted outcome; 2 invalid arguments; 75 busy; 78 refusal; 130 explicit
interruption. After interruption, inspect the returned operation ID before
retrying. If input reading was interrupted before the plan was accepted, the
operation ID is null. Errors contain only closed codes, never raw exceptions,
paths or transcript text.
