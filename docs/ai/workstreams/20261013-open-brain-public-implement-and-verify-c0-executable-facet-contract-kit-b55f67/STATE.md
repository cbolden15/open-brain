# Workstream State

- ID: `20261013-open-brain-public-implement-and-verify-c0-executable-facet-contract-kit-b55f67`
- Repo root: /Users/calebbolden/Projects/oss/open-brain-public
- Remote identity SHA-256 fingerprint: `0aaeca2a6d1b421eaa7439b39b3228c836f6c7c55026bac150b0aeda757566cf`
- Worktree: /Users/calebbolden/Projects/oss/open-brain-public
- Branch: feat/facets-c0
- Objective: Implement and verify C0 executable facet contract kit
- Created date: 2026-10-13

## C0 ownership and execution

Status: C0 verified; no runtime facet implementation is claimed.
Baseline: `bd368ec`. Coordinator branch: `feat/facets-c0`.

| Owner | Exclusive files | Verification |
|---|---|---|
| Wire worker | New engine facet contracts, wire schema/validator, wire contract tests | Focused contract tests, ruff, mypy |
| Evidence worker | New draft evidence module/schema, facet fixtures/manifest, evidence tests | Focused evidence tests, schema checks, ruff, mypy |
| Independent checker | New adversarial contract tests | Focused malicious-input and boundary cases |
| Coordinator | App test fake, adapter contract tests, guide, this ledger; integration fixes after workers finish | Focused combined tests, make verify, diff and workflow checks |

Workers use isolated worktrees. Two implementation workers and one independent checker are reserved; total dispatches
are three, with at most three active children and twelve total attempts. Workers may commit only
their assigned files and may not push. No shared production registration file is assigned.

Inputs for the next tasks are listed in `docs/design/facet-contract-kit.md` and the fixture task
manifest. No downstream task starts from an unverified C0 candidate.

## Verified C0 output

Contract revision: `facets-c0-v1`.
Fixture manifest SHA-256: `eceaa89f146eeb133f780916fb006da211e5ab479ea8231b63c705f3bde000ca`.
The C0 output commit is the commit introducing this verified state; consumers pin its immutable
Git ID before dispatch. Exact committed identity is also recorded in the coordinator run ledger.

Verification evidence:

- Combined C0 suites: 287 passed, including 95 independent adversarial cases.
- `make verify`: lint/typecheck passed; full Python suite reported 3,636 passed, five skips,
  and one pre-existing contributor-guide failure. The missing documented CLI/entrypoint rows were
  restored; the exact failed test then passed. No runtime code changed after that full run.
- `make lint typecheck build plugin-test desktop-test`: passed after the guide repair. Plugin:
  291 tests; desktop frontend: 259; desktop Python: one; Rust: 26. The engine wheel contains exact
  C0 module/schema bytes. The five Python skips are filesystem normalization checks.
- `git diff --check`, staged diff check, and `actionlint .github/workflows/ci.yml`: passed.

No unresolved C0 blocker. Historical schemas and production discovery are unchanged. No push,
publication or deployment occurred. Runtime tasks and their real-engine F01–F22 evidence remain
pending. The next authorized milestone expands the delivery plan to six workers plus a coordinator;
that plan update follows this C0 output and does not change its frozen inputs.
