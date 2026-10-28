# Gotcha registry

Non-obvious behaviors, sharp edges, and lessons learned belong here.

## Registry

### REVIEW-008: Live page diffs need current canonical read eligibility

Category: Review / Privacy.

A stale proposal's frozen sources can remain readable after a competing update
adds a source that is later retracted. Valid current page bytes alone do not
authorize exposing that newer body in `page_diff`.

Validate the exact current revision through `RecordProjector.canonical` with the
effective review authority in the same read snapshot before using its body or
source references. Omit the diff on refusal while retaining authorized frozen
inspection. Cover implicit MCP owner and local owner reads after real retraction,
plus eligible current-source projection and manually edited target omission.

Discovered: 2026-10-27, combined review I1 synthetic regression.

### REVIEW-007: Acceptance matrix deliberately publishes two pages from one capture

Category: Review / Testing.

The installed-executable operation matrix approves and edits separate proposals
from one capture. The second publication must explicitly opt in to duplicates;
otherwise the duplicate guard stops the remaining mandatory checks. Pass the
CLI opt-in as a unary flag and the MCP opt-in as a boolean.

Discovered: 2026-10-27, combined review publication verification.

### TYPE-003: SQLite row comparisons can leak Any through boolean helpers

Category: Type Safety.

Symptom: comparisons of dynamically typed SQLite row values made a helper
declared to return `bool` fail MyPy's `no-any-return` check. A redundant cast
of an already typed MCP tool description also failed typechecking.

Action: type SQLite row operands as `object` before comparing, avoid casts on
already typed values, and run the full typecheck after changes to shared code.
Wire responses typed as `dict[str, object]` also require narrowing nested values
before indexing them; assert the expected container and text types in tests.

Discovered: 2026-10-27, review publication Phase A verification.

### OBSIDIAN-007: Bridge publication DTOs are exact-key; shared review fields must be withheld

Category: Obsidian plugin / Wire compatibility.

Symptom: `ReviewPublicationService.show` and `list` serve MCP, the CLI and the
plugin bridge. The plugin parses facet `publication.show` responses and
`publication.list` rows with `exact(...)` (`facet-inspection.ts`,
`facet-publication.ts`), and
`test_bridge_uses_shared_organization_and_publication_services` pins the show
key set to `t06-client-api.json`. Adding an agent-facing field such as `stale`
to the shared output breaks facet publication in the plugin. New error codes
fail the same way: `PluginBridgeFailure` raises on any code outside its
allowlist.

Action: `plugin_bridge._frozen_publication_result` strips
`_BRIDGE_HIDDEN_REVIEW_FIELDS`, and `_BRIDGE_REVIEW_CODES` folds new review
codes into `review_conflict` before `PluginBridgeFailure`. Add each new
agent-facing review field or code there unless the plugin parsers change in the
same commit. Synthetic plugin contract producers that call the service directly
must apply the same bridge filter before comparing frozen fixtures.

Discovered: 2026-10-27, issue #126.

### REVIEW-006: Rejecting a proposal was bound to the live target revision

Category: Review / Publication.

Symptom: a full-page update proposal whose target page advanced after drafting
stayed `pending` forever. `review reject` and `brain_review_reject` returned
`review_conflict`, because `ReviewOperations._decide` ran
`validate_current_binding` for every outcome, including `REJECTED`, and that
check compares the live page head with the binding. A fresh `review show`
returned the same token and binding, so "inspect again" could not help.

Action: rejection now skips the live source, route and target check for
non-facet bindings; it writes no page. Approve and edit-and-approve still run
it, and a moved page head raises `ReviewBindingConflict(binding="target")`,
which the app reports as `review_target_stale`. Facet (binding v5) proposals
still validate on reject because `validate_facet_current_binding` also enforces
query and subject authority. When adding a check to `_decide`, decide per
outcome whether it protects a write.

Discovered: 2026-10-27, issue #126.

### CAPTURE-004: The capture redaction check flags any 32-character run

Category: Capture / Saved items.

Symptom: the first saved item design refused any record with a redaction finding.
`has_redaction_finding` (`open_brain_engine/capture/redaction.py`) is broader than
secrets: any run of 32 or more letters, digits, `_` or `-` counts, as do email
addresses, phone numbers and `token=` assignments. Measured on 2026-10-26: a
hyphenated 70-character article slug, a 36-hex `id` query value and an `fbclid`
value all trigger it. Refusing on it would drop ordinary article saves and most
links shared from Facebook.

Action: saved items mask the title and every text section with `redact_text` and
keep the save. Only the URL query that survives tracking-parameter removal and the
fragment are checked, and a hit refuses the record as `url_secret`; the URL path is
never checked. See docs/integrations/saved-items.md. Use the finding check as a
refusal gate only on values that should never carry long identifiers.

Discovered: 2026-10-26, saved item capture spec review.

### CAPTURE-005: Saved item boundaries and page ordering need explicit checks

Category: Capture / Saved items.

Context: URL parsing can strip controls before validation, and unencodable text
can raise instead of returning a named refusal. Filesystem exceptions can expose
entry names. Sorted JSON persistence also loses insertion order needed for index
eviction. Scan limits or early byte-budget rejection can let a later duplicate
win. Repeated long tokens in pressure fixtures can be redacted, removing the
intended byte pressure. A test patch through another module's unexported `os`
import also fails mypy.

Action: validate URL controls and UTF-8 before parsing and return named refusal
codes. Translate filesystem errors without raw entry names. Opt into unsorted
persistence for insertion-order eviction while keeping the shared serializer's
canonical default. Scan beyond output caps, resolve identity winners before
trimming to the byte budget, bound candidate state and defer overflow identities.
Use synthetic prose and assert encoded-size preconditions in pressure tests.
Patch the test module's own `os` import for mypy-safe fixtures.
Validate decoded non-URL text with strict UTF-8 encoding too: ASCII JSON can
contain escaped lone surrogates that pass byte decoding and size checks, then
crash custody admission. Refuse these locally as `invalid_why` or `invalid_field`.
Use ASCII-escaped surrogate fixtures so the regression reaches the parser;
preserve valid non-BMP characters and correctly paired escapes.

Discovered: 2026-10-26, saved item implementation and verification.

### DOCS-005: The Drive guide said an edited single file re-imports; the engine quarantines it

Category: Documentation / Google Drive.

Symptom: `docs/integrations/google-sources.md` and the 2026-09-16 live acceptance
record described a Drive body edit as a re-import of the selected file. On the
current engine that is false for the `drive_file` shape. The collector submits a
`drive_file` intake under a stable delivery id with a `Source revision:` text
prefix and no revision observation, so a changed body reaches the engine as a
`DeliveryConflict`; the collector records the receipt as `quarantined` with
`source_revision_conflict` and the first capture stays searchable. The live
"body edit" PASS at `9f22849` ran before the schema moved past `user_version 7`,
where the legacy replacement rule still applied. No Drive test on the current
schema pinned the behavior until the Docs-set work added one.

Action: the Drive guide now states that an edited `drive_file` is quarantined,
and only the `drive_folder` and `drive_account` shapes go through the
collector's revision-aware path (`observe_revision` and `drive-revisions`).
Drive set revisions have no exact-replay archive guard (that check is
Slack-only in `lifecycle.py`); Drive relies on its revision hash, which
includes `modifiedTime` and `version` and changes whenever Drive records an edit.
When a guide statement rests on a dated live receipt, re-check it against the
current engine path before repeating it; an acceptance PASS certifies the schema
it ran on, not later ones. Do not route `drive_file` through the revision sink to
"fix" the docs: existing records would turn their first edit into a conflict
against the stored capture.

Discovered: 2026-10-25, Drive Docs sets design review.

### BUILD-021: The daily brief pushed the base archive past the audit object budget

Category: Build Pipeline.

Symptom: the daily brief candidate passed `make verify`, but `make native-audit`
and `make homebrew-smoke` failed with `base archive failed the strict content
audit`. The worker's only finding was `artifact-invalid`, and the log named no
module. Running `artifact_audit.Scanner` in-process showed that `MarshalReader`
stopped on `require(0 <= count <= MAX_OBJECTS - self.scanner.objects)`. The base
archive decoded 550,577 objects against the 550,000-object aggregate cap. The
brief's share was about 7,100 objects: about 6,240 in its five new modules and
about 870 in the CLI, MCP and agent-setup modules it extended.

Action: the cap is now 600,000. The measured local scan took 6.82 seconds of CPU
(7.08 seconds wall) with no content findings, and the 30-second worker CPU limit,
`MAX_DEPTH` and all content rules remain intact. Read `artifact-invalid` as "the
scan did not finish", not as a bad archive (see INTEGRATION-010). To measure,
raise `MAX_OBJECTS` in-process, run `Scanner([]).archive(data, name)` on
`build/native/release/open-brain-*.tar.gz` and read `scanner.objects`. Exercise
the actual native archive after foreground CLI additions; `make verify` does not
build it.

Discovered: 2026-10-24, daily brief frozen-tree gate.

### TESTING-018: Full verification needs external temporary roots and modern Ruby

Category: Testing / Environment.

Context: A repo-local `TMPDIR` makes harness acceptance refuse evidence inside
the checkout. A repo-local pytest base also makes source-guide fixtures fail
`unscoped_client`: their synthetic projects have a Git repository ancestor.
On macOS, system Ruby 2.6 rejects the endless method syntax in embedding
formula tests. An interrupted full run exposed all three; representative cases
passed with temporary roots outside the checkout and Homebrew Ruby 4.0.7.

Action: When authorized, use a unique short temporary root under `/tmp` for
`TMPDIR` and pytest's base, and put a modern Ruby on `PATH` before `make verify`.
Keep logs outside pytest's base and retain failure evidence. Run the complete
gate; a partial diagnostic run does not establish a full pass.
When a fixture mocks a global filesystem function, scope the patch to the
assertion so pytest can restore the real function before temporary-path cleanup;
Python 3.14 cleanup calls `os.lstat` with `dir_fd`.

Discovered: 2026-10-27, saved item final gate.

### TESTING-017: The plugin process-group test times out under full `make verify` load

Category: Testing / Obsidian plugin.

Symptom: in `make verify`, `tests/bridge.test.ts > OpenBrainBridge > terminates a
descendant in the owned process group on unload` failed with `Timed out waiting
for .../open-brain-plugin-child-test-*/ready` after about one second. The branch
had not changed the plugin. `make plugin-test` alone passed 702 of 702 straight
after.

Action: if this is the only failure, rerun `make plugin-test` alone and record
both results, as for TESTING-014. A failure that also happens alone is a real
regression. The test's `waitForFile` deadline is the next thing to look at if
the timeout keeps recurring.

Discovered: 2026-10-24, daily brief frozen-tree gate.

### TESTING-016: Same-process readers fail fast while another thread holds the writer

Category: Testing / Concurrency.

Symptom: `test_concurrent_exact_commit_reconciles_inside_writer_fence` failed in
full-suite runs with `LockBusyError("lease already held by this process")`, about
6 times in 150 runs under CPU load on both main and PR #119, and never when run
alone. `commit_consumer_import` left its writer fence and then opened a reader in
`recover_consumer_import`. The other thread polls the writer every 10 ms; when its
poll landed in that sub-millisecond gap it took the writer, and
`acquire_shared_reader` raised at once. Writers wait on a bounded poll; readers
never wait for a same-process writer. Main's CI had not hit it because the
branch moved the test into a heavier shard.

Action: a read that follows a write in the same operation belongs inside the
writer fence (`_transaction`), not in `read_snapshot`. When a test fails only in
the full suite, compare failure rates on main and the branch under the same
artificial load before treating it as a branch regression. `_accepted` still
reads outside the fence and is the next candidate if this error returns.

### BUILD-020: A Portable bump must also update the native smoke pins

Category: Build / Release.

Symptom: after Portable moved from 17 to 18, `make native-audit` and
`make homebrew-smoke` failed with `native verified export failed`. The smoke
journey in `tools/open_brain_dev/base_native.py` still required
`schema_version == 17` and the Portable 17 sidecar list. `make verify` does not
run that journey, so the gap appeared only at the frozen-tree gate.

Action: every Portable version change updates `_PORTABLE_Vn_SIDECARS` and both
`schema_version` checks in `base_native.py` in the same commit as the engine
pin. Put `base_native.py` on the pin inventory of every coordinate plan.

### CONTROL-004: macOS refuses a socket receive timeout after the peer closes

Category: Control / Platform.

Symptom: the desktop test `control_roundtrip_binds_response_to_request` failed
on macOS CI about once in a few hundred runs with `collector_lost_response`,
although the responder thread had sent a complete reply. The responder writes its
reply and closes at once. When that happened before `receive()` called
`set_read_timeout`, macOS failed the `setsockopt(SO_RCVTIMEO)` call with
`EINVAL`, and `receive()` reported the buffered reply as lost. Linux accepts the
call, so the race never showed there. A real collector that answers and hangs up
quickly hits the same race in the desktop app.

Action: bound socket reads with `poll(2)` and the remaining deadline, as
`collector.rs` `readable()` does, instead of setting `SO_RCVTIMEO` per read. To
reproduce, write a complete frame, close the peer, then receive; the test
`receives_a_complete_response_after_the_peer_closed` does this.

Discovered: 2026-10-23, CI flake triage after PR #103.

### CI-003: Hosted-runner cold-start samples need the fastest sample, not the median

Category: CI / Measurement.

Symptom: the macOS `nw0_graphify_probe.startup` check failed twice, on
2026-10-20 and on PR #107. Baseline and candidate are the same executable, yet
the candidate's median cold start came out 0.68 s slower against a 0.5 s budget.
Single cold samples after `purge` ranged from 1.1 s to 3.3 s on hosted macOS
runners. Linux samples stayed within 0.05 s.

Action: the check compares each layout's fastest sample. Interference on a shared
host only adds time, and a real cost raises every sample. Across 20 retained macOS
runs the fastest-sample delta never exceeded 0.30 s, while the median delta
reached 0.68 s. A paired median reached 0.51 s, so pairing alone does not fix it.

Discovered: 2026-10-23, CI flake triage.

### TESTING-015: Ancestor trust checks pass on macOS and fail under Linux `/tmp`

Category: Testing / Platform.

Symptom: PR #100 passed the full suite on macOS, but Linux CI failed 154 W5
guide tests with `unsafe_path`. The guide's collector ancestor check rejected
every group- or world-writable directory. On Linux, pytest's `tmp_path` sits
under `/tmp`, which is `1777` and root-owned. On macOS it sits under
`/var/folders`, which is not world-writable, so local runs never reached the
case. An earlier local run with `--basetemp` under `/private/tmp` showed the same
failures and was misread as a test-setup mistake.

Action: path-trust walks must accept a root-owned sticky ancestor, as
`agent_settings_observation.py` does, and test that case directly. Before
pushing, run new path-trust tests on macOS with `--basetemp` under
`/private/tmp`. That reproduces the Linux layout.

### TESTING-014: Plugin `t07-ui` `vi.waitFor` default timeout flakes under load

Category: Testing / Host.

Symptom: during a coordinator `make verify` on the W5 branch, one assertion in
`packages/obsidian-plugin/tests/t07-ui.test.ts` failed because `vi.waitFor` hit
its default 1000 ms timeout at 1008 ms. Python tests were running concurrently
on the same machine. Three isolated reruns passed 702/702. W5 does not touch the
plugin.

Update, 2026-10-23: it also repeats in isolated plugin runs. The full plugin
suite alone, with nothing else running, failed 4 of 6 runs on `main` and 5 of 5
in a fresh worktree. The waited-for `read` follows a real child-process round trip
(spawn, handshake, status, reconcile). That round trip took 357–409 ms when the
test ran by itself and 816–1,733 ms beside the other plugin test files, so the
1,000 ms default sat inside its normal range. The test checks unload-during-read
behavior, not latency. It now waits up to 10 s, with the 20 s test timeout used
for other child-spawning plugin tests, and passed 8 of 8 full-suite runs.

Action: a `vi.waitFor` that waits on a real child process needs an explicit
timeout above that process's loaded latency. Measure the latency before choosing
one. A failure that persists with a 10 s wait is a real failure.

Discovered: 2026-10-22, W5 guided Slack final verify.

### TESTING-013: Any checkout write during `make verify` fails harness acceptance

Category: Testing / Verification.

Symptom: the W5 guided Slack baseline `make verify` reported 8745 passed and 26
errors, all in `tests/release/test_harness_acceptance.py` with
`source_changed_during_run`. The coordinator had written workstream notes into
the checkout while the run was in progress. The same file passed 32/32 on a clean
checkout of the same commit.

Action: treat these errors as a tree-changed signal, not a code failure (see
BUILD-018). Commit or move every file first, including untracked workstream
notes and gotcha edits. Then run the gate on a frozen tree with no other writer
in that checkout and keep logs and receipts outside it.

Discovered: 2026-10-22, W5 guided Slack workflow baseline.

### BUILD-019: Native fallback modules consume the fixed archive audit budget

Category: Dependencies; Build Pipeline.

Consumer intake increased the harness archive to506301 marshal markers, above
its fixed500000 limit. Full source verification and native smoke do not establish
archive acceptance. Keep all scanner ceilings, content rules and reviewed stdlib
payload hashes unchanged. Do not strip assertions or exempt the new engine code.

The supported native interpreters already provide compiled decimal: a dynamic
extension on the tested macOS build and a built-in compiled symbol in the prior
Linux CI artifact. Require C decimal at build and installed self-check, prove
exact decimal arithmetic and implementation identity, exclude `_pydecimal` from
the core/harness closure, and forbid that fallback in the core module audit. The
ordinary Python package retains its standard-library fallback behavior. Final
Linux CI must prove the installed check works; symbol presence alone is not
execution evidence.

Graphify has a separate frozen dependency closure and does not require compiled
decimal. Keep this core-only restriction out of the shared forbidden-prefix list:
Graphify consumes that list too, and its unchanged fallback is not a core payload.

Diagnostic metadata compaction and docstring removal were insufficient and were
not shipped. This fix removes an unused alternative implementation while keeping
all required engine modules and the complete strict artifact inspection.

### LIFECYCLE-010: Consumer recovery must reconcile inside the writer fence

Category: Database/Migrations; Type Safety; Build Pipeline.

An optimistic accepted-operation lookup does not serialize duplicate commits.
Repeat exact request reconciliation after obtaining the writer fence. A recovery
runner may encounter a group another runner just activated; reconcile the exact
completed manifest and custody before applying its now-old source preimage.
Public owner lifecycle mutation entrypoints must reject typed consumer requests.
Private consumer steps revalidate accepted proof/profile/session under the writer
fence; generic activation must not bypass scope/archive/outcome publication.
Predecessor restore audits must refuse all unexpected successor tables, even
when the older serializer cannot see them. Pin each old test producer and add
the new current version to recovery-closure and relationship catalog matrices.
Audit parameterized producer pins and digest maps, not only numeric assertions.
Current owner approvals require the explicit successor validator; the frozen
predecessor validator must continue refusing journal2. Extend the separate
managed-write recovery schema allowlist while preserving the current writer
floor; ordinary bootstrap admission does not cover this owner recovery path.
Privacy changes on identical text are conflicts, and a known noncurrent revision
cannot be promised as a fresh replacement. Test these interleavings directly.

Ordinary startup capture recovery must leave consumer reservations alone.
Accepted delivery IDs need protection before custody exists. Every current
member's route and effective privacy contributes to whole-conversation reads.
Preserve predecessor owner-history behavior while withholding all staging.

A halted capture can have a stage0–2 row or partial file. Portable15 validates
these against retained custody and normalizes only a disposable export snapshot;
never finish materialization on halt or delete source rows to make export pass.
Stage3 history remains retained. Re-export and status must match after restore.

Split exact-ID and normalized-text input objects. Hex-encoded maximum IDs plus
JSON-escaped text can exceed32MiB even when both independent budgets are valid.
Use strict booleans for scope/replacement flags; Python equality admits0/1 as
False/True. Avoid shadowing dataclasses.field with a local validation field name;
alias the import explicitly and run Ruff plus strict mypy before the full gate.

### LIFECYCLE-009: Consumer identifiers and provenance need distinct admission

Category: Type Safety; Database/Migrations.

Portable canonical JSON NFC-normalizes strings, while provider identity is an
exact UTF-8 scalar sequence. Encode provider-ID bytes in explicit hexadecimal
wrappers before identity hashing; normalize transcript text separately. Do not
let canonically equivalent provider IDs collapse to one conversation/message.
Frozen owner lifecycle envelopes reject third-party captures. Use an explicit
successor consumer envelope and shared replay adapter, preserving the original
bytes and validators. Decoding a general capture envelope alone does not prove
capture-only authority; require exactly the injected capture.accept role.

Annotate extensible DDL tuples as tuple[str, ...] before appending generated
triggers. In adversarial dataclass tests use typed replacement keywords or an
explicit dynamic field name; formatter rewrites of literal setattr can turn an
intentional runtime refusal into a static read-only-field error.

### LIFECYCLE-008: Approval receipts need exact typed bytes and a bounded commit

Category: Type Safety; Database/Migrations; Build Pipeline.

Python dictionary equality treats1, True and1.0 as equal. Compare canonical
receipt bytes to the derived receipt, so a changed count type cannot pass while
retaining the old receipt ID. Test boolean/float substitutions through Portable
validation. Keep source-module imports explicit in strict mypy tests; importing
a private helper through another module is not a declared re-export.

Session retraction commits an authoritative marker and receipt without scanning
or deleting every search row first. Current read predicates must reject retained
projection rows immediately. Preserve frozen predecessor semantics and test
search, direct/history reads and Portable rebuilds with stale rows present.
Compatibility cutovers also update both CLI verified-export receipt allowlists,
native journey coordinates and migration/recovery no-op version sets. Update
the executable documentation-example coordinate map, feature matrix, and its
mutation test together; release tests bind them to the engine constants. Recovery
closure validation and capture replay must admit the new Portable baseline via
its exact catalog and validator. Parameterized predecessor producers must pin
the old schema when a new current version is added. Include each owner CLI module
in the explicit foreground architecture inventory.
An opener that skips recovery must also skip constructor initialization and
metadata publication. Verify filesystem publishers are never called for status
or page reads; an unchanged SQLite dump alone does not prove a read-only open.

### LIFECYCLE-007: Retraction effects extend beyond selected captures

Category: Type Safety; Database/Migrations.

Retraction inspection must include unselected current-group siblings and reverse
historical, sharing and development dependencies. Graph eligibility also checks
all retained managed revisions and follows inactive links. A current-head-only
impact query understates the approval scope. Test the full closure and exact
100/101-item boundary, then bind effective privacy and the whole selected
membership to freshness checks. Explicitly annotate variable-length tuples:
mypy otherwise infers a one-element capture tuple before the session branch.
Keep public preview approval evidence separate from the frozen journal v1.
Freshness needs more than the affected set: bind preserved newer heads and
forward dependencies/co-provenance as well. A co-support's tombstone can change
an affected object's eligibility while every displayed effect remains identical.

### GIT-001: Recreating a merge can lose its manual edits

Category: Git; Verification.

In the competitive-gap rebase, W4's recreated merge lost README and first-use
updates even though the rebase completed. Save a backup ref and compare the old
and new trees against the incoming main diff. Restore feature edits in files that
main did not change, and check production patches with range-diff. Preserving the
merge graph does not guarantee that its manually resolved tree is preserved.

### BUILD-016: Plugin handshake coordinates must follow engine cutovers

Category: Dependencies; Build Pipeline.

The schema18/runtime13 engine passed Python verification while the plugin still
required17/12. Real-child facet tests correctly refused capability negotiation.
Update the plugin contract constants and current-admission test wrappers together
with engine and desktop coordinates. Keep frozen historical DTO corpus bytes
unchanged; test refusal of both predecessor and future coordinates. Run the full
plugin build/typecheck and live transport tests before packaging plugin assets.
The full700-test plugin suite passed with one worker after two one-second
readiness checks failed at default and two-worker concurrency on this workstation.
Keep the assertions and deadlines intact; record the worker cap with the receipt.

Follow-up, 2026-10-16: The same two one-second readiness waits failed once with
one worker after a full Python run. Both affected files passed unchanged (33
tests), followed by all700 plugin tests. Independent review found no branch
change in the readiness or unload behavior. Record the failed aggregate and
successful retries separately; a passing retry does not make the first run green.

### BUILD-018: Acceptance source binding includes workstream notes

Category: Build Pipeline.

The harness acceptance fixture hashes every Git-visible repository input,
including untracked workstream state and review notes. Updating those notes
while `make verify` runs correctly fails with `source_changed_during_run`.
Freeze bookkeeping as well as code for verification. Keep live logs outside
the repository and write receipts after the bound acceptance run finishes.

### LIFECYCLE-006: Lifecycle cutover spans capture wrappers and Portable proof readers

Category: Database/Migrations; Type Safety.

Schema18 adds a materializer reservation keyword. Full mypy caught an existing
fault-injection wrapper that omitted it and a history predicate using the wrong
connection variable. Keep typed wrapper signatures aligned with the materializer;
run full typecheck after changing a shared capture signature.

Portable13 also needs both verified-export receipt writer and reader allowlists.
A valid archive can otherwise fail CLI `--verify` after export. Exercise the CLI
proof path as well as engine roundtrips. Pending physical captures need retained
history-only source metadata for frozen Portable source coverage; current and
owner-history reads must still check journal admission before exposing them.


### SQLITE-001: Context managers do not close SQLite connections

Category: Config/Environment; Database/Migrations.

Use `contextlib.closing` for owned read connections in capture admission.
`with connection:` ends a transaction but leaves the connection open. New
lifecycle checks initially leaked parent connections across plugin child writes.
The child recreated WAL/SHM files; fresh parent reads then failed with extended
SQLite error522 (`SQLITE_IOERR_SHORT_READ`) and appeared to have an invalid schema.

A controlled synthetic A/B left two connections with raw context managers and
reproduced the error; garbage collection restored reads. Explicit closing left
zero connections and passed the live plugin, stdio and CLI facet regressions.
Exercise multi-process foreground readers when adding an admission read.

### DOCS-004: Passing documentation unit tests do not certify the pinned artifact journey

Category: Documentation / Verification.

Main at `8889181` has eleven marked first-use/doctor blocks, but its unchanged
`mcp-grant-isolation` body SHA `8377509a…` differs from the pinned `adc3c687…` hash. The ten focused documentation tests
pass while validate_example_contract rejects that pre-existing mismatch.
Preserve both bodies and pins as evidence; renewing current guide/artifact
proof is a reviewed candidate task, not a blind expected-hash update.

A synthetic README smoke also found that macOS /tmp is a symlink and fails
the existing no-follow data-directory admission. Resolve the newly created
temporary parent with pwd -P before using it as --data-dir. The corrected
capture/search/setup-preview source CLI smoke passes without changing any
filesystem or production admission rule. Neither check launches a real
client/provider or certifies an installed native artifact.

