# Harness acceptance evidence

Run acceptance with synthetic content and explicit absolute private output paths.
The contributor entry point is `tools.open_brain_dev.harness_acceptance`. It
requires the public development environment and Node for the optional hook and
setup fixtures. Reports and evidence contain synthetic source context and must
stay outside the repository. Files are owner-only; use a private parent directory.

```sh
python -m tools.open_brain_dev.harness_acceptance --mode fixture \
  --report /absolute/private/run/report.json \
  --evidence-dir /absolute/private/run/evidence
python -m tools.open_brain_dev.harness_acceptance --mode fixture --validate \
  --report /absolute/private/run/report.json
```

Choose a new report and evidence directory for each run. Existing output is
refused. Stdout contains status, counts, and closed error codes. It never includes
transcript bodies or exception details. Every missing, failed, skipped, stale, or
mismatched required fixture check returns a nonzero exit code. The fixture command
does not run the complete repository merge gates.

## What fixture mode proves

Each run creates a disposable local Brain with explicit synthetic work-source
rules and grants for all three readers. It submits supported Codex and Claude
transcript events and Pi branch messages through the public normalizer and durable
outbox. A different useful statement appears earlier than the final four messages
in each source. It checks durable acknowledgements and stable repeated-event
identities, then exercises recall, MCP search, and retained source resolution for
all nine source/destination combinations.

Destination IDs are distinct fixture session labels. Recall observes the bridge
result before a hypothetical task; it does not prove that a real harness injected
that result before inference. Fixture mode invokes no model or live harness.
Mocked transports in the existing scenario tests also remain fixture evidence.

The runner executes a bounded list of existing assertion tests for selection,
recipe details, changed decisions, source truth, local privacy denial, revocation,
credentials, exclusion, partial/rotated transcripts, fork continuity, restart,
ambiguous acknowledgement, retry custody, wrong destinations, deadlines, opt-out,
isolated setup, and preservation of unrelated settings during uninstall. A retained
JUnit artifact must contain every required test group with no failed or skipped
case. This supplements the public-interface matrix; it does not promote mocked
setup executables or simulated hook input into installed-package evidence.

## Evidence contract

The JSON schema name is `open-brain-harness-acceptance.v1`. Fixture reports have
`mode: fixture`, `evidence_kind: public-interface-fixture`, and
`goal_complete: false`. They retain these bindings:

1. Source commit, source-tree digest including uncommitted implementation and test
   inputs, Python executable digest, and run start/end timestamps.
2. Three source rows with distinct session/request/capture IDs, Brain destination,
   introduction/acknowledgement timestamps, enqueue duration, expected text digest,
   and SHA-256 references to retained canonical outbox requests and receipts.
3. Nine pair rows with source/destination, fresh fixture destination ID, matching
   capture ID, observation timestamp, recall duration, paraphrased query digest,
   and a reference to observed recall, MCP search, and source results.
4. A scenario command receipt with the exact required test selectors, zero exit
   code, and SHA-256 reference to JUnit results. Each selected parametrization must
   pass; absent mandatory scenarios and skipped cases fail.

References must point to owner-only bounded files directly inside the declared
private evidence directory. JSON duplicate keys, nonfinite constants, symlinks,
unsafe modes, altered bytes, source/runtime drift, reused source/destination
identities, broken citation/context bindings, and evidence older than 24 hours
fail validation. Capture acknowledgement must occur within 120 seconds; enqueue
must take at most two seconds and recall at most three. Automatic result shape is
limited to five rows and eight KiB of wrapped context.

Hashes detect changed evidence and bind observations; they are not signatures or
proof against an owner forging an entire fixture bundle. Native execution needs a
collector that observes actual supported harness commands and hook lifecycles.

## Native collection boundary

```sh
python -m tools.open_brain_dev.harness_acceptance --mode native \
  --report /absolute/private/native/report.json \
  --evidence-dir /absolute/private/native/evidence
python -m tools.open_brain_dev.harness_acceptance --mode native --validate \
  --report /absolute/private/native/report.json
```

The entry point delegates native collection and verification exclusively to
`tools.open_brain_dev.harness_native_acceptance`. Its integration functions are
`run_native(report_path=..., evidence_dir=...)` and
`validate_native(report_path=...)`, with keyword-only absolute `Path` arguments
and a metadata summary containing `status` and `mode`. If that collector is
unavailable, native mode returns `native_collection_unavailable`. Fixture reports
are rejected before native verification. A mode flag or owner-authored success
boolean cannot certify native acceptance.

The native collector must bind actual Codex/Claude CLI or guarded Pi SDK
execution, new synthetic session logs, observed lifecycle hook output, durable
receipt/capture identity, exact installed runtime/asset hashes, timestamps,
pre-inference context and on-demand tool results. Its validator owns that proof
boundary and must fail on missing actual observations. The fixture implementation
does not supply those observations.

Fixture success keeps native sessions/scenarios, clean-account installed-package
setup, private backup/restore and cutover/rollback, reviewed merged installed
versions, required merge gates, and private handoff incomplete. The current
selection and native-format limits are described in `harness-memory.md`; optional
installation and failure handling are described in `harness-setup.md`.
