# Workstream State

- ID: `20261015-open-brain-public-python-test-performance-e2d42a`
- Repo root: $REPO/.worktrees/python-test-performance
- Remote identity SHA-256 fingerprint: `0aaeca2a6d1b421eaa7439b39b3228c836f6c7c55026bac150b0aeda757566cf`
- Worktree: $REPO/.worktrees/python-test-performance
- Branch: perf/python-test-performance
- Objective: Profile and safely improve Python test performance with explicit tiers, measured before/after, local commits only
- Created date: 2026-10-15

## Source and scope

- Source: `feat/competitive-gap-reconciliation` at `8ef1d069259c291e669341079806103c8fa90e86`
  (no advancement found; W4 integration `7b511c2` is an ancestor).
- Authorized: isolated worktree, task environment, synthetic benchmarks, test/dev tooling,
  documentation and local commits. Not authorized: push, merge, deployment, publication,
  real client/model/provider checks, real Slack or Brain access, private pilots, W5 work.
- Known inherited issue left untouched: the strict historical MCP documentation-pin mismatch
  (DOCS-004).

## Decisions

1. Profiling uses the opt-in reporter `tools/open_brain_dev/pytest_timing.py`; the baseline
   is `5afee33` (8ef1d06 plus the inactive-by-default reporter).
2. O1, production: the owner approved raising `_normalized_sql` from `lru_cache(128)` to
   `lru_cache(1024)` after evidence that the cyclic schema scan never hit the cache. No other
   production, schema, Portable, dependency or packaging change.
3. O2, workers: 2 and 4 workers compared on the frozen candidate. Acceptance keeps the
   established two-worker configuration; four workers are documented as the measured faster
   local option. Makefile `-n auto` defaults unchanged.
4. O3, prepared-store reuse: declined. A prepared Brain embeds its absolute root in
   `.open-brain/cursors/identity.json` and keeps live WAL and lease files.
5. Fixed sleeps (29 s cumulative) and xdist scheduling (under 2.5 s idle per worker) were
   measured as immaterial and left unchanged.
6. Tiers are behavior-classified from the measured baseline profile; `make test` and
   `make verify` still run the complete suite.
7. Verification authority: implementers ran scoped checks only (no make/uv, TOOLING-003);
   the coordinator runs `make verify` (two workers) and the combined native gate on the frozen
   candidate.

## Candidate

- Code candidate reviewed and benchmarked: `86cb78cd97f0ddbc43e04b5f3fe5f0254fa6369c`.
- Independent GPT-6 Astra review: first pass REQUEST CHANGES (5 P2, 1 P3; production fix
  clean); all six resolved and re-review APPROVE with zero findings.
- Documentation and this continuity are added in the following commit; final gates run on
  that frozen commit.

## Measurements (pytest wall time; details in docs/engineering/test-performance.md)

| Run | HEAD | Workers | Result | Wall |
|---|---|---:|---|---:|
| Baseline full | 5afee33 | 2 | 7,480 passed, 5 skipped, 1 warning | 2,330.7 s |
| Candidate full | 86cb78c | 2 | 7,535 passed, 5 skipped, 1 warning | 1,474.9 s |
| Candidate full | 86cb78c | 4 | 7,535 passed, 5 skipped, 1 warning | 897.7 s (backup overlap) |
| Candidate full, repeat | 86cb78c | 4 | 7,535 passed, 5 skipped, 1 warning | 819.3 s |
| Fast tier | 86cb78c | 2 | 4,725 passed, 5 skipped | 106.5 s |
| Mix baseline | 2c8eafb | 2 / 4 | 407 passed x6 | median 195.8 / 111.2 s |
| Mix candidate | 86cb78c | 2 / 4 | 407 passed x6 | median 125.5 / 69.2 s |

Collection audit: 7,540 collected = 4,730 fast + 2,810 integration, disjoint, every one of
the 7,485 baseline IDs present (`baseline_checked: true`). Benchmark execution used about
118 of the 180 budgeted minutes.

## Status

Complete local candidate. Tested HEAD `330c243cf9002146d552c238db53be8e9f31ea59` (frozen
inventory of 1,955 inputs, SHA-256 `c0c2eee042a58473b4095c8f27a96ffa26cb7812bb2400fb10a3d33a6fc5ffc5`,
identical before and after every gate):

| Gate | Result | Elapsed |
|---|---|---:|
| `make verify`, two workers | PASS; Python 7,535 passed, 5 skipped, 1 warning in 1,433.96 s; plugin 700, desktop frontend 286, desktop Python 30, Rust 26 | 1,555.47 s |
| `make native-audit homebrew-smoke` (NATIVE_OUTPUT outside checkout) | PASS; journey and self-check passed; existing product preserved | 511.05 s |
| `git diff --check` | PASS | 0.02 s |
| `actionlint .github/workflows/ci.yml` | PASS | 0.15 s |

The first native, diff-check and actionlint attempts were refused by the measurement harness
before starting (free disk 1.48 GB after `make verify`); they ran after removing this task's
own Cargo target. Receipts, evidence digests and native artifact digests are in
`PERFORMANCE-RESULTS.json`. This status and that file were committed after the tested HEAD
as documentation-only continuity.

Not done: deployment, publication, live client/model/provider checks, real Slack or Brain
access, W5 or competitive-gap features.

## Merge to main

The owner authorized push, PR and merge, and chose to rebase onto `main` so the 32 unmerged
feature-base commits stay out. The nine commits were cherry-picked onto `origin/main`
(`d5cf64e`) as branch `perf/python-test-performance-main`. Changes from the tested tree:

- The gotcha registry kept only TESTING-008, TESTING-009 and BUILD-015; the feature-base
  entries were dropped.
- `tests/tiers/integration.txt` was filtered to `main`'s collection: 1,076 of 1,118 entries
  kept, 42 feature-base-only function IDs removed. Tier audit on `main`: 7,239 = 4,585 fast
  + 2,654 integration, disjoint, nothing stale. The one renamed `main` test absent from the
  profile (`test_schema_newer_than_sixteen_fails_closed`) matches a counterpart measured at
  0.19 s with no spawns or sleeps, so it stays fast.
- Local free disk was 1.2 GiB, below the 2 GiB capture watermark, so a full local
  `make verify` could not run. Local checks were lint, typecheck, the tier audit and the
  changed tests. The merge gate is the six required CI checks, including
  `make contributor-check` on Linux x86_64 and macOS arm64, on the PR head.