### DOCS-003: Historical release statements and compatibility pins need source reconciliation

Category: Documentation / Product evidence.

At main d5cf64e, README still names schema/runtime/Portable9/4/5 and the
first-use script asserts10/5/5, while active engine/runtime/catalog constants
establish16/11/11. A historical successful documentation gate does not prove
these literal examples target the current binary. Derive current claims from
the actual source and preserve the distinction from older released resources.

The September8 roadmap also says no release exists, but the public v0.1.0
release was published September13 and the tap references its paired platform
assets. Verify current public release metadata before treating an old roadmap
sentence as absence. Release existence does not certify current main or an
unmerged candidate. The October15 competitive-gap audit records source anchors
and a fresh read-only release/formula metadata check; it claims no new install,
provider, GUI or runtime acceptance.

### COLLECTOR-008: Source health reads can initialize state and overstate evidence

Category: Source status / Recovery.

Existing source status dispatch constructs LiveSourceManager/LiveCaptureService;
PrivateJsonStore construction and directory reads create absent directories and
locks. A no-write status path must bypass those constructors, credential access,
control IPC and engine bootstrap. Existing per-file atomic replacement does not
provide a committed snapshot across source, preview and custody files. Source
generation hashes configuration, not every mutation; stable reads support
per-store evidence, with global consistency unknown.

Direct foreground imports update source last_run without recording runner health.
An overwritten failed last_run cannot prove a previous successful completion.
Stored next_run may be a scheduling fallback, so it does not prove a provider
retry deadline. Existing live_manager pending_retries also reads a nonexistent
top-level custody.quarantined_count and defaults to zero; do not reuse that metric
for selected-source health. W5 P0 records these source observations and specifies
honest unavailable fields; no runtime metric fix is included in that phase.

Source: packages/collector/src/open_brain_collector/live_capture.py:146,729,924;
packages/collector/src/open_brain_collector/live_manager.py:529;
packages/connectors/src/open_brain_connectors/runtime/live_storage.py:29,36,161.

### TESTING-007: Scoped stdio fixtures need grants and complete source scans

Category: Config/Environment / Verification.

A launcher policy narrows authority; it does not enable the CLI tool facades.
Synthetic MCP subprocess fixtures must pass explicit search/content-read grant
flags as well as policy/consent. Diagnose EOF with the child status and stderr
before classifying it as an oversized frame. Keep an overall line deadline:
readability of the first byte does not bound a later blocking readline.

A complete mocked Slack history scan must retain unchanged prior messages.
Omitting them models an observed deletion and correctly invalidates their sharing
approvals. Citation fixtures must also compare exact returned projected text:
intake normalizes Unicode to NFC, so a decomposed spelling is not an exact quote
of the returned composed text. These are acceptance-fixture corrections, not
reasons to weaken runtime currentness or projection.

A history-only Slack transport fixture must not advertise a positive `reply_count`
unless it also implements `conversations.replies`. That count triggers real thread
expansion; `latest_reply` alone does not. W4's first new reaction-refresh case failed at
that transport assertion; narrowing it to reaction-only metadata exercised the
intended revision-stability oracle and passed without a runtime change. Keep
thread-refresh cases in fixtures that support the reply endpoint. The complete
failed log and byte-preserved synthetic fixture are retained in the W4 receipts.

The inherited journey fixture also left read-only connections alive because
SQLite context exit does not close them. Explicitly close its identity/source
reads before cross-process sessions; see SHARING-001 and COLLECTOR-002. Select
a known non-anchor message for edit tests rather than a random source-ID order,
so the still-approved anchor remains a valid external selection entry point.

The plugin facet transport fixture launches checkout/.venv/bin/open-brain directly,
so UV_PROJECT_ENVIRONMENT alone is insufficient in a fresh worktree. Provide its
expected executable using the same task environment before a full gate. An actual
ignored .venv directory with an external bin alias preserves the short environment
path; a .venv symlink itself does not match the directory-only ignore pattern.
Verify the alias is ignored and points to the current source environment. The
first W3 full gate passed all 7,474 Python cases but failed eight plugin children
until that local environment prerequisite was present; all eight then passed.
On a reused worktree, check that the alias target still exists: a retained
`.venv/bin` can point at a deleted temporary environment even after a separate
`UV_PROJECT_ENVIRONMENT` has been synchronized successfully.

Discovered: 2026-10-15, W3 real-stdio journey; failed logs/fixtures retained.

### MCP-002: Source-only selection expansion must not receive canonical page IDs

Category: Privacy / Compatibility.

The complete record reader serves both captures and canonical pages. A source-copy
selection helper historically returned no descriptor for page IDs. Adding a
capture eligibility guard to that helper made ordinary canonical MCP reads fail
with not_found after their canonical projection had succeeded. Call source-only
selection expansion only for source records; retain canonical member eligibility
before every current/history read. Cover ordinary canonical current and paginated
history reads through real stdio alongside protected mixed-source refusals.

Discovered: 2026-10-14, W2 full-suite history regression; failed fixture retained.

### BUILD-014: New app modules must update the exact architecture inventory

Category: Build Pipeline.

The application package has an exact source-file allowlist in the architecture
security tests. Adding the finite settings observer and scoped authority validator
without registering those two foreground modules failed the full suite after
focused behavior tests passed. Update the exact inventory for intended modules;
keep the equality assertion and prohibited-surface checks intact. Include the
architecture tests when adding an application source file.

A later full run also found a W0 CLI baseline asserting that the planned policy
flags were unrecognized. Once those flags exist, incomplete authority must still
refuse, but through invalid_arguments rather than invalid_command. Update this
pre-feature expectation while preserving the refusal/no-write assertions and all
frozen wire resources.

The complete run then found the exact cursor authority-field inventory omitted
internal session_origin. When adding an authority dimension, update both the exact
key set and a one-field mutation variant proving the cursor digest changes. Keep
the exact-key assertion intact.

The 2026-10-16 consumer-intake CLI repeated the module-inventory failure:
8,211 Python cases passed, but the new foreground adapter was missing from the
exact set. Register intended app modules in the same patch and run this security
file alongside focused adapter tests. The equality assertion remains mandatory.

The same candidate passed all source checks but failed the macOS native harness
archive audit: its shared module closure decoded 500,261 objects against a
500,000-object aggregate cap. The measured local scan took 6.67 seconds without
content findings. The cap was raised to 550,000; the 30-second worker CPU limit and all
content rules remain intact. Exercise the actual native archive after foreground
CLI additions, and test that the object budget remains aggregate across payloads.
Category: Build Pipeline.

Discovered: 2026-10-14, W2 full verification; failed logs and fixtures retained.

### BUILD-013: Settings observation callbacks need explicit types

Category: Type Safety.

The W2 observation prototype used an untyped local lambda for a reusable snapshot
check. Mypy rejected it under the repository's strict settings. Use a typed local
function for the callback and check the observer together with scoped setup and
agent setup, then run the complete build/typecheck gate. Shared callback contracts
also affect CLI/plugin consumers; a single-file check cannot establish closure.

The full gate subsequently caught dynamic fixture dictionary and nullable-ID
annotations in new tests that production-only mypy had not checked. Include new
test modules in focused typechecks; pytest success does not establish typing.
Retain the failed full-gate log, fix the annotations and repeat the full gate.

Discovered: 2026-10-14, W2 settings implementation and full candidate verification.

### STORAGE-001: Review reads can borrow the active writer transaction

Category: Database/Migrations.

Bound proposal creation reads its newly reserved proposal before its writer
transaction exits. The store can lend that transaction to the read path;
unconditional `BEGIN` raises a nested-transaction error. Start a read snapshot
only when `connection.in_transaction` is false. Preserve the existing owner's
commit/rollback boundary, and run propose, show and terminal replay regressions
alongside the full verification gate.

Discovered: 2026-10-14, W2 final auxiliary authorization regression.

### MCP-001: Global graph caches and counters bypass record filtering

Category: Privacy / Authority.

A filtered graph snapshot does not make a whole-workspace structural cache safe.
The cache can retain denied endpoint IDs and diagnostics, while its global digest
and failure state reflect hidden notes. A hidden duplicate basename can also
change extraction of otherwise visible edges. Implicit MCP must refuse this cache
before reading it unless extraction has independently scoped provenance. Continue
using engine-filtered inferred suggestions. Suppress global workspace policy
counters and bind facet continuations to visible state for implicit sessions.

Discovered: 2026-10-14, W2 independent source review and synthetic graph/cursor tests.

### CONFIG-001: Claude local MCP entries and OS aliases need finite handling

Category: Config/Environment.

Claude user JSON can store selected-project MCP entries inside
`projects[absolute_project_path].mcpServers`, as well as top-level user entries.
Check that exact selected entry when detecting unscoped coexistence. A no-follow
reader must also account explicitly for macOS's system `/etc` alias using its
known `/private/etc` location; do not resolve arbitrary user-controlled links.
Reject writable configuration parents and commit their identities.

Discovered: 2026-10-14, W2 synthetic settings and independent review.

### SHARING-006: New copy policy must not redefine historical Work authority

Category: Privacy / Compatibility.

A blanket ban on externally reading ordinary Work records broke a supported
Portable-6 recovery fixture whose pre-existing policy explicitly permits egress.
Keep tier classification separate from the record's existing egress authority.
Treat the new `sharing-slack-work-copy.v1` policy as independent managed-copy
custody: its marker, delivery, provenance and linkage must satisfy current approval
checks even if another identity hint is damaged. Ordinary historical policy
semantics stay intact; raw Slack originals retain their local-only decisions.
Test both old-policy recovery and forged new-policy refusal.

Discovered: 2026-10-14, W1a/W1b combined compatibility regressions.

### SLACK-004: Payload commitments use canonical UTF-8

Category: Config/Environment / Source Proof.

Slack observation admission hashed `bounded_json(payload)`, which escapes Unicode,
while the engine verifies `portable_canonical_json_bytes(payload)` as UTF-8. ASCII
fixtures passed, but a real synthetic Unicode edit failed source observation
validation before sharing. Keep the bounded admission check, then hash the exact
canonical payload bytes required by the engine. Exercise Unicode through collector
intake, revision replacement and owner reapproval rather than testing only the
sharing envelope.

Discovered: 2026-10-14, W1a synthetic Slack cited recall acceptance.

### BUILD-012: Dynamic DTO construction and fixture spreads need full typechecks

Category: Type Safety.

Dataclass field reflection mixed integer defaults with arbitrary field values,
so mypy rejected the validated maps passed into versioned DTO constructors.
Type the validated keyword map explicitly, narrow variant-only fields before
access and update every protocol implementation when adding a task method.
In TypeScript, spreading `Record<string, unknown>` into a fixture does not give
arbitrary keys a named property type. Use the validator's returned record or an
explicit fixture type when asserting those keys. Vitest transpilation passes
without finding these TypeScript errors; run both package typechecks as well as
the tests, followed by the full build/typecheck gate.

Discovered: 2026-10-14, W1a implementation and disjoint wire compatibility checks.

### SLACK-003: Work classification does not authorize provider recall

Category: Privacy / Authority.

Collector Work privacy remains local-only with external egress disabled. Work
space routing is separate from privacy and provider consent. Existing sharing
requires saved-Markdown managed evidence and produces a public-tier v1 copy; raw
Slack revisions do not satisfy that proof. A Slack recall extension must preserve
the old format and explicitly bind its new source evidence, Work copy and approved
citation metadata. Never substitute an owner-local session for an external-provider
session to bypass these checks.

Discovered: 2026-10-14, Slack cited-decision recall implementation planning.

### DOCS-002: Distinguish capture routing from logical-source routing

Category: Documentation / Source Continuity.

The spaces/inbox guide warns that routing a capture prevents automated updates.
Current owner `source route` instead binds the logical source using its expected
head and route version; accepted managed successors inherit that source's space.
Use the actual logical-source path for continuing Slack intake. Do not add a
`space_id` provider option or grant collector-wide organize authority to work
around the older capture-routing description.

Discovered: 2026-10-14, source routing and revision acceptance inspection.

### BUILD-017: Native packaging must pin the reviewed Python patch

Category: Build Pipeline; Dependencies.

PR83 macOS attempts1 and2 selected Homebrew Python3.14.8 from the floating
3.14 CI input. Both passed application checks and failed the Graphify release
archive audit. Local native audit/Homebrew checks passed with Python3.14.7.
The archive policy admits exact reviewed standard-library marshal payloads;
Python patch selection can change those payloads. Pin both packaging jobs to
3.14.7, the reviewed and verified patch, and verify the complete CI pipeline.
Do not add new payload exceptions or weaken the audit to admit a newer patch.
The original error does not identify the failed rule, so version drift remains
the working diagnosis until the pinned CI run completes.

### TESTING-008: An LRU cache smaller than a repeated scan never hits

Category: Performance / Verification.

Symptom: The Python suite is CPU-bound and per-test profiles show most time in
`local_schema.classify_local_schema`, although `_normalized_sql` is already cached.

Cause: Every database open normalizes each `sqlite_master` row in the same order. With
`lru_cache(maxsize=128)` and about 180 statements per scan, each entry is evicted before
its next use, so the cache missed 1.76 million of 3.29 million calls in one 101-test slice.

Fix: Size a cache for the whole repeated working set. `_normalized_sql` now holds 1024
entries; supported schemas need 197. Regression tests require zero misses on a repeated
classification and enough capacity for every supported era. The complete two-worker suite
dropped from 2,330.7 s to 1,474.9 s. Check `cache_info()` hit rates when a cached hot path
still dominates a profile, and grow the bound when the schema grows.

Discovered: 2026-10-15, python-test-performance profiling.

### TESTING-009: Benchmarks need frozen inputs and an exclusive timing directory

Category: Verification.

Symptom: A profiled full run reported `hashes_unchanged: false`, and a reused timing
directory could have doubled per-test times without any error.

Cause: A new tool file was written into the checkout while the baseline run was in
progress, and the first timing reporter appended to any existing directory.

Fix: Do not write to a checkout during a measurement; edit in a separate worktree or
between runs, and record input hashes before and after each run. The reporter now refuses a
directory that already holds timing records and stamps every record with one run
identifier. Run benchmarks one at a time with no other test runs, packaging, or agent tests
on the machine; record load, swap and free disk, and repeat a run that overlapped a
scheduled backup or another heavy job.

Discovered: 2026-10-15, python-test-performance profiling and independent review.

### BUILD-015: Check `.PHONY` tokens after editing the Makefile

Category: Build Pipeline.

Symptom: After adding tier targets, `.PHONY` contained `linttypecheck`. `make verify` still
passed, but a file named `lint` or `typecheck` would have silently skipped that gate.

Cause: An automated edit dropped the space between two phony names.

Fix: Restored the separate names. `tests/unit/test_pytest_tiers.py` now checks that each
verification and tier target is its own `.PHONY` token. Diff the phony token set against
the previous Makefile after any edit to that line.

Discovered: 2026-10-15, independent Astra review of the tier targets.

### DOCS-001: First-use version pins can lag the current candidate

Category: Documentation / Verification.

The first-use and desktop guides still name schema/session `10/5`, while the
current feature matrix and native runtime require `16/11`. Source controls also
exist beyond older desktop README descriptions. Read the exact runtime constants,
current catalog and feature matrix before turning a guide into an acceptance
claim. Keep implementation, installed assets, provider readiness and public
acceptance separate; an older guide is not evidence that a feature is absent or
that a current candidate is released.

Discovered: 2026-10-14, source-backed work-laptop product evaluation.

### SLACK-001: Freeze evidence closure separately from semantic identity

Category: Database/Migrations, Verification.

A source-bound development can support another development through ordinary
supporting capture IDs. Traversing only explicit development relationships missed
that ancestor. Portable validation also accepted removing the evidence sidecar
operations after rehashing the manifest, preserving the original approval digest.
Commit the complete bounded, cycle-free receipt closure, including absent leaves,
into owner review binding 6. Validate those commitments during approval and full
Portable restore. Keep frozen historical validators closed to the new binding.

Semantic deduplication cannot retain an old evidence operation when the raw source
changes. Keep stable semantic identity and immutable evidence-specific operations,
using supported source revisions for current developments. Preflight currentness,
retain capture acknowledgement before binding, and isolate typed item failures so
independent observations progress. Capture and bind remain separate transactions;
uncertain acceptance requires visible owner recovery rather than a false success.

Discovered: 2026-10-14, independent synthetic Slack audit and regression repairs.

### SLACK-002: Scheduled completion does not prove caught-up coverage

Category: Config/Environment, Verification.

A fresh rolling window skipped source history after a week offline. Freeze each
paginated discovery interval and overlap the last completed coverage watermark.
Advance it only after completion. A daily single pass also left ordinary pages and
retry deadlines until the next day. Use bounded catch-up under the same lease and
return deferred status when bounds or owner attention retain work. Preserve public
coverage codes separately from execution outcome; completed partial search is
still partial coverage. Exercise the actual live scheduler with a virtual clock.

Draining pages must not reset the processing budget on each pass. Even a page
smaller than the budget can repeat extraction under a fresh budget on the next
pass. Drain ingestion separately, then process once per activation. Record one
activation health summary covering all observed sources and cumulative known
usage; the final pagination pass alone loses earlier source limitations.

Discovered: 2026-10-14, independent synthetic discovery and scheduling audit.

### BUILD-011: Shared Portable validators need explicit binding capabilities

Category: Type Safety, Build Pipeline.

Adding review binding 6 affected shared Portable validator helpers. Annotate their
version capability collections and preserve historical default refusal; do not
widen every old format. Python 3.14 connections may already be in a transaction,
so read-only evidence preflight must not issue an unconditional `BEGIN`. Nullable
revision receipts and nested test JSON also need explicit narrowing for strict
mypy. Run full lint/typecheck after targeted regressions and include the new
Portable commitment module in the native required-module inventory.

Discovered: 2026-10-14, Slack repair typecheck and native integration preparation.

### BUILD-009: Desktop preparation must select its pair from the complete release manifest

Category: Build Pipeline.

The optional harness added a valid third native resource. Desktop preparation
collected every target resource and required exactly two, so both platform builds
failed before packaging. Validate the complete native manifest contract, including
optional resources and other platforms, then copy only base and Graphify into the
desktop runtime. Reject malformed rows, duplicate roles, inconsistent resource
sets and wrong executable digests. Generate fixtures with the native manifest
writer so desktop tests exercise its actual contract.

An earlier contributor-check failure skips the later NW0 probe. Its always-run
artifact upload then reports missing proof output; that does not make the probe's
output path stale. Preserve artifact identities and fix the first failed step.

Discovered: 2026-10-12, actual macOS and Linux contributor CI packaging failure.

### MEMORY-004: Owner authority ignores read tiers, and revision keys cannot repeat

Category: Database/Migrations, Config/Environment.

Policy B gives a verified local-only Pi an owner authority with every tier except
`secret`. The engine's `permits_read_tier` returns true for any owner authority,
so `allowed_read_tiers` did not withhold a `secret` record. Never write a harness
capture with tier `secret`: refuse it before the spool, and again in the
workflow. A regression test now captures into a `secret` source and checks that a
verified Pi cannot read it.

A source revision key must be unique within its namespace. Keying a revision only
by tier and content made a return to an earlier tier (work, then unknown, then
work again) reuse the first key, and the engine refused it as
`invalid_arguments`. Reuse the head's key when tier and content are unchanged;
otherwise add the predecessor capture ID to the key and delivery ID.

Discovered: 2026-10-22, Policy B harness rewrite.

### MEMORY-003: Revalidate durable restrictions before custody and sources after cleanup

Category: Database/Migrations, Config/Environment.

Superseded 2026-10-22: Policy B removed session/origin restrictions, the custody
outbox and the post-cleanup source scan. Kept for history.

An unmarked Hermes event can inherit a session or origin restriction. Screening
only the current payload allowed the rejected body into custody before the SDK
refused it. Resolve durable restrictions before any request, replay or origin
publication. Embedding callers with their own custody must apply the read-only
capture guard before retaining bodies, and protect new metadata-only latches
through their normal operation path.

A retained origin reference proves its session already existed. Require its
validated state even when it names the current session; a missing restriction
file must fail closed instead of initializing an unrestricted session.

Policy and consent equality do not prove a cached source is still readable.
Canonical routing, lifecycle, privacy, head, historical authorization or retained
bytes can change during runtime cleanup. Hold canonical exclusion through release
and recheck the complete scoped source state after cleanup without another SDK
startup. Check external policy, consent and restriction stores after the final
source scan. Regressions must mutate actual canonical state at these boundaries.

Discovered: 2026-10-12, independent harness and embedding-boundary review.

### STORAGE-002: Reading a held POSIX lock file can release the lock

Category: Database/Migrations, Config/Environment.

POSIX record locks belong to a process. Closing any descriptor for the locked
inode releases that process's lock, even when its original descriptor stays
open. A complete same-process file inventory can therefore defeat a held writer.
Identity and lifetime checks cannot prove continuous kernel ownership afterward.

Keep borrowed SDK acquisitions from reopening the writer inode. Run complete
external inventories in a separate foreground process and retain the parent
lease. Verify contention from another process throughout the real inventory;
mocked backup callbacks do not exercise this failure. Do not reacquire the lock
to hide a gap or omit control files from a complete inventory.

Discovered: 2026-10-12, existing local transaction integration review.

### BUILD-008: Async refusal tests still need valid static return handling

Category: Type Safety.

A negative test passed a coroutine-returning callback to a synchronous API and
discarded its annotated return. Mypy rejected the unused coroutine even though
the runtime correctly refused and closed it. Assign the return inside the
expected-error context while preserving the runtime refusal assertion. Run the
full typecheck and build; do not weaken the production API's return type.

Discovered: 2026-10-12, existing local transaction verification.

### BUILD-007: Acceptance evidence helpers need explicit types and source bindings

Category: Type Safety, Build Pipeline.

The harness acceptance tooling initially failed targeted mypy on unannotated
evidence collections and helper exports. Give JSON evidence, byte digests and
filesystem references explicit contracts, then check the complete build and
typecheck. A passing fixture matrix does not prove native inference. Bind every
selected test parametrization to its retained JUnit result and keep unavailable
native collection a failing outcome.

Independent review also found that hashing selected tests without their active
`conftest.py` missed behavior-changing fixture edits. Bind the complete Git source
inventory, including uncommitted files, test configuration, imported fixtures
and lockfiles; verify an in-memory conftest change invalidates that digest.

Follow-up, 2026-10-12: A Hermes assertion in `test_harness_bridge.py` indexed a
nested JSON `counts` value typed as `object`. Cast the bounded mapping before
indexing it. The repaired full typecheck passed all 571 source files.

Follow-up, 2026-10-12: Starting the full gate while final tests or documentation
are still being edited produces `source_changed_during_run`. Freeze the entire
checkout before the source-bound acceptance fixture, including gotcha updates.
Retain evidence outside the checkout. A passing focused test does not exempt its
file from the acceptance source binding.

Discovered: 2026-10-09, automatic-memory acceptance tooling verification.

### BUILD-006: Public command changes must update every exact inventory

Category: Build Pipeline.

Adding the foreground harness entry point and source/setup/MCP commands passed
focused integration checks but failed four full-suite assertions: two exact
package script maps and two catalog command counts. Update those inventories
with the explicitly supported additions while retaining exact equality,
dependency restrictions and hidden-owner-command checks. Check the app and
collector package boundaries together, then run the full verification gate.

Follow-up, 2026-10-12: Hermes `harness capture` increased the exact catalog count
from 72 to 73. Include that command explicitly and update both count assertions,
preserving the hidden owner-command exclusions and exact inventory checks.

Discovered: 2026-10-09, native harness integration verification.

### MEMORY-002: Confinement status and enforcement are different host operations

Category: Type Safety, Config/Environment.

The native Pi enforcement assertion throws when a fresh session is unconfined.
Using that assertion as a boolean probe prevented ordinary capture, recall and
search from reaching the bridge. Supply a session-bound `isLocalOnly()` probe
for durable restriction state; keep `assertLocalOnly()` strict before every
private read and release. A probe never grants access. Poisoned state and a
replaced or disposed session must still throw. Test the real native capability
with the public extension, as separate passing suites missed this mismatch.

Discovered: 2026-10-09, independent native integration review.

### BUILD-005: Native hook setup must finish cold runtime validation first

Category: Config/Environment, Build Pipeline, Type Safety.

The optional native harness helper took 5.539 seconds on its first launch from
a freshly extracted prefix. Later launches took about 0.3 seconds; synthetic
enqueue and recall took 0.250 and 0.514 seconds. First-launch validation can
therefore exceed the two-second capture and three-second retrieval deadlines.
Apply setup only after the exact selected runtime completes side-effect-free
`harness --help`. Refuse failed warm-up and recheck all preview preimages before
activating hooks. Keep lifecycle deadlines unchanged.

The shared setup exception exposes `code`, not `reason`. A CLI integration that
read the wrong field failed typecheck and hid the intended refusal status.
Trace the service's exception and environment contracts through its actual CLI
caller; verify setup in a disposable HOME, then run the full build/typecheck.

Discovered: 2026-10-09, native automatic-memory setup verification.

### BUILD-004: Retained test fixtures can cross the storage admission watermark

Category: Config/Environment, Build Pipeline.

Symptom: A later full test run fails collector and connector capture cases with
`storage_high`, although the same cases passed in the preceding run.

Cause: Those test trees use the real storage probe. Each full run retained about
four GiB of disposable fixtures; repeated runs reduced available storage below
the engine's two-GiB watermark. App and engine fixtures use hermetic probes, so
their passing results alone do not reveal this host condition.

Fix: Check available bytes before a full run. Remove only completed synthetic
fixtures created by the current task, retaining their logs. Rerun the failed
cases after storage recovers, then the full gate. Keep production admission
guards enabled. Leave space for package and native build artifacts as well.

Follow-up, 2026-10-12: Pytest's `tmp_path_retention_policy=none` retains passing
fixtures during the run and cleans them at session end. Use `failed` with
`tmp_path_retention_count=0` for immediate passing-fixture cleanup, plus failfast
and a bounded worker count. Create a short task-specific `TMPDIR` outside the
checkout: acceptance evidence intentionally refuses paths under the source root.
Retain sanitized logs outside build output so subsequent gates cannot unlink them.
Host swap growth can still exhaust disk with tiny fixtures. An eight-worker run
hit `storage_high` and a helper timeout; both cases passed serially after workers
exited, and the full two-worker gate passed with Cargo limited to two jobs.
Keep the real admission limits and helper deadlines intact.

Follow-up, 2026-10-14: A zero retention count also removes failed session
directories at exit. That erased the nested harness JUnit behind a shared
`scenario_run_failed` setup error; all 38 nested cases passed on direct replay,
so the original cause remains unverified. Use retention count one with policy
`failed` when diagnosing a full run. Passing function fixtures still clean up
immediately, while failed evidence survives. Keep each run's log outside its
temporary directory and inspect the entire progress log before reporting success.
The receipt-protection success fixture also configured only one second for a
fresh interpreter and real engine imports. Clean imports measured 0.526–1.416
seconds, with three of eight samples over that limit. Its success case now uses
an explicit five-second test configuration; production deadlines and the separate
50 ms timeout-refusal case remain unchanged.

