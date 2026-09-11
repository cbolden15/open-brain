# NW0 continuation: verified progress and remaining gates

Status: NW0 remains incomplete. This record updates current status without replacing historical
failures or treating private prototypes as shipping implementation. No real-provider sample has
run. The required four-path, two-platform matrix still needs 32 samples.

## Native packaging and startup

B12's selected separate-helper candidate passed native builds, runtime checks, artifact inspection
and the scoped installation evidence. B13 adds the missing startup comparison at commit
`fc493715f58239ae01e196eb97693bf8fb15bf04`, in
[CI run 34520192774](https://github.com/cbolden15/open-brain/actions/runs/34520192774).
Both native jobs passed. An independent review checked the downloaded artifacts, all samples,
receipt correspondence and median arithmetic. Each series has five observations per layout.

| Native target | Series | Baseline median, ms | Candidate median, ms | Added, ms | Limit, ms |
|---|---|---:|---:|---:|---:|
| Linux x86_64 | Disk-cache-cold | 420.936932 | 420.755297 | -0.181635 | 500 |
| Linux x86_64 | Warm | 395.115709 | 394.934668 | -0.181041 | 200 |
| macOS arm64 | Disk-cache-cold | 805.717542 | 778.886583 | -26.830959 | 500 |
| macOS arm64 | Warm | 311.478458 | 321.606375 | 10.127917 | 200 |

All four comparisons pass. Cold means a disk-buffer-cache approximation on disposable hosted
runners, not a reboot. Both layouts contain identical base executable bytes; the candidate's
audited helper remains dormant. This does not measure helper activation or the whole user journey.
Do not repeat B's builds or measurements without a relevant input change.

The Linux helper archive SHA-256 is
`aa5af8f718a1d5e98d59d9678472632950fe5491c960f0daa3af2902959b0f49`;
the macOS archive SHA-256 is
`0c4f8f888cc2c52dd1612b2fdb37b8e24076641d7a908a44353c6a45b9c6811a`.
Each includes the expected executable and four legal files. All 84 frozen runtime checks pass on
each target. The coordinator's corrected private audit passed; the independent reviewer inspected
that receipt, not the private terms. Its initial JSON-as-native-artifact audit error remains recorded.

## Durable state and desktop evidence

A12 composes the existing private lifecycle contracts over a genuine validated Engine export.
Its retained verifier reports 18 passing checks, three focused tests and passing lint, including
rename/edit, consent revocation, explicit link acceptance, conflict resolution, delete/restart/restore,
byte-exact portability and inactive restored consent. Independent review passed with no findings:
two reviewer-owned lifecycle tests and seven targeted negative tests passed, with unchanged candidate
hashes. A13's corrected draft passed its three-finding independent review and preserves all nine
requirement rows. The [representation decision](2026-09-10-ob1-nw0-representation.md) selects a
sibling projection and first-release no-automatic-overwrite policy. Setup, explicit owner
materialization and recovery of an already-authorized pending operation remain supported; NW1
owns their production writer, journal and enforcement. The trusted in-process owner and generic
shared-record mapping are not OS authentication or installed schema support.

D12 installed official Obsidian 1.12.7 in the isolated Ubuntu 24.04.5 x86_64 GNOME guest, displayed
Canvas evidence and navigated to a synthetic note. D17 completed the Mac deterministic bridge flow:
evidence, both endpoint navigations, revocation, exactly one explicit acceptance, verified export
and restore, and one retained link with inactive consent on reopen. The plugin was disabled and
its helper exited. Preserve that consumed fixture; do not rerun it as an empty workspace. The
initial Mac trust-prompt dismissal was not observed and is not counted as an observed setup step.

D18 produced an 18,815,231-byte Linux package with passing host tests. Its installer review found
four issues; a separate corrected installer has eight passing defensive regressions on macOS.
Independent approval and native Linux installation/runtime/GUI evidence remain open. The original
Linux guest is preserved. A new isolated VM is stopped and unbooted; creating it proves no journey.

D19's private scheduler passes eight tests and a nine-combination synthetic batch comparison.
Independent review reproduced four defects: stale asynchronous publication, failed pause persistence,
uncancelled reconciliation on unload, and redundant inference on unchanged reload. A separate correction
passed 17 tests, but re-review found asynchronous callback effects could still escape rejection and
an automatic run could publish after durable pause. A data-only owned publication correction passes
17 focused and six boundary tests and resolves those two publication findings. Its independent
review found an unbounded sparse-array traversal and a lost automatic retry on immediate pause/resume;
both are being corrected in a new candidate. It is not connected to Obsidian. The 500 ms debounce
and 2 s maximum delay are measurement candidates,
not final product values. Real event binding, pause persistence, cancellation, visible edges and
the Canvas-versus-offline-HTML choice still require desktop evidence.

## Provider boundary

C14 checked current official guidance and installed public SDK interfaces. Anthropic's
[Agent SDK documentation](https://code.claude.com/docs/en/agent-sdk/overview) requires prior approval
for third-party products offering claude.ai login or rate limits. No such approval is established
by the retained evidence. Supported access identity, effective policy/change semantics, confinement
and complete attempt/cancellation proof also remain missing. Claude subscription stays closed and
required; neither token relay nor an API-only scope change is implicitly authorized.

C15 has 14 passing offline tests for the common authorization boundary, three direct-API codecs,
response-derived attribution and fake-connector cleanup. Independent review found an Anthropic
cache-token undercount and a reported lint check that did not reproduce. The separate correction
passed independent coordinator review: all 16 tests passed normally and with Python optimization,
two additional reviewer tests passed in each mode, and both lint commands reproduced successfully.
The original failed candidate and its review remain preserved.
C15 itself has not performed DNS, a TLS handshake, real IPC, authentication or inference. C16 now
composes it with a killable child and actual loopback TLS/IPC. Eight test groups pass normally and
with Python optimization, including provider-host SNI, wrong-host/untrusted-CA rejection, bounded
response reads, cancellation and wait-based reaping. Resolver/connect stalls and malformed IPC
also have explicit injected seams. Independent review found exceptional setup/cleanup could lose
child ownership or falsely report reaping, the connector did not bind exact method/path, and a
delayed synchronous spawn could exceed the declared total deadline. The first two include actual
owned-process or loopback evidence; delayed spawn and false reaping also use explicit seams.
These findings keep the containment gate open. C16 has contacted no actual provider and proves
no authentication, billing, retention or live response compatibility. Existing
API access blockers and the full matrix remain open; a credential's presence is not access evidence.

## Next gates and owners

| Gate | Remaining NW0 proof or decision | Later implementation owner |
|---|---|---|
| A | Integrate and review the selected sibling projection, typed compatibility and first-release write restriction | NW1 reader/migrations, materializer/writer, journal and owner enforcement |
| B | Cite the completed scoped evidence in final coverage; no new benchmark | NW2 runtime adapter; NW3 installation integration |
| C | Supported Claude boundary/approval, C16 direct-API containment and all 32 real samples | NW2 adapters; NW3 credential custody/onboarding |
| D | D19 corrections/review, Linux bridge approval/GUI, integrated refresh measurements and presentation choice | NW2 presentation; NW3 plugin |
| E | Reconcile every requirement and independently review the complete decision record | NW4 exact release-candidate journey |

The separate-helper choice and no-automatic-writeback restriction preserve stronger boundaries
without claiming their later implementation. A canonical in-vault store and in-process Graphify
remain recorded alternatives, not silent fallbacks. No NW1 schema, production writer, provider
scope reduction, service deletion or completed five-minute result follows from this checkpoint.
