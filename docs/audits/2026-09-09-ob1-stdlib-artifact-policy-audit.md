# OB1 approved standard-library artifact policy audit

Date: 2026-09-09

## Scope and approval

The owner approved the two exact name/hash exceptions in the
[standard-library policy decision](2026-09-09-ob1-stdlib-content-policy-proposal.md).
The implementation adds those two pairs to the bounded artifact auditor. After a PYZ module passes
complete code parsing and content scanning, only that module location's `private-ip-address`
finding is removed. The entire expanded marshal payload must match the approved SHA-256.

Owner-denylist terms, every other rule, other locations, malformed input, parser/resource limits,
and worker failures remain blocking. Source and history checks are unchanged. No runtime code,
packaging specification, upstream constants, toolchain selection, or dependencies changed.
The product plan and artifact contract record the approved scope. No push, merge, release, or tap
change is part of this decision.

## Verification

Seventeen new regression cases exercise the approved key set, exact name/hash matching, changed
bytes, other rules, enclosing archive/loader/TOC findings, another module and member, malformed
marshal, trailing bytes, resource limits, ordinary archives, and current-tree/history isolation.
All 121 focused release-audit tests passed.

The metadata-cleaned macOS archive passed the actual bounded worker with the canonical owner
denylist and zero findings. Its SHA-256 is
`bf8303aa3f3a8355f22135295b3b64330faf5859fafd6284c1ec368063a49de5`.
The same archive passed native and Homebrew smoke in the
[preceding metadata milestone](2026-09-09-ob1-native-release-metadata-audit.md).
It was not rebuilt for this audit-tool-only change.

`make verify` passed: Ruff, MyPy across 587 source files, 3,686 tests passed with five filesystem
skips, and all six wheel/source-distribution builds. The six newly built distributions also passed
the bounded owner artifact inspector. The candidate source audit passed in an isolated single-branch
clone. `git diff --check` and workflow lint passed. The independent read-only review found no
blockers. Source/history gates must also pass at the exact final commit before handoff.

## Remaining release gate

A rebuilt Linux CI executable still needs its own owner artifact audit. The older CI
`urllib.request` payload is not approved; the matcher must not be widened to accept it.
Python patch or build changes may produce different marshal hashes. Changed payloads require a
new provenance review and explicit approval. A clean macOS result does not establish Linux or
release readiness.
