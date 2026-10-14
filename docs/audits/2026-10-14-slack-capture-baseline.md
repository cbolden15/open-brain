# Slack capture implementation baseline

Baseline checkout: `eb3961e83da391a9282b501734e852fdaed1d463`, branch `feat/slack-capture`, inspected October 14, 2026. This record uses public synthetic fixtures. It grants no Slack access and does not activate a collector schedule or private pilot.

## Installed and source boundaries

The installed executable at `/opt/homebrew/bin/open-brain` reports `open-brain 0.1.0`. Its `capture --help` lists text, `--json`, and `--data-dir`; it does not list `--privacy-tier`. Its top-level help does not list catalog, source, facet, or review commands. `catalog --help` returns the metadata-only parse failure “Open Brain could not parse the command.” These inventory commands do not bootstrap a user Brain.

The baseline checkout supports owner CLI `capture TEXT --privacy-tier TIER`. Ordinary MCP `brain_capture` intentionally refuses that owner-only argument. Separately granted destination-bound `brain_capture_submit` accepts requested tiers within its startup-policy capture tier set. A rejected argument on ordinary capture does not justify expanding that tool's authority.

Checkout `catalog --json`, invoked without a session or Brain bootstrap, reports state schema 15, runtime session 10, Portable metadata 10, bridge protocol 1, catalog schema 2, and task contract `t03.v1`. The implementation target advances state/runtime/Portable to 16/11/11. Compatibility verification must include migration and retained evidence export/import; installed 0.1.0 is not proof that these source-only features ship in that release.

## Existing contracts and extensions

| Surface | Baseline | Implementation boundary |
| --- | --- | --- |
| Owner privacy tiers | Existing in source, absent from installed capture help | Preserve owner authority and provide supported invocation guidance |
| Destination-bound capture | Existing constrained tier capability | Reuse its Brain identity, issuer epoch, policy and replay checks |
| Privacy narrowing | Existing canonical admission boundary | Narrow before retrieval; default classifier remains disabled |
| Slack source identity | Existing account/channel/message/revision identity | Reuse managed revision capabilities across discovery paths |
| Protected staging and retry | Existing collector custody and atomic private JSON storage | Retain unresolved work and freeze immutable processing operations |
| Source privacy mappings | New extension | Bind validated local tiers to selection, staged intake and replay |
| Derived developments | New extension | One capture per supported claim with canonical evidence binding |
| Attention and candidates | New extension | Preserve engine proposal terminals and owner facet decisions |
| DM discovery | Separate opt-in work | No implied DM access or permission expansion |
| Scheduling | Optional collector boundary | No automatic service installation or activation |

Baseline Slack live intake uses the exact personal/local-only decision returned by `local_source_privacy()`. Collector preview and retained intake decoding also require that decision. Per-source mappings therefore require changes at both live admission and replay validation, not only a Slack configuration field.

A missing or invalid processing policy must not trigger provider processing. Unknown-classification observations remain protected locally. Model or provider extraction requires explicit policy and applicable source/privacy permission before input dispatch. Space membership remains organization metadata, not access control.

## Synthetic verification

Initial focused baseline checks passed: 20 privacy/tier/capture-submit tests, five boundary/classifier tests, and ten live-capture integration tests. These establish checkout contracts; they do not establish installed-release parity or real Slack coverage.

Independent implementation acceptance resides in `packages/collector/tests/integration/test_slack_capture_acceptance.py`. It checks configured work privacy, unresolved preview retention, invalid policy rejection before fetch, policy-generation fencing, shared identity deduplication, revision succession, retry, and explicit deletion-marker succession. After engine schema 16 became available, the independent Slack acceptance module and candidate/review module passed together: nine tests in 5.11 seconds. Ruff passed for the independent acceptance module, and its strict mypy check passed. The real engine existing-value assignment test confirms that discarding an acknowledgement and replaying the frozen request returns the original facet receipt.

Processing tests cover two developments, dual discovery, repeated material, changed revisions, lost capture acknowledgement, exclusion, unknown privacy, queue limits and input budget. Candidate/attention tests cover owner authority, scope separation, retained rejection/deferral, frozen facet requests, lost acknowledgement, weekly bounds, deferred resurfacing and engine terminal-status preservation.

Explicit deletion-marker succession is distinct from proving deletion from source absence. Connector coverage must retain its complete-scan rule; an incomplete or failed scan cannot prove removal.

## Remaining acceptance

No private selections, credentials, taxonomy, channel names or messages were inspected. No real-source recall, precision, cost, daily reliability, or seven-day pilot claim is made here. Those require a separate owner-authorized private deployment and the plan's pilot gates.

Use a matching 16/11/11 runtime for implementation acceptance and portable roundtrip verification. Do not run an older installed binary against migrated state.

## Public implementation verification

The public candidate on `feat/slack-capture` completed all repository verification
components using synthetic evidence:

- `make verify`: lint and strict mypy passed; the full Python suite passed 7,142
  tests with five platform-dependent filesystem skips. Package builds passed.
  The plugin stage exposed missing current admission metadata in historical codec
  fixture consumers. Their test harnesses were corrected without editing pinned
  fixture bytes.
- `make build plugin-test desktop-test` passed after that correction: all 664
  plugin tests, 259 desktop UI tests, 30 desktop Python checks and 26 Rust tests.
- `make native-audit homebrew-smoke` passed for the actual macOS arm64 candidate.
  Native capture, search, verified export, Markdown import, local MCP, Graphify and
  Obsidian journeys passed. Guarded cleanup verified the existing installed product
  was preserved.
- `actionlint .github/workflows/ci.yml` and `git diff --check` passed.

The five Python skips reflect this filesystem's filename normalization and
non-UTF-8 filename behavior. An existing multithreaded-fork test emits a Python
deprecation warning. No product deadline or validation rule was relaxed.

Canonical development evidence, transitive approval checks and append retry
reservations now survive Portable 11 restoration. Current schema/admission/Portable
coordinates are 16/11/11; historical migration checksums and format inventories
remain frozen. Private collector policy, queues and checkpoints still require the
documented collector backup alongside the Brain export.

The default extraction baseline recognizes labeled statements and preserves
uncertainty. It does not establish general conversational recall. Richer private
adapters, live quality measurements, schedule activation and the private pilot
remain deployment work.
