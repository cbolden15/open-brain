# Consumer conversation exports: G1 contract

Date: 2026-10-15. Baseline: `8ef1d069259c291e669341079806103c8fa90e86`, integrated W4, schema/runtime/Portable **17/12/12**. Historical parser specification; current engine status is recorded below. Nothing here activates an external parser profile.

Both ChatGPT and Claude consumer exports remain first-implementation scope. The internal contract below is proposed and frozen for implementation review. **Both external profiles are inactive pending authoritative format evidence.** Internal synthetic fixtures prove this contract only; they cannot certify a provider format.

Shared authority, compatibility, recovery and Portable behavior are specified in `PERSISTENCE.md` in this directory. Its 18/13/13 allocation records the original design reservation. The implemented successor uses the concrete schema/evidence contract in `CONSUMER-INTAKE.md`.

## Current implementation status (2026-10-16)

The baseline/version reservations below are historical planning evidence. The
current implemented development candidate is schema20/runtime15/Portable15.
Engine-owned preview/commit/status/recover/halt, atomic conversation activation,
source provenance, successor journal and Portable restore are implemented under
goal110. Final independent review and exact-candidate gates remain required.
See `CONSUMER-INTAKE.md` for the concrete contract. Both external provider
profiles remain inactive; synthetic engine tests do not certify either export
format or provide an end-user archive parser/CLI.

## Current seams and implementation boundary

Current source was read in `$REPO/.worktrees/competitive-gap-reconciliation`. The global CodeGraph index targets another worktree and was not used as source authority.

| Current source, relative to that worktree | Observed seam and consequence |
|---|---|
| `packages/app/pyproject.toml`; root `CLAUDE.md` | App depends exactly on engine. Consumer JSON/ZIP parsing uses Python stdlib in the app; no mandatory connectors, collector, network client or parser dependency. |
| `packages/app/src/open_brain/services/local_entrypoints.py` | Existing `import DIRECTORY` is Markdown-only. Add a separate owner CLI family; preserve its parser, output, confirmation and replay behavior. |
| `packages/engine/src/open_brain_engine/engine/contracts.py` | `TextPayload` normalizes NFC and line endings, rejects empty/NUL text and caps text at 65,536 characters. `CaptureSubmission.for_public_job` uses injected capture-only identity and third-party/unknown origin, quick capture, no space routing or canonical-note authority. |
| `packages/engine/src/open_brain_engine/engine/source_intake.py` | Explicit four-part namespace, revision identity, request digest, head/control/lifecycle CAS and retained delivery receipts already exist. Unordered changes do not establish promotion order. Whole-envelope equality matters on replay. |
| `packages/engine/src/open_brain_engine/engine/source_observation.py` | Original/transformed/admitted payload and privacy-policy hashes have a distinct observed-delivery envelope; do not alter its frozen bytes to add importer metadata. |
| `packages/engine/src/open_brain_engine/engine/markdown_import.py` | Reserved importer actor/delivery and durable reservation patterns exist, but Markdown's operational-only ledger is insufficient for portable import tombstones and atomic conversation membership. Do not copy it as the new lifecycle authority. |
| `packages/engine/src/open_brain_engine/engine/source_lifecycle_contracts.py` | Withdrawal is explicit owner authority bound to source head, lifecycle, Brain and issuer. Consumer import cannot grant restoration or withdrawal authority. |

The proper architecture is a bounded app parser producing a versioned normalized plan, with engine-owned grouped intake, receipts, source lifecycle and active membership. A separately installed optional parser is architecturally possible for future formats requiring third-party libraries, but is unnecessary for bounded JSON/ZIP and would add an install step. It cannot silently become a collector prerequisite. No UI/parser writes SQLite, constructs owner credentials from export fields, or calls a provider.

## External evidence: ChatGPT

Evidence checked on 2026-10-15 by opening the linked primary pages. “Observed” means documented by that page, not independently inspected in an export. “Unverified” means the opened primary material does not define the mapping.

