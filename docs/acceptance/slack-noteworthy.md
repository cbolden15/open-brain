# Slack noteworthy-channel acceptance

This record contains only safe acceptance metadata. It does not contain Slack message bodies,
account identifiers, access tokens, raw receipts, or private Brain paths.

## Synthetic disposable-Brain acceptance

Status: passed on 2026-09-18.

Command:

```sh
UV_CACHE_DIR=/tmp/open-brain-uv-cache uv run --frozen pytest -q \
  packages/collector/tests/integration/test_slack_policy.py \
  packages/collector/tests/integration/test_slack_patches.py \
  packages/app/tests/integration/engine/test_review_patches.py \
  packages/engine/tests/unit/review/test_review_models.py \
  packages/connectors/tests/contract/test_slack_auth.py \
  packages/connectors/tests/contract/test_slack_live.py \
  packages/connectors/tests/contract/test_slack_source_adapter.py
```

Result: 35 passed. The acceptance uses an injected synthetic Slack transport and disposable Brain
roots. It covers candidate suggestions remaining pending until approval, explicit mappings,
revision-bound append patches, derived diffs, target drift returning `review_conflict`, idempotent
restart catch-up, revoked access, and capture-failure recovery.

## Real-source owner-run evidence

Status: owner-run required; not executed by this checkout session.

Run against a disposable Brain and a small owner-selected Slack test channel after registering the
PKCE app and consenting to the narrow user scopes. Record only the date, pass/fail per scenario,
opaque source/proposal IDs if needed, and safe error codes. Do not commit message bodies, account
data, tokens, URLs containing private identifiers, or raw provider receipts.

## Repository verification

`git diff --check` and `actionlint .github/workflows/ci.yml` passed. `make verify` passed Ruff, then
stopped at strict MyPy errors already present in the Slack implementation and focused tests; no
runtime or package code was changed in this documentation phase, so native/package checks were not
run.