Follow-up, 2026-10-14: W0 Slack recall verification reproduced 142 connector/collector
failures after a full run retained 4.7 GiB. Clearing that task's completed fixtures
was insufficient: a two-worker retry still reached `storage_high` with an empty
task TMPDIR. An approved cleanup of ignored build artifacts recovered 5.38 GiB.
All 190 recovery cases then passed, followed by the full gate with four bounded
workers and immediate passing-fixture cleanup. Check broader host headroom as
well as fixture size; do not change production admission limits or helper deadlines.

Follow-up, 2026-10-16: A four-worker consumer-intake run started with6GiB free
but exhausted disk as host swap grew; retained fixtures occupied only72MiB.
Clearing this worktree's disposable Cargo target and using one pytest worker
allowed all8185 Python cases to pass. An external supervisor monitored the
existing2GiB high watermark without altering admission limits; its stop guard did
not trigger. The successful
run's minimum free space was2958929920 bytes. Keep rollback clones and unrelated
projects intact, and rebuild disposable artifacts with bounded Cargo concurrency.

Discovered: 2026-10-09, repeated automatic harness bridge verification.

### MEMORY-001: Harness boundaries can lose restrictions and cancellation

Category: Config/Environment, Type Safety.

Partly superseded 2026-10-22: Policy B lowers a local-only capture to the
`unknown` tier at write time instead of keeping custody, and the retry spool
moves each failed line behind the others. The consent and deadline repairs still
apply.

Independent review of the foreground memory bridge reproduced four failures:

- A Pi host's local-only state disappeared during message normalization. Keep
  the restrictive boolean in immutable custody and source revisions. It may
  narrow capture, but it never grants permission to read private memory.
- Consent revoked during runtime cleanup missed the last read gate. Recheck
  the retained authorization after cleanup and pending-source inspection,
  immediately before releasing results.
- A one-shot alarm raised `TimeoutError`, which ordinary error handlers caught.
  Use distinct deadline cancellation that escapes those handlers; cleanup must
  propagate it. Test the nested drain path, not only a bare sleep.
- Hash-sorted retries repeatedly selected the same sixteen failures. Try new
  requests first and oldest attempts next while retaining failed requests.

Verification: Disposable engine tests and independent repros cover each repair,
including consent revocation during the final guard, private replay, cancellation
inside delivery and a seventeenth healthy request behind sixteen refusals.

Discovered: 2026-10-09, independent automatic harness bridge review.

### BUILD-003: Mixed literal branches need an explicit collection type

Category: Type Safety.

Symptom: Harness selection and subprocess fixture tests pass, but the full type
check rejects a candidate kind as `str` and untyped JSON results as `Any`.

Cause: A conditional list of literal tuples loses its narrow type without an
annotation. `json.loads` also returns `Any`; a passing fixture does not establish
the type of its return value or nested fields.

Fix: Annotate the collection before assigning either branch. Give fixture JSON
an explicit result type and check nested object shapes before indexing. Run the
whole repository type check and build when changing the shared selector or CLI.

Discovered: 2026-10-09, automatic harness bridge verification.

### BUILD-002: Exception aliases still require a parenthesized type tuple

Category: Type Safety.

Symptom: Ruff cannot parse a Python 3.14 exception handler that catches several
types and binds the exception with `as error`.

Cause: Python 3.14 permits an unparenthesized exception list only when there is
no `as` binding. `except FirstError, SecondError as error` remains invalid.

Fix: Use `except (FirstError, SecondError) as error`. Run the full type check and
build after changing a shared entry point, then exercise its actual CLI caller.
The foreground harness adapter emits closed error metadata; parser exceptions
must never print source data or configuration contents.

Discovered: 2026-10-09, automatic harness bridge implementation.

### RECOVERY-006: Historical checkpoints retain the original physical binding

Category: Database/Migrations.

Symptom: A historical Saved baseline refuses after a device number changes, even
though the same approved content and logical Brain remain at the same path/inode.

Cause: The serialized historical envelope includes its original root fingerprint.
Source scan rebinding does not change that immutable destination coordinate.
Constructing an ordinary collector revision sink also invokes engine startup
recovery before baseline lookup, so a refused stage does not prove mutation absence.

Fix: Keep the exact retained page, original history and genuine protection
evidence. Use the separate owner-authorized historical checkpoint host with a
trusted continuity validator and the currently admitted physical root. It opens
state read-only, preserves original envelope coordinates, revalidates content,
privacy and source CAS, and uses the existing writer fence for checkpointing.
Do not substitute an old fingerprint into an ordinary capture sink, rewrite
historical bindings, reset the cursor, or infer a completed AFTER from a refusal.

Verification: Device-only continuity fixtures reconstruct exact V1/V2 history,
preserve canonical SQL and immutable bytes, and refuse identity, content, privacy,
CAS and admission changes. An existing writer prevents checkpoint persistence.

Discovered: 2026-10-06, historical checkpoint recovery.

### BUILD-001: Native packaging needs a writable PyInstaller cache

Category: Config/Environment.

Symptom: Python, desktop and Rust checks pass, then native packaging fails with
`PermissionError` while creating PyInstaller's macOS cache under `~/Library`.

Cause: A restricted build environment permits the checkout and temporary storage
but cannot write the default user cache. `TMPDIR` alone does not redirect it.

Fix: Set `PYINSTALLER_CONFIG_DIR` to a dedicated writable cache directory before
running the complete native build, audit and Homebrew smoke gates. Retain failed
logs and artifacts; keep the source and installed-runtime pins unchanged.

Verification: PyInstaller's supported configuration reads this environment
variable before choosing its platform cache directory. Re-run the full build
and type check, then native audit and Homebrew smoke.

If the compiled bootloader then fails with `semctl: Operation not permitted`,
the execution environment denies its required semaphore. Verify the exact commit
on an authorized test host with the matching architecture and Python version;
the denied local smoke run remains failed evidence.

Discovered: 2026-10-05, restricted macOS native packaging.

### RECOVERY-003: Clean restore must bound immutable history decoding

Category: Database/Migrations.

Symptom: Clean restoration slows sharply as the historical authority chain grows.

Cause: Reopening and decoding the entire prefix before and after every historical
append repeats expensive validation of all earlier registry memberships.

Fix: Only in the hidden Portable9 restore stage, derive the complete typed SQL
projection once and check every exact SQL prefix and newly persisted intent.
Keep durable pending/completion fences and registry checks. Reopen and audit the
complete immutable history before promotion; interrupted stages never promote.

Tests: Immutable-history read growth, SQL/intent tampering, interruption/retry
fidelity and independent mixed V1/V2 chains with both revocation versions.

Discovered: 2026-10-05, production-shaped Portable9 recovery rehearsal.

### RECOVERY-004: Ordinary writes still audit history with startup recovery disabled

Category: Database/Migrations.

Symptom: A multi-operation canary exceeds its bound after historical adoption.
Disabling abandoned-session recovery does not disable ordinary writer authority
checks. Each write boundary rereads and strictly decodes the retained history.

Fix: Reuse only successful decoding keyed by exact freshly confined bytes, with
byte and entry limits and transitively immutable results. Continue checking
current files, pending state, registry, identity and every SQL projection on each
boundary. Never cache settled authorization, absence, paths or modification times.

Provider candidate filtering also repeatedly derives identical expected SQL rows.
Reuse one pure derivation only for exact registry bytes and identical immutable
decoded input objects, bounded by complete canonical input bytes and record count.
Keep strong input references and return fresh mutable containers. Every current
confined-file read and every current SQL-row comparison still runs on every call.

Tests: Same-size/mtime byte changes, removal and path replacement after a warm
read, mutable-result isolation, bounded eviction, nested evidence immutability
and SQL projection tampering. Measure the complete retained chain separately;
small fixtures cannot establish a production-shaped canary deadline.

Category: Type Safety. Test spies must import storage functions from their owning
module; an internal import in a consumer is not a declared mypy re-export.

Discovered: 2026-10-05, bounded isolated historical authority startup profile.

### RECOVERY-005: Historical filtering needs a guarded query lifetime

Category: Database/Migrations.

Symptom: Startup is fast after immutable decoding is reused, but a provider
query still rereads the complete retained history for each candidate.

Fix: Own validated historical inputs inside one read-only SQL transaction and
held reader lease. Read current registry and pending state for every candidate;
freshly audit the entire versioned history and SQL projection before output.
Recheck every distinct historical grant, including dependencies beyond the
returned page, with current retained bytes and exact authority parameters.
Keep initial denial sticky and latch late invalidity even when callbacks hide it.

SQLite pragma names are case insensitive. Guard transaction control and all
`query_only` setters, retain unchanged `total_changes`, and reject nested scopes
on the same connection before they replace its authorizer. ContextVar values
alone do not prove ownership: reject copied contexts, other threads and expired
scopes. Restore the context and authorizer before owned transaction cleanup.

Tests: Entry/terminal audit counts, same-metadata changes, initial and late
registry/pending failures, namespace collisions, retained-body damage,
transaction restart, context isolation, nesting and fresh-query projection loss.
Unexposed cursor allocations remain retained if the terminal barrier fails.
Caller consent revalidation remains at the boundary that owns that policy.

Discovered: 2026-10-05, bounded isolated provider-query profile and source review.

### SQLITE-002: Save the original directory inside the SQLite open lock

Category: Config/Environment.

Symptom: Later service tests fail in `Path.cwd()` after a concurrent SQLite test
leaves the worker in a temporary Brain directory that fixture cleanup removes.

Cause: Both SQLite openers saved the process directory before acquiring their
shared lock. A second opener could save the first opener's temporary directory,
wait for the lock, then restore that directory instead of the original one.

Fix: Hold the same lock while saving the original directory, opening SQLite,
restoring the original directory and closing its saved descriptor. Keep the
existing confined parent descriptor, no-follow checks and read-only URI intact.
This fixes restoration between SQLite openers; unrelated code must still avoid
changing the process directory concurrently with storage operations.

Tests: Deterministically block both actual SQLite openers behind a competing
holder and require that neither snapshots the directory before acquiring the
lock. Both cases fail before the fix and pass afterward; the service tests also
pass with successful pytest fixture cleanup enabled.

Discovered: 2026-10-05, full gate with concurrent SQLite clients.

### SQLITE-001: Reinsert replacement parents after dropping the old table

Symptom: A same-transaction historical table rebuild passes a final
`foreign_key_check` but fails COMMIT with a deferred foreign-key violation.

Cause: Copying parent rows into a differently named replacement before dropping
the referenced old table leaves SQLite's deferred violation counter unresolved.
Renaming the already-populated table does not reinsert the referenced keys.

Fix: Keep foreign keys enabled. Retain exact rows in a transaction-local backup
table, replace the old parent with the empty new definition, then insert those
rows into the original parent name. Check foreign keys and the complete retained
history before commit. Roll back the whole rebuild on interruption.

Tests: Populated V1 relation-chain preservation, migration interruption/retry,
old-runtime refusal and genuine escaped-max Portable5→V2→Portable9 restoration.

Discovered: 2026-10-05, schema14 versioned historical bounds.

### TYPE-001: A versioned runtime projection must not widen a frozen exporter

Symptom: Adding V2 history to the shared projection verifier makes mypy reject
Portable8's exporter: its closed V1 serializer receives a V1/V2 transition union.
A runtime-only cast would hide the same transitive compatibility boundary.

Cause: A shared consumer name represented both current runtime history and the
frozen Portable8 format, which only admits V1 transitions.

Fix: Keep `verify_historical_projection` closed over V1 and use the explicit
`verify_versioned_historical_projection` for current runtime consumers. The V1
wrapper verifies the full chain and refuses every V2 transition. Preserve the
Portable8 serializer and validator bytes; put mixed history in Portable9.

Tests: Static checking of engine/app source plus frozen V1 history regressions
and the mixed V1/V2 Portable9 export/import fixture.

Discovered: 2026-10-04, schema14 historical compatibility implementation.

### TYPE-002: Reducer branches share mypy's local-variable scope

Category: Type Safety. Action: use separate names for optional lookups and validated
domain objects, then run the repository typecheck after reducer changes.

The lifecycle journal's first typecheck failed because `manifest` and `segment`
were reused across event branches as both validated objects and optional lookups;
`revision` was also reused for a manifest and a hash string. Conditional branches
do not give those names independent type scopes. Distinct custody/activation
names retain strict checks without casts that conceal missing-state handling.
Focused checking and the full repository typecheck now cover these consumers.

### TESTING-003: macOS killpg reports EPERM for a zombie-only process group

Symptom: A required NW0 proof job on macOS fails in about 20 seconds with
`phase=tests`, `RuntimeError` and an empty runner log, then passes on re-run.
The probe code is unchanged and a successful run also leaves an empty log.

Cause: The shared command runner kills the child's process group after the
child exits. On macOS, `killpg` returns EPERM, not ESRCH, when every remaining
member is a zombie: the leader before reaping, or an orphaned descendant that
launchd has not reaped yet. The runner treated that EPERM as unverified cleanup
and failed the passing proof. Measured: 5 of 5 unreaped single-member groups
raised EPERM, and ESRCH after reaping.

Fix: On EPERM, reap the leader, then retry the group kill for a bounded two
seconds. The group disappearing or accepting the kill is verified cleanup; a
persistent EPERM still fails. Do not re-run the job as the fix.

Tests: `test_zombie_only_group_permission_denial_settles_after_reap` and the
existing persistent-denial test in `tests/test_nw0_graphify_probe.py`.

Discovered: 2026-10-04, PR62 CI run 37185252077.

### RECOVERY-001: Every replay entrypoint needs the same completed-record checks

Symptom: Mixed owner replay refuses a damaged completed source file, but the
capture-only replay API accepts the same damaged closure and returns a duplicate
receipt. Separately, a Portable8 capture metadata sidecar with a changed intent
or capture reason validates and later replaces the restored capture row.

Cause: The completed source and blob check lived in one replay module, and the
capture-only path stopped after comparing database fields. The Portable8
cross-record comparison listed fields one by one and omitted two that the
retained capture record also carries.

Fix: Keep completed-file validation in the shared capture replay module and
call it from every entrypoint before any materialization. Bind every immutable
field the retained capture record carries; routed fields such as `space_id`
change legitimately and need their own receipts, not a record comparison. Add
a negative case per entrypoint and per field.

Tests: `test_repeated_capture_replay_refuses_damaged_completed_files_before_any_write`
and `test_v8_rejects_owner_intent_or_reason_disagreeing_with_archived_capture`.

Discovered: 2026-10-04, independent final-candidate review of the 13/8/8
recovery extension.

### RECOVERY-002: Protect every journal transition, not only the terminal one

Symptom: Under a recovery guard, a queue item with any later event (a failed
attempt, a quarantine, an owner retry) raised a pending-protection error on
every drain and blocked every item behind it. A guard-protected closure holding
an allocation without its terminal journal, or a journal without an allocation,
could not be replayed by any entrypoint. Items queued before the guard existed
could never compact because the port held no original cue for them.

Cause: `protect_pending` only knew two shapes, initial custody or a reservation,
and the only journal protector required a terminal history and the cue already
present in the port. Failure, retry and discard commits never protected at all,
and the replay classifier refused everything except initial queues and completed
captures.

Fix: One `protect_journal(compact)` rebuilds the original cue from the item's
first queued event, protects the reservation when present, then protects the
exact retained history. Every cue or allocation is compared with the
authenticated closure before it is appended, so a changed local row refuses
with the protected head unchanged instead of poisoning the append-only chain.
Allocation protection ensures the cue first on every reservation path,
including startup resume of a reservation that predates the guard. Failure and
retry commits protect their non-terminal history immediately. A guarded discard commits the event and
tombstone, protects the compacted journal, and only then deletes custody; an
interrupted discard resumes at the protection step. Replay restores pending
allocations (resume the bound stage machine, keep the queue item; the next drain
writes the terminal event), exact non-terminal histories and tombstones.
Quarantined or discarded originals skip destination re-admission on replay,
since their destination may no longer exist, but keep the privacy check. A
discarded journal that carries or is followed by an allocation is refused, and
`discard` itself refuses an allocated item (`allocated`): recovery resumes every
reservation, so a discarded allocation would still publish. With one permitted
attempt that state is reachable inside a single drain. A quarantined allocation
is deliberately completed on replay: the primary applies the same
resume-before-ingress policy at its next drain, and the owner's later retry
terminalizes the item as a duplicate. When only the allocation binding changed
since the last protected journal, no new journal is emitted; an equal-length
twin would fork the history. Every journal protection is checked as a
progression of the latest protected journal before the port sees it: an
identical history re-proves, a strict prefix is stale and skipped, a longer
history must extend the protected one, equal length admits only a binding or
compaction transition, and a compacted journal is final; anything else refuses
with the protected head unchanged. A terminal snapshot is always protected as
compacted so a resubmission racing a drain cannot leave an uncompacted twin. Owner retry and discard
take the writer fence like drain, so a racing edit is refused as `writer_busy`
instead of reordering protected journals.
Existing pending-custody resubmissions also take that fence before reading,
comparing and protecting their history. A lookup followed by append is not
atomic: without the fence, owner retry can protect a longer history between
those calls and the resubmission can append its stale prefix. Busy guarded
resubmissions retain custody and return pending protection; already locked
source and Markdown callers reuse their surrounding fence.
Build Pipeline prevention: when adding an enqueue keyword, update monkeypatched
callers to accept it and assert the expected lease ownership. The Markdown
reservation-stop fixture otherwise fails before reaching its recovery fault.
Source rows are validated for every
replayed cue before any branch: an intake row never belongs to an owner
delivery, and an alias must carry the request digest and name the logical
source that holds a revision of exactly the matched capture. An interrupted replay (capture committed, queue
item not yet restored) resumes on the next attempt. Terminal events and the
tombstone are written before custody is deleted, because `BEFORE DELETE`
guards refuse to drop items or payloads without them. Duplicate validation
accepts several protected journals per delivery in authenticated record order,
each bound to the one original cue and allocation, each strictly extending the
previous, and only the latest the compacted terminal. Replay exempts a delivery
from capture-row collision checks only when an explicit capture plan matched
that row exactly; a row alone proves nothing.
Deleting a queue item cascades its local events: the protected journal is the
retained history, so tests must not expect local events after compaction.

Tests: `test_pending_allocation_recovery.py`.

Discovered: 2026-10-04, pending-allocation recovery milestone grounded on the
reservation45 static map.

### PORTABLE-010: Historical authority tests need genuine historical exports

Symptom: A sharing forgery test fails on Portable inventory validation before
reaching the intended authority refusal.

Cause: The fixture exports the current format, removes one newer sidecar, then
labels the result as Portable7. Other required current sidecars remain. This
does not produce evidence from the historical catalog.

Fix: Generate Portable7 from its actual schema12 catalog in a scoped test
context. Assert the original schema and manifest version, then restore current
runtime definitions before testing import. Preserve all forged receipt, job,
provider, independent witness and no-promotion assertions. Keep Portable8
coverage separate; do not weaken frozen inventory validators.

Discovered: 2026-10-03, full verification and exact synthetic reproduction.

### CUSTODY-002: Capture allocation evidence omits terminal journal history

Symptom: Protection retains an original queue and capture allocation, but its
mixed closure cannot restore an acknowledged capture through either homogeneous
replay API. The original terminal event disappears during local compaction.

Cause: Protecting the stage3 capture repeats the stage0 allocation commitment.
It does not retain a separate operation containing original event sequences,
receipt bytes, attempts or recorded times. Hash closure alone is not recovery.

Fix: Protect the exact terminal journal transition before compaction, separately
from its original custody and capture allocation. Validate the whole mixed chain
before replay writes. Preserve capture rows, original receipts and journal
high-water marks; retain unallocated queues and test interrupted/repeated replay.
Same-process terminal retries must also require journal protection. After local
compaction, a capture-only protected prefix cannot acknowledge a duplicate.
Authenticate the append chain unchanged, but derive queue materialization order
from original journal sequences: protection callbacks can finish out of order.
Before repeated replay, check completed physical source and file-blob bytes;
a matching stage3 database row does not prove that those files remain intact.
The owner replay seam supports initial queues, protected completed captures,
pending allocations, failed/quarantined/retried histories and discards (see
RECOVERY-002). Source/control/non-owner recovery, mandatory all-writer
configuration and real independent disaster recovery remain required before
activation. Never filter or renumber an authenticated recovery chain.

Discovered: 2026-10-03, actual synthetic guard closure clean-restore diagnostic.

### PORTABLE-009: Fenced source receipts do not settle retained custody

Symptom: Export of fenced source custody reaches the source-authority validator
and fails there instead of refusing with `ingestion_pending` before staging.

Cause: Portable8 permits ordinary queued journal custody. The schema13 bypass
also admitted source-intake bodies retained after fencing, although their terminal
quarantine receipts do not make that custody representable by the frozen source
authority format.

Fix: Refuse export when a pending journal delivery belongs to a source intake.
Keep ordinary queued exports, including managed-workspace owner custody, enabled.
Test reservation, source-write and blob-write faults, exact replay, absence of an
export destination and unchanged journal bodies, receipts and quarantine records.
This refusal preserves custody but does not provide fenced-history recovery;
complete typed source/control recovery remains required before live cutover.

Discovered: 2026-10-03, five synthetic source-task full-gate failures.

### CUSTODY-001: Restart draining must protect the original queue too

Symptom: A failed initial protection call retains the queued body, but a later
startup drain protects only a newly allocated capture and omits the original
custody identity from recovery evidence.

Cause: Protection runs on foreground enqueue but startup enters the drain
directly. Protecting a capture allocation does not preserve its earlier cue ID,
queue order or recorded time.

Fix: Read the exact initial plan from a consistent SQLite snapshot and require
the configured protection guard before every drain, including startup. Protect
the original allocation before materialization and require evidence again before
terminal compaction. A protection failure is not an ingestion attempt failure.
Test timeout, missing and mismatched evidence, restart, original cue/allocation
identity and retained bodies. The optional owner guard is not a real independent
backend, persisted all-writer policy or complete mutation recovery acceptance.

Discovered: 2026-10-03, synthetic protection-guard restart regression.

### PORTABLE-008: Managed titles require whole-envelope restore evidence

Symptom: A managed source restores with its original body and receipt, but its
owner-history title changes to a title inferred from redacted body text.

Cause: The frozen capture record has no supplied-title field. Base materialization
sets the capture title to null even when a managed delivery retains the exact
admitted submission and its supplied title. Archive round-trip equality alone
does not detect this loss because both exports retain the same envelope bytes.

Fix: For the unreleased Portable8 format, recover managed titles from validated
whole-envelope terminal evidence. Refuse conflicting titles for one capture.
Preserve Portable1–7 materialization semantics. Test three managed revisions,
withdrawal, exact history responses and terminal replay with original Brain,
source files and sender caches unavailable. A local fixture is not independent
encrypted protection or proof of post-snapshot write coverage.

Discovered: 2026-10-03, synthetic managed-record recovery format rehearsal.

### PORTABLE-005: Owner replay identity outlives derived capture keys

Symptom: Retrying an original owner delivery after clean Portable restore creates
new custody and an extra capture reservation instead of returning its old receipt.

Cause: Restore regenerates capture-table delivery keys, but retains the original
delivery and request digest in source aliases. Journal identity lookup ignores
that retained evidence when the derived capture key differs.

Fix: Resolve a missing ordinary replay identity through the retained alias and
exactly one completed imported revision. Refuse missing or ambiguous evidence.
Return an owner receipt without new custody, preserving admitted privacy because
the legacy owner request digest does not include that decision. This path cannot
authenticate public-job or destination-bound requests. Test clean restore and
rebuild, changed payload, privacy widening, and damaged completion evidence.

Discovered: 2026-10-03, synthetic historical-adoption recovery rehearsal.

### COLLECTOR-002: Close revision admission read connections explicitly

Symptom: A parent collector rejects a valid schema as invalid after a real
foreground CLI sharing approval. A fresh process still reads the same database
as current schema 12.

Cause: Revision-head and terminal-receipt reads use a SQLite connection context
manager, which ends the transaction but does not close the connection. Retained
connections interfere with the subsequent cross-process WAL reads.

Fix: Close revision-head, receipt verification and owner lifecycle inspection
connections in `finally`. Close the synthetic receipt snapshot helper too.
Exercise actual collector admission, CLI approval and external stdio together;
do not add garbage collection or schema-repair retries to hide the failure.

Discovered: 2026-10-03, Phase D synthetic trusted transport regression.

### SEMANTIC-001: Retained link targets remain inference ancestry

Symptom: Graph refresh dispatches an ordinary note whose accepted link targeted
a now-revoked sharing copy, after an owner edit removes the visible link and the
target file is absent from current selection.

Cause: Release checks walk same-note parent provenance but omit separately
retained managed-link target revisions. Current file selection is not the full
ancestry of accepted transformations.

Fix: Walk immutable parent and target revisions, requiring exact note/revision
identity and failing closed on missing or cyclic ancestry. Apply the common
current sharing and privacy predicate at prepare and release. Preserve the
historical pre-sharing schema path. Test all three mapped backend boundaries
with zero denied provider calls.

Discovered: 2026-10-03, synthetic checkpoint C retained-suggestion regression.

### SHARING-004: Inspection budgets include terminal receipts

Symptom: An approved-and-revoked inspection exceeds its response budget even
though the preview alone fits.

Cause: The read path measures only the preview before attaching decision and
revocation receipts.

Fix: Measure the complete inspection before returning it. Test exact-budget
success and one-byte-over-budget refusal with both receipts retained. Keep the
separate transformed-text UTF-8 ceiling and never truncate an inspection.

Discovered: 2026-10-03, synthetic checkpoint C inspection-budget regression.

### SHARING-002: A damaged marker does not erase managed custody

Symptom: A revoked copy becomes externally readable when its capture-row source
reference loses the managed prefix.

Cause: Eligibility identifies managed copies from one mutable projection and
falls back to ordinary-public policy when that projection is damaged.

Fix: Recognize independent delivery, link/decision membership, retained record
and provenance markers. Require exact linked evidence before external release.
Test individual projection damage after active approval and revocation.

Discovered: 2026-10-03, synthetic checkpoint C corruption regressions.

### SHARING-003: Local history authority is not retained-owner authority

Symptom: A local non-owner history grant reads a canonical page after its managed
source is withdrawn, although direct source reads refuse.

Cause: The shared member projector selects owner-history mode from local egress
alone. Canonical reads lack the direct source path's separate owner guard.

Fix: Require the actual owner for the retained-owner exception. Local-agent
history must check current managed state while preserving older ordinary source
revisions. Test canonical history and ordinary historical policy together.

