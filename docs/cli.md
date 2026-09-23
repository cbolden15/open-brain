# Command-line interface

`open-brain` is one foreground command. Stateful commands create or reopen the platform-local Brain,
perform the requested operation, close SQLite resources, and exit. No separate init, daemon, HTTP
server, service unit, container, or privileged setup is required.

## Commands

This reference describes the Core v0.1 candidate. `open-brain --help` lists command families and
`open-brain COMMAND --help` gives exact options. Product version alone does not prove the installed
release contains them. `catalog --json` reports actual registrations and compatibility without
opening a Brain. See the [feature/version matrix](core-v01-features.md).

| Command family | Purpose / reference |
|---|---|
| `init`, `capture`, `import`, `status`, `export` | Local storage lifecycle; [first-use](first-use.md), [install](install.md) |
| `capture-submit` | Destination-bound capture under a trusted startup policy; [capture contract](capture-contract.md) |
| `journal status/drain/retry/discard` | Owner-only durable ingress inspection and recovery; [operations](operations.md) |
| `consent grant/inspect/replace/revoke` | Owner-only durable external-provider consent for scoped sessions |
| `catalog` | Versioned metadata; no grants or public certification |
| `search`, `search-page`, `read` | Lexical retrieval and complete projected text; [records](records-and-history.md) |
| `history list/show`, `relationship list/decide`, `decision history` | Retained evidence; owner relationship mutations |
| `space create/list/rename`, `inbox list/route`, `source route` | Explicit organization; [spaces](spaces-inbox.md) |
| `review propose/list/show/approve/reject/edit-and-approve` | Source-bound drafts and inspected decisions; [review](review-publication.md) |
| `agent setup` | Preview/apply/remove client fragments with explicit grants; [agent setup](agent-setup.md) |
| `workspace` | Setup/status/refresh, note lifecycle/conflicts and recovery; [workspace recovery](managed-workspace-recovery.md) |
| `obsidian-plugin install/status/remove`, `plugin` | Packaged assets and foreground stdio bridge; [install](install.md) |
| `graph` | Existing structural graph/consent/exclusion/suggestion operations; provider readiness remains unassessed |
| `doctor --check NAME --json` | One bounded check; [doctor](doctor.md) |
| `mcp` | Explicitly granted foreground stdio session |

Table slashes denote alternative actions, not literal shell syntax. Detailed placeholder templates
are in the linked references; the first-use and doctor guides contain the executable examples.
There is no `vault` command; the managed vault is the `Open Brain Vault` sibling of the Brain root.

Commands that support machine output accept `--json`. The shared `--data-dir` option must be an
absolute path. Without it, the CLI uses the platform data directory. `OPEN_BRAIN_ROOT` is ignored.

`capture` accepts `--privacy-tier` with one of `public`, `work`, `personal`, `secret`, or `unknown`
as an owner-only explicit privacy tier. `import` accepts the same `--privacy-tier` for the whole
invocation plus `--privacy-manifest` pointing at a validated per-root privacy manifest JSON file;
see [import design](import.md). `capture-submit` submits one destination-bound capture under a
trusted startup policy and requires `--policy` with an absolute `launcher-policy.v1` JSON path; see
[capture contract](capture-contract.md) for the tier rules and admission limits.

`workspace setup` creates or reopens the dedicated `Open Brain Vault` sibling beside the private
Brain directory. Explicit `workspace refresh` adds newly accepted canonical pages and advances
eligible existing notes to the current approved publication, keeping their registered paths.
Unchanged publications do not rewrite bodies; representative-capture metadata changes can create
an identical-body revision without a file write. Owner edits and links are retained; divergence
requires explicit conflict resolution and materialization. Missing, inactive, excluded, and already
conflicted notes are not overwritten. Retrying a refresh request finishes only its original bound
work. Observation, acceptance,
materialization, deactivation, restoration, conflict resolution, consent, exclusions, and suggested
link acceptance are explicit owner operations. State-only actions do not rewrite Markdown.

## Journal operations

Journal commands are owner-local only and return metadata, never envelope content. `status` shows
opaque ingestion IDs, delivery IDs, attempt counts, journal sequence for the owner, state, and
bounded summary fields. The sequence is never exposed to scoped capture adapters.

```sh
open-brain journal status --data-dir /absolute/brain --json
open-brain journal drain --data-dir /absolute/brain --json
open-brain journal retry DELIVERY_ID --data-dir /absolute/brain --json
open-brain journal discard DELIVERY_ID --reason 'owner-confirmed discard' \
  --confirm --data-dir /absolute/brain --json
```

`drain` first resumes incomplete canonical captures and then processes a bounded journal batch.
`retry` applies only to quarantined items. `discard` requires explicit confirmation and writes a
durable tombstone before removing the retained payload. A writer-busy result is retryable; it does
not mean that acknowledged custody was refused.

## Provider consent

Provider consent is deployment authority, not Brain content. Store it in an existing owner-only
directory outside the Brain. The directory must have mode `0700`; Open Brain writes the canonical
state file with mode `0600`. The file is bound to the durable Brain ID and issuer epoch and contains
no credential or record content.

