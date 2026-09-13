# Phase 4 Codex dispatch ledger

## Milestone budget

- Coordinator: current P4-W4 continuation from clean source `18dd32b`
- Maximum active children: 6
- Maximum total children per milestone: 12
- Current active children: 0
- Total child lineages used in P4-W4: 0
- Implementation writers: 0
- Independent reviewers in P4-W4: 0

The lineage budget resets when the coordinator crosses a verified milestone
boundary. A resumed child retains its existing lineage number.

Shared package metadata, the move manifest, imports, root lockfile, shared
tests, Git state, external actions, and final verification remain
coordinator-owned.

## Child 1: P4-W0 read-only implementation map

- Status: complete
- Agent ID: `01a05f60-5e5d-7392-8a75-a5e983948511`
- Model: `gpt-5.4`
- Reasoning effort: `high`
- Role: read-only exploration
- Write scope: none
- Source scope: public Open Brain repository only
- Prompt: `P4-W0-RESEARCH-PROMPT.md`
- Required output: current mechanism map, proposed schema/invariants, harness
  seams, exact file list, ranked risks, and READY/NEEDS_DECISION
- Result: `READY`; P0/P1/P2 `0/2/2`. Both P1 findings are accepted into
  P4-W0: widen release-audit paths and place `tools/phase4` under strict MyPy.

## Child 2: prior-session P4-W1 read-only review

- Status: complete; invalidated by the later CI repair
- Agent ID: not retained in the compact cross-session packet
- Role: read-only reviewer
- Write scope: none
- Source scope: public repository through
  `2174dc2cfbc0e942c0842991650427b8a75ef91f`
- Result: `READY`, P0/P1/P2 `0/0/0`; the later restart-test repair required a
  new exact-candidate review.

## Child 3: P4-W1 fresh read-only review after CI repair

- Status: complete
- Agent ID: `01a05fce-621c-78c2-a5cc-fff2de60a45a`
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only reviewer
- Write scope: none
- Source scope: public repository through
  `4ab916d27dacdccefdf1619120d3b323da5dc707`
- Result: `NOT_READY`; P0/P1/P2 `0/0/4`. Three repository findings were fixed
  at `a82485dd699ca7cd56c86d1904da7487fa1f87c1`: exact artifact
  membership, installed-wheel engine tests, and current wave evidence. The
  fourth finding corrected the next review packet to the sole canonical
  manifest path; no second manifest was added.

## Child 4: P4-W1 rereview after artifact and Linux isolation repair

- Status: complete
- Agent ID: `01a06000-b402-7331-a920-893ba208ece5`
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only reviewer
- Write scope: none
- Source scope: public repository through
  `7266e494f431ac84d9635b1a044bd15ce303c731`
- Result: `NOT_READY`; P0/P1/P2 `0/0/1`. Artifact membership, all moved
  engine tests under wheel isolation, and the sole canonical manifest path
  were independently resolved. The only P2 was stale completion evidence;
  this bounded evidence-only successor records the completed push, exact-head
  CI runs, and verdict before a fresh review.

## Child 5: P4-W1 final evidence rereview

- Status: complete
- Agent ID: not retained in the compact cross-session packet
- Role: read-only reviewer
- Write scope: none
- Source scope: public repository through
  `aab6f1e0891d551a87a2968ef9fb0dae1a8e62e2`
- Result: `READY`; P0/P1/P2 `0/0/0`. Exact-head CI and repaired completion
  evidence closed P4-W1 before any P4-W2 movement began.

## Child 6: P4-W2 source-candidate review

- Status: complete
- Agent ID: `01a0605c-1ea9-7c43-80ce-eda630776e52`
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only reviewer
- Write scope: none
- Source scope: public repository through
  `85428b125ddd370cfa6a6c2be20f2aab4669bf7e`
- Result: `NOT_READY`; P0/P1/P2 `0/3/1`. The review found unreachable
  installed supervisor mode, a hard-coded isolation interpreter, incomplete
  private/undeclared app import enforcement, and stale completion evidence.
  All three P1 findings are repaired at
  `7d87c3968e15de5d98f7e509c8c8a31c4c5b500c`; exact-head CI, release audit,
  and CodeQL passed before this bounded evidence repair.

## Child 7: P4-W2 dynamic-import rereview

- Status: complete
- Agent ID: `01a06077-219b-7cf3-a2e1-b5245ae2297f`
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only reviewer
- Write scope: none
- Source scope: public repository through
  `579b45bc12247d34ed612798fc2a6021ccc1d30a`
- Result: `NOT_READY`; P0/P1/P2 `0/1/0`. Prior supervisor, interpreter-matrix,
  and evidence findings were closed. The sole P1 found dynamic-import
  bypasses through `builtins`, assignment aliases, and reflective access,
  plus a false positive for a shadowed local `importlib` object. The repair at
  `e27ac3c3ad7daa4748b094490fdadab6e66a3773` adds lexical provenance,
  exact callsite binding, unresolved-capability rejection, and focused
  positive and negative regressions.