Discovered: 2026-10-03, synthetic checkpoint C canonical withdrawal regression.

### PORTABLE-004: Per-row restore columns must not mutate the base catalog

Symptom: A valid sharing archive with two previews fails clean import at a
strict zip length check; single-preview roundtrips pass.

Cause: The first restored preview appends its local imported flag to a column
list reused by subsequent rows.

Fix: Keep base columns unchanged and derive each insertion's columns separately.
Test multiple approved, rejected and undecided previews in one archive, duplicate
import and exact non-manifest reexport.

Discovered: 2026-10-03, synthetic checkpoint C Portable 7 restore regression.

### SEARCH-003: Canonical page IDs are not retained revision IDs

Symptom: Mutating a provider-ineligible canonical page makes an unrelated valid
external search cursor stale.

Cause: Candidate and generation filters join membership revision IDs against
search page IDs, so secondary members never participate in the filter.

Fix: Resolve the exact current publication using the projector's head precedence.
Require full membership eligibility before ranking and digest generation, using
the same predicate. Test a separately eligible nonempty cursor around a hidden
canonical mutation.

Discovered: 2026-10-03, synthetic checkpoint C hidden-member cursor regression.

### MCP-003: Entry-time consent does not authorize callback output

Symptom: A content callback revokes durable provider consent, but its result
still reaches the stdio serializer.

Cause: Trusted authority is revalidated before dispatch, not after callback work.

Fix: Revalidate at the common tool-result boundary before serialization. Test
durable consent revocation inside a real negotiated read callback and require
zero protected output bytes, while keeping between-request revocation tests.

Discovered: 2026-10-03, synthetic checkpoint C in-flight stdio consent regression.

### SHARING-001: Close sharing read snapshots explicitly

Symptom: A second foreground session can intermittently classify a valid local
schema as invalid or turn a bounded contention response into a generic tool
failure while owner sharing recovery reads are active.

Cause: `with sqlite3.Connection` commits or rolls back a transaction but does
not close the connection. A sharing read left its SQLite snapshot alive after
returning.

Fix: Close every sharing read connection in `finally`, including startup
pending-copy scans. Test concurrent MCP review and capture through the real
stdio session after the change.

Discovered: 2026-10-02, checkpoint C synthetic MCP regression.

### COLLECTOR-001: Check cancellation before reading pending custody

Symptom: A collector run crashes with `collector_custody_not_found` when another
controller pauses or disables the source after capture acknowledgement.

Cause: Cancellation discards the active run's receipts. A pending-custody check
outside the selection barrier can read those receipts after they are removed.

Fix: Check the current generation, control epoch, and enabled state under the
selection barrier before inspecting pending receipts. Keep the pending check and
deferred-state update inside that barrier. Test cancellation after terminal
acknowledgement and queued replay separately.

A second window sits between a completed submit and its recorded outcome: the
barrier covers only the submit, so a waiting pause can discard the still-pending
receipt first. Widening the barrier changes deliberate control ordering; instead,
tolerate `collector_custody_not_found` from outcome recording only when the
source state confirms the cancel, which yields the same end state as a cancel
after the outcome. A missing receipt without that confirmation still fails.
Parallel test load exposed this one (2026-10-04).

Discovered: 2026-10-01, full saved-Markdown integration verification.

### REVIEW-004: Restore causal identities, not incidental row order

Symptom: An imported pending review rejects a valid route, accepts a stale route, or finishes a
decision without creating its intended page. Reindexing can also mistake every provenance source
for a canonical page owner.

Cause: Materialization order is not route order, pending pages have no current file to locate,
and membership in a page's provenance does not mean a capture owns that page.

Fix: Find the route with no successor. Rebuild pending destinations from validated frozen page and
space identities. Restore capture-owned pages separately from reviewed publication heads. Verify
import followed by approval and live reindex, in addition to comparing exported bytes.

Discovered: 2026-09-16, independent review of CLI/MCP review and publication.

### REVIEW-005: Project full evidence before taking its preview

Symptom: A protected reference longer than an evidence preview appears as an unredacted prefix.

Cause: Truncation removes the full literal needed by privacy projection. A later projection cannot
recognize the prefix as the original protected reference.

Fix: Project the complete evidence candidate against every contributing reference, then take the
bounded excerpt. Keep source bytes and their binding hashes unchanged and disclose projection.
Exercise both new bound proposals and historical single-source inspection.

Discovered: 2026-09-16, independent review and long-reference regression.

### PRIVACY-001: A redaction receipt does not authorize a sink

Symptom: Redacted content appears safe but still carries a `secret`, `unknown`, classification-failure, explicit-local-only, or unconfirmed `personal` privacy decision.

Cause: Redaction and authorization answer different questions. A receipt records a transformation; it does not widen the original privacy authority.

Fix: Have every work-tier event or Markdown adapter enforce the immutable privacy decision before any I/O.

Discovered: 2026-08-13.

### REVIEW-001: Approval evidence must be bound to one review

Symptom: A valid approval event can be paired with another review or approved-intent record during deserialization.

Cause: The event shape was validated without checking its review ID and deterministic approved record against the enclosing review.

Fix: Reject mismatched review IDs and approved records at the model boundary.

Discovered: 2026-08-13.

### STORAGE-001: SQLite paths are root capabilities

Symptom: A database path under the configured root escapes through a symlink, or SQLite sidecar files receive permissive default modes.

Cause: Lexical path checks and default SQLite file creation do not enforce filesystem containment or private permissions.

Fix: Traverse parents without following symlinks and maintain database, WAL, and SHM files at `0600`.

Discovered: 2026-08-13.

### CAPTURE-001: Private raw storage preserves canonical bytes

Symptom: Persisting a capture changes its deterministic capture ID.

Cause: Redaction was applied to identity-bearing raw fields before private persistence.

Fix: Store canonical raw captures unchanged. Apply redaction only when producing typed work-tier records.

Discovered: 2026-08-13.

### INDEX-001: Excluded symlinks must not block canonical indexing

Symptom: An index rebuild fails even though a retained source symlink is explicitly outside
the indexed content policy.

Cause: Traversal rejected the presence of every symlink instead of excluding links without
following them.

Fix: Ignore source symlinks before file classification, never follow file or directory targets,
and regression-test that target content is absent from the index.

Discovered: 2026-08-19.

### RELEASE-001: Assemble detector canaries at runtime

Symptom: The release audit flags a test fixture that resembles a real credential assignment.

Cause: Secret scanners correctly inspect committed test text without knowing whether a value is synthetic.

Fix: Build detector canaries from harmless fragments at runtime instead of committing assignment-shaped literals.

Discovered: 2026-08-13.

### CAPTURE-002: Resume from a durable event after a crash

Symptom: A retry after event persistence fetches mutable source content again and creates a second event with different bytes.

Cause: Recovery restarted the full extraction pipeline instead of checking the event stream first.

Fix: If one matching durable extraction event exists, rebuild only the deterministic distillation item and continue from that boundary.

Discovered: 2026-08-13.

### CAPTURE-003: Extracted metadata does not always repeat the source URL

Symptom: A provenance-bound YouTube capture retries forever during saved-content
publication because its normalized extraction has a platform and video ID but no canonical
URL.

Cause: Publication required `metadata.canonical_url` even though some approved extractors
represent source identity through typed platform fields.

Fix: Require the immutable envelope URL to match its provenance source reference, prefer an
extracted canonical URL when present, and otherwise use the provenance-bound envelope URL.

Discovered: 2026-08-26.

### SECURITY-001: A closed gate must be unreachable

Symptom: Documentation says an executor or redactor is unavailable, but callers can inject any implementation through a public constructor.

Cause: Policy checks were mistaken for runtime confinement or approved-policy selection.

Fix: Reject arbitrary injection until a concrete adapter passes its gate; use one policy-version-locked redactor for work-tier events.

Discovered: 2026-08-13.

### SECURITY-002: Authorize before every special-case protocol handler

Symptom: A scoped session's discovery omits an owner operation, but a direct wire request still
reaches its bootstrap or status handler.

Cause: Special-case handlers ran before the shared operation registry and dispatch authority gate.
Discovery and the main dispatcher were correct, but the early branch bypassed both.

Fix: Derive one session operation registry from authority and injected implementations, then reject
unavailable known operations before any handler, credential lookup, Brain initialization, or task
access. Exercise the complete stdio path, not only the downstream dispatcher.

Discovered: 2026-09-21, independent session-authority review.

### SECURITY-003: Synthetic authority tests do not prove the installed launcher mapping

Symptom: Scoped adapter tests pass, but the installed MCP command still constructs its own default
read tiers and has no durable provider-consent input. A deployment cannot distinguish a general
route from a personal-capable or external-provider route.

Cause: The immutable authority contract was tested below the real CLI composition boundary. The
launcher policy was wired only to destination-bound capture, and provider consent existed only as
an in-memory value.

Fix: Load one trusted whole-session policy, persist Brain-bound owner consent outside Brain content,
and reuse one narrowed `EffectiveAuthority` through every adapter. Revalidate policy and consent
before discovery and dispatch so revocation terminates the foreground process. Test the installed
stdio subprocess, not only synthetic adapter construction.

Discovered: 2026-09-23, Mac Mini CUT-G3 launcher-readiness audit.

### MEDIA-001: Resource limits must be enforceable

Symptom: A command object lists timeout, memory, or process limits that the runtime never applies.

Cause: Descriptive metadata was treated as execution enforcement.

Fix: Apply operating-system limits, process-group cleanup, output bounds, and staging checks. Return `tool_unavailable` on unsupported platforms.

Discovered: 2026-08-13.

### PROVIDER-001: Cloud authority does not prove prompt safety

Symptom: A cloud-authorized privacy decision allows secret-shaped prompt text to reach adapter construction or credential resolution.

Cause: Routing authority and content redaction were treated as the same gate.

Fix: Scan the final cloud prompt before factory construction. A finding returns a closed code with zero factory, credential, import, or provider calls.

Discovered: 2026-08-13.

### LEDGER-001: Frozen dataclasses are not trusted constructors

Symptom: A caller directly constructs a sanitized leaf or trusted citation and injects Markdown structure or traversal into output.

Cause: Merge code checked the Python type but not the value's invariants.

Fix: Validate during construction and again at every merge, render, apply, and synthesis boundary.

Discovered: 2026-08-13.

### LEDGER-002: Two sink writes need one visibility decision

Symptom: A crash after the first Markdown write exposes half of a ledger publication.

Cause: Individual atomic files were mistaken for an atomic document set.

Fix: Readers use only the durable applied manifest that binds every intended document and sink digest. Reconciliation completes partial physical writes before publishing the manifest.

Discovered: 2026-08-13.

### LEDGER-003: Slim authorization comes from durable state

Symptom: Caller-provided booleans or citation tuples authorize archive/slim without a persisted applied ledger row.

Cause: Evidence was passed as mutable orchestration input instead of loaded from the store.

Fix: Use a store-issued row identity, verify persisted citations, and atomically record archive digest, successor digest, and `slimmed` after both writes verify.

Discovered: 2026-08-13.

### LEDGER-004: A writer cannot certify its own persistence

Symptom: A no-op or memory-only writer returns plausible IDs, digests, and cached read-back bytes, causing durable state to advance although no artifact exists.

Cause: Apply trusted caller-provided receipt fields or allowed the same adapter to attest both the write and its persistence.

Fix: Validate receipt type, disposition, ID, and deterministic rendered-byte digest, then read exact bytes through a separate approved root-confined reader before the manifest or slim state advances.

Discovered: 2026-08-13.

### LEDGER-005: A model lock guard cannot be optional

Symptom: Constructing synthesis with no lock probes invokes a model without proving transaction and writer locks are clear.

Cause: A safety invariant was represented as an optional callback tuple.

Fix: Reject construction without an authoritative probe and fail closed when a probe errors or reports a held lock.

Discovered: 2026-08-13.

### LEDGER-006: A purge exception belongs to one closure

Symptom: A record retained or activated as a replacement under one purge remains visible even
though it also descends from a separately tombstoned source.

Cause: Loaded-state validation combined every retain and replacement into one global exception set
before checking each tombstone closure.

Fix: Scope visibility exceptions to the purge that owns the tombstone. Derive suppression and
purge-pending sets exactly from stored tombstones and purge resolutions, then reject orphan flags.

Discovered: 2026-09-07.

### REVIEW-002: Review audit text is not owner output

Symptom: Third-party instructions in a source reference or proposal reason survive approval and appear in an owner-authored record.

Cause: Delivery copied audit fields instead of deriving an output-safe reference.

Fix: Render owner text plus a deterministic opaque capture reference only, then verify the sink receipt before marking the outbox delivered.

Discovered: 2026-08-13.

### REVIEW-003: Review creation receipts bind the initial aggregate

Symptom: Intent routing reports an open review after a no-op boundary returns the right review ID with unrelated bytes.

Cause: Routing checked identity and disposition but not the canonical aggregate digest.

Fix: Hash `ReviewAggregate.create(proposal)` canonically and require the creation receipt to match before returning `review_open`.

Discovered: 2026-08-13.

### SYNTHESIS-001: Citation authority includes destination and durable proof

Symptom: A persisted citation ID is paired with a forged destination, or a memory-only persistence port returns synthesis success.

Cause: Authority compared IDs only and accepted an unconstrained persistence result.

Fix: Recompute the deterministic destination from the published capture document and require the approved SQLite store to return and confirm an exact typed durable record.

Discovered: 2026-08-13.

### TAXONOMY-001: Printable labels only

Symptom: NEL or Unicode line/paragraph separators create extra structure inside a rendered topic heading.

Cause: Label validation rejected ASCII controls but not all non-printable Unicode characters.

Fix: Reject non-printable characters and Markdown structural punctuation before route construction.

Discovered: 2026-08-13.

### CLI-001: Dependency injection is not output authority

Symptom: A typed adapter returns success while echoing argv through a command value or dynamic JSON key, asserting reserved readiness fields, or pairing a failure status with exit zero.

Cause: The composition root trusted the adapter's result type without independently validating its public schema and process semantics.

Fix: Use closed command and schema-key allow-lists, scan converged encoded keys and values against argv and secret/path residuals, reserve live/parity/cutover fields for a separate evidence boundary, and enforce status/exit coherence.

Discovered: 2026-08-14.

### AUDIT-001: Unclassifiable runtime references are unsafe

Symptom: An old-source path passes audit because the deny-root set is empty or a path-bearing argument uses an unsupported representation.

Cause: No extracted path was treated as proof that no path existed.

Fix: Require a non-empty deny policy and reject executable, working-directory, referenced-file, or argument values that cannot be completely classified into the canonical path representation.

Discovered: 2026-08-14.

### HOOK-001: Path strings do not grant hook-install authority

Symptom: A manual hook installer can target a replaced, symlinked, or different repository after planning.

Cause: Installation authority was represented by caller-provided paths instead of a bound filesystem identity.

Fix: Bind an explicit capability to repository, Git, and hooks-directory identities; revalidate before descriptor-relative writes; keep dry-run as default; and make post-commit delivery bounded and fail-zero.

Discovered: 2026-08-14.

### MIGRATE-001: Atomic files do not make an atomic state generation

Symptom: A crash after the first migrated state file leaves readers with a partial seven-family target that cannot safely resume.

Cause: Per-file atomic replacement was mistaken for one visible migration commit.

Fix: Stage and verify a complete generation off-path, hold an identity-bound exclusive lease, publish one atomic `CURRENT` pointer as the reader-visible commit, and use a durable journal to recover pre- and post-pointer crashes.

Discovered: 2026-08-14.

### PORTABLE-001: UTC partitions come from persisted recording time

Symptom: A record near a month boundary is written under different partitions on hosts with
different local time zones or after a retry.

Cause: Partitioning used the process wall-clock month instead of the immutable acceptance or
recording timestamp stored with the record.

Fix: Normalize the persisted timestamp to UTC before deriving `YYYY/MM`. Occurrence-time
corrections append a superseding row; they do not move the original record.

Discovered: 2026-08-31.

### ARCHITECTURE-002: Package entry points are runtime dependency edges

Symptom: Static imports report zero architecture debt while the installed command imports a
legacy composition module before selecting the current app path.

Cause: Ownership checks classified Python files but did not verify the targets in package script
metadata.

Fix: Point default scripts directly at app-owned entrypoints and test their imports in a fresh
process. Keep predecessor and scheduled behavior behind an explicit legacy facade.

Discovered: 2026-08-31.

### PRIVACY-002: Encoded residue must be compared after bounded decoding

Symptom: A protected source reference is removed in raw form but survives in percent-encoded or
HTML-entity form, or a query term is echoed by a result explanation.

Cause: Projection compared only the stored literal and renderers treated encoded output as
unrelated text.

Fix: Apply bounded repeated percent/HTML decoding when checking public tokens, fail closed when
decoding does not converge, protect exact digests, and keep query explanations generic.

Discovered: 2026-08-31.

### CLI-002: Prefix flags must survive family dispatch

Symptom: `--dry-run` before a command family is discarded, or the process creates a Brain root
and operational state before the adapter rejects the request.

Cause: The process shell opened application composition before handling the global non-mutating
flag, then passed only arguments after the family name to the adapter.

Fix: Reject unsupported global dry-run requests before opening the application. Direct adapter
dispatch still receives non-representation prefix flags, and the process regression must prove
the requested root was never created.

Discovered: 2026-08-31.

### PRIVACY-003: A hash of private input is not an opaque ID

Symptom: A metadata-only result omits the query text but returns a deterministic digest or digest
prefix derived from it.

Cause: A content hash was treated as an identifier even though callers can verify guesses against
private input.

Fix: Generate retrieval IDs independently with a cryptographic random source, return them only as
opaque correlation values, and scan public results for full and truncated query digests.

Discovered: 2026-08-31.

### CLI-003: Root-free operations normalize representation flags first

Symptom: `--version` works without configuration, but adding `--json` before or after it opens
the application and fails for a missing Brain root.

Cause: Root-free detection compared the raw argument tuple instead of removing the
representation-owned output flag.

Fix: Validate duplicate flags, remove `--json` before recognizing help/version, and test each
supported ordering without runtime configuration.

Discovered: 2026-08-31.

### CLI-004: Direct lifecycle tasks need typed error translation

Symptom: Owner source inspection or withdrawal refuses an unknown source, stale binding, or
altered retry, but the CLI reports a generic operation failure with exit 78.

Cause: Direct lifecycle calls raise engine `T03Error`; the CLI's typed error handler catches
app `T03AppError`. Unlike the negotiated task adapter, the direct path did not translate errors.

Fix: Translate typed engine errors at the lifecycle CLI boundary. Test real command dispatch,
exact error codes, replay, and unchanged lifecycle state after refused operations. Do not weaken
engine authorization or expected-version checks to make the CLI succeed.

Discovered: 2026-10-01, owner source inspect/withdraw acceptance tests.

### LIFECYCLE-005: Late retirement checks can poison capture recovery

Symptom: A changed saved item returns after owner withdrawal. Its source remains retired,
but an extra unfinished capture is reserved and ordinary startup fails while completing it.

Cause: Retirement was checked only during stage-three source linkage, after intake and journal
admission. Managed delivery lifecycle validation also ran after capture acceptance. A separate
response-loss case could accept a revision but never finish its managed receipt after withdrawal.

Fix: Check current lifecycle/head expectations before new managed reservation and refuse retired
sources before new source intake. Keep the stage-three guard as defense in depth. Pending managed
receipts block withdrawal until exact recovery completes. Completed historical replay after
withdrawal must not admit another capture or reactivate the source. Test changed returns, pending and lost-response
retries, stale lifecycle requests, retained evidence, and ordinary restart after refusal.

The shared source task must honor the managed namespace barrier too. Protecting only the collector
sink still lets a direct competing source submission overtake it. Validate fresh order/head and
already-pending intake before reservation, using the same order decision as source admission.

Discovered: 2026-10-02, saved-Markdown checkpoint B return and recovery acceptance.

### PORTABLE-003: Self-hashes do not prove linked admission evidence

Symptom: A forged managed submission passes Portable validation after its envelope and manifest
hashes are recomputed, while pointing at a different retained capture receipt.

Cause: Validation checked each row's internal hash without comparing the whole managed submission
against the independently validated source intake. Open binding/receipt shapes also admitted
unknown fields and boolean or floating-point versions.

Fix: Require exact intake bytes, namespace/destination/delivery linkage, closed typed binding and
receipt values, and explicit observed-envelope privacy/payload commitments. Test semantic forgery
with recomputed self-hashes; checksum failures alone do not exercise these invariants.

Discovered: 2026-10-02, independent checkpoint B review and synthetic Portable tamper tests.

### PRIVACY-004: Case-insensitive values have many reversible digests

Symptom: A protected URL is removed regardless of case, but the SHA-256 of a case-varied spelling
still appears in a public result.

Cause: The projection knew the canonical value's digest but could not enumerate every
case-equivalent preimage digest.

Fix: Treat standalone SHA-256-shaped output tokens as private residue. Use prefixed opaque record
IDs for public correlation instead of publishing bare content hashes.

Discovered: 2026-08-31.

### PORTABLE-002: Promote the exact validated snapshot

Symptom: Validation succeeds, but the bytes materialized or promoted during import/export differ
from the bytes that were checked.

Cause: The operation validated a pathname and read it again after another process or filesystem
transition changed the content.

Fix: Retain one immutable validated snapshot, materialize and verify that snapshot, then perform
the identity-bound atomic promotion. Retry evidence must bind to the same manifest and snapshot.

Discovered: 2026-08-31.

### CONNECTOR-001: Host evidence owns connector checkpoints

Symptom: A connector reports successful fetches or checkpoint advancement that do not match the
captures durably accepted by the Brain.

Cause: Connector-mutated counters or receipt claims were treated as authoritative at the host
boundary.

Fix: Meter discovery, fetch, extraction, and submission at host-owned capabilities. Record the
exact sink receipt with its delivery ID and source reference, and advance the checkpoint only when
that evidence matches the host receipt.

Discovered: 2026-08-31.

### ARCHITECTURE-001: Line-numbered debt is a live coordinate

Symptom: An import-debt report points to the wrong source line after a nearby edit, making a
violation appear fixed or assigning it to the wrong code.

Cause: Debt entries stored line numbers from an earlier source snapshot.

Fix: Regenerate line-numbered debt from the current source tree after every source edit and verify
the reported edge before accepting the classification or marking the debt resolved.

Discovered: 2026-08-31.

### INGRESS-001: Rejected pages must not starve later eligible rows

Symptom: A rejected first row keeps the cursor pinned forever, so later eligible input is never
processed; advancing the whole page can instead lose retry evidence when an eligible sink write
fails.

Cause: Cursor advancement treated a mixed page as all-or-nothing and did not distinguish policy
rejection from durable sink failure.

Fix: Advance past policy-rejected rows, but retain the cursor at the eligible row when its sink
submission fails. Keep bounded retry evidence for both outcomes.

Discovered: 2026-08-31.

### MCP-001: Scope retrieval before capability injection

Symptom: MCP tool calls enforce a space allow-list, but the adapter's stored retrieval attribute
still exposes unrestricted search or fetch to other callers.

Cause: The app injected the full retrieval task and asked the representation to derive a scope for
each tool call.

Fix: Derive the scoped retrieval capability at composition, inject only that capability, and reject
objects that still expose the unrestricted `scoped` factory.

Discovered: 2026-09-01.

### INTEGRATION-001: Installed optional modules are not necessarily preloaded

Symptom: An explicitly enabled optional integration reports `optional_dependency` even though its
module is installed and importable.

Cause: Availability checked `sys.modules`, which describes prior import state rather than installed
module availability.

Fix: At the reviewed app extension host, import the composition-declared module only after its
capability is enabled. Keep disabled/default profiles import-free.

Discovered: 2026-09-01.

### BACKUP-001: Replay-identical backups cannot include mutable scheduler state

Symptom: Replaying one backup request produces different app-state bytes even though the
canonical Brain content did not change.

Cause: The backup included scheduler cursors and claims that can advance while the request is
being verified or replayed.

Fix: Back up only schema-validated immutable run receipts. Recreate scheduler runtime state after
restore instead of treating it as recoverable instance data.

Discovered: 2026-09-01.

### BACKUP-002: An allow-listed backup path does not validate its contents

Symptom: A known app-state filename enters a backup with arbitrary JSON or credential-shaped
content.

Cause: Inventory validation treated the relative path as authority for every byte stored there.

Fix: Give each allowed app-state artifact an exact bounded schema, reject extra fields, and scan
validated values for forbidden residue before publishing the manifest.

Discovered: 2026-09-01.

### LIFECYCLE-001: A pending journal does not prove that its owner crashed

Symptom: A concurrent lifecycle request sees a pending upgrade and rolls back work that another
process is still performing.

Cause: Durable intent records survive crashes, but they do not carry live process authority.

Fix: Hold a distinct root-scoped kernel lease for the full lifecycle attempt. Recover a pending
record only after acquiring that lease, and bind each staged effect and terminal receipt to the
same request fingerprint.

Discovered: 2026-09-01.

### LIFECYCLE-002: A daemon cannot safely coordinate its own replacement

Symptom: Upgrade or uninstall restarts or removes the daemon unit before the coordinator can
record success, failure, or rollback evidence.

Cause: The lifecycle coordinator runs inside the process and supervisor unit that it is replacing.

Fix: Run lifecycle orchestration from a short-lived owner command outside the daemon, inject the
artifact and supervisor ports, and keep the default source-checkout composition fail closed.

Discovered: 2026-09-01.

### AUDIT-002: Cleaning the current tree does not clean reachable history

Symptom: The source tree is clean, but the public-history audit still reports an older planning
path or synthetic private-network fixture.

Cause: Replacing the current file leaves every earlier blob reachable, while broad path or rule
exceptions would hide unrelated future residue.

Fix: Sanitize the current tree and record only reviewed historical false positives by exact blob
SHA-256, normalized repository path, and allow-listable rule. Never permit credential or private
denylist findings through that policy.

Discovered: 2026-09-01.

### CI-001: Short Unix-socket test roots must exist on every supported host

Symptom: Daemon, UI, recovery, and subprocess tests pass on macOS but fail before setup on every
Linux CI version.

Cause: Tests shortened Unix-socket paths by creating temporary roots below macOS-specific
`/private/tmp`, which is absent on Linux.

Fix: Resolve `/tmp` before creating deliberately short temporary roots. It becomes canonical
`/private/tmp` on macOS and remains `/tmp` on Linux, preserving both path identity and host
portability. Treat the full multi-version Linux jobs as required evidence.

Discovered: 2026-09-01.

### CI-002: Syntax-valid action pins can still fail before checkout

Symptom: A newly copied CI job fails during job setup on every matrix version, before checkout or
any project command runs, while `actionlint` passes locally.

Cause: A one-character commit-SHA drift still matches workflow syntax but does not identify the
same valid pinned action revision used by sibling jobs.

