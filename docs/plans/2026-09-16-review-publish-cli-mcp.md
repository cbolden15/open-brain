# Review and publication through CLI and MCP

Status: implementation complete; execution receipts and merge gates are recorded in the PR. Baseline: `acdbbb8` (spaces/inbox, PR #38).

## Outcome

Owners and explicitly enabled agent clients can turn selected routed captures into a reviewed
canonical note, inspect the draft and evidence, and approve, reject, or edit it. Several captures
can contribute to one note. Later updates retain the note's identity and all earlier provenance.
Completion includes installed CLI/MCP acceptance, independent review, required CI, and merge.
Releasing a new Homebrew version and changing a user's live Brain are separate operations.

## Decisions

### D1: extend the engine and share the application service

The engine owns source validation, immutable proposal snapshots, review bindings, publication,
conflict checks, and recovery. A `ReviewPublicationService` presents bounded public DTOs to CLI
and MCP. Neither adapter queries SQLite or reconstructs private history. The caller supplies
draft Markdown. No model provider, daemon, or automatic title-based deduplication is added.

The existing `ReviewTask.propose(capture_id, drafts, delivery_id=...)` string form remains a
compatible legacy call. Passing an ordered source sequence enters the bound workflow, including
for one source. The new application service always uses that sequence form. Optional
`target_page_id` selects an update; otherwise the proposal creates a page. Bound decisions require
the `expected_review_digest` returned by inspection. Legacy proposals can also be inspected and
decided with an explicit digest; existing internal tokenless calls retain their old behavior.

`review.list` gains bounded pagination and space filtering. `review.show` returns full projected
Markdown and evidence, explicit selected sources, cumulative provenance, destination and target
revision, status, and the review digest. The digest binds the underlying unprojected record.
The response discloses projection; it never silently truncates a draft advertised as complete.

### D2: preserve sources and use publication identity as revision identity

Selected source IDs are unique and ordered. All sources must be routed to the same space.
An update retains the previous page's provenance in order and appends newly selected sources.
The first cumulative source remains the compatibility primary source for old retrieval fields.
The complete source collection is retained and exposed separately; it is never reduced to that
primary source. Original source records, content, trust, and identifiers do not change.

The canonical page keeps its ID and path. Existing immutable publication records already contain
revision bytes, so their publication IDs identify revisions. A second revision-record family
would duplicate that authority. Each approved update names the exact preceding publication and
its content digest. At most one approved successor may follow a revision. Manual file divergence
produces a conflict rather than being overwritten.

### D3: Portable v3 adds one closed review-binding sidecar

Frozen v1 schemas have source arrays but no target/predecessor binding. Preserve v1/v2 semantics
and archives. Add `history/review-bindings/YYYY/MM/proposal_<uuid>.json`; select Portable v3 when
these records exist. The managed-workspace extension remains optional in v3.

Each binding contains the proposal and page IDs, create/update operation, expected predecessor
publication and page digest, selected capture IDs, cumulative provenance, frozen source hashes
and route identities, and a review digest. The digest covers the exact canonical proposal record
plus the binding fields other than the digest itself. A bound decision uses that digest as its
expected state digest. Legacy decisions continue binding the proposal alone.

Validation checks source/evidence coverage, identities, hashes, target space, and a single linear
publication chain whose head matches the current canonical page. Pending/rejected proposals may
share a predecessor; approved successors may not. A legacy publication may be an update's root.
Import must restore pending bindings as well as completed history. Older readers refuse v3.

### D4: additive local migration and serialized recovery

Add migration 5 without changing earlier SQL or checksums. New tables retain immutable proposal
context, ordered source membership, and the current bound page head. The runtime compatibility
record reports schema 5 while retaining session protocol version 1. Historical schema fixtures
remain independent of the new migration SQL.

The existing canonical writer lease is exclusive across processes and threads despite its
`acquire_shared_writer` name. Reuse it. Before a new bound review write, drain incomplete review
decisions so an old reservation cannot later overwrite a newer update. Validate bindings under
that lease; publish with a confined compare-and-swap page operation. Preserve idempotent stage
replay, immutable history, and deterministic reconstruction of the latest search projection.
Prevent rerouting sources once a successful review decision has reserved their publication.

### D5: independent MCP grants and direct owner CLI authority

Use `--allow-review-read`, `--allow-review-propose`, and `--allow-review-decide`, all off by
default. They expose list/show, propose, and terminal decisions respectively. Existing capture,
search, inbox-read, and organize grants do not imply any review capability. Direct owner CLI
commands do not require MCP grants. Claude Code and Codex setup previews bind the new flags and
preserve unowned configuration; desktop setup keeps its existing surface through false defaults.

Public tools are `brain_review_list`, `brain_review_show`, `brain_review_propose`,
`brain_review_approve`, `brain_review_reject`, and `brain_review_edit_and_approve`.
CLI commands live under `open-brain review`. Mutations accept retry keys and return the effective
key plus stable IDs. Errors and tool denial reveal no private source references or host paths.

Input ceilings are 32 cumulative sources, 64 KiB UTF-8 draft Markdown, 200-character titles,
100 list rows, and offset 1,000,000. Evidence uses a bounded excerpt for every contributing
source, projected against all protected references before truncation. MCP limits are 500 reads,
100 proposals, 100 decisions, and 16 MiB aggregate encoded response content per session. Mutation
receipts fit 4 KiB. Account for the actual encoded MCP envelope, duplicated text/structured
content, and request-ID size before executing a write. Oversized inspection returns a clear
limit error without misrepresenting a partial draft as complete.

## Implementation phases

Independent review starts before the final native build. Its repairs receive fresh focused checks,
the full project gate, and a rebuilt executable before merge. This order avoids treating a native
build of an unrepaired candidate as final evidence. CI may start on a draft PR while the final
installed-binary acceptance runs against the same committed source. The PR stays unmerged until
both the local native evidence and every required CI check pass. Local schema rehearsals use verified Portable
backups and clean restores; private receipts retain fingerprints, elapsed time, and rollback assets.

Review repairs preserve legacy request hashes while adding the inspected digest to bound decision
hashes. A fresh key against a terminal bound proposal conflicts instead of claiming an unreserved
retry alias. Imported route identity follows `supersedes`, never insertion order. Imported pending
pages recover validated destinations from their frozen content, while only actual legacy page owners
receive capture-owned canonical fields. V3 validates edited publication frontmatter as well as drafts.

Proposal mutation receipts contain stable IDs and the effective key. Full titles and source arrays
remain in list/show; this keeps receipts within 4 KiB even for maximal Unicode inputs. All new review
reads are confined and bounded. Evidence projection precedes shortening, with disclosure preserved
when inspecting both new and legacy snapshots.

1. Establish baseline tests, add regression tests, and stabilize the shared engine/portable/DTO
   contracts. Keep schema and shared contracts with the coordinator.
2. Implement engine snapshots, migration, decisions, page replacement, recovery, retrieval, and
   Portable v3 together. Rehearse upgrade and verified backup/restore on disposable Brains.
3. Integrate shared service, CLI, MCP, and both clients' setup. Parallel implementation may
   proceed against the agreed DTO contract; dependent acceptance waits for engine validation.
4. Add user docs and a native acceptance runner; execute full verification and native/Homebrew
   checks using isolated data/configuration and the exact candidate executable.
5. Obtain independent read-only review, resolve findings, run required CI, and merge the tested
   source. Record evidence and a validated handoff. Do not publish a release as part of this plan.

## Acceptance matrix

The [acceptance map](2026-09-16-review-publication-acceptance.json) contains literal pytest node IDs
and native check names for every row. The installed runner executes all six operations through the
owner CLI and both generated client configurations on disposable Brains. It records every mandatory
check as passed, failed, or skipped and exits nonzero unless all pass.

Run the native acceptance entry point:

```sh
uv run --frozen python -m tools.open_brain_dev.review_publish_acceptance \
  --executable /ABS/PATH/TO/CANDIDATE/open-brain \
  --output /ABS/PATH/TO/PRIVATE/report.json
```

| ID | Required proof | Evidence |
| --- | --- | --- |
| A1 | All six operations work through CLI and real MCP stdio; rejection leaves pages unchanged. | [A1 exact test nodes and native checks](2026-09-16-review-publication-acceptance.json) |
| A2 | Seven synthetic captures in groups of 2/3/2 produce exactly three notes with full provenance and unchanged sources. | [A2 exact test nodes and native checks](2026-09-16-review-publication-acceptance.json) |
| A3 | Explicit updates retain page identity/path, prior history/provenance, and one current search result; edit/reject/manual-change cases pass. | [A3 exact test nodes and native checks](2026-09-16-review-publication-acceptance.json) |
| A4 | Stale digest/source/route/target, invalid sources, and input limits refuse without partial publication. | [A4 exact test nodes and native checks](2026-09-16-review-publication-acceptance.json) |
| A5 | Retry identity, payload conflicts, concurrent winners, and crash/replay boundaries cannot duplicate or overwrite newer revisions. | [A5 exact test nodes and native checks](2026-09-16-review-publication-acceptance.json) |
| A6 | New/old schema convergence, existing pending/terminal history, restore, refusal, timeout, and upgrade concurrency pass. | [A6 exact test nodes and native checks](2026-09-16-review-publication-acceptance.json) |
| A7 | Create plus two updates survives restart/reindex/export/import/re-export without byte or identity drift; v1/v2 and managed archives remain supported. | [A7 exact test nodes and native checks](2026-09-16-review-publication-acceptance.json) |
| A8 | MCP grants are independent/default-off; direct owner CLI needs no grant flags; both clients' setup preserves unrelated state. | [A8 exact test nodes and native checks](2026-09-16-review-publication-acceptance.json) |
| A9 | Pagination, Unicode/escaping, protected references, adversarial text, unknown fields, and quota/wire-size boundaries pass. | [A9 exact test nodes and native checks](2026-09-16-review-publication-acceptance.json) |
| A10 | The exact native executable completes the synthetic workflow directly and through both clients' generated MCP configurations. | [A10 exact test nodes and native checks](2026-09-16-review-publication-acceptance.json) |
| A11 | Tested help/docs explain the full workflow, permissions, conflicts, privacy, and release availability. | [A11 exact test nodes and native checks](2026-09-16-review-publication-acceptance.json) |

Baseline focused suite: 284 tests passed on the unmodified merged source. This is baseline
evidence only, not acceptance of the new implementation.

Required final gates: `make verify`, `make native-audit`, `make homebrew-smoke`,
`git diff --check`, `actionlint .github/workflows/ci.yml`, and every required GitHub check.
No mandatory goal check may be skipped. Existing platform-specific skips need evidence from
their applicable platform. Retain private backup/rollback assets for at least seven days;
no live Brain cutover or user installation replacement is authorized.