## Child 8: P4-W2 reflective-import rereview

- Status: complete
- Agent ID: `01a0609c-413f-7443-a219-d3a1e6821517`
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only reviewer
- Write scope: none
- Source scope: public repository through
  `0a67a26210b79d0c91fa8953846560db34903034`
- Result: `NOT_READY`; P0/P1/P2 `0/1/1`. Prior supervisor and interpreter
  findings were closed. The P1 reproduced an importer escape through
  `sys.modules["builtins"]` and identified related runtime namespace helpers.
  The P2 corrected the installed app-suite evidence from 402 tests to its
  exact 400-test scope. The repair at
  `d8e2cb20f0268e16ebdd5b46053d5081dab7ac7c` rejects reflective and dynamic
  evaluation capabilities, records the private target, and adds focused
  positive and negative regressions.

## Child 9: P4-W2 flow-sensitive import rereview

- Status: complete
- Agent ID: `01a060b9-cab4-7a83-b9b5-07b8fafe9081`
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only reviewer
- Write scope: none
- Source scope: public repository through
  `e483973e7f8153b97aa5a44b049d787d08fd884f`
- Result: `NOT_READY`; P0/P1/P2 `0/1/0`. Prior supervisor, interpreter,
  evidence, direct dynamic import, and runtime namespace findings were closed.
  The P1 found bounded reflection through `sys.__dict__`,
  `sys.__getattribute__`, and function `__globals__`; path-insensitive
  rebinding; and false positives for loop, `with`, and exception shadows. The
  repair at `bd994a0288f8711f216e130c25c45f7a654eb90f` adds conservative
  provenance joins, complete binding scopes, reflective-path handling, and
  focused positive and negative regressions.

## Child 10: P4-W2 import-alias and comprehension rereview

- Status: complete
- Agent ID: `01a060e4-6405-7c11-8637-f637c056e0d1`
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only reviewer
- Write scope: none
- Source scope: public repository through
  `0cff2ea6077b2450febd07f456310aff1f6ffd25`
- Result: `NOT_READY`; P0/P1/P2 `0/2/0`. Prior lifecycle, interpreter,
  evidence, reflection, and control-flow findings were closed. One P1 found
  missing provenance for aliases of modeled module members. The other found
  incorrect enclosing-scope semantics for assignment expressions inside
  comprehensions. The repair at
  `e103255b2ab03c3312206383b71ce38fcde67b8e` unifies member provenance,
  implements PEP 572 scope, and adds positive and shadow-negative regressions.

## Child 11: P4-W2 equivalent-loader and argument-provenance rereview

- Status: complete
- Agent ID: `01a06108-dde1-7571-85ce-51f0af170988`
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only reviewer
- Write scope: none
- Source scope: public repository through
  `995bd781869901e772eee0c7fdfd0ab8132065d5`
- Result: `NOT_READY`; P0/P1/P2 `0/2/0`. Prior package, interpreter,
  reflection, scope, alias, and comprehension findings were closed. One P1
  found equivalent import and reflection spellings with untracked authority.
  The other found that a reviewed dynamic-import argument could be replaced
  without changing its approved name-based signature. The repair at
  `559690e14b9a1dd935566b54e97ec8f2b73f8d06` adds semantic capability and
  pristine-parameter provenance, rejects internal optional-loader roots, and
  adds focused positive, negative, and installed-wheel regressions.

## Child 12: P4-W2 closed-provider-registry final review and rereviews

- Status: complete
- Agent ID: `01a06141-8911-7f61-a7d6-159e04063ef5`
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only reviewer
- Write scope: none
- First source scope: public repository through
  `ce909a525dbc66ec5d893892984f2814f7bb9e71`
- First result: no verdict. The coordinator stopped the in-progress review
  when the generic loader architecture was superseded.
- Completed source scope:
  `9ca31ba36c44e4e4a269e5c932fae27fa174831e`
- Evidence scope:
  `ee2f8c22ac1bd00fd6a3d2924071b8ff23a32238`
- Result: `NOT_READY`; P0/P1/P2 `0/0/1`. The sole P2 found a stale
  dynamic-import review in the canonical manifest and proved that the normal
  architecture gate filtered the moved source before stale-review validation.
  The repair at `30d49d31d35f86e26be3c0ac99b884a47d76b5f6` removes the stale
  record, validates reviews against current source locations, and adds a
  moved-source regression.
- Final source scope:
  `30d49d31d35f86e26be3c0ac99b884a47d76b5f6`
- Final evidence scope:
  `e20be9debc6cd68cd443a3df00f6c2cd76041cb3`
- Final result: `READY`; P0/P1/P2 `0/0/0`. The reviewer confirmed the prior
  P2 is closed and declared P4-W2 ready for milestone closure. The lineage was
  then closed. The reviewer budget resets for P4-W3.

## P4-W3 milestone budget