```sh
open-brain consent grant --state /absolute/private/provider-consent.json \
  --provider-id synthetic-provider --allowed-tier public --allowed-tier work \
  --operation-id grant-1 --data-dir /absolute/brain --json
open-brain consent inspect --state /absolute/private/provider-consent.json \
  --data-dir /absolute/brain --json
open-brain consent replace --state /absolute/private/provider-consent.json \
  --consent-id CONSENT_ID --provider-id synthetic-provider --allowed-tier public \
  --operation-id replace-1 --data-dir /absolute/brain --json
open-brain consent revoke --state /absolute/private/provider-consent.json \
  --consent-id CONSENT_ID --operation-id revoke-1 --data-dir /absolute/brain --json
```

Grant, replace, and revoke are operation-ID idempotent. Replace and revoke advance the authorization
generation once. A missing, malformed, foreign-Brain, wrong-epoch, or concurrently changed state
file fails closed.

## Status and doctor

Status reports profile `local`, storage `sqlite`, daemon false, and application encryption false.
Doctor checks the private data directory, foreground runtime boundary, base dependency closure, and
search index. The four valid names are `private-data-directory`, `foreground-runtime`,
`base-dependency-closure`, and `search-index`; bare `doctor` is invalid. JSON returns `check` and
`status` (`ok` or `failed`), with exit 0 or 1 respectively. Neither command probes or manages a service.

## MCP

`open-brain mcp` speaks MCP over inherited stdio. At least one capability flag is required. Workspace
read exposes only path-free status and pending suggestions. Graph refresh uses the already configured
provider and current owner consent; it cannot select a provider, change consent or exclusions, accept
a link, resolve a conflict, or mutate a note. Until a provider adapter is configured, refresh returns
`provider_not_configured` without an inference attempt. The process owns no listener and ends at EOF.

Capture, search, content-read, history-read, inbox-read, organize, review-read, review-propose and
review-decide are independent `--allow-...` flags; workspace-read and graph-refresh are additional
manual MCP grants. A scoped deployment passes `--session-policy` with one absolute trusted
`launcher-policy.v1` path. The session receives only the intersection of policy capabilities,
selected `--allow-...` flags, injected implementations, and the scoped-safe operation matrix.
Owner operations remain unavailable even if a non-owner policy names their flags. An
`external_provider` policy also requires `--consent-state` with the durable state described above.
Open Brain rereads both trusted files before discovery and every tool call. A changed policy,
generation advance, replacement, revocation, or malformed state terminates the process; a revoked
mapping cannot restart.

`capture-submit` is a further manual grant: `--allow-capture-submit` exposes the destination-bound
`brain_capture_submit` tool. `--capture-policy` remains a compatibility alias for a capture-submit
session policy and cannot be combined with `--session-policy`; new launchers should use
`--session-policy`. Search alone does not grant full content or history. Review
decisions publish only after token-bound inspection. `tools/list` reflects session grants, while
`brain_catalog` accepts `{"schema_version":2}` for metadata. Neither catalog discovery nor source
text can widen permissions. CLI setup and the shared bridge support nine grants; capture-submit is a
launch-time grant with no setup fragment. The desktop setup UI exposes
capture/search; Obsidian has no agent-setup control. See [agent setup](agent-setup.md) for privacy
and client activation.

Per process, workspace reads are limited to 500 calls and 16 MiB of serialized results. Graph refresh
is limited to 20 requests, 40 actual model attempts, and 1 MiB of selected note input. These limits are
in addition to the engine's durable per-workspace provider budget. Restarting an explicitly launched
MCP process resets only the process limits.

A successful `brain_capture` or `brain_capture_submit` tool result may be either an existing
terminal capture receipt or a `capture-custody.v1` receipt with `status: "queued"`. MCP treats the
queued variant as a successful tool result, not an error. A scoped client may verify only the
receipt bindings and opaque `ingestion_id`; it cannot inspect journal sequence, queue depth,
payload, or owner journal status.

```json
{
  "contract_version": "capture-custody.v1",
  "status": "queued",
  "ingestion_id": "ingestion_opaque-synthetic-id",
  "brain_id": "brain_synthetic-id",
  "issuer_epoch": 7,
  "delivery_id": "delivery.synthetic-1",
  "request_sha256": "synthetic-request-digest",
  "requested_tier": "work",
  "final_admitted_tier": "work",
  "queued_at": "2026-09-22T12:00:00Z",
  "protection_acknowledgement": null
}
```

The identifiers and digest above are illustrative placeholders, not valid credentials or live
references.

## Exit behavior

Invalid usage returns 2. Temporary SQLite writer contention returns 75 for capture, capture-submit,
search, import, or MCP work. A private-directory or operation failure returns 78 without exposing
sensitive paths; a refused startup policy returns 78 with `destination_mismatch`, `issuer_mismatch`,
`stale_policy`, or `consent_unavailable`. Consent-state write contention returns 75; invalid owner
consent input returns 2; unavailable or unsafe consent state returns 78. A refused `capture-submit`
admission reports the stable result value with a `retryable` flag: retryable refusals exit 75 and
terminal refusals exit 65. Interrupted Markdown import returns 130.

