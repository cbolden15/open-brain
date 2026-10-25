# W0 contract freeze and baseline

Status: W0 complete. Focused baseline, future-red probes, storage recovery and full repository verification passed. W1 production implementation has not started.

## Pinned inputs

Execution worktree: `$WORKTREES/slack-cited-recall-w0-20261014-006b67`.
Branch: `feat/slack-cited-recall-w0-20261014-006b67`. Starting HEAD: `01a904e2c310e9820ec083b480185cda846e6ebb`; application baseline: `d5cf64e652366dc14a8cf18732f7cb1e82ae4187`.

The implementation plan and hardening report are content-exact copies of the dirty planning inputs. Their hashes and metadata-only observations are in [BASELINE.json]($WORKTREES/slack-cited-recall-w0-20261014-006b67/docs/ai/workstreams/20261014-open-brain-public-slack-recall-w0-20e138/BASELINE.json). Local origin/main still points to the application baseline; it was not fetched, so W1 must recheck the compatibility reservation.

The source catalog reports schema/runtime/Portable `16/11/11`, task contract `t03.v1`, and public acceptance `not_certified`. The installed executable at `/opt/homebrew/bin/open-brain` returns exit 2 for `catalog --json`; its compatibility is unknown. No installed command opened the owner's Brain. Existing MCP accepts policy/consent flags; generic agent setup currently rejects both.

## Frozen contract examples

[slack-recall-contracts.v2.json]($WORKTREES/slack-cited-recall-w0-20261014-006b67/packages/app/tests/fixtures/slack-recall-contracts.v2.json) contains canonical bytes and SHA-256 goldens for the unchanged saved-Markdown request, new Slack request, untrusted evidence envelope, immutable selection and structured read selection. These are synthetic specimens, not retained source proof, approved receipts or Portable exports. W1 must replace every example identity with proof from the real fixture producer.

1. Keep the saved-Markdown DTO, request digest domain and public-copy policy unchanged. Introduce a separate `SlackSharingPreviewRequest` dispatched only for `dto_version=2` and `sharing_kind=slack_work_evidence`. Unknown request fields and version/kind combinations fail closed. The new request binds destination Work space and optional selection ID without changing v1 serialization.
2. Construct `slack-recall-evidence.v1` from exact current projected message text and canonical intake proof. Current Slack reference projection includes the `[protected] ` URL prefix; do not silently strip or change approved bytes. Freeze source time when proven, observation time separately, unknown upstream currency/coverage and the owner-approved safe permalink. Missing/unproven source time is null; permalink and its unavailability reason are mutually exclusive. Original IDs are provenance, never read authority.
3. Make selection membership immutable, ordered and keyed by logical source; reject duplicates and more than 24 members. Changing membership creates another selection ID. Re-approving an edited member keeps the selection and all other member approvals. Each approval binds the selection digest, not other members' revisions. In the read snapshot, resolve only currently eligible approvals for this selection/source/destination/session and choose the lexicographically smallest authorized copy record/revision pair. Return `unavailable` without hidden source/copy IDs when none qualify.
4. Use one field-wise projector at preview, approval and read. Only the envelope's validated citation-permalink field receives the narrow exception. Require approved-byte idempotence; a changed read projection returns `reapproval_required`. Message text containing nested envelope/selection JSON stays quoted content. Structured response IDs and selection fields establish authority.
5. Reserve `17/12/12` only after rechecking main. Persist source proof, independent intake receipts, preview/decision commitments, Work-copy privacy, copy/link/retry/revocation evidence and immutable selection receipts in one W1a/W1b merge unit. Preserve earlier validators and mixed v1/v2 history. Source/route/lifecycle/approval changes use the returning database snapshot; launcher policy and consent remain revocable until the MCP output boundary.