- Status: active
- Active children: 0 of 6
- Total children: 1 of 12
- Identical failures: 0 of 2
- Consecutive timeouts: 0 of 2
- Implementation writer: coordinator only; no child write scope is reserved
- Review lineage: child 13 reserved after exact-head CI, Release audit, and
  CodeQL passed at `2c05dead068ef517ed365d100f3b6273c29eeba9`
- Reset authority: D-031 closes the P4-W2 lineage and starts this milestone
  with a fresh child budget

## Child 13: P4-W3 connector-isolation final review

- Status: complete; lineage closed
- Agent ID: `01a06202-4d7a-7331-a038-239a6e93a630`
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only independent reviewer
- Write scope: none
- Source scope: public repository through
  `2c05dead068ef517ed365d100f3b6273c29eeba9`
- Required result: `READY` with P0/P1/P2 `0/0/0`; otherwise return bounded
  findings with exact source evidence
- Result: `NOT_READY`; P0/P1/P2 `0/1/0`. `P4W3-001` proved the default
  in-process `ConnectorRegistry.resolve()` can load and execute the installed
  `open_brain.connectors.v1` entry point outside the bounded worker. Repair must
  make installed connector resolution child-only and add a wheel-only parent
  non-load regression.
- Rereview source scope:
  `27344c6cf83b8b74c11d6ec0c1c3075cbfa98e09`
- Rereview evidence: exact-head CI `33629757827`, Release audit `33629757695`,
  and CodeQL `33629754431` passed before resuming the same lineage
- Final result: `READY`; P0/P1/P2 `0/0/0`. The reviewer reproduced the
  installed parent non-load boundary, confirmed `P4W3-001` closed, found no new
  issues, and declared P4-W3 ready for milestone closure

## P4-W4 milestone budget

- Status: complete
- Active children: 0 of 6
- Total children: 1 of 12
- Identical failures: 0 of 2
- Consecutive timeouts: 0 of 2
- Implementation writer: coordinator only; no child write scope is reserved
- Review lineage: child 14 returned final `READY` at
  `9098ff5e676a76ef1637f30dee99ff6508d46a30`; lineage closed
- Reset authority: D-038 closes the P4-W3 lineage and starts P4-W4 with a fresh
  child budget

## Child 14: P4-W4 and P4A final review

- Status: complete; lineage closed
- Agent ID: `01a0628c-d723-73e0-96cc-f2b3d8ca3f63`
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only independent reviewer
- Write scope: none
- Source scope: public repository through
  `7927162096330355520f2de756aa45b48ccb6493`
- Evidence: exact-head CI `33642529540`, Release audit `33642529483`, and
  CodeQL `33642524132` are green
- Required result: `READY` with P0/P1/P2 `0/0/0`; otherwise return bounded
  findings with exact source evidence
- Result: `NOT_READY`; P0/P1/P2 `0/1/1`. `P4A-001` rejects D-040's broad
  legacy-to-app/connector dependency as weaker than the governing plan and
  published migration-interface boundary. `P4A-002` identifies the removed
  artifact-policy path retained in `CLAUDE.md`.
- First rereview source scope:
  `b86beacbc7005b0a7f2ceeb5c009f0a2849579a6`
- First rereview evidence: exact-head CI `33650699793`, Release audit
  `33650699895`, and CodeQL `33650693028` passed before resuming the same
  lineage
- First rereview result: `NOT_READY`; P0/P1/P2 `0/1/1`. The reviewer closed
  `P4A-001` and `P4A-002`. `P4A-003` found the sole external `_compat` import,
  an ambient undeclared `openai` loader. `P4A-004` proved that readiness probe
  `SystemExit` escaped with raw detail. The lineage remains retained for one
  rereview after the focused regressions, full local gates, and new exact-head
  remote checks pass.
- Final source scope:
  `9098ff5e676a76ef1637f30dee99ff6508d46a30`
- Final evidence: exact-head CI `33654478123`, Release audit `33654478071`,
  and CodeQL `33654473793` passed before the second same-lineage rereview
- Final result: `READY`; P0/P1/P2 `0/0/0`. The reviewer independently
  reproduced the focused repair checks and private artifact hashes, explicitly
  closed `P4A-001` through `P4A-004`, found no new issue, and declared P4-W4
  and P4A ready for milestone closure.

## P4-W5 milestone budget

- Status: complete
- Active children: 0 of 6
- Total children: 1 of 12
- Identical failures: 0 of 2
- Consecutive timeouts: 0 of 2
- Implementation writer: coordinator only; no child write scope is reserved
- Review lineage: child 15 returned final `READY` at
  `c7c4fad1b109ac7d7c55d55cdfa57b64a9c910db`; lineage closed
- Reset authority: P4-W4 child 14 is closed; D-031 and D-048 start P4-W5 with
  a fresh milestone budget and separate source/evidence identities

## Child 15: P4-W5 native artifact and lifecycle review

- Status: complete; lineage closed
- Agent ID: `01a0636c-3ef9-7823-ad08-d8dcdb5a48d9`
- Nickname: Kierkegaard
- Role: read-only independent reviewer
- Write scope: none
- Initial source scope:
  `6c2c82de89b554cca8ec6c27b10a57959766c39e`