Secure Node and predecessor command families are historical source under `archive/`. They are not
installed commands and are not supported through the Open Brain executable.

## Optional connectors commands

The optional `open-brain-connectors` package installs its own console scripts alongside `open-brain`.
The source CLI `open-brain-source` is documented per source in the Google Calendar, workspace content,
and PDF/DOCX import guides. The outbox CLI `open-brain-outbox` is the owner-facing foreground command
for the offline outbox described in the [capture contract](capture-contract.md). Every subcommand
takes the shared options `--outbox-dir` (required, the owner-supplied outbox directory), `--max-items`
(default 1000), `--max-bytes` (default 8 MiB), and `--json` to print one JSON document instead of the
human-readable line. Output is metadata only and never echoes payload text.

| Subcommand | Purpose |
|---|---|
| `enqueue` | Read one JSON request from stdin and store one durable item |
| `drain` | Run exactly one bounded delivery cycle through the stdio transport |
| `status` | Print metadata-only counts and capacity by state |
| `retry DELIVERY_ID` | Requeue one quarantined item under a new bounded window |
| `discard DELIVERY_ID --confirm` | Owner-confirmed terminal discard |
| `convert DELIVERY_ID --destination-brain-id ID --issuer-epoch N` | Enqueue a new item for a new destination with lineage |

An enqueue request carries the required keys `destination_brain_id`, `issuer_epoch`, `tenant_id`,
`principal_id`, `requested_tier`, and `text`, plus the optional keys `delivery_id` (a fresh identifier
is generated when absent), `policy_ref`, `retry_age_limit_seconds` (default 86400),
`retry_attempt_limit` (default 8), and `enqueued_at`. The result prints `delivery_id`,
`request_digest`, and `result`. A drain takes exactly one of `--transport-argv`, a JSON array of
strings, or `--transport-command-file`, a path holding that array, plus `--max-batch-items`
(default 16), `--max-batch-bytes` (default 1 MiB), and `--timeout-seconds` (default 60). The drain
JSON summary carries `result`, `stale_lease_reclaimed`, `admitted_items`, `skipped_items`,
`delivery_attempts`, `accepted`, `duplicate`, `quarantined_age_exhausted`,
`quarantined_attempts_exhausted`, `quarantined_receipt_mismatch`, `quarantined_refused`, `retried`,
`transport_errors`, and `batch_too_large_items`. Status JSON carries the two caps, the reserved
`headroom_bytes`, and item counts and sizes for the `queued`, `quarantined`, `terminal`, and `corrupt`
states. Convert additionally accepts `--new-delivery-id` and prints both delivery IDs.

Exit codes are 0 for success, 2 for a usage error including an invalid transport command, 65 for
terminal refusals such as owner-operation refusals, store or contract failures, malformed requests,
delivery conflicts, and oversized items (`item_too_large`), and 75 for the temporary failures
`drain_busy` and `outbox_full`.

The stdio transport is a deployment-supplied command speaking the `capture-submit --json` shape: it
reads exactly one JSON request document from stdin, writes exactly one JSON result document to
stdout, and exits 0 for a capture, 75 for a retryable admission refusal, 65 for a terminal admission
refusal, 78 for a policy failure, or 2 for a usage error. Both documents are bounded to 256 KiB.
Exit 0 with a parseable receipt becomes a terminal receipt; exit 0 with an unparseable, non-object,
or shape-invalid success document becomes terminal `receipt_malformed`; exit 75 becomes a retryable
failure under the reported code; exit 65 becomes a terminal failure under the reported code; exit 78
becomes terminal `policy_mismatch`; exit 2 becomes terminal `transport_misuse`. A timeout, an
over-limit result, an unknown exit code, or a command that cannot start becomes a retryable
`transport_error`. The capture-submit CLI reports every policy-class failure as exit 78, so the
adapter maps all of them to `policy_mismatch` and a delivery conflict is not distinct on this path.
The command runs with an empty environment plus one explicit allowlist, a fresh working directory,
and child process limits, and its process group is killed on timeout or over-limit output. The
command, its hosts, and its credentials are private deployment inputs.

A synthetic enqueue looks like:

```sh
open-brain-outbox enqueue --outbox-dir /absolute/path/to/outbox --json <<'JSON'
{
  "destination_brain_id": "brn_aaaaaaaaabaabaaaaaaaaaaaae",
  "issuer_epoch": 1,
  "tenant_id": "tenant-example",
  "principal_id": "principal-example",
  "requested_tier": "work",
  "text": "synthetic offline capture text"
}
JSON
```

All names in the example are synthetic. The printed `queued` result is reported only after the
complete item is durably stored, a full outbox prints `outbox_full` and exits 75 instead, and an
item whose serialized envelope exceeds the per-item limit, 248 KiB by default under the 256 KiB
request-document cap, prints `item_too_large` and exits 65.