| Claim or mapping | Status | Primary evidence / activation consequence |
|---|---|---|
| Consumer account export contains chat history in a ZIP | Observed documentation | [Exporting your ChatGPT history and data](https://help.openai.com/en/articles/7260999-exporting-your-chatgpt-history-and-data). |
| Conversation JSON member named `conversations.json`; large exports may use numbered conversation JSON files | Observed documentation | [Transfer exported conversations between ChatGPT accounts](https://help.openai.com/en/articles/9106926-transfer-exported-conversations-between-chatgpt-accounts). Exact numbering grammar and precedence are unverified. |
| Top-level JSON structure, conversation/message IDs, title/timestamp fields, graph edges, selected leaf, text part layout, attachment markers | Unverified | No authoritative schema or specimen established by the opened pages. Field names such as `mapping` or `current_node` are not accepted merely because community parsers use them. |
| Account scope and conversation-ID stability across exports; edit/regeneration ordering; numbered-file duplicate rules | Unverified | Needed before identity and revision mappings can activate. Export time is not a provider revision sequence. |
| `chatgpt-consumer-v1` | Proposed internal profile ID; inactive | Reserve the ID only. Do not recognize an invented external structure under it. Provider profile version is distinct from consumer product/export date. |

API Conversations/Responses JSON, Codex session JSONL, shared-link HTML and browser scraping are different products. They are excluded as evidence and as fallback inputs.

## External evidence: Claude

| Claim or mapping | Status | Primary evidence / activation consequence |
|---|---|---|
| Individual account export includes conversation and account data | Observed documentation | [Export your Claude data](https://support.claude.com/en/articles/9450526-export-your-claude-data). |
| Container type, member names, JSON root, conversation/message IDs, sender/text fields and timestamp representation | Unverified | The opened primary page provides no consumer archive schema or specimen. No `conversations.json`, `uuid` or `chat_messages` mapping is claimed as verified. |
| Branch/regeneration selection, structured content, attachment representation and account/ID stability | Unverified | Required profile gates; do not infer from Claude Code, API Messages, organization exports or memory import. |
| Uploaded file contents | Limited documented exclusion | The same help page excludes file contents for the Team/Enterprise member-export path. This does not establish individual-export attachment layout; G1 omits attachment bytes for both providers regardless. |
| `claude-consumer-v1` | Proposed internal profile ID; inactive | Reserve the ID only. No external filename/schema detector is enabled until the evidence packet is accepted. |

A public provider-authored schema/specimen, or a separately authorized structural verification process, must resolve the unverified rows. This task did not access private exports or accounts. Public community posts, anonymized third-party examples, generated schemas and coding-session fixtures do not upgrade these rows to observed primary evidence.

## Profile activation record

Each provider needs its own reviewed, immutable profile record: `profile_id`, provider, evidence URLs and retrieval date, evidence specimen/source digest and license where applicable, exact container/member grammar, root schema/discriminators, mandatory and optional field mapping, unknown-field policy, provider identity rules, branch/text/role/attachment rules, timestamp rules, duplicate rules and synthetic fixture digests. Evidence must cover every accepted variant; one example cannot certify undocumented fallback variants.

An inactive or unknown profile returns `unsupported_profile` before import intent or capture state is created. Detection is structural against one active allowlisted profile; filename alone cannot certify a format. Ambiguous detection returns `ambiguous_profile`. Unknown optional metadata may be ignored only where that active profile explicitly allows it. Unknown body-bearing, role, graph, identity or revision structure refuses the affected conversation; it is never silently coerced to text. Schema drift receives a new reviewed profile version, not a permissive fallback. Activation requires BOTH first-release provider profiles; do not advertise a completed consumer importer with only one activated.

## Identity and provenance

The engine registers a durable opaque `account_scope_id` and `import_namespace_id` per provider/account scope and destination Brain. An owner label identifies the scope operationally; it is an assertion, not provider authentication. Reimports explicitly select the registered scope. Initial registration requires an explicit new-scope request, bound in preview and registered at commit; the engine never infers a new scope from archive path, changed/missing IDs, absent items or profile drift. Provider IDs need to be stable within the evidenced account scope; global uniqueness is not assumed. A one-account-per-provider alternative is smaller but would narrow the requested import scope; the selected architecture supports registered account scopes now.

There is no reset, rebind, namespace override, copy-as-new, force, or re-enable-retracted import flag. A restored Brain retains scopes, namespaces and identity/tombstone mappings through the coordinator's portable authority. Known archive digests and any profile-verified provider-account anchor are checked for rebound to another scope; a mismatch refuses with `scope_conflict` before new admission. Labels/account hints alone cannot prove distinct provider accounts. This prevents routine replay/reimport from bypassing the registered scope's tombstones; it does not claim to confine an owner deliberately copying text into new captures or prove global content non-resurrection across unrelated scopes. Cross-scope identity ambiguity must be surfaced, not “fixed” by silently making a new namespace.

Provider IDs are exact UTF-8 scalar strings, 1–1,024 bytes, with no NUL, C0/C1 controls or format characters. Do not case-fold, NFC-fold, trim, parse as numbers, or replace them with title, array offset, timestamp, archive hash, path or body hash. A profile must prove the conversation-ID and message-ID mappings; missing IDs refuse that conversation. Duplicate IDs with unequal normalized data refuse before any operation; identical duplicates may collapse only under the active profile's documented rule.

Let `C(x)` be the engine's existing `portable_canonical_json_bytes(x)` and `H(x)` be lowercase hexadecimal SHA-256 of `C(x)`:

```text
conversation_key = H(["consumer-chat-v1", provider, import_namespace_id,
                      {"utf8_hex": UTF8(provider_conversation_id).hex()}])
message_key = H([conversation_key, {"utf8_hex": UTF8(provider_message_id).hex()}])
segment_key = H([conversation_key, "chat-segment-v1", segment_index])
```

Provider-ID UTF-8 byte wrappers preserve distinct scalar sequences through NFC-normalizing canonical JSON. Engine-issued import namespace IDs use1–128 printable nonwhitespace ASCII characters; profile IDs use1–96 ASCII lowercase letters/digits/hyphens beginning with a letter. These are registered opaque identities, never arbitrary Unicode labels.

Canonical source namespace for each segment is `{connector_name:"consumer_chat", connection_id:import_namespace_id, resource_id:conversation_key, external_id:segment_key}`. The engine also binds the conversation group to `conversation_key`; segment admission cannot independently publish a group. A proposed source reference is `urn:open-brain:consumer-chat:<provider>:<conversation_key>:<segment_index>`. No account email, private provider ID, archive filename, absolute path or token enters that reference. Source refs are inert identities.

All imported content, including messages labeled “user,” has third-party origin and unverified trust. Labels describe transcript speakers, not local owner/agent authority. The app receives an engine-injected non-owner `PublicJobCaptureContext` restricted to `capture.accept`, uses quick/reference intent, local-only personal privacy by default, and no canonical-note action, publication, routing, grants or provider egress. An explicit owner privacy-tier selection is preview-bound and follows existing engine privacy rules. The transcript cannot supply it. Imported dates are metadata and never local admission order or authority.

## Normalized conversation and segmentation

`consumer-chat-v1` is an internal DTO, not either provider's export schema. Required top-level fields are exact; reject extra fields at this seam:

| Field | Type and rule |
|---|---|
| `contract_version`, `normalization_version`, `segmentation_version` | Exact strings `consumer-chat-v1`, `chat-normalize-v1`, `chat-segment-v1`. |
| `provider`, `profile_id`, `import_namespace_id`, `conversation_key` | Provider enum `chatgpt`/`claude`; active profile; registered namespace; computed key. |
| `title` | Nonempty NFC text up to 200 Unicode scalars; safe generated `Imported conversation` if absent. Oversized supplied titles refuse, never truncate. |
| `messages` | Ordered list of `{message_key,role,text,occurred_at}`; role is exactly `user` or `assistant`; nonempty text; date is null or profile-verified timestamp normalized to UTC RFC3339. No guessed dates. |
| `omissions` | Exact nonnegative integer counts: `nonselected_branch_messages`, `system_messages`, `developer_messages`, `tool_messages`, `attachment_parts`, `unsupported_parts`. |
| `conversation_revision_sha256` | `H` of contract/provider/profile/normalization/segmentation versions, conversation key, title, ordered messages and omissions. Excludes archive packaging, local observation/operation times and receipt IDs. |
| `segments` | Ordered list of `{segment_index,segment_key,body_sha256,message_spans,capture_request_sha256}`; contiguous zero-based indices; at most 128. |
| `membership_sha256` | `H` of the ordered segment descriptor list. |

Raw provider IDs and original byte/member digests belong to protected operational evidence bound to this DTO, not ordinary preview/status output. `body_sha256` hashes exact admitted UTF-8 text bytes; `capture_request_sha256` uses existing `CaptureSubmission.request_sha256()`. `message_spans` is an ordered list of `{message_key,start,end}`, with zero-based, half-open Unicode scalar offsets into the normalized message text. Split pieces of one message retain the same message key and nonoverlapping spans. All capture envelopes and segment bodies are immutable parts of the internal intent even where omitted from metadata read DTOs.

Normalization decodes strict UTF-8, normalizes CRLF/CR to LF and Unicode NFC, rejects NUL, unpaired surrogates and non-UTF-8 input. LF and TAB remain text. Other C0/C1 controls, DEL and Unicode format controls are transformed into visible ASCII `\\uXXXX` or `\\UXXXXXXXX` escapes before hashing/segmentation; record `chat-normalize-v1`. This avoids terminal/bidi escape execution while preserving an explicit representation of the original characters. Do not HTML-decode or execute Markdown/HTML. JSON escape decoding happens once. Original byte hashes bind the pre-normalization evidence.

Only the profile-verified selected conversation branch is imported. A verified linear export uses its documented list order. A graph requires a uniquely verified selected leaf/root path, bounded iterative traversal, no cycle, dangling edge, duplicate ID or contradictory parentage. Never sort by timestamps, concatenate alternate answers or guess a current leaf. Known system/developer/tool messages and nonselected branches are omitted with exact counts. Attachment bytes, filenames, OCR, audio/image/video decoding, tool payloads and linked content are omitted. Known text portions accompanying attachments remain only where the profile separates them unambiguously. Recognized unsupported parts are counted and omitted under the profile rule; unknown part structure refuses instead of disappearing. If no eligible text remains, return `no_text`, without creating a capture.

Build an inert transcript by appending `User:\n<text>\n\n` or `Assistant:\n<text>\n\n` for each included message. Partition this complete string deterministically into consecutive pieces of at most 60,000 Unicode scalars, with no overlap, in transcript order. Prefix each piece with the exact generated line `Transcript segment <segment_index>:\n`, using its zero-based decimal index without padding. That bounded label keeps a piece containing only source whitespace admissible under `TextPayload`'s nonblank rule. Each segment retains its intersecting message spans; generated labels and separators have no message-text span. Empty pieces are forbidden. No source text is truncated; UTF-8 sequences are never split. Each labeled piece is a `TextPayload`, remains below the existing 65,536-character cap after engine normalization, and uses the conversation title. A single long message may span segments. The 128-segment and conversation-byte gates both apply.

The segmentation algorithm/version is part of revision identity. Repacking or renaming an archive does not change it. Changing segment size, separator text, normalization, omission policy or profile mapping requires a new contract/profile version and explicit migration preview against the same conversation group; it cannot create a new namespace or resurrect a tombstone.

Synthetic internal example, deliberately not an official export:

```json
{
  "contract_version": "consumer-chat-v1",
  "provider": "chatgpt",
  "messages": [
    {"message_key": "<computed-hex64>", "role": "user", "text": "What did we decide?", "occurred_at": null},
    {"message_key": "<computed-hex64>", "role": "assistant", "text": "Keep the first draft.", "occurred_at": null}
  ]
}
```

This excerpt illustrates normalized messages only; it is not a valid complete DTO or a provider detection fixture.

Its exact first segment body is the following inert string, including two trailing LF characters after the final sentence:

```text
Transcript segment 0:
User:
What did we decide?

Assistant:
Keep the first draft.

```

An edited answer changes `conversation_revision_sha256` and the segment request while preserving `conversation_key`, message keys and ordinal segment key. Repacking identical normalized messages preserves that revision identity. A shorter explicit replacement publishes only its new complete membership; an archive omitting this conversation leaves its group unchanged.

## Proposed CLI and read DTOs

These commands do not exist at baseline. Paths below are illustrative absolute paths, not requests to read files. The command is owner-local only, with no MCP/agent import surface in v1:

```sh
open-brain import-chat preview --provider chatgpt --new-account-scope personal --file /absolute/path/export.zip --json
open-brain import-chat preview --provider claude --account-scope <scope-id> --file /absolute/path/export.zip --json
open-brain import-chat preview --preview-id <opaque-id> --body-page 0 --json
open-brain import-chat commit --preview-id <opaque-id> --file /absolute/path/export.zip --operation-id <uuid> --json
open-brain import-chat status --operation-id <uuid> --json
open-brain import-chat recover --operation-id <uuid> --json
open-brain import-chat halt --operation-id <uuid> --json
```

Normal local `--data-dir` and `--json` conventions apply. Initial preview requires exactly one of `--account-scope <opaque-scope-id>` or `--new-account-scope <owner-label>`. Labels are 1–120 normalized Unicode scalars with no controls/format characters, are unique per provider locally, and are not included in captured content. Reusing a label means selecting its existing scope, not registering another. Initial preview accepts optional `--privacy-tier TIER` and `--replace-current`. The latter is explicit owner replacement selection, bound to every changed conversation's expected current head; it does not imply provider chronology. Without it, changed already-known conversations report conflict and cannot displace current content. There is no unbounded import/large-export bypass. First release selects the entire supported conversation dataset; arbitrary JSON selectors or provider fetch URLs are not accepted. A future selection grammar requires its own completeness/identity contract.

Preview creates no captures, source head or session membership. It may persist protected, bounded operational preview evidence using the coordinator's engine interface. `preview_id` is an opaque random 256-bit base64url token prefixed `chatprev_`, exactly 52 ASCII characters with no padding. It is an identifier, not authorization and not a publicly exposed body/archive hash. Issuance binds the complete canonical intent digest; at most 16 live previews per owner, one hour lifetime, no renewal by body/status read. Commit is owner-authorized by that exact preview, never by export text. Preview expiry does not remove an accepted operation's replay authority.

Initial JSON preview has exact keys `dto_version:1`, `status:"preview"`, `preview_id`, `expires_at`, `provider`, `profile_id`, `account_scope_id`, `counts`, `items`, `items_omitted`, `body_pages`, `history_retained:true`. The scope ID is durable after commit; a first-registration preview returns its proposed opaque ID, and cancellation does not register it. `counts` has `conversations`, `new`, `unchanged`, `changed`, `conflict`, `retracted`, `no_text`, `segments`, `omitted_parts`. Every item is `{item_token,outcome,segments,omissions}`; tokens are opaque run-local identities and outcomes come from those conversation counts. At most 100 items, failures/conflicts first then normalized key order. Counts always cover the entire plan. No raw provider ID, title, body, path, digest or exception enters ordinary summary/status output.

Body preview is the explicit content-output exception: one complete segment per `body_page`, in the frozen plan order, at most 128 pages per conversation and at most 1 MiB encoded JSON per response. Its exact keys are `dto_version`, `status:"preview_body"`, `preview_id`, `body_page`, `body_pages`, `item_token`, `segment_index`, `title`, `body`, `omissions`, `next_body_page` (integer or null). Include all admitted body text, with no excerpt/truncation. Invalid page refuses; it cannot alter selection. JSON escapes control characters and uses UTF-8 with ordinary JSON string escaping; consumers must display strings as inert text. Human mode escapes terminal controls and emits plain text only. No browser/HTML/Markdown renderer or hyperlink activation is launched. Oversize output refuses instead of clipping the segment. Users may page through all bodies before committing; the CLI does not pretend a metadata preview is body review.

Commit reopens and revalidates the selected file under descriptor confinement, recomputes its byte digest and the normalized plan, and compares with the bound intent before first new mutation. It requires the same provider, mapping, privacy, selection, replacement mode, destination Brain/issuer and expected source/group generations. Changed bytes require a new preview even when the normalized text happens to match. If the operation already exists, perform exact operation/request reconciliation first using the engine interface; stale/expired previews cannot turn a lost acknowledgement into duplicate work. A different intent with the same operation ID always refuses.

Commit/status/recover/halt JSON has exact keys `dto_version:1`, `status`, `operation_id`, `session_id`, `counts`, `items`, `items_omitted`, `history_retained:true`. Session ID is null until the first new conversation activation, then identifies the engine-owned session containing only newly activated captures from this operation. Unchanged existing captures retain their old membership and appear as unchanged outcomes, so undoing this session does not retract previously present content. Status is `reserved`, `custodied`, `materializing`, `ready`, `committed`, `conflict` or `halted` as defined in `PERSISTENCE.md`. Counts cover `conversations`, `committed`, `unchanged`, `conflict`, `retracted`, `no_text`, `halted`, `pending`, `segments_materialized`; item entries are `{item_token,outcome,receipt_id}` with nullable receipt ID. Counts and bounded items are read from durable engine state, never fabricated from CLI counters. Status requires neither export file nor preview, has no mutation, and returns `operation_not_found` for an unknown ID. Recover is an explicit owner mutation using only the accepted engine-custodied intent; it requires no export file and cannot replace the intent, bypass freshness or resume a terminal operation. Halt explicitly seals unfinished work under the persistence contract, preserving successful earlier conversations and custody; repeat halt is idempotent and never reactivates a segment. No status read resumes work.

Errors are `{"dto_version":1,"status":"failed","error":{"code":"<closed-code>","retryable":false,"item_token":null}}`; item token is nullable, no raw diagnostic/message/path/body. Closed codes: `invalid_arguments`, `input_unavailable`, `unsafe_archive`, `archive_limit`, `invalid_json`, `invalid_unicode`, `unsupported_profile`, `ambiguous_profile`, `unsupported_conversation`, `identity_collision`, `scope_conflict`, `conversation_limit`, `preview_not_found`, `preview_expired`, `preview_changed`, `revision_changed`, `retracted`, `operation_conflict`, `operation_not_found`, `local_operation_busy`, `import_interrupted`, `local_operation_failed`. Only `local_operation_busy` is retryable without a new preview; interrupted accepted operations are recovered by exact operation status/replay. `unsupported_profile` includes inactive profiles. Human errors use fixed messages keyed by code, without parser exception text. Exit: 0 committed/read success; 1 completed mixed/conflict result; 2 usage; 75 busy; 78 preflight/profile/stale-preview refusal; 130 interruption at a safe boundary. Interrupted JSON instead has exact keys `dto_version:1`, `status:"interrupted"`, `operation_id` (null until accepted), `error:{code:"import_interrupted",retryable:false,item_token:null}` so status can recover an accepted operation's truth; it never claims rollback.

## Durable intake and lifecycle interface required from G1 persistence

The coordinator owns exact schema/version/fencing, journal, terminal receipt and Portable representation. The parser must receive these engine interfaces rather than create another ledger:

| Required operation | Required behavior |
|---|---|
| `preview_consumer_import(plan, owner_context)` | Resolve account scope/namespace/tombstones/current group heads and cross-scope rebound evidence; retain canonical intent digest and opaque preview binding. Bind Brain, issuer, provider/profile, existing or proposed scope registration, archive digest, complete selection, all capture envelopes, privacy/replacement mode and expected head/lifecycle/control/eligibility coordinates. |
| `commit_consumer_import(preview_id, operation_id, rebuilt_plan, owner_context)` | Exact operation/request lookup before freshness CAS. Persist immutable operation and per-conversation/segment intents before capture. Same ID+intent reconciles; same ID+different intent refuses. |
| Per-conversation staged revision | At most 128 segments; immutable manifest plus per-segment `reserved`/`custodied`/`materialized` receipt states. Staged records are globally ineligible until activation; existing current revision remains eligible. Files/custody need not be one atomic transaction. |
| Atomic activation | One SQLite transaction verifies every canonical receipt and CASes group head, lifecycle/control/eligibility; publishes new group head, complete active membership, terminal receipt and eligibility-generation change together. No lexical/semantic/MCP/direct-read/sharing leak of staged bodies. |
| `recover_consumer_import(operation_id, owner_context)` / `halt_consumer_import(operation_id, owner_context)` | Explicit accepted-intent recovery or terminal halt; same operation fence/CAS, no payload override or staging activation on halt. |
| `consumer_import_status(operation_id, owner_context)` | Durable body-free counts/receipts and recovery state. Lost acknowledgement and restart must return the actual committed outcome without recapture or a second session membership. |

Each conversation's intent contains the exact normalized vocabulary above plus `replacement_mode:"new_only"|"replace_current"`, expected group head/lifecycle/control/eligibility and the complete capture envelopes. An invocation receives one trusted import session containing only its newly activated captures, as specified in `PERSISTENCE.md`; unchanged items are outcomes, not new members. Operation replay cannot append another membership. Per-segment operation/delivery identity must deterministically derive from immutable conversation operation identity and segment index; it must not reuse the existing connector delivery function without the group/revision binding.

Exact unchanged reimport retains the original mapping and activation receipt reference, returns a new operation-bound unchanged outcome receipt, and creates no new capture. An archive can have new byte packaging but the same normalized revision; its new preview can resolve unchanged. A changed revision requires explicit `replace_current` and current CAS. No comparable provider sequence is assumed. Without replacement selection it is conflict/history-only under the shared engine policy and cannot become current. Whether conflict evidence is retained requires explicit admission; a preflight conflict alone creates no capture. Do not auto-promote an older export merely because it was imported later.

An explicit complete changed manifest can remove old tail segments from active membership only in the activation transaction. It preserves immutable captures/history. A conversation missing from a later archive has **no lifecycle effect**; it is not evidence of deletion and never finalizes “missing” withdrawal. Failed/incomplete archive parsing cannot change prior membership.

The expected eligibility coordinate is an internal global fence: changes require exact selected-preimage revalidation, with rebasing only as specified in `PERSISTENCE.md`. This operation's earlier verified activations cannot stale later conversations merely by advancing that counter; source/group/privacy/authority or session-barrier changes still refuse.

Check tombstones before staging and again at activation. Exact replay of an old success may return its old receipt but cannot reactivate it. An unchanged or changed export cannot restore retracted captures, revive a withdrawn source, reset namespace, or route around a session tombstone. Stale expected coordinates halt/refuse new activation. Restoring eligibility requires a separately specified explicit owner lifecycle action; importer flags do not provide it. Pending groups must be recoverable or halted under the same shared authority, including source withdrawal/control changes between segments.

## Bounded input and parser behavior

All numbers below are proposed importer policy, not provider export limits. Exact boundary and one-past fixtures are required. No limit override in v1.

| Dimension | Maximum / refusal |
|---|---|
| Selected regular input file | 134,217,728 bytes (128 MiB); no leaf symlink, hardlink or special file. |
| ZIP inventory | 4,096 entries; 256 MiB aggregate declared expanded bytes across every entry and aggregate actual bytes from opened members; 100:1 declared expansion ratio per nonempty member and aggregate, also enforced on actual opened streams. Permit only stored/DEFLATE compression. Reject encryption, multi-volume, overlapping members, malformed sizes/CRC on opened streams and duplicate canonical names. Unopened asset integrity is not certified. |
| Selected JSON | 32 MiB actual bytes per member, 128 MiB aggregate selected bytes; at most 32 selected conversation JSON members. Only active-profile allowlisted members are decompressed. |
| JSON structure | Strict UTF-8 JSON, no BOM, duplicate object keys, NaN/Infinity, trailing data or unpaired surrogates; depth 64; 1,000,000 total values/tokens; 100,000 messages and 10,000 conversations per input; strings bound before allocation beyond allowed input limits. |
| One conversation | 8 MiB aggregate normalized message UTF-8 bytes, 10,000 messages, 128 segments; title 200 scalars; provider IDs 1,024 UTF-8 bytes. Oversize refuses that complete conversation, never truncates. |
| Aggregate normalized content | 64 MiB UTF-8 including transcript labels; limits checked before any operation/capture reservation. |
| Parser worker | Foreground stdlib app worker, 30-second wall/15-second CPU/512 MiB memory guard; bounded pipe output and kill/reap on timeout/oversize. No inherited Brain handles, provider keys, credential environment or writable Brain capability. |

The worker is resource isolation, not a claim of a same-user security sandbox. Linux can enforce address-space/CPU limits; macOS requires parent resident-memory observation and kill, whose brief peaks can exceed the threshold. The implementation must state/measurably verify that limitation instead of claiming a hard cross-platform 512 MiB cap. Resource exhaustion maps to a closed code and cannot leave capture state. A bounded streaming lexical guard must enforce depth/token/string constraints before the stdlib decoder constructs an unbounded object graph; do not rely on catching recursion errors after allocation. No new third-party parser dependency is implied.

Accept only active-profile-supported ZIP or standalone conversation JSON. A standalone JSON is a file selected locally, not a schema bypass. Parent aliases are canonicalized using the current descriptor-confined import pattern; lexical `.`/`..`, relative paths, NUL and filesystem root refuse. Pin canonical file descriptor/device/inode/size, read bounded bytes once per pass, revalidate before/after read and compare preview/commit digests. Do not hold a canonical writer lease while a user reviews bodies or while parsing untrusted input; the engine takes its existing writer fence for bounded intent/admission/activation transactions.

Inventory every ZIP name before selecting members, including names of ignored assets. Interpret names as inert metadata, never host paths. Reject absolute/drive/UNC names, backslashes, empty/dot/dot-dot components, NUL/C0/C1/format controls, invalid Unicode, names above 1,024 UTF-8 bytes or 32 components, canonical NFC collisions and case-fold collisions. Directory entries may have one terminal slash; other empty components refuse. Reject symlink/special-file external attributes. Never call `extract`/`extractall`; use bounded member streams by validated identity. Declared and actual bounds both apply; unselected entries count toward declared aggregate/inventory limits but are never inflated. Encrypted/nested archives and asset files are not recursively opened. Percent-encoded names are not URL-decoded.

Archive members, transcript text, links, HTML, tool output and attachment references never trigger network access, shell commands, OS open handlers, external relationship resolution, entity expansion, browser rendering or executable loading. The importer neither downloads the export nor follows its emailed URL. Original ZIP/JSON and attachment bytes are not retained as capture blobs in this slice; only admitted transcript text plus protected identity/hash/omission evidence and durable receipts persist.

## Required acceptance and evidence gates

1. **Provider evidence:** activate both reviewed profiles with primary mapping records; prove stable identity across repeated exports, selected branches, text/attachment/role layout and date treatment. Until then, both return `unsupported_profile`; synthetic success is labeled internal-only.
2. **Synthetic parser corpus:** valid linear/branched normalized cases; same-ID duplicate/divergence; regeneration; empty text; missing/invalid IDs; all omitted roles; mixed text/attachments; Unicode normalization, bidi/ANSI/NUL/surrogate controls; changed title/body; exact/one-past every bound; numbered-file ambiguity; ZIP traversal/collision/CRC/encryption/bomb and file replacement races. No private export fixtures.
3. **Durability and lifecycle:** kill/restart/lost-ack at each receipt/materialization/activation boundary; exact operation replay; same ID changed intent; partial segment visibility exclusion; replacement shrinking membership; old export conflict; missing conversation non-deletion; concurrent head/withdrawal/retraction/control changes; repeated import and Portable restore preserving tombstones and namespace identity. Search and every read/egress path must agree on eligibility.
4. **Distribution:** prove the app-engine-only dependency closure, foreground worker behavior, bounded native CLI preview/body/commit/status and failure recovery on macOS arm64 and Linux x86_64. Native acceptance runs later on the exact candidate; this specification runs no tests/builds or private/model/provider/client checks.
5. **Release wording:** BOTH verified export profiles plus shared lifecycle/receipt/Portable gates must pass before claiming the gap closed. Publication and any private export verification remain separately authorized. Schema/runtime/Portable changes come from the coordinator's concrete persistence contract; the implemented engine successor is20/15/15. Historical parser limits below do not activate either external profile.

Planning estimate after accepted external mappings: 8–14 engineer-days for the bounded two-profile app adapter/parser/CLI and synthetic/native cases, plus shared engine lifecycle/persistence work already counted in the PRD's 24–41-day G2+G3 range. This is a work-package allocation, not an additive total or elapsed delivery promise. Authoritative mapping evidence, both-platform execution and separate owner acceptance may extend elapsed time. Next gate: obtain acceptable schema/specimen evidence for each provider without using private exports under this task.