- Initial evidence: exact-head CI `33667187568`, Release audit `33667187309`,
  and CodeQL `33667180599` passed before review
- Initial result: `NOT_READY`; findings `P4W5-001` through `P4W5-007`
- First rereview source scope:
  `28f1fa7055a9194e433caea666a0c13bf2c126da`
- First rereview evidence: exact-head CI `33672312445`, Release audit
  `33672312619`, and CodeQL `33672307671` passed
- First rereview result: `NOT_READY`; P0/P1/P2 `0/3/1`
- Second rereview source scope:
  `47aee48e248156816b863071b7df199513a7da43`
- Second rereview evidence: local frozen gates and macOS native proof passed;
  exact-head CI `33675602201`, Release audit `33675602206`, and CodeQL
  `33675596478` passed. CI used one failed-only retry for Python 3.12 and did
  not rerun either native job.
- Second rereview result: `NOT_READY`; P0/P1/P2 `0/4/0`. D-050 was accepted.
  `P4W5-002`, `P4W5-003`, `P4W5-005`, and `P4W5-006` are closed.
  `P4W5-001`, `P4W5-004`, and `P4W5-007` remain open; `P4W5-008` is new.
- Third rereview source scope:
  `e55d7488a60a98f2bf5f06cebc18f8fe485e169f`
- Third rereview evidence: exact-head CI `33681462262`, Release audit
  `33681462105`, and CodeQL `33681457587` passed on their first attempt; the
  two target-native jobs and separate local macOS subject are digest-bound in
  `EVIDENCE.md`
- Third rereview result: `NOT_READY`; P0/P1/P2 `0/2/0`. D-050 and D-051 were
  accepted. `P4W5-001`, `P4W5-002`, `P4W5-003`, `P4W5-005`, `P4W5-006`,
  and `P4W5-007` are closed. `P4W5-004` and `P4W5-008` remain open.
- Final source scope:
  `c7c4fad1b109ac7d7c55d55cdfa57b64a9c910db`
- Final evidence: 115-test preflight, 119 Phase 4/security tests, all 3,221
  repository tests, local macOS ARM64 native proof, exact-head CI
  `33684763227`, Release audit `33684763223`, and CodeQL `33684759266`
  passed. Linux job `100429455057` used literal `ubuntu-24.04`; macOS job
  `100429455034` used `macos-14-arm64`. Exact artifact and membership digests
  are recorded in `EVIDENCE.md`.
- Final result: `READY`; P0/P1/P2 `0/0/0`. D-050 and D-051 are accepted,
  `P4W5-001` through `P4W5-008` are closed, and no finding remains. P4-W5 is
  complete; the reviewer lineage is closed.

## P4-W6 milestone budget

- Status: complete
- Active children: 0 of 6
- Total children: 3 of 12
- Identical failures: 0 of 2
- Consecutive timeouts: 0 of 2; breakers applied separately to children 16 and 17
- Implementation writer: coordinator only; all children were read-only
- Review lineage: child 18 returned final `READY` at
  `537bc4f1059ef4b4e8f0916702f38f4e531b13fe`; lineage closed
- Reset authority: P4-W5 child 15 is closed; the validated private
  notarization receipt and the rendered P4-W6 phase contract start a fresh
  milestone budget without changing P4-W5 or the immutable readiness snapshot

## Child 16: P4-W6 pre-implementation architecture review

- Status: closed without verdict after timeout breaker
- Agent ID: `01a064fc-4f72-7bb1-bc45-11d4a8535957`
- Nickname: Fermat
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only adversarial architecture reviewer
- Write scope: none
- Source scope: public repository at
  `6d8ce32d2e5fcca187b28ae7b3a740d356fe220c`, the reviewed Phase 4 plan,
  accepted D-047 through D-051, and current official Apple documentation
- Required result: `READY` or bounded P0/P1/P2 findings plus the recommended
  module, CLI, tests-first, CI artifact-flow, and clean-host matrix shape
- Result: no result was returned after one 60-second wait and one 30-second
  wait. The agent remained running when closed. No finding or approval is
  inferred. D-052 records the coordinator's bounded architecture decision;
  the final exact-source/artifact acceptance review remains mandatory.

## Child 17: P4-W6 exact-source and artifact review

- Status: closed without verdict after timeout breaker
- Agent ID: `01a0658a-9e01-71f3-98fa-6753cacc2c4e`
- Nickname: Bacon
- Model: `gpt-5.6-sol`
- Reasoning effort: `xhigh`
- Role: read-only independent final reviewer
- Write scope: none
- Source scope: `537bc4f1059ef4b4e8f0916702f38f4e531b13fe` and its exact 23-coordinate
  unpublished candidate
- Result: no result was returned after two consecutive 60-second waits. The agent was still
  running when closed; no finding or approval is inferred.

## Child 18: P4-W6 replacement final review

