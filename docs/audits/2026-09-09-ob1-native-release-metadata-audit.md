# OB1 native release metadata audit

Date: 2026-09-09

Implementation: `e5fdc3b95dc5cf05bcbd456e19e628dbd7217a04` on
`fix/ob1-native-release-metadata`, based on the bounded auditor at `46bf308`.

## Result

Native build-path remediation is implemented and verified locally. The new macOS archive has no
home-path or owner-denylist findings. Its remaining two findings are the standard-library address
matches described in the [separate policy proposal](2026-09-09-ob1-stdlib-content-policy-proposal.md).
That proposal is not approved or enabled. Publication remains blocked.

The earlier report called all standard-library address matches examples. Inspection established
that three strings in `ipaddress._IPv4Constants._private_networks` are runtime classification data;
the `urllib.request._proxy_bypass_winreg_override` match is a docstring example. Neither module was
changed to disguise or remove the matching content.

## Changes

- Native distribution metadata now uses positive selection. Dependency/version metadata, entry-point
  metadata, and legal files survive. Installer provenance, RECORD, and uv cache files do not.
  Normal resources are unaffected; metadata roots are recognized in nested and case-varied paths.
- Generated sysconfig data keeps every key and scalar type. References to the source mapping's own
  `prefix` and `exec_prefix` become runtime-prefix expressions, with longer prefixes handled first.
  Unrelated values remain unchanged. Unexpected home paths and nonliteral generated modules fail
  the build. Original build prefixes are absent from the compiled replacement.
- The pinned PyInstaller code cache replaces only the expected sysconfig module. Other cached code
  objects retain their existing transformations. The supported build always uses `--clean`.
- The existing frozen self-check now verifies bin/library paths and the pointer ABI. Its output
  schema is unchanged. Ordinary CLI behavior and source/history/artifact content rules are unchanged.

This relocates runtime metadata; it does not make the frozen application a Python extension SDK.
No private denylist entry, scanner limit, or content finding was exempted.

## Verification

| Check | Result |
|---|---|
| Focused native distribution, metadata, and self-check tests | 29 passed |
| `make verify` | Lint, types, and builds passed; 3,669 tests passed and 5 filesystem tests skipped in 117.20 seconds |
| `make homebrew-smoke` | Fresh native build, strict macOS signature check, installed self-check, capture/search/import/MCP/export journey, and guarded teardown passed |
| Existing product installation | Absent; the isolated smoke formula was used |
| Canonical `make audit` and `make audit-history` | Passed in a disposable single-branch clone of the implementation commit |
| Six newly built wheels/source distributions | All passed the bounded artifact inspector |
| `git diff --check` and `actionlint .github/workflows/ci.yml` | Passed |

The native bundle retains 12 distribution-metadata files, including app/engine LICENSE and NOTICE
and the rfc8785 license. Installer records and cache files are absent.

A passive data probe of the original Linux CI sysconfig module recovered its literal mapping,
without executing that bundled module. Applying the same relocation retained all 1,142 keys and
scalar types, preserved non-prefix values, and produced no generic content finding. Host-source
regressions also compare every generated metadata key and scalar type. This is not a Linux runtime
execution result. A rebuilt Linux executable still needs CI and its own final owner artifact audit.

The new macOS archive is `open-brain-0.1.0-macos-arm64.tar.gz`, SHA-256
`bf8303aa3f3a8355f22135295b3b64330faf5859fafd6284c1ec368063a49de5`.
Its complete worker inspection returns exactly two `private-ip-address` findings, one in each
reviewed standard-library module. There are no path, owner-term, parser, or resource-limit findings.

Both current module sources match CPython `v3.14.4` byte for byte. Compiling those official sources
and applying PyInstaller filename normalization matches every bundled code field and typed constant,
including nested code objects. Exact source and marshal digests are in the proposal. The older CI
`urllib.request` payload is not covered by the proposed approval.

## Independent review

A read-only design review endorsed positive metadata selection and relocation from the declared
source prefixes, with preservation and fail-closed checks. A separate implementation review found no
remaining blocker after reconciliation. Its initial Python 3.12 syntax concern was withdrawn: the
baseline already requires Python 3.14 syntax, and the documented supported v0 runtime is 3.14.
The metadata lower bound exists only for frozen P4 replay.

## Next decision

Review and explicitly approve or decline the two exact module/hash/rule exceptions in the policy
proposal. If approved, implement the narrowly scoped disposition and its negative contract tests,
then rebuild and audit both final platform archives. A Python patch update that changes a payload
cannot use the old exception. The current build selects Python 3.14, so this remains a release check.

No push, PR, merge, repository-setting change, tap update, or release publication was performed.