A valid permalink must match proven Slack channel/message identity, use HTTPS with exact `slack.com` or a genuine workspace subdomain of `slack.com`, and have no userinfo, credentials, unexpected port or arbitrary destination. A substring host match is insufficient. Allow only the canonical message-link shape produced by the verified source receipt. Unsafe or redacted links are unavailable with an explicit frozen reason; never recover a URL from untrusted message text.

New response selection fields must be admitted by the versioned closed read schema and runtime handshake. Old clients must refuse new live state; do not weaken existing admission or silently return fields an old validator cannot consume. The contract specimens do not claim the new schema or Portable validator exists yet.

## Verified W0 behavior

The new journey uses actual `PriorityRuntime → SlackSourceClient → LiveSourceManager → engine`, with synthetic credentials and HTTP responses only. CLI operations prove exact projected proposal/rationale text, duplicate import, acknowledged-operation replay after restart, a routed source edit and a separate unrouted correction. Five logical sources retain six revisions. A valid provider-scoped MCP session cannot search or read any originals, including Personal and unrelated Work decoys. Saved-Markdown sharing rejects the real current Slack head with `binding_mismatch` and reserves no preview.

The canonical fixture tests preserve v1 bytes/digests and verify that the frozen v1 DTO rejects v2. Existing regression tests remain the proof for other baseline behavior, not a claim that v2 implements it.

## Deliberately red future probes

[W1-W2-RED.patch]($WORKTREES/slack-cited-recall-w0-20261014-006b67/docs/ai/workstreams/20261014-open-brain-public-slack-recall-w0-20e138/W1-W2-RED.patch) remains unapplied. It was applied temporarily to the synthetic journey, executed, and the green file restored byte-for-byte.

| Probe | Observed baseline failure | Future owner |
|---|---|---|
| Real current Slack source submitted through owner CLI with frozen v2 request | exit 2, `invalid_arguments`; required success assertion fails | W1a sharing dispatch/proof owner |
| Synthetic policy and durable consent first load successfully, then scoped agent setup requests both paths | exit 2, `invalid_command`; required preview-success assertion fails | W2 setup owner after owner risk decisions |

The probe run returned exit 1: two expected failures in 12.89 seconds, 13.35 seconds wall time. These isolate absent contracts. They do not yet prove approved-copy retrieval, selection repair, Portable restoration, setup apply/removal or model reasoning. W1/W2 must expand the probes into those actual acceptance journeys as their components become available. No skip or xfail hides them in the green suite.

## Acceptance ownership

The source plan's acceptance matrix remains normative. Every scenario belongs to the groups below; W0 anchors are baseline evidence only. Later packages must add their new variant-specific tests before marking acceptance complete.

| Group | Scenario allocation | Owner and regression anchors |
|---|---|---|
| Capture and continuity | Actual parsing/admission, duplicate import, lost acknowledgment, restart, routed edit, new source unrouted, partial scan, rate limit and deletion only after complete coverage | W0 journey owns the local vertical slice. Existing collector acceptance, connector Slack live and capture revision-processing suites cover transport/recovery baselines. W1a owns new approval invalidation consequences; W4 reruns the complete matrix. |
| Scope and release | Space/tier separation, Personal/unrelated Work decoys, same-space originals, another-provider copies, no policy, missing/revoked consent, wrong declared provider, approved-copy currentness and prompt-injection boundaries; no restricted metadata influencing output | W1a sharing/projection owners; W2 setup/session owner. W0 proves originals/decoys refused under valid external authority. Existing sharing-surfaces and session-authority tests anchor v1 behavior. Client non-Brain tool control is W3 guidance/optional actual-client evidence, not an engine guarantee. |
| Cited evidence | Unlabeled rationale, multi-message context, proposal versus decision, complete nested fake envelope/foreign permalink, unsafe URL, Unicode quote integrity, incomplete reads/bytes/calls, unavailable/revoked/stale/never-approved member, edited-member repair, 9–24 members, fake membership text and unknown Slack coverage | W1a projection/selection owner plus W3 journey/validator owner. W0 proves exact source bytes/IDs and unlabeled proposal/rationale. The new fixtures freeze authority fields; all selection and approved-envelope behavior remains pending. |
| Changes and retries | Source edit/withdraw/reroute across preview/approval/search/read and both snapshot sides; consent change before output; revoked approval, capture-link loss, historical reads, projection change/idempotence, Portable tampering/restore, reaction-only import, unseen upstream edit, observed re-import and separate correction | W1a sharing/race owner and W1b Portable owner. W0 proves raw routed edits and new-message correction identity. Existing sharing recovery, lifecycle continuations, source-evidence and Portable-v7 suites anchor old history/retry semantics. W4 owns cross-process integrated regression. |
| Client and answer quality | Real stdio negotiation/tools/grants, setup preservation/removal after policy deletion or revocation, coexisting unscoped entries, lexical paraphrases, contradictions, unknown decision owner/effective date, source versus observed time, unknown coverage and separate model-quality reporting | W2 setup owner and W3 stdio/journey/guide owner. Existing local-MCP tests anchor negotiation and output-boundary revocation. W0's adapter test is not real stdio acceptance. Actual-client check remains `not_run` until separately owner-authorized and cannot be replaced by deterministic answer validation. |