- Status: complete; lineage closed
- Agent ID: `01a0658f-0169-7d41-8084-093c4f90d7f7`
- Nickname: Dirac
- Model: `gpt-5.6-sol`
- Reasoning effort: `high`
- Role: read-only independent final reviewer
- Write scope: none
- Source scope: `537bc4f1059ef4b4e8f0916702f38f4e531b13fe` and its exact 23-coordinate
  unpublished candidate
- Evidence: exact-head CI `33714932363`, Release audit `33714932452`, CodeQL
  `33714929770`, exact Python/Linux/macOS artifacts, five passed host records, one bounded macOS 14
  unavailable-runner record, and immutable snapshot hash
  `753a1635aa2be81f3ebe6b3723dbc8e46a6d6aa46f1b39003b9d9c39da769d1b`
- Initial result: `NOT_READY`; P0/P1/P2 `0/1/0`. `P4W6-AR-001` recorded that a coordinator
  interruption left critical diff, P4-W5 default, notarization flag, and snapshot checks unfinished.
- Final same-lineage result: `READY`; P0/P1/P2 `0/0/0`. The reviewer completed each missing
  bounded check, closed `P4W6-AR-001`, found no product issue, and confirmed P4-W6 ready for
  milestone closure.

## P4-W7 milestone budget

- Status: complete
- Active children: 0 of 6
- Total children: 5 of 12
- Identical failures: 0 of 2 for Codex CLI 0.153.0; children 19 and 20 closed
  the unchanged 0.152.1 CLI path at 2 of 2
- Consecutive timeouts: 0 of 2
- Implementation writer: coordinator only; reviewer write scope is read-only
- Review lineage: children 19, 20, and 21 dead without verdicts; child 22
  returned final `READY`, P0/P1/P2 `0/0/0`; all slots released and lineage
  closed
- Reset authority: P4-W6 child 18 is closed. The validated P4-W6 handoff,
  rendered P4-W7 contract, and D-054 start a fresh milestone budget without
  changing P4-W5, the readiness snapshot, source, or artifact bytes.

## Child 19: P4-W7 exact-candidate and aggregate P4B review

- Status: dead without verdict; slot released
- Dispatch ID: `p4w7-review-01`
- Model: `gpt-5.6-sol`
- Reasoning effort: `high`
- Role: read-only independent final reviewer
- Write scope: none
- Source scope: accepted source
  `537bc4f1059ef4b4e8f0916702f38f4e531b13fe`, its exact 23-coordinate
  unpublished candidate, exact run state, P4-W7 audit, D-054, and all P4A/P4B
  completion criteria
- Required result: `READY` with P0/P1/P2 `0/0/0` and all three D-054
  adjudications accepted, or `NOT_READY` with bounded findings
- Result: the Codex CLI received HTTP 404 from both WebSocket and HTTPS
  transports before model execution and produced no closed report. No review
  occurred and no verdict is inferred.

## Child 20: P4-W7 replacement exact-candidate and aggregate P4B review

- Status: dead without verdict; slot released
- Dispatch ID: `p4w7-review-02`
- Model: `gpt-5.6`
- Reasoning effort: `high`
- Role: read-only independent final reviewer
- Write scope: none
- Source scope: identical to child 19
- Retry strategy: use the documented family selector after the explicit Sol
  endpoint returned 404 before execution; prompt and evidence remain unchanged
- Required result: `READY` with P0/P1/P2 `0/0/0` and all three D-054
  adjudications accepted, or `NOT_READY` with bounded findings
- Result: the changed model selector reached the same HTTP 404 on both Codex
  CLI transports before execution and produced no closed report. The
  two-identical-failure breaker closes the CLI path; no review or verdict is
  inferred.

## Child 21: P4-W7 in-app exact-candidate and aggregate P4B review

- Status: errored before execution; slot released and agent closed
- Dispatch ID: `p4w7-review-03`
- Agent ID: `01a067c2-50dd-7fe0-909b-7f3d8f1ae181`
- Nickname: Halley
- Model: `gpt-5.6-sol`
- Reasoning effort: `high`
- Role: read-only independent final reviewer
- Write scope: none
- Source scope: identical to children 19 and 20
- Retry strategy: use the separate in-app subagent runtime after closing the
  failed CLI transport path; the rendered prompt and evidence are unchanged
- Required result: `READY` with P0/P1/P2 `0/0/0` and all three D-054
  adjudications accepted, or `NOT_READY` with bounded findings
- Result: the separate in-app runtime reached the same shared response
  endpoint and received HTTP 404 before execution. No review, verdict, or file
  change occurred. The milestone is blocked rather than weakening or
  self-certifying the mandatory review gate.

## Child 22: P4-W7 post-update exact-candidate and aggregate P4B review