Fix: Keep repeated action pins identical and assert that invariant in a repository test. Treat
`actionlint` as syntax validation, not remote action-revision validation.

Discovered: 2026-09-02.

### CONTROL-001: Default Unix-socket backlog behavior varies by host

Symptom: A partial client occupies the serial daemon reader and the next owner request gets
`EAGAIN` on Linux, while the same stalled-client regression passes on macOS.

Cause: Calling `listen()` without an explicit backlog left queue capacity to platform defaults.
The accepted-client timeout bounded the first read but did not guarantee that the next connection
could queue.

Fix: Set an explicit bounded backlog larger than one and keep the accepted-client timeout. Assert
the backlog in the stalled-client regression instead of adding sleeps or client-side retries.

Discovered: 2026-09-01.

### PACKAGING-001: A moved regular subpackage hides unmoved workspace modules

Symptom: Root tests cannot import legacy or connector modules even though both source roots are on
`PYTHONPATH`.

Cause: A moved `open_brain.cli`, `open_brain.integrations`, or `open_brain.services` initializer
creates a regular package whose search path excludes the still-classified directory under
`src/open_brain`.

Fix: Extend those search paths only in the root test harness while the phased move is incomplete.
Never add workspace path extension to the shipping app. The wheel-only harness must remain green,
and P4-W4 removes the test overlay with the old monolith tree.

Discovered: 2026-09-01.

### TOOLING-001: Ruff cache can hide import reclassification after package moves

Symptom: Local Ruff verification passes after moving modules between source roots, but a clean CI
checkout reports many `I001` import-order failures.

Cause: Cached lint results predate the relocation even though isort's first-party classification
depends on the module's source root and distribution boundary.

Fix: Configure `open_brain` as first-party and `open_brain_engine` as its third-party dependency.
Run Ruff once with `--no-cache` after package moves and treat that result as the migration gate.

Discovered: 2026-09-01.

### PACKAGING-002: Module depth does not prove a source checkout

Symptom: An installed supervisor manifest contains a nonexistent `PYTHONPATH` and working
directory below the interpreter's library directory.

Cause: The factory counted parents above `__file__`. Source and installed modules have similar
depth, so a `site-packages` path was mistaken for a checkout.

Fix: Enable source mode only when the module resolves to the exact declared source layout. Exercise
the production factory from the installed wheel, not only constructors with `checkout_root=None`.

Discovered: 2026-09-01.

### PACKAGING-003: An installed CLI does not imply an installed daemon launcher

Symptom: The app and engine wheels install and `open-brain init` succeeds, but a user cannot start
the required daemon without discovering uv's private environment interpreter and invoking an
internal module path.

Cause: Wheel acceptance exercised daemon behavior in-process and checked console scripts, but the
documented daemon command existed only for a source checkout.

Fix: Expose the foreground daemon through `open-brain daemon`. Verify that command through the CLI
contract, isolated wheel help, and a clean installed-wheel journey.

Discovered: 2026-09-04.

### PACKAGING-004: Requires-Python must match the current source grammar

Symptom: A source package advertises Python 3.12 compatibility, but that interpreter rejects
unparenthesized multi-exception handlers before the application can start.

Cause: Package metadata retained `>=3.12` for historical replay while the workspace, formatter,
native build and CI targeted Python 3.14. PEP 758 intentionally permits the syntax in 3.14 and
catches every listed type; it does not restore Python 2 exception-name binding.

Fix: Require `>=3.14,<3.15` in all active distributions and regenerate the workspace lock.
Check the built wheels through the installer on supported and rejected interpreters. Historical
replay belongs to the original revision, not the current distribution's compatibility promise.
Homebrew executables bundle the supported runtime.

Discovered: 2026-09-16, new-laptop source installation report.

### TESTING-001: An inner fixed interpreter can falsify a CI version matrix

Symptom: Python 3.13 and 3.14 jobs pass even though their wheel-isolation subprocesses run on
Python 3.12.

Cause: The outer matrix selected an interpreter, but the acceptance harness hard-coded another
version when creating product and test environments.

Fix: Derive isolation environments from the active matrix interpreter. Run the installed journey
independently on every declared Python version.

Discovered: 2026-09-01.

### AUDIT-003: ImportFrom.module alone misses private child and lazy dependencies

Symptom: An artifact scan accepts a private engine child imported through a public parent, or an
undeclared dependency loaded only on a lazy path.

Cause: The scanner recorded only the parent in `from package import child` and inspected only
engine-prefixed static imports.

Fix: Resolve aliases against the complete manifest module map, compare external roots with wheel
metadata, reject forbidden workspace roots, and allow-list each variable dynamic import by exact
artifact path and signature.

Discovered: 2026-09-01.

### AUDIT-004: Callable spelling does not establish dynamic-import provenance

Symptom: An artifact scan misses imports reached through `builtins`, assignment aliases, or
reflective lookup, while rejecting an unrelated local object whose name happens to be `importlib`.

Cause: The scanner trusts global names instead of following bindings from actual importer
capabilities. An allow-listed call signature also leaves untracked capability escapes in the same
file.

Fix: Track importer bindings and calls through lexical provenance. Treat every capability use as a
review event, and bind the sole exception to its exact artifact path and function signature.

Discovered: 2026-09-02.

### AUDIT-005: Importer provenance also flows through runtime namespaces

Symptom: Direct and aliased dynamic-import checks pass, but an artifact can still reach the same
importer through `sys.modules["builtins"]`, namespace helpers, or dynamic evaluation.

Cause: Python exposes built-in objects through module registries and namespace dictionaries. A
scanner that follows only import statements and local aliases loses that provenance.

Fix: Treat `sys.modules`, `globals`, `locals`, `vars`, `eval`, and `exec` as reviewed artifact
capabilities. Reject them by default and keep the one authorized dynamic import bound to its exact
file and function.

Discovered: 2026-09-02.

### AUDIT-006: AST walk order is not control-flow provenance

Symptom: A dead-branch assignment erases a real importer path, while a loop, context-manager, or
exception target is mistaken for the standard-library module it shadows.

Cause: One mutable binding map follows AST visitation order. It neither joins alternate outcomes
nor applies Python's lexical and compound-target binding rules.

Fix: Track sets of possible provenance, join control-flow outcomes, predeclare function-local
names, and model loop, `with`, exception, match, and comprehension scopes before inspecting uses.

Discovered: 2026-09-02.

### AUDIT-007: Import aliases and comprehension walrus targets share existing authorities

Symptom: Attribute-based reflection is rejected, but importing that same member with
`from ... import ...` passes; or a walrus target disappears when a comprehension scope exits.

Cause: The analyzer maintains separate syntax-specific member rules and treats every name inside a
comprehension as comprehension-local. Python resolves imported members through the module object,
while PEP 572 binds assignment-expression targets in the enclosing scope.

Fix: Use one module-member provenance function for attribute and `ImportFrom` syntax. Keep
iteration targets local to the comprehension, but propagate walrus targets to the nearest enclosing
scope and predeclare them as function locals.

Discovered: 2026-09-02.

### AUDIT-008: A reviewed name does not prove a reviewed dynamic-import value

Symptom: An artifact passes because a dynamic loader and argument retain approved spellings, even
though an equivalent module alias reaches the loader or the argument was reassigned first.

Cause: The analyzer keys exceptions to syntax instead of semantic authority and value provenance.
Python also exposes import state through package `__init__` modules, `pkgutil`, frames, and type
reflection.

Fix: Normalize equivalent authorities, distinguish pristine parameters from unknown or reassigned
values through control-flow joins, and reject internal package roots again at the runtime optional
loader boundary.

Discovered: 2026-09-02.

### AUDIT-009: Architecture gates should close authority, not simulate a sandbox

Symptom: Each review finds another Python spelling that reaches the same generic loader, and the
acceptance analyzer grows without producing a finite security boundary.

Cause: P4H009 is treated as malicious-code containment even though its contract is to catch app
architecture regressions. The generic string loader keeps the unwanted authority open.

Fix: Replace arbitrary module strings with a closed typed provider registry, reject internal
identifiers at runtime, and review the gate against a named finite adversarial corpus.

Discovered: 2026-09-02.

### AUDIT-010: Source projections must not erase stale review evidence

Symptom: A removed exception remains in the canonical review inventory, but the normal
architecture gate passes after the reviewed file moves to another source root.

Cause: A helper filters both source records and their review entries before stale-review
validation. The evidence disappears from the test instead of becoming stale.

Fix: Validate the canonical review inventory against current source locations, including moved
records. Keep legacy source projections limited to the code or debt they were created to select.

Discovered: 2026-09-02.

### CONNECTOR-002: A `python -m` protocol module creates a second type identity

Symptom: A valid worker request reaches the child, but the connector rejects it because exact type
checks see a different `ConnectorWorkerRequest` class.

Cause: Running the protocol module with `python -m` defines its classes under `__main__`. The
connector imports the canonical module name and receives a second set of class objects.

Fix: Execute a small child bootstrap module that imports and invokes the canonical protocol module.
Keep all request, receipt, and error types defined only under the canonical module name.

Discovered: 2026-09-02.

### CONNECTOR-003: A valid child receipt does not prove host-budget compliance

Symptom: A connector worker returns schema-valid metadata with counts above the parent-issued
budget, or claims that replay created another capture.

Cause: Receipt validation checked field types and local count relationships without binding both
runs to the request limits or the replay contract.

Fix: Revalidate each run against the exact parent budget. Require replay to submit no captures and
bind the reported capture count to the created receipts before accepting worker output.

Discovered: 2026-09-02.

### CONNECTOR-004: Metadata discovery must not retain installed execution authority

Symptom: The app discovers an installed connector without importing it, but a later call through
the same registry loads and executes that connector in the parent process.

Cause: Metadata-only discovery and in-process compatibility shared one loadable entry-point group
and registry implementation, so the bounded worker was optional rather than exclusive.

Fix: Reserve the installed public group for metadata and child execution only. Use a distinct
group for explicitly injected compatibility sources, reject installed registry resolution, and
prove from wheels that every parent resolver leaves the connector module unloaded.

Discovered: 2026-09-02.

### RELEASE-002: Artifact coordinates and manifest labels must use the same number

Symptom: Connector wheels and sdists build, but artifact policy reports a stale rewrite or empty
canonical membership.

Cause: The policy coordinate `connectors` generated `connectors-wheel` and `connectors-sdist`, while
the canonical manifest uses singular `connector-wheel` and `connector-sdist` dispositions.

Fix: Name the policy coordinate `connector`. Keep the distribution directory and Python package
names independently plural where their published identities require it.

Discovered: 2026-09-02.

### LEGACY-001: Importing every wheel module does not exercise lazy dependency edges

Symptom: A private legacy wheel installs beside engine and imports every packaged module, but an
enabled compatibility path later imports an undeclared third-party SDK from the host environment.

Cause: Import smoke tests execute module bodies, not lazy loaders. A copied compatibility snapshot
can therefore retain ambient package authority while its metadata still claims an engine-only edge.

Fix: Inspect every compatibility source in the built wheel for static and dynamic import roots.
Require optional providers through an injected callable, and exercise an enabled injected path in
the engine-plus-legacy-only environment.

Discovered: 2026-09-02.

### READINESS-001: CLI-style probe exits bypass ordinary fail-closed handling

Symptom: One readiness probe terminates the aggregate preflight and exposes raw exit text even
though ordinary probe exceptions become opaque unavailable receipts.

Cause: `SystemExit` inherits directly from `BaseException`, so `except Exception` does not catch a
CLI helper that exits.

Fix: Catch `SystemExit` explicitly beside `Exception` at the probe boundary, preserve
`KeyboardInterrupt`, and test both paths with sensitive canaries.

Discovered: 2026-09-02.

### RELEASE-003: Build tools may add control files beside requested outputs

Symptom: Every requested wheel and sdist builds, but exact output inventory validation reports an
extra `.gitignore` containing one `*` byte.

Cause: `uv build --out-dir` creates its own ignore marker in the destination directory.

Fix: Keep exact output inventory validation. Remove only the tool's exact known marker before
validation, and reject the same filename when its bytes differ.

Discovered: 2026-09-03.

### RELEASE-004: A zero-issue notarization log may encode issues as null

Symptom: Apple accepts a submission, but the bounded parser rejects its log before stapling because
`issues` is `null` instead of an empty array.

Cause: Notary service output has two observed zero-issue representations.

Fix: Accept only `null` or an empty list when status is accepted. Reject nonempty lists and every
other shape, then require stapling and validation independently.

Discovered: 2026-09-03.

### RELEASE-005: Homebrew rejects local formula files outside a tap

Symptom: `brew install --formula /absolute/path/open-brain.rb` fails even though the formula and
archive are valid.

Cause: Current Homebrew requires formulae to belong to a tap. Invoking a developer command such as
`brew tap-new`, even for help, can also enable Homebrew developer mode in user configuration.

Fix: For local smoke tests, create a temporary Git-backed tap, install the fully qualified formula,
then uninstall and untap it. Avoid `tap-new`; if a developer command was probed, run
`brew developer off` and verify cleanup.

Discovered: 2026-09-08.

### RELEASE-006: A repository transfer can disable security scanning

Symptom: A public repository keeps its branches, workflows, and branch protection after an
organization-to-personal transfer, but secret scanning and push protection change from enabled to
disabled.

Cause: GitHub reevaluates account-scoped security settings when repository ownership changes.
Preserved repository identity does not guarantee that every feature setting remains enabled.

Fix: Snapshot `security_and_analysis`, branch protection, workflows, access, and repository ID
before transfer. Query the new repository directly afterward, restore any changed security control,
verify the old URL redirect, and update local remotes to the new URL.

Discovered: 2026-09-08.

### LIFECYCLE-003: Copying a symlink path is not portable target-copy behavior

Symptom: A lifecycle copy produces a candidate directory on macOS but a preserved `current`
symlink on GNU/Linux.

Cause: `ditto` and `cp -RPp` do not treat a symlink supplied as the source path the same way.

Fix: Validate the managed activation link, resolve it to the enrolled candidate identifier, and
copy that explicit candidate directory. Never rely on platform-specific source-link handling.

Discovered: 2026-09-03.

### LIFECYCLE-004: Metadata-preserving fixture copies can retain a foreign UID

Symptom: Artifact installation succeeds in a Linux container, then initialization rejects existing
owner-only state even though modes and bytes are correct.

Cause: Root runs `cp -p` against a bind-mounted fixture owned by the host runner, preserving UID
1001 inside the container. Docker Desktop ownership remapping can hide the defect locally.

Fix: Copy private fixture contents into a newly created destination without preserving ownership.
Preserve bytes, modes, and symlinks, then exercise a synthetic foreign-owner fixture regression.

Discovered: 2026-09-03.

### TOOLING-002: Make prints path-bearing recipes unless they are silent

Symptom: A command returns bounded JSON but Make prints the expanded recipe first, including local
absolute artifact, fixture, or evidence paths.

Cause: Output safety covered the child process but not Make's default command echo.

Fix: Prefix path-bearing recipes with `@` and test the Makefile text as part of the public-output
contract.

Discovered: 2026-09-03.

### AUDIT-011: A later shell command can mask an earlier audit failure

Symptom: An audit prints findings, but the overall shell invocation exits zero after a following
history check succeeds.

Cause: Sequential commands ran without fail-fast shell behavior. The final command supplied the
reported exit status.

Fix: Run release gates with `set -eu` or as separate checked commands. Keep binary-native media in
its dedicated validator instead of over-scoping a text scanner with a fixed content-size limit.

Discovered: 2026-09-03.

### TOOLING-003: Parallel uv commands can race on one project environment

Symptom: Independent verification commands fail to spawn Python or fail while removing `.venv/bin`
even though their inputs are valid.

Cause: Concurrent `uv run` processes select different Python versions and replace the same project
`.venv` at the same time.

Fix: Run shared-environment `uv` gates sequentially, or give each parallel command an isolated
environment. Restore a raced environment with one bounded `uv sync` before retrying.

Discovered: 2026-09-03.

### SIGNING-001: A read-only sandbox can falsify macOS signature verification

Symptom: `codesign --verify --strict` reports unchanged signed DMG bytes as modified only inside a
macOS read-only agent sandbox, while the same relative, absolute, and deep checks pass elsewhere.

Cause: The sandbox can restrict macOS security-service or metadata access while `codesign` reports
the result as a signature failure instead of a permission failure.

Fix: Hash the subject before and after, reproduce the exact command outside that sandbox, and use
a bounded sandbox that permits the required verification services while retaining a no-write task
contract. Never rebuild a frozen candidate from one unreproduced sandboxed signature error.

Discovered: 2026-09-03.

### SEARCH-001: Project private text before deriving snippets

Symptom: A protected source reference disappears from stored search text but a partial prefix or
suffix still appears in a result snippet.

Cause: Snippet generation or length clamping ran before the complete field passed through the
public-safe projection boundary. The shortened fragment no longer matched the protected value.

Fix: Apply the public-safe projection to complete title and body fields before indexing,
highlighting, snippet generation, or truncation. Reapply the projection at the output boundary.

Discovered: 2026-09-08.

### SEARCH-002: Search coverage derives from logical source heads, not projection deletes

Symptom: `portability.export` fails with `repair-aware search coverage mismatch` after a Markdown
import updates, reverts, or deletes a note, although the live search results look correct.

Cause: Markdown import superseded and missing revisions by deleting their `search_documents`
rows while leaving each capture's logical source active and available. Portable Brain v5 and
restore rederivation derive expected search identities from logical source heads, so the durable
heads promised coverage the projection had removed. Interrupted imports had the same drift because
capture registration inserted every source as available before activation.

Fix: Keep availability aligned with the projection rule. Registration consults the Markdown
projection rule when inserting a source; activation sets the head available and marks the
superseded head missing; scan finalization marks missing files missing. Never fix coverage by
teaching the Portable validator about importer tables, because the Portable snapshot does not carry
them and restore would resurrect superseded revisions into search.

Discovered: 2026-09-20, full `make verify` after the CUT-G2C focused suites passed.

### TOOLING-006: Writer-contention timing bounds must allow stacked timeouts

Symptom: CI on macOS fails `test_upgrade_writer_contention_is_bounded_and_retry_safe` with an
elapsed time just over ten seconds, while the same test takes about five seconds locally and the
branch never touched the database open path.

Cause: `open_local_database` waits up to five seconds to connect and then up to five more seconds
on the SQLite busy handler when another connection holds a write transaction. On a slow runner both
waits elapse, so a `< 10` bound sits exactly on the worst case.

Fix: bound such tests at a value that proves the wait is finite, not short. The test now asserts
under twenty seconds with a comment naming the stacked timeouts. Do not shorten either timeout to
make the test faster.

Discovered: 2026-09-21, twice in a row on pull request CI for unrelated branches.

### TOOLING-004: MyPy recognizes static platform guards

Symptom: A platform-only import passes MyPy on its supported host but fails with `import-not-found`
and `unused-ignore` on another CI operating system.

Cause: The runtime branch used `platform.system()`, which MyPy does not use to prune unreachable
platform code.

Fix: Guard platform-only imports with `sys.platform` and run focused MyPy checks with each supported
`--platform` value.

Discovered: 2026-09-08.

### CONTROL-002: Process exit can race pipe observation

Symptom: A worker sends a valid receipt and exits, but the supervisor reports `probe-failure` because
the first nonblocking pipe poll found nothing just before process exit became visible.

Cause: The supervisor treated a dead process as proof that no unread IPC result remained.

Fix: Join a completed worker, drain its receive pipe once more, and classify failure only when no
valid receipt remains.

Discovered: 2026-09-08.

### CONTROL-003: Short readiness retries can poison a socket backlog

Symptom: A daemon process stays alive and owns its Unix socket, but a readiness loop never receives a
status response on a slower host.

Cause: Each short-lived probe connects, times out, and closes before the single-threaded server begins
accepting. Those abandoned connections remain queued, so the server drains stale requests while the
probe loop adds more. Starting a fresh interpreter for every attempt adds avoidable startup variance.

Fix: Use one isolated probe process. Retry only while the socket is unavailable; after connecting,
keep that request open for the remaining bounded startup budget. Exclude unrelated listeners from a
control-only integration test.

Discovered: 2026-09-08.


### SQLITE-001: Read-only validation cannot recover a hot rollback journal

Symptom: After an interrupted migration with dirty-page spill, read-only SQLite inspection reports
that recovery needs a writable database. Treating that result as malformed state prevents retry.

Cause: SQLite must restore committed pages from its rollback journal before it can read the schema.
A read-only connection cannot perform that recovery. Separately, committing a validation transaction
before the caller's query leaves a race between validation and use.

Fix: Classify a private, confined hot-journal candidate as `recovery_required` and allow SQLite to
recover before locked schema validation. Keep normal readers in their validated read transaction;
revalidate application writes inside `BEGIN IMMEDIATE`. Test process exit after dirty-page spill,
read-only refusal, recovery retry, and schema changes between opening and application work.

Discovered: 2026-09-09, OB1-W5 migration implementation and read-only review.


### MCP-001: Default MCP cannot import the retained work adapter

Symptom: Adding a stdio command imports a module excluded by the default native dependency audit.

Cause: The retained transport imported concrete work-scoped adapters from the integrations package.
Those adapters belong to a separate composition boundary.

Fix: Keep protocol types and bounded framing in the neutral `services/mcp_protocol.py` module.
Default MCP injects a non-owner capture sink and a separate whole-Brain read operation. The retained
`mcp_stdio.py` wrapper keeps its scope check and remains excluded from the default artifact.

Discovered: 2026-09-09.


### TOOLING-005: Homebrew name and version inventory need separate commands

Symptom: A preservation check fails before its smoke starts with `Options --full-name and
--versions are mutually exclusive`.

Cause: `brew list` cannot combine fully qualified formula names with version output.

Fix: Read identities with `brew list --formula --full-name`, then read the selected product's
versions with `brew list --formula --versions <full-name>`. Treat any inventory failure as an error,
not as evidence that the product is absent. Test the actual smoke shell with a command-recording
fake Homebrew, including an installed product and interrupted-run recovery.

Discovered: 2026-09-09, OB1-W7 contributor path.


### RELEASE-007: Source audit success does not establish native artifact safety

Symptom: Both platform CI jobs and source/history owner audits pass, but the required artifact-aware
owner audit rejects both actual release archives with `content-scan-limit-exceeded`.

Cause: The archive scanner feeds the bundled executable into a source-text rule capped at 2 MiB.
The observed native executables are about 10 MB and 13.7 MB. The native dependency inventory verifies
module names and format/signature, but does not scan packaged contents with the owner denylist.

Fix: `artifact_audit.py` retains source limits and adds bounded inspection of native containers,
compressed CArchive/PYZ members, base-library ZIPs, and marshaled code data. Synthetic large and
hostile fixtures cover the parser; both actual platform archives complete inspection. They still
fail content policy on build paths and standard-library address examples. No exemption was added.

Parser trap: standard-library archive readers can allocate metadata before yielding a member.
Preflight ZIP directories and tar extension records before those readers, and reject uncovered ZIP
payload gaps or nonzero tar padding. PYZ keys are module identities; map them to module paths before
applying file-suffix rules so a module ending in `.sqlite` is not mistaken for a database file.

Discovered: 2026-09-09, public release readiness audit at goal commit `d81bb64`.

### RELEASE-008: Frozen metadata must describe the runtime, not its builder

Symptom: A native bundle contains installer paths in distribution metadata and generated sysconfig
values, even though its Python code filenames were normalized by PyInstaller.

Fix: Positively select runtime and legal dist-info files. Retain the full sysconfig scalar mapping,
but compile references to its own installation prefixes as frozen runtime-prefix expressions.
Do not derive those prefixes from the builder's virtual environment, delete unknown variables,
or remove `sysconfig` from the module graph. Preserve all other PyInstaller cached code objects.
Test the actual frozen metadata paths and pointer ABI as part of the existing self-check.

Related policy trap: `ipaddress` contains three runtime private-network constants; they are not
disposable examples. `urllib.request` also contains a documentation example. Changing those strings
to hide them from a scanner is not remediation. Any exception needs a separate exact-module,
payload-hash, and rule-bound decision while owner terms and all other findings remain enforced.
The [approved payload policy](../../audits/2026-09-09-ob1-stdlib-content-policy-proposal.md)
now records that decision. Python patch changes can alter marshal hashes even for similar source;
never copy an older CI hash into the policy merely to make its artifact pass.

Discovered: 2026-09-09, native metadata remediation after `46bf308`.

### ARCH-001: Optional protocols cannot live in the base application namespace

Symptom: Removing appliance code from `open-brain` breaks the separately packaged connector tests,
even though `open-brain-connectors` does not declare an app dependency.

Cause: The connector distribution imported worker protocol contracts from
`open_brain.extensions`. The metadata graph looked separate, but the source graph still crossed the
base application boundary.

Fix: Keep the worker protocol and runtime in `open_brain_connectors.runtime`. Test both declared
dependencies and parsed imports, and pin the complete base app source inventory so an optional
protocol cannot drift back into the shipping application.

Discovered: 2026-09-11, foreground-runtime package separation.
### INTEGRATION-002: A narrow Graphify callable does not imply a narrow runtime

Symptom: Planning treats `graphify.extractors.markdown.extract_markdown` as a standalone
Markdown-only dependency and assumes it resolves links throughout an Obsidian vault.

Cause: At upstream commit `3f82bf7f837a07fb0f7668fbdbd5662801906942`, the extractor package
initializer eagerly imports other extractors. Vault-global wiki-link resolution also depends on
root context supplied by the larger extraction facade, which checks tree-sitter before dispatch.

Fix: Measure the complete import closure and use root-aware extraction as the correctness baseline.
Test cross-folder links and duplicate basenames, keep the cache outside the source snapshot, and
verify any proposed narrow API against that baseline. Process isolation alone does not reduce the
packaged module inventory. Runtime and native compatibility remain unverified until the spike.

Discovered: 2026-09-09, native workspace planning and independent upstream source verification.
See [the integration plan](../../plans/2026-09-09-ob1-native-workspace.md) for pinned source anchors.

### INTEGRATION-003: Graphify's scan root and raw link IDs need an adapter boundary

Symptom: Root-aware Markdown extraction resolves a link to an unselected note, chooses one of two
same-basename notes, or emits an unresolved target ID containing an encoded absolute source path.

Cause: At the pinned `graphifyy==0.9.57` source, vault-wide lookup walks the scan root independently
of the selected input list. Duplicate-name fallback uses a deterministic tie-break, while unresolved
links retain path-derived IDs. The NW0-B1 synthetic probe reproduced all three behaviors and an
unresolved frontmatter alias; relocation changed dangling IDs while selected-page mappings remained stable.

Fix: Stage only eligible notes under the scan root. Keep alias/ambiguity resolution, stable Brain-ID
mapping, and path-free unresolved diagnostics at the engine boundary before graph publication or
permanent-link acceptance. Keep Graphify cache output private and outside the source snapshot.
Do not treat a selected input list as the full exclusion boundary or export raw upstream IDs.