Source routing uses `operation_` plus UUIDv4, whereas sharing has its own operation-ID grammar. Lexical search can fall back to disjunction across words: use unique terms when testing superseded-head exclusion. The fixture therefore uses `ten` and `twelve`, rather than assuming `ten teams` cannot match `teams` in the new revision.

## Execution boundary

The first full gate passed lint and type checking, then reported 142 failed, 7,046 passed and five platform skips in 787.82 seconds. Connector/collector failures show `storage_high`; app and engine suites have hermetic storage probes. The completed run retained 4.7 GiB of synthetic fixtures. Its diagnostic reports were archived privately with verified hashes before that one disposable run directory was removed. No other pytest run or real data was removed.

A two-worker recovery run with immediate passing-fixture cleanup passed 123 cases before two more `storage_high` failures stopped it. Its task-specific temporary directory was empty, but host free space fell below two GiB again. This confirmed broader host headroom was required; changing capture admission or merely weakening assertions would hide the problem. The owner approved four exact ignored artifact directories outside active W0. The janitor agent revalidated and deleted them, recovering 5.38 GiB and leaving 9.29 GiB available. Source worktrees, all 14 worktree records and branch refs were unchanged.

After artifact cleanup, the recovery check passed all 190 cases in 295.75 seconds: every one of the 142 previously failed cases plus the existing storage-watermark/admission tests. Full verification then passed on candidate `2bd071a884f3ceacbbb1d650a92fee61e8ff6ef8`, with no source changes during the gate. It bounded auto workers to four and retained immediate passing-fixture cleanup; production admission and helper deadlines remained unchanged.

Final `make verify` results: Ruff passed, mypy passed across 646 files, Python passed 7,188 tests with five existing filesystem-platform skips and one existing fork warning, all four distributions built, Obsidian passed 673 tests, and desktop passed 259 JavaScript, 30 Python and 26 Rust tests plus formatting/type/build checks. The Python stage took 1,703.45 seconds; the complete gate took 1,840.84 seconds. `git diff --check` and `actionlint` also passed. No W0 test was skipped or xfailed.

Follow `BUILD-004` before another full run: check free bytes, use a short task-specific temporary root outside the checkout, bound workers, clean passing fixtures promptly, retain failure diagnostics and reserve space for builds. `TESTING-006` additionally keeps native packaging separate from timing-sensitive tests.

Only W0 is authorized in this milestone. W1 production code, migration and Portable activation remain absent. W2 owner risk choices remain pending. No real Slack/provider calls, client profile edits, service install, push or publication occurred.

W0 is committed locally. Use the validated handoff for a fresh execution thread; start W1a/W1b only when requested and recheck main before reserving compatibility coordinates.