- Status: complete at 2026-09-03 12:37:38 CDT; slot released
- Dispatch ID: `p4w7-review-04`
- Session ID: `01a06848-e728-7242-95a6-f298cb1f5fe7`
- Model: `gpt-5.6-sol`
- Reasoning effort: `high`
- Role: read-only independent final reviewer
- Write scope: none
- Source scope: identical to children 19 through 21
- Retry strategy: Codex CLI was explicitly updated from 0.152.1 to 0.153.0;
  `codex doctor --summary` then reported 22 checks OK, zero warnings, zero
  failures, WebSocket HTTP 101, and reachable provider endpoints. This is one
  bounded attempt against materially changed client state, not a third retry
  of the closed 0.152.1 path.
- Required result: `READY` with P0/P1/P2 `0/0/0` and all three D-054
  adjudications accepted, or `NOT_READY` with bounded findings
- Initial result: `NOT_READY`; P0/P1/P2 `0/1/0`. `P4W7-001` reported that
  strict signature verification failed, but the failure occurred only inside
  the CLI's macOS read-only sandbox.
- Same-lineage adjudication: the coordinator and child 22 each reproduced
  relative, absolute, and deep strict signature passes against unchanged DMG
  SHA-256 `aa78303a1b1ac7b42215adada8d7932fe55114391292f622073b9aec825a95ac`
  outside that sandbox. Child 22 retained a no-write contract under a
  workspace-write sandbox, observed no worktree or candidate change, and
  closed `P4W7-001` as a sandbox-induced false positive.
- Final result: `READY`; P0/P1/P2 `0/0/0`. All three D-054 adjudications and
  all P4A/P4B completion criteria are accepted. No file changed and no concern
  remains.

## P4-W8 milestone budget

- Status: stopped at fresh-machine inventory gate
- Active children: 0 of 6
- Total children: 6 of 12
- Identical failures: 0 of 2
- Consecutive timeouts: 2 of 2 for the closed CLI path; 0 of 2 on the in-app
  path
- Implementation writer: coordinator only; no child write scope is reserved
- Review lineage: Children 23, 24, and 27 closed without verdict after bounded
  timeouts or stalls. Child 25 returned final architecture `READY`,
  P0/P1/P2 `0/0/0`. Child 26 returned the initial implementation findings but
  stalled on rereview. Child 28 closed IR-004 on its second pass and returned
  one P0 mapping-contract finding before the remaining questions were reached.
  Its third pass closed that mapping finding and found one P1 in the
  descriptor-missing orphan transport before the remaining questions were
  reached. Its fourth pass closed the orphan protocol and found one P0 in
  unsupported launch-manifest handling before the remaining questions were
  reached. Its fifth pass closed the manifest inventory and every deferred
  question, returning final pre-execution `READY`, P0/P1/P2 `0/0/0`. Its slot
  is released. A final receipt review remains mandatory.
- Reset authority: P4-W7 Child 22 is closed. The validated P4-W7 handoff,
  protected P4A/P4B merge, rendered P4-W8 contract, and D-057 start a fresh
  milestone budget.

## Child 23: P4-W8 pre-implementation architecture review

- Status: closed without verdict after timeout breaker; slot released
- Dispatch ID: `p4w8-architecture-review-01`
- Model: `gpt-5.6-sol`
- Reasoning effort: `high`
- Role: read-only adversarial architecture reviewer
- Write scope: none
- Source scope: static merged public P4-W8/P4B inputs plus the private
  companion's current Goal #63 state and historical Goal #24 helper,
  controller, tests, and bounded receipts
- Required result: `READY` with P0/P1/P2 `0/0/0`, or bounded
  `P4W8-AR-*` findings plus exact module, command, receipt, red-test, live
  sequencing, and rollback guidance
- Result: no closed report after about 190 seconds. The process was still
  recursively inspecting static source at interruption and had consumed a
  disproportionate context budget. No finding or approval is inferred. The
  replacement must prohibit broad search/codegraph and limit inspection to
  named source regions.

## Child 24: P4-W8 bounded architecture review

- Status: closed without verdict after timeout breaker; slot released
- Dispatch ID: `p4w8-architecture-review-02`
- Model: `gpt-5.6-sol`
- Reasoning effort: `high`
- Role: read-only adversarial architecture reviewer
- Write scope: none
- Source scope: only named public/private files and line ranges; broad search,
  codegraph, Git history, web, and live commands are forbidden
- Retry strategy: narrow Child 23's recursive static exploration to the exact
  controller/helper/stager/verifier interfaces required by P4-W8
- Required result: bounded architecture `READY` with P0/P1/P2 `0/0/0`,
  or exact `P4W8-AR-*` findings and implementation guidance
- Result: no closed report after the bounded wait. The fixed source set was
  still being inspected at interruption; no finding or approval is inferred.
  This is the second consecutive timeout, so D-058 closes equivalent review
  dispatches before implementation or live P4-W8 work.

## Child 25: P4-W8 in-app bounded architecture review

- Status: final architecture review complete; slot released, lineage closed
- Dispatch ID: `p4w8-architecture-review-03`
- Agent ID: `01a06884-a664-7722-9705-01351d9e4e94`
- Nickname: Leibniz
- Model: `gpt-5.6-sol`
- Reasoning effort: `high`
- Role: read-only adversarial architecture reviewer
- Write scope: none
- Source scope: identical named files and line ranges from Child 24
- Retry strategy: use the distinct in-app subagent runtime discovered after
  D-058 closed the two timed-out Codex CLI attempts