Discovered: 2026-09-09, bounded macOS arm64 NW0-B1 closure/import probe. See the
[decision record](../../plans/2026-09-09-ob1-native-workspace-nw0.md) for results and remaining gates.

### INTEGRATION-004: Official-client flags do not establish a complete isolation boundary

Symptom: A client accepts empty-tool, safe-mode, or ephemeral flags, but the adapter treats that as
proof of tool-free inference with no ambient effects or retained prompts.

Cause: Codex `0.153.4` can register tools from model metadata independently of shell suppression.
Claude Code `2.1.265` offline diagnostics added 687 system-tool tokens when `--json-schema` was
present despite empty/denied tools; the category disappeared without schema mode. Claude safe mode
also retains managed hooks. Session-persistence controls do not mean no configuration/cache writes.

Fix: Verify the effective tool catalog and policy before sending notes. Keep plain-text JSON with
engine validation as the strict tool-free candidate; measure its quality. Reject incompatible
managed policy, enforce runtime egress/time/output bounds, count internal attempts, and test prompt
retention and active cancellation separately. An idle offline startup does not pass a subscription
gate, and a staged-executor interface does not supply OS confinement.

Discovered: 2026-09-09, NW0-C1 source/control inspection and synthetic macOS startup probes.
See the [control audit](../../audits/2026-09-09-ob1-nw0-c1-client-isolation.md).

### INTEGRATION-005: Project instruction limits do not suppress Codex home instructions

Symptom: A Codex thread with no environments, no runtime roots, and zero project-document bytes
still reports the client-state `AGENTS.md` as an instruction source.

Cause: The `0.153.4` home-instruction provider loads global instructions independently of the
project-document byte setting. C2 also observed a denied host-skills discovery attempt with the
proposed skill-suppression flags. Empty hook/MCP inventories do not establish empty model context.

Fix: Test user-level and project-level canaries separately. Keep discovery effects, loaded
instructions, model tool catalogs, and runtime persistence as separate assertions. A future Codex
adapter needs verified runtime/auth separation or a supported completion interface; copying tokens
into an empty client-state directory is not an acceptable workaround. Codex subscription is deferred
under the user-authorized C2 scope change.

Discovered: 2026-09-09, synthetic NW0-C2 offline thread probe and pinned source inspection.
See the [C2 audit](../../audits/2026-09-09-ob1-nw0-c2-codex-preflight.md).

### INTEGRATION-006: Claude login preference and parent policy do not establish subscription isolation

Symptom: Claude Code `2.1.265` initializes with an API-key source despite
`forceLoginMethod: claudeai`. Valid parent policy retains permission denials but drops
`disableAllHooks`; a separate SDK policy resolver can return admin hooks and routing unchanged.

Cause: Login selection, active credential precedence, and managed policy are distinct controls.
Host-supplied policy is filtered and can be displaced by admin policy. The inspected resolver uses
an older bundled client version and does not execute policy helpers. A no-auth startup cannot
establish the account's effective policy.

Fix: Check active credential source and effective policy before note bytes. Reject missing,
stale, incompatible, or unverified policy; preserve organizational restrictions. Pair version-matched
observation with independent runtime containment and client-owned authentication. Include
session-discovery metadata in crash cleanup checks: persistence-off left such a file in C3,
although no synthetic context was retained in the tested startup/control paths.

Discovered: 2026-09-09, synthetic NW0-C3 offline Claude and policy-resolver probes.
See the [C3 audit](../../audits/2026-09-09-ob1-nw0-c3-claude-preflight.md).

### INTEGRATION-007: A matching settings snapshot is not a dispatch authorization

Symptom: A native settings read looks compatible, but does not establish complete policy,
subscription identity, an empty tool catalog, or runtime confinement. Settings can change after
the read. A replayed or fake permit can appear to fill those gaps unless its trust boundary is explicit.

Cause: Metadata observation, independent enforcement, and engine consent are separate inputs.
A timestamp records when metadata was observed; it does not establish remote-policy freshness.

Fix: Keep content out of client startup, reject missing controls, and bind any future authorization
to the request, client instance/version, accepted revisions, current policy generation, and lifetime.
Recheck before release and reject observed changes. A second read alone does not close the external
policy-change race. Keep fake-only witnesses out of native transports. C4's live rejection precedes
termination; its first post-stop iteration remains labeled as development evidence.

Discovered: 2026-09-09, NW0-C4 supervisor contract tests and native metadata rejection probes.
See the [C4 design and probe](../../audits/2026-09-09-ob1-nw0-c4-claude-supervisor.md).

### INTEGRATION-008: Local login metadata can succeed with a synthetic credential

Symptom: Claude's offline status reports a logged-in subscription from a nonfunctional synthetic
credential file. The initialization response carries a subscription label but lacks the source field
required by the prototype. Neither observation verifies a real subscription or authorizes content.

Cause: Local credential selection, server authentication, and same-process dispatch authority are
different checks. A settings/status response also does not prove that denied bootstrap writes worked.

Fix: Preserve those distinctions in adapter diagnostics and acceptance criteria. Keep native dispatch
closed without source/policy/runtime evidence. Test the existing official namespace and Keychain
behavior separately from file-backed fixtures; helper replacement writes do not prove native refresh.
Treat loader/system-data failures as runtime failures, and retain their evidence when refining a
file-data allowlist. A profile that prevents startup has not passed integration.

Discovered: 2026-09-09, synthetic NW0-C5 native runtime-layout and status probes.
See the [C5 runtime-layout proof](../../audits/2026-09-09-ob1-nw0-c5-native-runtime-layout.md).

### INTEGRATION-009: SDK lifecycle support does not supply fresh policy authorization

Symptom: The real SDK starts with held input and returns native metadata, but `accountInfo()` retains
the initial account response. Runtime JavaScript has a settings getter absent from public Query types.

Cause: A usable completion lifecycle, cached metadata, and a supported live policy contract are
different interfaces. An internal wire request type does not prove completeness or freshness.

Fix: Reuse the lifecycle with held asynchronous input and supervisor-owned rejection/cleanup. Treat
cached account data as an initial observation. Record missing settings as unknown and reject before
release; do not cast to an internal method to imply a supported contract. Keep native authorization
separate from fake-only release tests. On the tested Mac, use the proven sibling-process prototype
for further interface work: applying a nested sandbox from the confined SDK host failed.

Discovered: 2026-09-09, NW0-C6 real-SDK/fake-process contracts and native supervisor bridge.
See the [C6 interface check](../../audits/2026-09-09-ob1-nw0-c6-sdk-supervisor-interface.md).

### INTEGRATION-010: Frozen extraction can work while bounded artifact inspection is incomplete

Symptom: The pinned Graphify Markdown fixture executes in signed Mac binaries, but content auditing
finds upstream home-path examples and a tree-sitter binary match, then stops at the object budget.
The combined archive reports `artifact-invalid`; the separate helper reports `artifact-limit-exceeded`.

Cause: Installed dependency closure, exercised imports, compressed size, and inspection work are
different measurements. The combined candidate's container-length check fails with 499,997 objects
already decoded; the helper reaches 500,001. Collecting every dependency root adds unrelated modules
and resources. Even the narrow candidate's Markdown root lookup still imports broad Graphify code.

Fix: Preserve failed/incomplete audit results and inspect bounded diagnostics before interpreting
their labels. Keep the strongest separate-helper option visible, but audit that helper too. Pursue
a supported root-aware component boundary or a concrete reviewed dependency/content-policy change;
do not raise limits, waive findings, or remove validation just to obtain a green probe. Treat observed
findings as a lower bound until the entire artifact is inspected.

Discovered: 2026-09-09, NW0-B2 frozen Graphify builds and unchanged native content auditor.
See the [B2 packaging record](../../audits/2026-09-09-ob1-nw0-b2-frozen-graphify.md).

### INTEGRATION-011: A direct Markdown import can still load every language extractor

Symptom: Removing Markdown's root lookup back-edge reduces the frozen closure, but still leaves
36 Graphify modules and an incomplete content audit.

Cause: The extractor package initializer eagerly loads its language registry. Markdown also reaches
runtime discovery and path code through shared skip rules and sanitization. A per-file function name
does not establish a small import boundary. Separately, undeclared optional PyYAML changes frontmatter
behavior: the pinned closure drops nested metadata even though basic synthetic extraction passes.

Fix: Verify both runtime and frozen module inventories. B3's private prototype passes root context
explicitly, makes compatibility exports lazy, and moves unchanged shared logic into small modules.
Preserve source provenance and upstream tests when evaluating adoption; keep pre-existing failures
visible. A clean artifact audit cannot establish nested-frontmatter support or a supported dependency.

Discovered: 2026-09-09, NW0-B3 component, artifact and original/patched upstream test comparisons.
See the [B3 component record](../../audits/2026-09-09-ob1-nw0-b3-markdown-component.md).

### INTEGRATION-012: A YAML parser does not make sanitized metadata an alias catalog

Symptom: Adding PyYAML fixes nested metadata, but cyclic values raise recursion errors, duplicate
keys keep the last value, unquoted alias names become booleans, and Graphify escapes alias text.

Cause: Parser availability, bounded construction, property-specific types and display sanitization
are separate concerns. Graphify's string/list caps and HTML escaping are unsuitable for canonical
alias identity. The optional parser accelerator can also introduce a failing native-artifact path.

Fix: Pin and audit the actual parser profile. Preflight eligible snapshots before extraction;
reject duplicate/cyclic/oversized input and preserve literal alias names in the Engine-owned catalog.
Keep original source bytes and display metadata separate. Verify native content and startup limits
independently: B4's pure parser passes content inspection but still exceeds the warm budget.

Discovered: 2026-09-09, NW0-B4 parser, frontmatter, native-artifact and startup comparisons.
See the [B4 audit](../../audits/2026-09-09-ob1-nw0-b4-component-adoption.md).

### INTEGRATION-013: Portable validation and import do not prove workspace revision semantics

Symptom: A synthetic page with two known capture provenance references validates and imports,
but authoritative reconciliation rejects its provenance. An archived page's original source capture
remains searchable after import. Its canonical row is omitted, which also fails reconciliation.

Cause: Portable syntax, import success and active Engine behavior enforce different contracts.
The current reconciliation path requires one indexed capture reference. Existing archival status
does not implement managed-workspace deletion from search and graph. A2's active single-provenance
page and generic event carrier pass reconciliation, but embedded policy flags remain ordinary content.

Fix: Test capture, export, import, authoritative reconciliation and retrieval together before
selecting a same-page revision or inactive-state representation. Keep private lifecycle simulations
distinct from Portable compatibility evidence; do not infer that every v1 encoding is impossible
from one failed candidate. No shipping schema change was made by this probe.

Discovered: 2026-09-09, NW0-A real Engine compatibility cases in the
[parallel checkpoint](../../audits/2026-09-09-ob1-nw0-parallel-feasibility.md), isolated by the
[A2 comparison](../../audits/2026-09-09-ob1-nw0-a2-portable-representations.md).

### INTEGRATION-014: A native helper needs a distribution contract as well as a clean binary

Symptom: Both executables pass individual content inspection, but a two-executable bundle fails.

Cause: Generic archives have a smaller member cap; the native archive contract expects one
executable named `open-brain`. A helper also needs distinct asset identity, installation and updates.

Fix: Keep the auditor unchanged. Compare separately audited native resources with an explicitly
versioned layout. Verify resource naming, manifest/formula and installation before shipping.
Measure ordinary base status separately from helper execution; do not equate it with cold startup
or the full five-minute journey. Same-input archive comparisons must use the same gzip filename.

Discovered: 2026-09-09, [NW0-B5](../../audits/2026-09-09-ob1-nw0-a3-b5-feasibility.md).

### INTEGRATION-015: Disabling Homebrew API installation can trigger a shared core clone

Symptom: A local-only resource smoke begins cloning the core tap while indexing its private tap.

Cause: `HOMEBREW_NO_INSTALL_FROM_API` requests a local core checkout; it is not an offline switch.
Stopping the harness process group did not stop the observed orphan git clone automatically.

Fix: Omit that flag for the local resource proof. Keep auto-update disabled and cache/log/home state
isolated. On interruption, identify owned descendants and verify cleanup and product preservation.
Do not infer that a local archive URL confines every Homebrew side effect or that killing the parent
reaps all work. Preserve the failed receipt and charge its effort.

Discovered: 2026-09-09, [NW0-B6](../../audits/2026-09-09-ob1-nw0-b6-resource-install.md).

### INTEGRATION-016: Source authority can change inside adapter construction

Symptom: A private constructor accepts an edit after the handoff policy check. Stale synthetic input
reaches the loopback endpoint, even though later publication is rejected.

Cause: A reentrant coordinator lock serializes other threads but permits same-thread callbacks.

Fix: Revalidate authoritative body, revision, privacy and policy after construction and before the
trusted held-input start. Keep the failing regression; require zero requests, not only zero results.

Discovered: 2026-09-09, [NW0 private slice](../../audits/2026-09-09-ob1-nw0-private-vertical-slice.md).

### INTEGRATION-017: A normalized graph identifier is not evidence that a target exists

Symptom: A missing reference normalizes to the identifier of a different selected page.

Cause: Upstream identifiers can collide across missing and existing targets.

Fix: Join an existing-file target stamp to the eligible inventory. Reject unresolved references;
do not promote them using the normalized identifier alone. Test the actual frozen helper.

Discovered: 2026-09-09, [NW0 private slice](../../audits/2026-09-09-ob1-nw0-private-vertical-slice.md).


### INTEGRATION-018: Test-only native routes can bypass the public input contract

Symptom: A helper advertises selected-body extraction, but a bundled diagnostic accepts a directory
and reads fixture files directly. Reusing the base archive writer also gives the helper the wrong
executable name and leaves its distribution contract implicit.

Cause: Private diagnostic entrypoints and base-product packaging assumptions survive a proof's
move into reproducible CI support.

Fix: Keep filesystem diagnostics source-only and test rejection by the actual frozen executable.
Use an explicit helper envelope with a fixed executable name and bounded legal-file inventory.
B12 adds that narrow envelope to the native auditor; it does not widen the base one-member contract,
content exceptions, decompression limits, or generic archive member cap. Test both accepted envelopes
and missing, extra, duplicate, oversized, symbolic-link, and incorrectly named members.

Discovered: 2026-09-10, independent review of the
[B12 portable native proof](../../../tools/nw0_graphify_probe/README.md).

### INTEGRATION-019: A disposable desktop can depend on its live boot medium

Symptom: A preserved live guest needs new test assets, but its data image is fixed and only the
boot CD can be exchanged through the available controls.

Cause: Disposable changes are not persistent across shutdown, and the live filesystem may still
read its boot image. A paused VM is not a portable snapshot of the current desktop.

Fix: Preserve the running guest and its boot medium. Prepare a separate isolated test environment
with a distinct removable read-only data CD before boot. Verify actual saved configuration rather
than assuming a wizard click added the drive. Do not count the new environment as prior GUI evidence.

Discovered: 2026-09-10, [NW0 continuation](../../audits/2026-09-10-ob1-nw0-continuation.md).

### INTEGRATION-020: An asynchronous child API can still wait before returning its PID

Symptom: A parent starts its timeout before process creation but cannot cancel an already-created
child because the launch call has not returned the child's PID.

Cause: Python's fork-based `Popen` path waits on an exec-error pipe. Replacing it with direct
`posix_spawn` removes that Python read but does not establish early ownership on glibc Linux:
`CLONE_VFORK` suspends the caller until child exec or exit. API naming and normal fast-start samples
do not prove the controlled pre-exec-stall case.

Fix: Trace PID acquisition before relying on cancellation or cleanup claims. Test a controlled
child-side pre-exec stall, retain the original deadline, and confirm actual terminal wait rather
than eventual process disappearance. A native async-signal-safe fork/exec routine in the existing
owner is an unproved alternative, not an approved implementation. Verify each supported platform;
do not hide a concrete child-side wait inside a general scheduling assumption.