- Required result: bounded architecture `READY` with P0/P1/P2 `0/0/0`,
  or exact `P4W8-AR-*` findings and implementation guidance
- Initial result: `NOT_READY`; P0/P1/P2 `0/7/2`. Direct adaptation of the
  historical production controller is rejected. The clean architecture must
  close capability isolation, complete owner-map/journal rollback, online
  backup consistency, namespace/TOCTOU safety, exact stage bindings, an
  independent data-only verifier, the surface matrix, and postflight cleanup.
  `P4W8-AR-001` is a bounded path-resolution gap: the public prompt exists
  but its exact path was not supplied to the fixed-region reviewer.
- Rereview input: the exact public phase-prompt path and SHA-256, a private
  nine-finding adjudication with SHA-256
  `78f6242fda1f948c3d50f186c600caed32c5265930425f01fe7363f95a9a456e`,
  and a bounded isolation-feasibility receipt with SHA-256
  `0386cf4b7b0f8eff240f9041b18eab5e72b51c502c295fdd63caa80f7e5ab966`.
- Required rereview result: explicitly close or retain each
  `P4W8-AR-001` through `P4W8-AR-009`, then return `READY` with P0/P1/P2
  `0/0/0` or a bounded remaining finding set. No file write or live command is
  authorized.
- Second result: `NOT_READY`; P0/P1/P2 `0/2/0`. AR-001 and AR-004 through
  AR-009 are closed. AR-002 requires proof that the sandbox denies external
  same-user signals, pre-existing loopback listeners, non-transaction Unix
  sockets, all bounded production roots, and every non-allowlisted service
  control. AR-003 requires durable write-ahead `planned` and `applied` records
  plus crash-point reconciliation tests.
- Third-review inputs: amended adjudication SHA-256
  `f5dfa1a12580a9230a55b2f3e08ffdcaabc9faf0afda05bc3453036d60a2edd6`
  and expanded isolation receipt SHA-256
  `760a55ccd0c1b4f639a5df7ef83f77b55aaf5f8b045b83d12f09af39b33f2841`.
  The probe source matches the receipt at SHA-256
  `4c8bb414ae8ff270d3b05e3184bfed7fb823dd012f8a1e86bd278cbb5bfa8b9c`.
- Required third result: accept or retain AR-002 and AR-003 only, confirm the
  other seven closures remain valid, and return architecture `READY` with
  P0/P1/P2 `0/0/0` before implementation.
- Final result: `READY`; P0/P1/P2 `0/0/0`. AR-002 and AR-003 are closed and
  AR-001 plus AR-004 through AR-009 remain closed. The verdict authorizes
  red-first implementation only. Live inventory, helper, backup, restore,
  service, and rehearsal work remain gated on populated hashes, tests, and
  independent pre-execution review.

## Child 26: P4-W8 populated pre-execution implementation review

- Status: closed after stalled rereview; slot released
- Dispatch ID: `p4w8-preexecution-review-01`
- Agent ID: `01a06929-aae0-7c50-9d04-eb8f4711ce22`
- Nickname: Tesla
- Model: `gpt-5.6-sol`
- Reasoning effort: `high`
- Role: read-only adversarial implementation and safety reviewer
- Write scope: none
- Source scope: exact named public/private implementation, generated stage,
  candidate, tests, schemas, profiles, and accepted-source regions in the
  private review prompt; broad search and live commands are forbidden
- Required result: explicitly adjudicate implementation deltas ID-001 through
  ID-007 and return `READY`, P0/P1/P2 `0/0/0`, before any live metadata,
  helper, backup, restore, candidate-daemon, or rehearsal action; otherwise
  return a closed `P4W8-IR-*` finding set
- Result: `NOT_READY`; P0/P1/P2 `3/4/1`. ID-002 and the corrected portions
  of ID-001, ID-003, and ID-004 were accepted. Findings require preserving
  record locks across recapture, candidate execution on migrated state, closed
  writer-to-root-to-lease inventory, operational reattachment, measured
  surface/writer evidence, a presealed independent verifier contract,
  descriptor-relative copy/migration, and the full exact-profile V2 probe.
  Metadata-only preflight is the sole authorized live action; no repair,
  backup, restore, candidate launch, or rehearsal is authorized yet.
- Rereview result: no terminal verdict after the original bound, an extended
  wait, and a forced-verdict interrupt. The agent was closed while still
  running. No approval or additional finding is inferred.

## Child 27: P4-W8 bounded pre-execution rereview

- Status: closed without verdict after bounded stall; slot released
- Dispatch ID: `p4w8-preexecution-review-02`
- Agent ID: `01a06986-7add-7fd3-9892-3d7a7a0dfed8`
- Nickname: Fermat
- Role: read-only adversarial implementation and safety reviewer
- Write scope: none
- Source scope: the same closed named-file set as Child 26, updated to the
  repaired transaction and all eight implementation deltas
- Retry strategy: fresh bounded in-app context after the retained Child 26
  lineage failed to emit a terminal rereview verdict
- Required result: explicitly close or retain IR-001 through IR-008 and return
  `READY`, P0/P1/P2 `0/0/0`, before any remote stage, helper, backup, restore,
  candidate, or rehearsal action; otherwise return one closed finding set
- Result: no terminal verdict after the bounded review interval. The lineage
  was closed without approval or a new finding.

## Child 28: P4-W8 compact delta rereview

- Status: final pre-execution review complete; slot released, lineage closed
- Dispatch ID: `p4w8-preexecution-review-03`
- Agent ID: `01a06992-e518-7e52-a22a-85445605e731`
- Nickname: Parfit
- Role: read-only adversarial implementation and safety reviewer
- Write scope: none
- Source scope: closed named implementation, test, schema, transaction, and
  bounded receipt set in `P4-W8-DELTA-REREVIEW-PROMPT.md`
- Retry strategy: compact eight-question review after Child 27 did not emit a
  terminal verdict
- Initial result: `NOT_READY`; P0/P1/P2 `0/1/0`. IR-001 is closed. IR-004
  remains open because rollback accepted an empty journal and the SIGKILL test
  did not exercise the real operational reattachment path. IR-002, IR-003, and
  IR-005 through IR-008 were not reached, so no approval is inferred.
- Required rereview result: verify the real `OperationalRehearsal` SIGKILL and
  nonempty-journal repair, complete the six deferred questions, and return
  `READY`, P0/P1/P2 `0/0/0`, before any remote write, recovery, or rehearsal
  action; otherwise return one closed finding set
- Second result: `NOT_READY`; P0/P1/P2 `1/0/0`. IR-001 and IR-004 are closed.
  IR-002 is open because the sealed mapping declared `legacy/slot-*` while
  runtime independently used `.open-brain/retained/<role>`. IR-003 and IR-005
  through IR-008 were not reached, so no approval is inferred.
- Third result: `NOT_READY`; P0/P1/P2 `0/1/0`. IR-001 and IR-002 are closed.
  IR-004 remains open because descriptor-missing orphan shutdown used a
  one-shot response read without retry or lost-response handling. IR-003 and
  IR-005 through IR-008 were not reached, so no approval is inferred.
- Fourth result: `NOT_READY`; P0/P1/P2 `1/0/0`. IR-001, IR-002, and IR-004 are
  closed. IR-003 remains open because unsupported `.plist` identities were
  silently skipped before classification. IR-005 through IR-008 were not
  reached, so no approval is inferred.
- Final result: `READY`; P0/P1/P2 `0/0/0`. IR-001 through IR-008 are closed.
  The exact reviewed implementation may proceed through owner-only remote
  staging, bounded metadata/preflight, dual recovery, and the disposable
  full-stop/forced-rollback rehearsal. Production ownership and content remain
  immutable; final receipt review is still mandatory.

## P4-W8 fresh-machine inventory gate

- Status: blocked before exact-profile and preflight; active children remain
  `0`, total child lineages remain `6`, and final receipt review has not begun.
- Completed gate: the private 20-test suite, Ruff, strict MyPy, seven schema
  checks, Child 28 final pre-execution `READY`, owner-only transfer, and all 16
  remote stage-coordinate validations passed.
- Fresh result: read-only inventory found one unexpected inactive but loaded
  Open Brain registration with a known job-class identity. Its five discovered
  roots overlap three governed production roots, so the writer map is not
  closed. The bounded blocker receipt SHA-256 is
  `3af46ad50b9e61207e080ce203f887c744dc82fdc7043e955852cac5d036b9f8`.
- Stop action: no exact-profile, backup-access repair, backup, restore,
  candidate launch, rehearsal, postflight, production mutation, or Child 29
  receipt review ran. Resume requires separate owner-authorized reconciliation
  of the unexpected registration followed by a fresh P4-W8 transaction.

## P4-W8 separate reconciliation review

- Separate workstream status: blocked pending bounded seven-root helper
  authority. Its child budget is independent from P4-W8: `0` active and `1`
  total lineage.
- Child 1 role: independent read-only adversarial architecture reviewer; no
  write or live-command scope.
- Result: `NOT_READY`, P0/P1/P2 `3/0/0`. REC-AR-001 requires a complete
  two-generation writer/root/lease model and two-lease fence. REC-AR-002
  requires encrypted recovery of all seven roots rather than helper v18's five.
  REC-AR-003 requires seven-root staging, isolation, receipts, and exact
  two-generation rollback.
- Next gate: explicit owner authority for a versioned helper successor whose
  full profile reads exactly seven roots, followed by implementation and fresh
  pre-execution review. No service/content change, backup, restore, rehearsal,
  final receipt review, publication, deployment, or P4-W9 action occurred.