Discovered: 2026-09-10, independent NW0 launch review; see the
[Linux spawn](https://man7.org/linux/man-pages/man3/posix_spawn.3.html) and
[vfork semantics](https://man7.org/linux/man-pages/man2/vfork.2.html).

### INTEGRATION-021: Validated usage can disappear at a narrower result projection

Symptom: A provider response passes strict token-usage validation, but the final source-bound
operation result has no usage fields.

Cause: A reused graph-result projection keeps source evidence and model attribution but intentionally
omits transport metadata. Validation before that projection does not preserve the discarded value.

Fix: Attach only the normalized, validated usage to the bound result before its final encoding and
byte-limit check. Test all provider shapes through the actual authority return boundary, including
cache and reasoning counters. A parser-only test cannot prove the returned result retains them.

Discovered: 2026-09-11, [NW0 continuation](../../audits/2026-09-10-ob1-nw0-continuation.md).

### INTEGRATION-022: An explicit lint configuration can still depend on the working directory

Symptom: The same frozen sources and Ruff configuration pass in the candidate directory but report
different import-order findings from a receipt directory.

Cause: Selecting the configuration file does not also freeze default source roots used to classify
first-party imports.

Fix: Record or explicitly set the source roots as well as the binary, configuration and source
hashes for standalone proof checks. Preserve the failed check; do not rewrite frozen imports to
chase unrelated working directories or claim that test execution verified a later formatting edit.

Discovered: 2026-09-11, [NW0 continuation](../../audits/2026-09-10-ob1-nw0-continuation.md).

### INTEGRATION-023: A current dependency snapshot does not establish its expected contents

Symptom: An isolated proof accepts edited dependency files because it compares later reads only
with a baseline captured after extraction. Unlisted bytecode can also remain loadable.

Cause: Archive hashes bind downloaded archives but do not independently bind the extracted runtime.

Fix: Verify the extracted tree against a reviewed external digest before importing it. Reject
loadable bytecode, links and extra files. Bind the candidate, first-party source and dependencies
to the same expected manifest at preflight and suite execution.

Discovered: 2026-09-11, independent
[archived authority proof](../../../archive/open-brain-secure-node/proofs/nw0_authority_probe/README.md)
review.

### INTEGRATION-024: A zero exit status does not erase a supervisor failure

Symptom: Both test counts pass and the child exits zero, but the supervisor recorded a timeout or
truncated output. A summary checking only return code and cleanup still reports success.

Cause: The final receipt projection drops the supervisor's failure classification.

Fix: Require no supervisor failure, complete untruncated output, exact test counts and known
cleanup for every mode. Keep negative controls where a failed supervisor receipt also has exit zero.

Discovered: 2026-09-11, independent
[archived authority proof](../../../archive/open-brain-secure-node/proofs/nw0_authority_probe/README.md)
review.

### INTEGRATION-025: Path ordering and full-name ordering produce different tree digests

Symptom: Every extracted file matches its wheel member, but the expected dependency digest differs.

Cause: Sorting Path objects compares components. Sorting slash-separated strings compares the whole
name. A package directory and its sibling distribution metadata can therefore appear in a different
order even when their files and bytes match.

Fix: Define one ordering for the digest contract and use it when deriving external anchors. Retain
a fixture containing both `package/module.py` and `package-1.dist-info/METADATA`; comparing only a
flat directory misses the difference.

Discovered: 2026-09-11, Linux metadata build for the
[archived authority proof](../../../archive/open-brain-secure-node/proofs/nw0_authority_probe/README.md).

### INTEGRATION-026: Runtime proof jobs must cross the product boundary with their source

Symptom: CI fails before or during proof execution because an active proof still imports a runtime
API that a product-boundary change deliberately quarantined.

Cause: A merge preserves the proof workflow and harness from one parent while selecting the smaller
foreground source tree from the other parent. Updating source hashes makes the bundle build, but it
cannot restore the removed semantics.

Fix: Quarantine the obsolete proof with the removed runtime and delete its active CI, build, test,
and coordinator paths. Add a boundary test for those paths. Do not rebind the proof to archived
source or restore service APIs to make CI pass.

Discovered: 2026-09-12, [foreground runtime boundary](../../architecture/decisions/0016-foreground-runtime-package-boundary.md)
reconciliation with the
[archived authority proof](../../../archive/open-brain-secure-node/proofs/nw0_authority_probe/README.md).

### SCHEMA-001: Historical fixtures must remove every later migration surface

Symptom: A migration test lowers `user_version` and restores an older table, but the engine rejects
the database as an invalid schema instead of adopting it.

Cause: Tables from a newer migration remain in the database, so its declared version and physical
shape describe different eras.

Fix: When rematerializing a historical fixture, remove every table, index, trigger, and migration
row introduced after that era. Assert the final current version through the exported schema-version
constant rather than repeating its number in tests.

Discovered: 2026-09-12, NW1 managed-workspace migration verification.

### SCHEMA-002: Cache expected shapes by DDL and build only the requested era

Symptom: Full verification spends almost an hour below 20% completion while one pytest process
uses a CPU core rebuilding SQLite schemas.

Cause: Removing the expected-shape cache to handle historical catalogs leaves an eager dictionary
that constructs every known schema on each classification. Caching only era/flags is also unsafe
when a historical fixture substitutes a different migration catalog or schema definition.

Fix: Select the requested era before building its shape. Cache the immutable shape by the exact
ordered DDL tuple, including nullability and ledger statements. Keep actual database inspection
uncached. Test definition changes, historical catalogs, and one-build classification deterministically.

Discovered: 2026-10-01, saved-Markdown lifecycle verification after historical-cache repair.

### INTEGRATION-027: Homebrew can delete an untrusted local tap before smoke installation

Symptom: A contributor smoke creates a temporary local tap, but `brew tap` rejects the tap as
untrusted and removes its checkout before the formula can be installed.

Cause: Homebrew 6 requires tap or formula trust before it will load third-party formulae. Trusting
the formula through the failed tap flow cannot preserve the checkout needed for installation.

Fix: Clone the exact generated tap into the path returned by `brew --repository`, verify its
ownership marker and formula bytes, then install the fully qualified formula. Snapshot formula
trust before the run and verify that uninstalling and untapping remove the temporary trust entry.

Discovered: 2026-09-13, NW2 paired native Graphify Homebrew smoke.

### INTEGRATION-028: A path-derived graph target does not preserve permanent note identity

Symptom: A materialized `[[page_<uuid>]]` link remains unresolved even though that exact selected
page is present in the same Graphify snapshot.

Cause: Graphify derives a target identity from a hypothetical filesystem path. That derived value
is not the Engine's permanent page identity and cannot establish which selected page was intended.

Fix: Parse the bounded link token and bind an exact permanent page ID directly against the selected
snapshot inventory. Keep the existing-file stamp for ordinary path links, and retain genuinely
unresolved references as diagnostics.

Discovered: 2026-09-13, NW2 installed Graphify and Canvas acceptance.

### INTEGRATION-029: Provider consent and suggestion review need one plugin-owned engine lifetime

Symptom: Provider setup succeeds, but the following semantic refresh or suggestion review reports
inactive consent or loses the pending suggestion when every plugin operation starts a new CLI
process.

Cause: Engine startup recovery deliberately revokes active provider consent and invalidates pending
inference work. A stateless one-process-per-operation bridge therefore crosses a recovery boundary
between setup, refresh, review, and acceptance.

Fix: Keep one bounded stdio child and one engine session for the Obsidian plugin lifetime. Store
session-only credentials, provider selection, and request budgets only in that process. Stop the
child on plugin unload; after a bridge restart, require provider setup again while preserving local
structural results. Test multiple framed requests through one child and terminal cleanup. This
remains a foreground, lifecycle-owned process rather than a daemon.

Discovered: 2026-09-13, NW3 Obsidian plugin integration.

### INTEGRATION-030: Client parsers must accept every bounded backend status

Symptom: The Obsidian plugin activates and the backend returns a valid failure Canvas, but the UI
reports an invalid response instead of showing the bounded failure card.

Cause: The backend's graph projection contract includes `failed`, while the TypeScript client union
and parser listed only `fresh`, `missing`, and `stale`. Happy-path mocks never crossed the initial
empty-vault failure boundary.

Fix: Keep client status unions synchronized with the backend response contract and add a parser
fixture for every terminal status, including safe failure projections. Exercise initial activation
against an empty synthetic vault so a renderer-facing contract mismatch cannot hide behind a fresh
projection.

Discovered: 2026-09-13, [NW3 Obsidian plugin audit](../../audits/2026-09-13-ob1-nw3-obsidian-plugin-audit.md).

### INTEGRATION-031: Generated views need stable leaf reuse

Symptom: Each graph refresh opens another `Open Brain Graph` tab, and the user can remain on an
older Canvas even though the generated file on disk is current.

Cause: Calling `workspace.getLeaf("tab")` for every publication always allocates a new tab. The
write and the visible leaf then have separate lifecycles.

Fix: Find the existing Canvas leaf by its exact generated file path, reopen the file in that leaf,
and reveal it. Allocate a new tab only when no matching leaf exists. Test both branches and verify
the before/after tab count in Obsidian.

Discovered: 2026-09-13, [NW3 Obsidian plugin audit](../../audits/2026-09-13-ob1-nw3-obsidian-plugin-audit.md).

### INTEGRATION-032: Archive reproducibility does not imply frozen-build reproducibility

Symptom: Two clean macOS builds from identical tracked source produce different executable, archive,
and component-manifest SHA-256 values, even though rebuilding an archive from either fixed executable
is byte-for-byte stable.

Cause: The current contract normalizes tar and gzip metadata around a completed PyInstaller
executable. It does not make independent PyInstaller and ad hoc signing runs deterministic.

Fix: Select one exact candidate build, preserve its paired archives, and bind those exact bytes in the
component manifest and release formula. Do not substitute an independently rebuilt artifact under an
already reviewed digest. Treat end-to-end reproducible frozen builds as a separate requirement with
its own toolchain proof.

Discovered: 2026-09-13, NW4 exact-commit macOS candidate preparation.

### INTEGRATION-033: `uv run --frozen` can hide a stale dependency lock

Symptom: CI stays green after raising dependency minimums in `pyproject.toml`, but `uv lock --check`
reports that `uv.lock` needs to be updated and the locked tools remain below those minimums.

Cause: `uv run --frozen` uses the existing lock without checking whether project metadata would
change its resolution. It proves the old lock still runs, not that the lock matches the edited
requirements.

Fix: Regenerate `uv.lock` whenever dependency requirements change, then run `uv lock --check` before
the normal frozen test commands. Review the resolved direct and transitive dependency changes.

Discovered: 2026-09-13, Dependabot Python development dependency review.

### INTEGRATION-034: Frozen proof dependencies have multiple version bindings

Symptom: Dependabot updates a hashed proof requirement, but the proof fails before testing because
its wheel inventory and executable payload still expect the previous version.

Cause: The proof binds each reviewed dependency across `requirements.txt`, the platform wheel
inventory, the payload's installed-version assertion, human-readable documentation, and the source
manifest. Updating only the requirement creates an intentionally rejected mixed evidence set.

Fix: Treat a frozen proof dependency update as one atomic evidence refresh. Inspect the selected
wheel metadata for every target, verify license bytes, update every version binding, regenerate the
source hashes, and execute the proof on each available platform before accepting CI evidence.

Discovered: 2026-09-13, NW0 async API proof cryptography 50.0.0 review.

### INTEGRATION-035: A Vitest major can raise hidden build-tool requirements

Symptom: A Vitest-only Dependabot update produces an npm resolution failure before tests run,
because Vitest's peer Vite selects a release that rejects the project's directly pinned esbuild.

Cause: Vitest, Vite, and esbuild form one development toolchain even when only Vitest is declared
directly. Vitest 5 also narrows its supported Node releases beyond a broad `>=22` package engine.

Fix: Review the complete peer dependency graph for major test-runner updates. Upgrade the directly
pinned build tool to a supported version, align the declared Node engine with the test runner, then
verify a clean install, type check, production bundle, and test run together.

Discovered: 2026-09-13, Obsidian plugin Vitest 5 dependency review.

### INTEGRATION-036: Engine opening is not evidence that another client crashed

Symptom: Starting a second foreground client revokes consent and invalidates inference belonging
to a healthy client. A first attempt at a session registry can also lose crash evidence by deleting
a marker before recovery commits.

Cause: Startup recovery treated every engine opener as the start of a new exclusive lifetime.
Separating the liveness check from recovery then admitted competing clients during that transition.

Fix: Register participating client lifetimes with lock-held markers, serialize admission and recovery,
and preserve a durable marker until recovery succeeds. Test delayed admission, partial metadata
writes, and process death during final shutdown. Older clients that lack this protocol require an
explicit compatibility gate; a shared product-version string does not make them participants.

Discovered: 2026-09-14, [desktop D0 verification](../../audits/2026-09-14-desktop-d0.md).

### INTEGRATION-037: A native wrapper needs its own startup and process-group proof

Symptom: A compiled desktop window reports a handshake timeout, cannot discover its bundled helper,
or leaves a descendant alive after the immediate child exits.

Cause: The measured frozen-core startup took 5.457 seconds, exceeding a three-second handshake
deadline. Side-by-side executables did not match the core's `bin`/`libexec` helper layout. Waiting
only for the immediate child missed surviving process-group members.

Fix: Give cold startup a separate bounded allowance, preserve the runtime's expected packaged
layout, and exercise the actual helper. Reap and terminate the whole process group, close child
admission before app shutdown, and verify the result in the native app. Keep writable test data
outside the app bundle and canonicalize temporary paths before strict core path validation.
Use a permanent page ID for the helper fixture's reference; an unresolved title reference can
produce a valid helper response with zero links and must not pass the integration proof.

Discovered: 2026-09-14, [desktop D0 verification](../../audits/2026-09-14-desktop-d0.md).

### INTEGRATION-038: Owned setup blocks must own their separators

Symptom: Removing a generated agent instruction block trims unrelated blank lines, or an atomic
replacement overwrites an edit made while its temporary file was being prepared.

Cause: Cleanup normalizes the surrounding document, and a preview-time preimage check does not cover
file preparation. Opening a FIFO before checking its type can also block setup indefinitely.

Fix: Include the leading separator in the owned block and preserve all outside bytes. Check the
expected preimage immediately before replacement, condition rollback on the current contents, and
open candidate files nonblocking before validating regular-file ownership. Test exact trailing bytes,
concurrent edits, partial setup recovery, and a FIFO with a bounded subprocess.

Discovered: 2026-09-14, desktop D1 setup verification.

### INTEGRATION-039: MCP discovery does not prove current-client tool calls

Symptom: Claude Code lists Open Brain tools, but every search and capture returns `invalid params`.

Cause: The client sends `params._meta` with `claudecode/toolUseId` and `progressToken`. The original
transport required exactly `name` and `arguments`, rejecting valid MCP metadata. Handwritten smoke
requests did not contain it.

Fix: Accept object-valued request metadata as transport context and never pass it to the adapter as
tool input or authority. Keep argument validation and capability checks unchanged. Test both valid
and malformed metadata, then exercise actual client tool calls against the exact native candidate.
See the [MCP metadata contract](https://modelcontextprotocol.io/specification/2025-11-25/basic#_meta).

Discovered: 2026-09-14, desktop D1 actual Claude Code acceptance.

### INTEGRATION-040: Patching hardlinked dependencies can corrupt the build cache

Symptom: The first Graphify native build passes, but the next build rejects the pinned
upstream source preimage before applying the maintained patch.

Cause: uv can hardlink installed files to its wheel cache. Writing a patch over that
installed file changes the cached inode as well. A fresh stage then receives patched
bytes instead of the original pinned wheel contents.

Fix: Both Graphify staging installers use `--link-mode copy` before applying patches.
Keep the original and candidate digest checks unchanged. Verify two successive installs
and patch applications against one isolated cache, including when the environment requests
hardlink mode. An already modified private cache must be replaced before retrying.

Discovered: 2026-09-16, repeated Linux native builds in the desktop contributor check.

### INTEGRATION-041: A cleanup permission error must not skip child reaping

Symptom: The Graphify build supervisor raises `PermissionError` during group cleanup
instead of reporting a bounded build failure, leaving its pipe close and child wait unrun.

Cause: Its cleanup handler only catches a missing process group. macOS CI also returned
a permission error when signaling a group whose short-lived leader was exiting.

Fix: Treat denied cleanup as a build failure, then close the owned pipe and wait for the
direct child with the existing timeout. Never report success when group cleanup is
unverified. A real subprocess regression injects signal denial and confirms the child
has been reaped while the operation still fails.

Discovered: 2026-09-16, macOS Graphify build-supervisor overflow test.

### INTEGRATION-042: Fixture startup is not the bridge deadline under test

Symptom: macOS bridge tests time out before their Python fixtures create a descendant
or answer a handshake, although the process-cleanup implementation passes locally.

Cause: Cold interpreter startup competes with parallel tests and can exceed a two- or
three-second fixture budget. The graceful-exit fixture also announced its descendant before that
process installed its signal handler.

Fix: Wait up to ten seconds for a complete, live fixture PID before exercising the
operation deadline. Have the signal-ignoring descendant publish its own readiness after
installing its handler. Preserve the 100 ms operation deadline and existing cleanup bounds;
continue asserting that the descendant is gone after shutdown.

The message-framing fixture also publishes a live PID after its imports. Wait for that
receipt before the three-second handshake and one-request session-exhaustion assertions.
A controlled 3.2-second startup delay reproduces the timeout without the readiness wait
and passes with it; the operation deadline remains three seconds.

Discovered: 2026-09-16, macOS desktop bridge contributor checks.

### DOCUMENT-001: PDF extraction can invoke an ambient image decoder

Symptom: A malformed PDF content stream can select pypdf's JBIG2 filter, which discovers
and invokes a host-installed decoder even when the caller only requested text.

Cause: Text extraction parses content streams through the shared PDF filter machinery.
Avoiding the public image API does not disable every decoder.

Fix: Explicitly set `jbig2dec_binary=None` in the parser's configuration, bound each
decompression family, and run parsing in a timed child with a minimal environment.
Keep parser dependencies out of the default app and native runtime.

Discovered: 2026-09-15, D5.1 selected-file extraction.

### CALENDAR-001: Incremental sync cannot repeat the initial time-range filter

Symptom: Calendar updates fail with an invalid request, leave moved events searchable,
or retain an old description after cancellation.

Cause: Google disallows `timeMin` and `timeMax` with `syncToken`. Incremental responses
can include changes outside the originally selected range. A cancellation may contain
only the event ID, and the engine needs the original source URL to replace the active
capture.

Fix: Preserve the fixed selection in private state, omit incompatible request filters,
and apply the range locally. Remember accepted event identities and their first source
links so cancellations and moved events can produce status-only replacement captures.
On an expired token, finish a bounded full snapshot before reconciling missing events.
Advance the checkpoint only after every capture succeeds; retain immutable history.

Discovered: 2026-09-15, Google Calendar provider and engine integration.

### SOURCE-LIVE-001: Keychain CLI success does not prove an intact token document

Symptom: A Keychain command exits successfully but stores an empty value, or an interactive
`security` command truncates a larger JSON document.

Cause: The `security` command's password and interactive input modes do not provide a reliable
bounded stdin channel for arbitrary credential JSON. Putting the value in argv exposes it.

Fix: Use Security.framework inside a bounded helper and send credential JSON through stdin.
Verify a synthetic large document by reading it back exactly, replacing it, and deleting only
the test entry. Keep both credentials and helper diagnostics out of public output.

Discovered: 2026-09-16, priority-source native Keychain acceptance.

### SOURCE-LIVE-002: Concurrent first queue opens can lose a hook event

Symptom: One of 32 simultaneous session events returns false before writing its metadata.

Cause: On the tested macOS filesystem, concurrent `openat` calls for the new queue lock
occasionally returned `ENOENT` despite `O_CREAT` and a held parent directory descriptor.
The hook's best-effort error boundary hid that failure from the native client.

Fix: Retry only this transient lock-open failure within the existing acquisition deadline.
Keep no-follow, nonblocking opens and owner/mode checks. Test both a raced first open and
deadline exhaustion, then exercise concurrent fresh queues. The repaired diagnostic retained
all 960 events across 30 independent queues.

Discovered: 2026-09-16, final priority-source verification on macOS.

### SOURCE-LIVE-003: Google Desktop token exchange can require the client secret

Symptom: Browser consent completes, but connecting the account fails at Google's token endpoint.

Cause: The downloaded Desktop client included `client_secret`, while Open Brain read and sent
only its client ID. PKCE does not remove this client's requirement. A bounded diagnostic using
an intentionally invalid grant returned `invalid_request` for the missing field; including the
configured field changed the response to the expected `invalid_grant`.

Fix: Preserve the optional Desktop credential from the local configuration, send it in token
exchange and refresh request bodies, and retain it only with refresh material in the OS store.
Test both Google sources, process restart, and exclusion from URLs and account metadata.

Discovered: 2026-09-16, actual Google development-account sign-in.

### SOURCE-LIVE-004: Codex exec can cancel an asynchronous Stop capture hook

Symptom: A successful native Codex turn leaves no Open Brain queue event.

Cause: Codex cancels unfinished background hooks when the session ends. In a bounded
Codex CLI 0.154.0 check, the asynchronous Stop hook lost its event after `codex exec`
returned. Running the same enqueue synchronously produced the event before exit.

Fix: Generate a synchronous Stop command with a two-second host timeout for both clients.
Only metadata enqueue runs in the hook; parsing and import remain in the collector.
Reapplying setup upgrades the owned Codex entry while preserving unrelated hooks.
Exercise the generated command in a subprocess and verify a fresh native Codex turn.

Discovered: 2026-09-16, isolated native Codex automatic-capture acceptance.

### SOURCE-LIVE-005: Codex user-role records can contain generated context

Symptom: A synthetic native turn is quarantined because plugin identifiers in a user-role
record match credential-shaped strings. That record was never a user's message.

Cause: Codex CLI 0.154.0 puts plugin recommendations, injected instructions and environment
context in user-role `response_item` records. Per-part `content_item_kinds` metadata distinguishes
that context from `user.text`; checking only the role and `input_text` type is insufficient.

Fix: Select only `user.text` parts when the metadata is present and reject malformed alignment.
Keep the credential scan on the selected conversation text. Cover mixed context/user records,
unknown kinds and actual user secrets in both transcript and extractive-summary tests.

Discovered: 2026-09-16, isolated native Codex transcript import.

### SOURCE-LIVE-006: Search polling can overlap a collector write

Symptom: A live acceptance harness fails a search while the background collector successfully
captures the new session revision. A later search returns the expected single active result.

Cause: The CLI reports contention as exit code 75 with `error.code: database_busy`. The
harness treated that retryable response as a permanent failure and discarded its error code.

Fix: Preserve private failure diagnostics and retry only that exit-code/error-code pair within
a bounded deadline. Continue to fail on other errors or deadline exhaustion. Verify the final
capture identity and drained queue; a busy response alone does not establish successful capture.
The native harness uses a five-second retry budget, and the existing cross-process CLI/MCP
contention test checks this contract and SQLite integrity after the writer releases its lock.

Discovered: 2026-09-16, native Claude Code background capture acceptance.

### SOURCE-LIVE-007: A disposable Brain does not isolate Google grant revocation

Symptom: Revoking a grant used by a disposable acceptance Brain also requires the owner's Gmail
and Drive connections to sign in again.

Cause: Keychain references isolate local storage, but copied refresh material still represents
the same upstream grant. Google's revocation applies across scopes and clients in that project.
Deleting a local Open Brain connection and revoking the Google grant are different operations.

Fix: Use an owner-authorized development project, record which existing connections will be
affected, and restore their equivalent read-only grants after the check. Verify fresh-process
reads and refresh requests before removing test credentials. A browser callback error page alone
does not establish failure; check the completed connect operation and a real provider read.

Discovered: 2026-09-16, live Google edit/removal/revocation acceptance.

### SOURCE-LIVE-008: Unlink and recreate does not guarantee a different inode

Symptom: The transcript inode-swap contract test passes on macOS but imports the
replacement on Linux instead of reporting `session_transcript_changed`.

Cause: The fixture deletes the original before creating its replacement. A filesystem
may immediately reuse the freed inode, so the fixture does not reliably exercise the
device/inode mismatch check.

Fix: Create the replacement while the original still exists, assert their inode numbers
differ, and atomically replace the original path. Keep both the empty-intake and changed-
transcript assertions. This verifies rejection of a changed inode without assuming
deleted inode numbers cannot be reused.

Discovered: 2026-09-16, Linux contributor CI for the desktop and priority capture PR.

### SOURCE-LIVE-009: A generic HTTP test client can initialize TLS on loopback

Symptom: A synthetic HTTP-only OAuth callback can spend its deadline initializing
the system TLS certificate context before sending its local request.

Cause: Python's first `urllib.request.urlopen` builds an HTTPS handler and loads its
default certificate context even for an HTTP-only callback. A controlled slow-context
probe reproduced the same authorization timeout before any callback reached the server.

Fix: Use `HTTPConnection` for the synthetic numeric-loopback callback and reject TLS
initialization in the successful connection test. Keep the real listener, wrong-state
rejection, PKCE assertions, and existing timeout limits. The controlled probe then passes
without creating a TLS context. Production provider HTTPS transport is unchanged.

Discovered: 2026-09-16, macOS contributor CI for the desktop and priority capture PR.

### SOURCE-LIVE-010: HTTPServer resolves a hostname while binding numeric loopback

Symptom: The first Calendar OAuth callback times out before its listener loop can
process a request, even when the callback client performs no TLS setup.

Cause: `HTTPServer.server_bind` calls `socket.getfqdn` after binding. The authorization
deadline already exists, so a slow reverse lookup for `127.0.0.1` can consume it. A delayed-
lookup probe reproduced the timeout with the direct HTTP callback fixture.

Fix: Bind the numeric loopback socket through `TCPServer.server_bind` and retain its
numeric server name and port. Reject reverse-DNS calls in the successful authorization
test. The same delayed-lookup probe then completes without changing timeout, PKCE,
state, Host-header, or provider HTTPS checks.

Discovered: 2026-09-16, repeated macOS Calendar OAuth acceptance-test failure.

### ORGANIZATION-001: Routing does not publish a captured note

Symptom: An assigned capture remains under source storage and no canonical note appears in its
space's notes directory.

Cause: Routing records assignment history and updates search membership. Publication is a separate
owner operation. Routing also protects that capture from replacement by automated source revisions.

Fix: Describe this distinction in CLI/MCP guidance and preserve capture bytes, identity, and trust
in workflow tests. Keep changing source items unassigned when automatic revisions must continue.

Discovered: 2026-09-16, spaces/inbox installation feedback.

### ORGANIZATION-002: Validate space names before durable reservation

Symptom: A name containing an escape control character passes the engine text-length check but
fails Markdown frontmatter rendering after the space operation is reserved.

Cause: General text validation permits characters that deterministic frontmatter rejects.

Fix: The shared CLI/MCP organization validator rejects unsafe control characters before opening the
Brain or acquiring an operation delivery. Test both invalid input and terminal-safe display.

Discovered: 2026-09-16, synthetic CLI organization checks.

### ORGANIZATION-003: Inbox projections must redact before truncating and fit the wire

Symptom: A preview leaks a protected reference prefix, or a valid Unicode page exceeds MCP's message
limit. An exhausted output budget can also report failure after committing an organization write.

Cause: Slicing before projection removes the complete literal needed for redaction. MCP sends both
structured data and an ASCII-escaped text copy, so counting UTF-8 payload bytes understates wire size.
Checking a write's output budget afterward is too late to prevent its side effect.

Fix: Protect the full source reference across title, reason, and preview before slicing. Bound page
JSON bytes and continue from the actual number returned. Reserve the bounded write-receipt capacity
before invoking a mutation. Regression tests cover long references, depleted write budgets, and
complete pagination of 100 Unicode-heavy captures through the real MCP serializer.

Discovered: 2026-09-16, independent organization review and synthetic reproductions.
### OBSIDIAN-001: Commands reuse the session opened during plugin startup

Symptom: Paged search and publication stop with invalid_arguments before opening their forms.

Cause: Startup calls workspace.status, which admits a persistent Brain session. Later commands
call brain.initialize on that same child. Tests with a fresh child per operation miss this order.

Fix: Treat a valid initialization request on an already admitted session as already_initialized.
Do not enter bootstrap again or bypass argument validation. Exercise startup status, repeated
initialization, capture, and search through one real stdio session.

Discovered: 2026-09-18, synthetic Obsidian acceptance and a persistent-bridge regression.

### OBSIDIAN-002: Renderer timers may return numbers

Symptom: Disabling the plugin reports failure even though its bridge processes exit.

Cause: Bridge disposal sends termination before calling unref on its force-kill timer.
Electron renderer timers can return a number, which has no Node unref method.

Fix: Call unref only when the timer provides it. Preserve the delayed force-kill fallback.
Test disposal with a numeric timer handle as well as normal Node timers.

Discovered: 2026-09-18, synthetic Obsidian acceptance and a numeric-timer regression.

### MCP-STARTUP-001: Retrieval tests should admit sessions before comparing cursors

Symptom: A test for cross-session cursor rejection intermittently receives database_busy while
starting its second MCP child.

Cause: Both children begin bootstrap at once. Initialization can require a write even when each
session later has only retrieval grants. The startup race obscures the cursor behavior under test.

Fix: Wait for the first child's initialization response before spawning the second. Retain both
live sessions for cursor isolation and grant checks, and clean up the first if second startup fails.
Use the dedicated contention tests to exercise retryable startup failures.

Discovered: 2026-09-18, full M2 verification; the isolated retrieval recheck passed.

### MCP-002: Paged search projections change after organization writes

Symptom: A native MCP smoke test finds the same capture after routing but rejects the later search
response as unequal to the earlier response.

Cause: The negotiated search projection includes current organization state such as `space_id`.
Routing correctly changes that projection even though the record identity, trust, and provenance
remain stable.

Fix: Across organization writes, compare stable record identity and safety fields, then assert the
expected new organization state. Do not require byte-for-byte equality with the pre-write page.

Discovered: 2026-09-22, final native session-authority contributor check.

### DESKTOP-001: Bridge tests with fixed two-second deadlines fail under load

Symptom: `make verify` or `make desktop-test` fails in the Rust bridge tests with `DeadlineExceeded`
where `MalformedResponse` or a successful probe was expected, most often
`t03_nested_duplicates_close_the_actual_bridge` and
`graphify_probe_executes_one_bounded_structural_operation`. Each test passes alone.

Cause: those tests spawn a Python child and give it a fixed two-second deadline. Cargo runs the
26 bridge tests in parallel, so under an already loaded machine (load average above 10 on the
development Mac while other suites run) child startup can miss the deadline before the child ever
writes its malformed reply.

Fix: the contributor check runs the Rust suite with one test thread. These tests exercise process
lifecycle and protocol behavior, not latency, so resource contention between fixtures should not
decide the result. A branch is suspect when the relevant test also fails alone.

Discovered: 2026-09-21, twice during the OSS-G5 release gate while GLM workers ran pytest.

### DESKTOP-002: Tauri resources cannot carry the one-folder base, so desktop runtime targets fail

Category: Desktop / Packaging.

Symptom: since the base became a one-folder build (T5.1), `make desktop-runtime`, `make
desktop-native` and therefore `make contributor-check` fail. `prepare-runtime.mjs` reports "the
one-folder base cannot be packaged as a Tauri resource".

Cause: Tauri walks resources without following links and copies each entry with `fs::copy`. File
symlinks would become duplicate files, and directory links (`Python.framework/Versions/Current`)
cannot be copied at all. The spec no longer builds a one-file base, so the desktop has no core to
package.

Resolved (T5.3, D18): `prepare-runtime.mjs` ships a copy of the one-folder base at
`runtime/libexec/open-brain/` in which every symlink is a regular copy of its resolved target
(directory links become directory copies). Link rules match the base inventory: relative, contained,
at most 4 hops, no cycles. It hashes every source member, verifies each output file against the member
it came from, and fails closed on any extra, missing, mismatched or symlinked output. The members
and hashes come from the released base archive (hashed against the manifest's `archiveSha256`), not
from the build folder, so a stale or edited `dist/` is refused. Links must stay below `_internal/`,
as in the release audit. File modes are normalized to owner-only (0700 executable, 0600 other), not
copied from the archive; only the executable bit is verified. The Rust host reads the core at `runtime/libexec/open-brain/open-brain`.
Keep new base members free of links the resolver refuses.

Discovered: 2026-10-17, T5.1. Resolved: 2026-10-18, T5.3.

### OBSIDIAN-003: Untrusted previews need display projection, not inbox rejection

Symptom: One capture with terminal control characters prevents the entire publication capture
list from opening, including unrelated valid captures.

Cause: The inbox client applied a rendered-text restriction to valid untrusted preview data.

Fix: Keep the original nonempty, NUL-free preview for selection and draft generation. Escape C0
controls as visible Unicode sequences only in the capture-choice label. Keep protocol, identifier,
title, and canonical Markdown validation strict. Raw control-bearing drafts still require editing.

Discovered: 2026-09-18, synthetic Obsidian publication acceptance with the S07 hostile fixture.

### OBSIDIAN-004: Suggestion selection closes before delivering its value

Symptom: Choosing an existing space or Create a new space silently cancels publication.

Cause: Obsidian calls close before onChooseItem. Resolving cancellation synchronously in onClose
wins the promise before the selected value arrives. Tests that call onChooseItem directly miss it.

Fix: Defer cancellation to a microtask so the synchronous choice callback can settle first.
Keep Escape cancellation and single settlement. Exercise the real close-before-choice ordering
through the publication command for existing-space, new-space, and cancellation paths.

Discovered: 2026-09-18, Obsidian 1.13.7 GUI acceptance and installed callback-order inspection.

### OBSIDIAN-005: External note creation registers asynchronously

Symptom: Publication succeeds and the exact managed note exists on disk, but opening it immediately
reports source_unavailable. The same note becomes available in Obsidian later.

Cause: Native workspace refresh writes the canonical note outside Obsidian's Vault API. Obsidian's
filesystem watcher registers that file after the bridge response, so an immediate synchronous vault
lookup can return null.

Fix: Guard the full open operation with plugin lifecycle state before and after managed-vault
validation and again before opening. Then check the normalized path, subscribe to the public vault
create event, and check again to close the lookup-to-listener race. Bound the wait and remove its
listener and timer on success, timeout, or plugin unload. Never bypass the vault to open the file.

Discovered: 2026-09-18, Obsidian 1.13.7 GUI publication acceptance and delayed Quick Switcher proof.

### OBSIDIAN-006: Wait on the registration event, not a one-second polling window

Symptom: the managed-file unload test reports that no vault listener was registered, while the same
test passes immediately when run alone.

Cause: the test waits up to one second for a real child bridge to start, handshake, validate the
vault, and register the listener. Machine load can consume that window before the behavior under
test starts.

Fix: have the test harness expose a promise resolved by listener registration. Await that event and
let the test runner's overall timeout catch a bridge that never reaches it.

Discovered: 2026-09-23, CUT-G2 full release verification on the development Mac.

### MCP-004: External adapter composition must omit local callbacks

Symptom: External tool discovery and dispatch deny local administration, but the adapter still
contains legacy capture, review, workspace and graph callbacks when every CLI flag is enabled.

Cause: Callback attachment depended on feature flags rather than the trusted session egress mode.
Unknown-tool responses alone did not prove that external composition was closed.

Fix: Attach those callbacks only in owner-local mode. Preserve authority-bearing typed reads and
destination-bound capture submit. Test the actual entrypoint object and a real external stdio
process with all flags, including positive public search and negative administration calls.

Discovered: 2026-10-02, Phase C external composition regression.

### TESTING-002: Fixed enqueue times can expire during later test runs

Symptom: Outbox drain tests report `age_exhausted`, or a lease-holder test never reaches its
transport callback, even though the transport and lease code did not change.

Cause: The fixture uses a fixed enqueue timestamp with a 24-hour retry-age limit, while the drain
uses the real clock. The test changes meaning once wall time crosses that boundary.

Fix: Pass a deterministic clock when a test depends on fixed timestamps. Tests that exercise real
subprocess concurrency should enqueue at the current time unless the age boundary is the behavior
under test.

Discovered: 2026-09-22, full durable-ingestion Phase 1 verification after the fixture's retry window
expired.

### TESTING-004: Parallel test workers must collect identical test IDs

Symptom: `make test` under pytest-xdist stops at collection with "Different
tests were collected between gw1 and gw5" before any test runs.

Cause: A test parametrized over a `frozenset`. Set iteration order depends on
the per-process string hash seed, so each worker collected the same IDs in a
different order. Serial runs never notice.

Fix: Parametrize over `sorted(...)` or another deterministic sequence. Check by
hashing `pytest --collect-only -q` output under several `PYTHONHASHSEED`
values. A test that forks inside a worker should use the `spawn` start method:
xdist workers are multi-threaded, and fork from a threaded process can
deadlock the child. Positive liveness waits (`join`, `wait` on an event that
must fire) need generous bounds because parallel load slows threads; keep
negative "must not happen yet" windows short. Parallel runs also multiply
temporary disk use: with little free space the engine's storage watermark
refuses captures and many unrelated tests fail with `storage_high` below the
configured 2 GiB high watermark, or `storage_critical` below the critical one.
Check free disk and retained pytest storage before reading those failures as
regressions. Leave headroom for the complete suite and subsequent native build;
do not weaken admission watermarks to make tests pass. Preserve diagnostics and
recovery inputs before clearing disposable stopped test fixtures.

Starting a pause thread does not establish that cancellation beat collector
completion. The capture guard blocks pause during a sink submission. For the
pause-before-completion interleaving, synchronize at the outcome boundary after
submission releases the guard, wait for the actual pause acknowledgement, then
record the outcome. Preserve the failed-run and durable paused-state assertions.

Discovered: 2026-10-04, enabling pytest-xdist for the full suite.

### SHARING-005: Pre-query canonical eligibility must match the full projector

Symptom: A valid externally eligible legacy unbound publication is directly
readable but absent from search. A malformed retained member set can also be
withheld from final output while its metadata still changes an eligible cursor.

Cause: SQL resolved only bound review/capture heads, omitting the supported
unique retained-byte legacy fallback. Matching a current publication alone also
does not prove that its indexed members match the retained publication bytes.

Fix: Use the projector's current-publication resolver in the snapshot-bound SQL
callback, then validate that exact current canonical projection before candidate
ranking and authorized-generation hashing. Keep exact current member eligibility;
do not remove the nonempty-membership guard or authorize all historical members.

Tests: `test_imported_public_legacy_unbound_page_is_searchable_when_directly_readable`
and `test_legacy_current_publication_damage_refuses_without_hidden_cursor_influence`
in `packages/app/tests/integration/engine/test_sharing_surfaces.py` cover supported
Portable1 import, nonempty continuations, reopen/rebuild, ambiguous or missing
publication custody, denied secondary members and hidden-metadata stability.

Discovered: 2026-10-03, synthetic checkpoint C legacy import/cursor regressions.

### COLLECTOR-003: Owner-section removal needs real Markdown block boundaries

Symptom: A heading quoted inside a fenced block in `Why Saved` ends the owner
section early, leaving later owner notes in the transformed source body.

Cause: Heading and fence regular expressions did not implement Markdown block
grammar. Thematic breaks after blank lines or list items looked like setext
headings. Invalid fence openers and non-ASCII closing whitespace also changed
the skip state incorrectly.

Fix: Use the connector's pinned CommonMark parser to locate true top-level
headings and remove source-line ranges, without rendering retained content.
Parse inline syntax only for heading labels, not discarded body text.
Pass the document reference environment to heading parsing; project image-alt
children, text/code and breaks deliberately. Transparent attribute-free HTML
wrappers/comments are recognized without rendering. Attribute-bearing,
executable or unknown HTML refuses intake instead of hiding an owner heading.
Classify declaration tokens before HTMLParser: its unknown declarations become
bogus comments, bypassing declaration callbacks. Only genuine comment syntax
may use the transparent comment projection.
Continuous normalization version 2 records the changed transform contract;
retain version-1 evidence unchanged. The parser dependency stays in the optional
connector package, outside the foreground engine.

Tests: Saved Markdown contract cases cover ATX/setext and marked-up headings,
entities, multiline labels, thematic breaks, nested blocks, backtick/tilde
fences and invalid fence markers, full/collapsed/shortcut references, image alt
text, HTML wrappers/comments/breaks and unsupported-markup refusal.
Actual collector publication/Portable recovery
tests assert owner canaries are absent. A pending version-1 envelope survives
restart unchanged; the same item's version-2 observation requires new approval
and invalidates the old public copy. Historical fixtures remain unchanged.

Discovered: 2026-10-03, private source preflight and independent synthetic probe.

### PORTABLE-006: Test Portable-first imports in an empty interpreter

Symptom: Portable validation works in pytest but importing its version dispatcher
in a clean process fails with a partially initialized module.

Cause: A format module imports engine reconciliation types at module scope. The
engine package initializer imports the local runtime, which imports that format
module again. Existing pytest imports can hide the cycle.

Fix: Keep runtime engine imports inside the format's validation/codec functions.
Use TYPE_CHECKING for annotations. Do not weaken validation or alter package
initialization to make the cycle disappear.

Test: `test_portable_first_import_in_clean_interpreter_has_no_engine_cycle` runs
the version dispatcher and v8 imports in an actual fresh subprocess.

Discovered: 2026-10-03, historical-authority Portable8 export integration.

### PORTABLE-007: Check CLI verification bookkeeping after a format bump

Symptom: Engine export succeeds, but the same foreground CLI export with
`--verify` exits78 and does not record verified-export status.

Cause: Manifest validation supports the new format, but the CLI's separate
verified-export evidence writer and status reader still reject its version.
Package version0.1.0 does not detect this drift.

Fix: Keep both bookkeeping version checks aligned with supported Portable
formats and require integer versions rather than accepting booleans. Preserve
the evidence record shape and old-format interpretation. Exercise the real CLI,
not just engine export, before claiming the format works end to end.

Tests: The source-executable review/publication acceptance journey and local
CLI data journey prove verified export and subsequent status. Older restore
tests must use actual older-format fixtures instead of passing a new archive
to a frozen decoder or widening that decoder.

Discovered: 2026-10-03, full historical-authority compatibility diagnostics.

### COMPAT-001: Advance every current consumer with a schema floor

Symptom: Schema14 export breaks owner recovery protection, the desktop rejects
its matching core, and schema13 pending history cannot settle before upgrade.

Cause: The historical extension updated export/bootstrap floors but left the
complete recovery closure at Portable8, the desktop at13/8, and maintenance
classification current-only while migration correctly requires settled history.
The separately executed native smoke checker also retained13/8/8 catalog and
Portable8 export expectations, so unit fixtures agreed with each other while the
actual packaged current runtime correctly failed certification.

Fix: Strictly dispatch complete recovery validation by canonical manifest8/9;
update desktop/docs executable coordinates to14/9/9; permit only validated13/14
restricted owner recovery before migration. Keep older formats and ordinary
writer refusal intact. Trace all current consumers when advancing a floor.
Require the current Portable9 mixed historical, capture-metadata and custody
sidecars in native smoke and include the V2/Portable9 modules in the native
archive audit. Keep exact integer coordinates and reject the previous13/8/8
catalog; run the real native build and Homebrew journey before final acceptance.

Tests: Actual Portable9 owner protection/closure/replay suites, schema13 pending
CLI recovery followed by schema14 migration, documentation matrix and desktop
handshake acceptance with old/fractional/unknown versions refused.

Discovered: 2026-10-05, exact full gate and independent final review.

Follow-up, 2026-10-14: Appending schema17 made the predecessor development
migration accidentally consume the new migration while still declaring schema16.
Its postflight also rejected schema16 after classification changed to supported-old.
Pin predecessor migration slices and settlement coordinates; test real schema15
and schema16 upgrades through schema17, including refusal before changing unsettled
historical state.
Issuer-migration idempotent re-entry and managed recovery also keep explicit
supported-schema lists; advance their current floor and verify exact no-op/recovery
behavior without broadening historical-only cutover phases. Recovery closure,
capture replay and verified-export receipts must also dispatch the new Portable
version through its exact catalog and validator. Update the documentation checker
coordinate map and its mutation probes together; a stale string replacement can
leave the supposedly invalid test input unchanged.

### FACETS-001: Wire history and Portable evidence share canonical values

Symptom: A valid facet event fails public history validation, or a saved selector changes bytes
between wire and evidence validators.

Cause: Copying the T03 timestamp rule loses the six fractional digits used by canonical engine
events. Validating selector set membership alone also accepts unsorted response arrays.

Fix: Preserve canonical event timestamps (`YYYY-MM-DDTHH:MM:SS.ffffffZ`) and sorted unique selector
arrays across both contracts. Run `test_facet_contract_integration.py` against the shared fixtures
when changing either schema. Empty optional selector filters still mean unrestricted.

Discovered: 2026-10-13, C0 cross-contract review and independent boundary tests.

### FACETS-002: Type-check the concrete fake against each protocol

Category: Type Safety.

Symptom: Fake-based tests pass, but strict mypy rejects a chained identity assertion between
three distinct protocol types or a redundant generic-response cast.

Action: Assign the concrete fake to each protocol separately and compare each assignment with
the concrete instance. Let `isinstance` narrow the generic response. Run the combined repository
type check after integrating independently typed workers, before the full test/build gate.

Discovered: 2026-10-13, first integrated C0 type check; production exports were unaffected.

### TESTING-003: Contributor-guide rows are checked by product-boundary tests

Symptom: The full suite fails in `test_contributor_guide_describes_the_foreground_only_boundary`
after the CLI and installed-entrypoint rows disappear from `CLAUDE.md`.

Action: Keep those accurate quick-reference rows when shortening contributor instructions, and
run `tests/security/test_open_brain_product_split.py` after changing the guide. C0 verification
found this pre-existing drift and restored the rows without weakening the boundary assertions.

Discovered: 2026-10-13.

### BUILD-011: Advance current compatibility consumers together

Category: Build Pipeline / Database/Migrations.

Schema 16, runtime admission 11 and Portable 11 require updates to native hidden
imports, desktop/plugin handshakes, verified-export receipts and recovery
allowlists. Leaving a consumer at the previous coordinates can reject an otherwise
valid export or restored store. Update current expectations together; preserve
historical fixture coordinates and frozen format inventories. Run the full build
and typecheck, plus restore/re-export and native smoke checks.

When consuming pinned codec fixtures in current client tests, add current runtime
admission metadata in the test harness. Keep the frozen fixture bytes and their
hash commitments unchanged.

Discovered: 2026-10-14, public Slack capture implementation verification.

### PORTABLE-011: Replace generated sidecars when re-exporting

Category: Database/Migrations.

A restored store retains portable history sidecars on disk. A new exporter also
generates those sidecars from canonical state. Exclude the generated path set from
the ordinary file inventory before staging regenerated bytes; otherwise exclusive
creation fails on a duplicate path. A real import/re-export regression exposed this
for Portable 11 development evidence and append reservations.

Discovered: 2026-10-14, synthetic restore/re-export acceptance.

### COLLECTOR-007: Reserve review capacity before creating proposals

Category: Recovery / Idempotency.

An attention queue cap applied after proposal creation leaves pending engine
proposals outside the bounded review queue. Reserve a durable weekly slot before
the engine write and reuse that slot on uncertain retries. Keep source observation,
derived capture, proposal and attention acknowledgements independently replayable.

Discovered: 2026-10-14, independent public Slack workflow review.

### PORTABLE-003: Historical format documentation is not the current import boundary

Symptom: A feature assessment treats clean Portable import as missing because the v4 document
states that v4 import is refused.

Cause: Version-specific documents and package instructions retain older compatibility boundaries.
The current implementation supports clean v5 import while still refusing standalone v4 import.

Fix: Check the latest Portable document and `PortabilityTask.import_clean` before scoping format
work. Extend the current supported round-trip path and derive schema/runtime requirements from
current source; do not infer them from an older milestone document.

Discovered: 2026-10-13, facet architecture assessment for issue #78.

### FACETS-004: Portable-first imports must not initialize the engine

Category: Dependencies.

Symptom: Focused tests pass, but full-suite collection fails while importing Portable v6.

Cause: A top-level import of an engine facet validator initializes engine/__init__.py, which
loads local composition and imports the partially initialized Portable module again.

Fix: Defer engine validation imports until Portable validation executes. Keep subprocess tests
for Portable-first, engine-first, and version-dispatch import order. Collection order is not a
valid dependency boundary. Discovered during the first stage-one integrated full gate, 2026-10-13.

### FACETS-005: MCP discovery applies two authorization filters

Symptom: The injected facet adapter advertises a granted write, but MCP discovery returns only
brain_catalog and rejects the authorized call as unknown.

Cause: LocalMcpAdapter applies its own scoped operation matrix after collecting adapter tools.

Fix: Route facet names through the injected adapter's capability intersection in both discovery
and invocation. Keep real-engine grant tests alongside fake transport tests. The independent F15
suite found this integration gap on 2026-10-13.

### TESTING-004: Shared virtualenv subprocesses can load a different worktree

Symptom: Focused in-process tests pass, while managed-recovery subprocesses reject a schema
checksum that belongs to the worker's current code.

Cause: Pytest adds the worker's source roots, but a child Python launched through the coordinator's
editable virtualenv resolves packages from the coordinator checkout.

Fix: Give worker subprocess checks explicit absolute source roots in `PYTHONPATH`, or use an
isolated worker virtualenv. Keep full candidate verification in its own worktree and environment.
Four V recovery cases failed with mixed imports and passed with the V source roots on 2026-10-13.

### FACETS-006: Store reads already hold a snapshot transaction

Symptom: Facet publication inspection fails with “cannot start a transaction within a transaction.”

Cause: The store connection starts its read transaction before the facet inspection path calls
`BEGIN` again.

Fix: Use the existing snapshot when it is active. Test inspection through the real store; an
in-memory fixture connection does not reproduce this boundary. Independent F09–F14 checks
found the publication path failure on 2026-10-13.

### FACETS-007: Wire limits include both MCP response representations

Symptom: A Unicode-heavy facet page fits its DTO limit but the actual MCP response is more than
twice that size. A large JSON-RPC request ID can also make a small mutation's receipt exceed its cap.

Cause: MCP encodes the result in both text content and structured content, with ASCII escaping.
Counting only parsed arguments also omits request metadata, whitespace and the request ID.

Fix: Pass the real raw request size through stdio, reserve a bounded complete receipt before
mutation, and give an immutable engine task clone the actual response encoder for pagination.
Keep the requested limit in cursor binding. Independent wire tests found these cases on 2026-10-13.

### FACETS-008: Source routing follows proven revision lineage

Symptom: Publication from a routed source successor succeeds, but Portable export rejects its
frozen source state because the successor's immutable capture has no route of its own.

Cause: Logical source space can be inherited from an ancestor. Route supersession chains are
per capture, so the latest logical route also needs the proven source revision sequence.

Fix: Bind current publication evidence to the newest routed revision's chain tip. Validate
historical bindings against retained source lineage, route order, destination and proposal time.
Keep legacy binding validation unchanged. Real successor/export acceptance exposed this on 2026-10-13.

### FACETS-009: A no-op receipt still needs reachable history

Symptom: Rehashed Portable evidence can claim an impossible unchanged version or membership.

Cause: A no-op has no event, but syntactic receipt validation alone cannot prove it was possible.

Fix: Require the unchanged request and receipt to match one reachable historical event boundary.
Test the importer with a tampered, rehashed real export and assert rejection before writes.
Independent stage-one review found this on 2026-10-13; contract revision v4 contains the correction.

### OBSIDIAN-001: Materialization does not establish retained file identity

Symptom: A freshly refreshed canonical note has no observation baseline for trusted resolution.
An atomic editor save also replaces the inode even when content is unchanged.

Cause: Stage 1 managed materialization and explicit observation are separate operations. A
path-to-note SQL lookup or editable frontmatter alone cannot prove custody.

Fix: Require explicit observation and compare retained device/inode/link identity with confined
reads and accepted/current canonical bytes. Return unavailable when stale. Do not silently use
reconcile to recover identity because it also accepts content. O0 real tests cover both cases.

### TESTING-005: JSON fixture producer boundaries need explicit types and paths

Category: Type Safety / Build Pipeline. O0 fixture authoring exposed a `no-any-return` error
after JSON-normalizing a real dataclass response, plus an incorrect fixture root ancestor.

Fix: Give the normalized producer return an explicit checked contract type, resolve temporary
roots before engine creation on macOS, and run the generated-fixture/hash test from each worktree.
Full repository mypy and plugin build are the prevention checks. No production types were loosened.

### OBSIDIAN-002: Inspection excerpt and source checksums describe different bytes

Symptom: The closed plugin inspection parser accepts synthetic bindings but rejects a real
publication inspection because its two SHA-256 values differ.

Cause: `ReviewEvidence.sha256` hashes the displayed excerpt; the v5 binding's source state
hashes the immutable source. Equality is not part of the engine contract.

Fix: Preserve both checksums and validate their grammar independently. Match evidence to
provenance by capture identity/order. Keep a fixture from the real inspection producer in the
TypeScript suite; independent O0 tests exposed this on 2026-10-13.

### COMPAT-002: Concurrent features cannot share migration or Portable versions

Category: Database/Migrations.

Symptom: A branch and main independently assign schema11/Portable6 to different formats.
Textual conflict resolution alone leaves a runtime that loses one feature or misreads evidence.

Action: Preserve the published main catalogs and append new versions. Facets now use
schema15/runtime10/Portable10 over main14/9/9. Trace registration, recovery closures,
historical snapshot admission, native smoke requirements and strict client handshakes.
Use real mixed-history restore and policy tests; adapter fakes cannot prove these boundaries.
Keep earlier verification pins as historical evidence and reverify the integrated candidate.

Discovered: 2026-10-13, PR79 reconciliation.

### FACETS-013: Frozen executables need engine schema resources

Category: Build Pipeline.

Symptom: Source tests pass while the frozen core cannot import facet validation.

Action: Include both facet_schema.json and facet_evidence_schema.json in the native
spec. Native core and optional harness share that data closure. Run the actual native
audit and Homebrew smoke after adding import-time JSON resources; source builds do
not prove frozen resource availability. Independent PR79 review reproduced the omission.

Discovered: 2026-10-13.

### FACETS-014: Reconcile current policy before retaining old feature expectations

Category: Type Safety.

Action: Preserve main eligibility and custody semantics when combining facet work: current
canonical targets with withdrawn or superseded provenance stay hidden, owner historical
inspection remains separate, and ordinary pending ingress is retained in Portable custody.
Use concrete typed request DTOs in tests and the full shared MCP signature for harness
adapters. Run full typecheck after integration and verify on a fixed commit; harness
acceptance fingerprints intentionally reject source edits during a test run.

Discovered: 2026-10-13, integrated PR79 tests and typecheck.

### OBSIDIAN-003: A malformed success can follow a committed mutation

Category: Transport / Recovery.

An actual plugin-child proxy corrupted a successful dimension receipt after the
engine committed it. Treat response parsing failures as uncertain protocol errors,
retain the exact operation ID, and retry that request. Request validation errors
before dispatch remain definitive. Recheck disposal after every awaited bridge
call so a late identity lookup cannot open a dialog after unload. Real child tests
cover corrupted receipts, dropped responses and restarts.

Discovered: 2026-10-13, independent Stage 2 review.

### OBSIDIAN-004: Restored note bodies do not restore write authority

Category: Managed Workspace / Restore.

Portable attachment retains accepted note bodies while deliberately omitting old
managed-operation write authority. Ordinary body refresh must still fail closed.
Use the owner-local facet-only refresh to rebuild classification metadata and
indexes without accepting body edits. A fresh actual export/import test verifies
both this rebuild and refusal of an old reconciliation preview.

Discovered: 2026-10-13, real Stage 2 restore acceptance.

### BUILD-010: Generic cursor pagination needs explicit response annotations

Category: Type Safety.

TypeScript inferred a cyclic initializer when a generic page response updated the
cursor used by its next request. Annotate the response with its generated contract
type rather than weakening the request or using any. Run plugin typecheck before
integrating a UI worker.

Discovered: 2026-10-13, facet browser and editor implementation.

### TESTING-006: Run deadline-bound acceptance separately from native builds

Category: Config/Environment / Verification.

A full Stage 2 run passed 7048 tests but one shared harness fixture exceeded its
3-second recall limit, causing 26 setup errors. The unchanged
fixture passed 32 checks separately, and the final full gate passed all 7074 tests
after the native build finished. Keep the candidate frozen and separate heavy
native packaging from deadline-bound acceptance. Preserve both failed and passing
receipts; do not relax the product deadline to hide local scheduling contention.

Fixture reports retain absolute evidence paths and the exact Python executable.
Archive their unchanged bytes with a relocation manifest before deleting scratch
data. A relocated copy cannot be presented as a newly collected scenario run.

Discovered: 2026-10-13, actual local Stage 2 verification and approved disk cleanup.

### FACETS-015: Check historical settlement inside additive migrations

Category: Database/Migrations.

Symptom: Startup runs schema migration before its ordinary historical settlement
check. An appended migration can therefore raise the writer floor while independent
historical authority is pending or missing.

Action: Check historical snapshot settlement inside the migration transaction before
DDL and again before commit. Revalidate runtime admission and foreign keys. Exercise
pending, missing and changed registry cases and assert exact database bytes and the
old schema remain unchanged on refusal. Independent PR79 review reproduced both
pre-migration bypasses and verified the corrected behavior.

Discovered: 2026-10-13.

### TESTING-010: Full parallel pytest runs retain several gigabytes of temp data

Category: Testing / Environment.

Symptom: A full `make verify` writes about 6.6 GiB below pytest's base temp directory, and pytest
keeps the last three base temp directories. On a nearly full Mac this filled the disk mid-run
(`ENOSPC`), after which even tool output files could not be created. Overriding
`tmp_path_retention_policy=none` is not a fix: with xdist it deletes directories other fixtures still
use and produced thousands of `FileNotFoundError` setup errors.

Action: Before a full run, check free space. Use an explicit, run-owned base temp
(`PYTEST_ADDOPTS="--basetemp=<tmp>/<run> -p no:cacheprovider"`), which pytest clears at start, and delete
it afterwards. Do not write files into the worktree during the run: the harness acceptance fixture
fails with `source_changed_during_run`.

Discovered: 2026-10-17, goal #111 offline semantic search.

### NATIVE-EMBED-001: Fresh copies of executables cost a macOS validation pass per first launch

Category: Native / Performance.

Symptom: Executing a freshly written helper binary and dylib on macOS arm64 cost about 600 ms of
system code validation, and freshly written model data about 320 ms more, every time. Per-invocation
staging copies made each semantic query cost about 700 ms (warm p95 1213 ms against a 500 ms budget).

Action: Reuse a persistent private stage per component and re-hash it before each launch
(EMBEDDING-RESOURCE.md section 4 amendment, decision D7). Measure first-launch and repeated-launch cost
separately when a design copies executables.

Discovered: 2026-10-17, G4 embedding spike.

### NATIVE-EMBED-002: ONNX Runtime host macOS builds ignore `--apple_deploy_target`

Category: Native / Build.

Symptom: `tools/ci_build/build.py --apple_deploy_target 11.0` left `CMAKE_OSX_DEPLOYMENT_TARGET` empty for
a host macOS build, producing `minos 26.0`. On Linux, a build Python without development headers failed
with `Python::NumPy` not found.

Action: Pass `--cmake_extra_defines CMAKE_OSX_DEPLOYMENT_TARGET=11.0` and confirm with
`otool -l | rg minos`. Build with a uv-managed Python on Linux. Recipe:
`tools/g4_embedding_probe/ort-build-recipe.json`.

Discovered: 2026-10-17, G4 embedding spike.

### TESTING-011: Linux hosts with umask 0002 fail hundreds of permission-checking tests

Category: Testing / Linux.

Symptom: on an Ubuntu host whose login umask is `0002`, `make verify` failed about 200 tests with
`writable ancestor`, `unsafe_config_path` or `resource_missing`. Pytest `tmp_path` directories and a
checkout made under that umask are group-writable, and the product correctly refuses them.

Action: clone the repository and run the suite under `umask 022`, matching macOS and CI. A copy made
with `git archive` also lacks `.git`, which breaks the harness acceptance tests.

Discovered: 2026-10-17, G4 Linux verification on a contributor host.

### NATIVE-EMBED-003: Homebrew's `Cellar` is group-writable, so only the real smoke catches ancestor checks

Category: Native / Homebrew.

Symptom: the embedding resource passed every unit test and the brew-free unpacked-prefix check, then
reported `resource_missing` under real `make native-integration-smoke`: `/opt/homebrew/Cellar` is
`0775 <user>:admin` by design.

Action: ancestors above the installed prefix may be group-writable when owned by root or the current
user (decision D15 in the G4 workstream); the Brain root keeps the strict rule because the helper is
launched by a path beneath it. Run the real Homebrew smoke for any change to installed-path checks.

Discovered: 2026-10-17, G4 gate 5.

### NATIVE-EMBED-004: Homebrew rewrites the `@rpath` dylibs inside the one-folder base

Category: Native / Homebrew.

Symptom: after a real `brew install`, 7 base `_internal` members no longer match
`inventories/base.json`: `Python.framework/Versions/3.14/Python`, `libcrypto.3`, `libssl.3`,
`liblzma.5`, `libmpdec.4`, `libsqlite3` and `libzstd.1` (`.dylib`). The base still runs and every
smoke passes.

Cause: `fix_dynamic_linkage` runs after `install`. It rewrites the install ID of every Mach-O dylib
whose ID is `@rpath/...` and re-signs it. The EXE, the `.so` extension modules and the links are
unchanged. `install_component` verifies before this rewrite, and no runtime check compares base bytes
to the inventory.

Action: do not hash installed base dylibs against the archive inventory. Verify before relocation, or
ship no dylib that Homebrew relocates, as the statically linked embedding helper does (D18). Run the
real Homebrew smoke whenever the base layout changes.

Discovered: 2026-10-17, T5.1 one-folder base smoke.

### NATIVE-EMBED-005: The first run of a freshly installed one-folder base takes about 10 s on macOS

Category: Native / Performance.

Symptom: on the development Mac, `open-brain --version` takes about 285 ms warm but 9.6–10.2 s on the
first run from freshly written bytes (a new install or copy). Hybrid search takes up to 14 s. A later
measurement (T5.4, 2026-10-18) put the first command after `brew install` at 6.2 s. The
old one-file base paid this cost on every call, because it extracted fresh files each time.

Cause: macOS validates each freshly written Mach-O file once, as in NATIVE-EMBED-001, and the folder
holds about 50 of them. Linux does not show the cost.

Action: measure warm runs and the first run after install separately. A cold budget that counts the
first run after install cannot be met by layout alone on this host.

Discovered: 2026-10-17, T5.1 timings.

### NATIVE-EMBED-006: The NW0 Graphify probes run only in CI and still assumed a one-file base

Category: Native / CI.

Symptom: after the base became a one-folder build, `make verify`, `make native-audit` and the
Homebrew smoke all passed locally, but both CI platform jobs failed at "Verify experimental Graphify
native helper" with `IsADirectoryError` on `build/native/dist/open-brain`.

Cause: `tools/nw0_graphify_probe/run.py` and `startup.py` defaulted `--base` to the one-file path.
No local target runs them, so nothing caught it before the PR.

Action: when the base layout changes, run `uv run --frozen --python 3.14 --no-dev --group
native-build python -m tools.nw0_graphify_probe.run` locally (delete `build/nw0-graphify` first), and
search for `dist/open-brain` across `tools/` and `.github/`.

Discovered: 2026-10-18, PR #89 CI.

### TESTING-012: Low free disk turns capture tests into `storage_high`/`storage_critical` refusals

Category: Testing / Host.

Symptom: with under ~10 GiB free on the Mac, collector and connector integration tests failed with
`capture admission refused: storage_high` or `storage_critical` midway through a full parallel run.
The engine watermark (2 GiB high) is working as designed.

Action: pass `-o tmp_path_retention_policy=failed` with an explicit `--basetemp` so passing tests'
temp trees are deleted at teardown, and re-run the affected suite alone to confirm before treating it
as a code failure. Never write into the worktree during a full verify: the harness acceptance tests
fail with `source_changed_during_run`.

Discovered: 2026-10-17, G4 gate 5.
