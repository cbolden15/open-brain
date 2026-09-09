# OB1 bounded native-artifact audit

Date: 2026-09-09

Implementation branch: `fix/ob1-native-artifact-audit`

Implementation commit: `112580ce0bcf94e9d458fe098dff9368e8566b2f`

Code baseline: merged W7 `d81bb6444e5ce9a2710401eed62d8ab41454e872`, with the local readiness
report at `888e2069db157a6364843f5449f46590e137eceb`.

## Result

The native content auditor now inspects both existing CI archives completely. Neither archive fails
because of its executable size or an unsupported parser path. Both still fail the unchanged content
policy on packaged build paths and standard-library private-address examples. No owner-denylist
match was found in either CI artifact. Publication remains blocked; this change adds no exemption.

The owner CLI's explicit `--artifacts` path now invokes a bounded worker. Source/history checks keep
their 2 MiB limit and canonical denylist. The existing history-only exception remains unchanged.
Contributor checks and CI do not acquire private audit dependencies.

## Implementation and review

The worker reads native archives as data: tar/gzip, ELF or Mach-O payload boundaries, PyInstaller
CArchive, compressed PYZ modules, base-library ZIP, and Python 3.14 marshal records. It does not
import bundled modules, construct code objects, execute code, or extract files. Raw and decoded
contents use the same generic rules and normalized owner terms.

Explicit input, expansion, entry, object, nesting, metadata, time, and output limits fail closed.
The worker also has a Linux address-space ceiling. Tar and ZIP metadata are bounded before their
standard-library readers allocate member objects. Native locations are opaque and matching bytes
never appear in diagnostics. Exact limits and the owner invocation are documented in
`docs/artifact-characterization.md`.

Two independent read-only reviews covered the design and implementation. The implementation review
confirmed four issues, each repaired with a regression: PAX allocation before entry counting,
uncovered ZIP payload gaps, nonzero tar member padding, and a native gzip filename accepting plain
tar. The final static rereview found no remaining blocker. The reviewer did not run tests or access
the private denylist; the coordinator owns the execution evidence below.

PYZ keys are import names, not physical file extensions. The inspector scans their original spelling
and maps them to module paths before applying path policy. This avoids mistaking the engine's
`storage.sqlite` module for a SQLite database. Linux's generated sysconfig module also requires the
hyphens that its import identity actually contains. Neither handling change exempts content.

## Actual CI archives

Both archives came from successful goal-head CI run
[34348556945](https://github.com/cbolden15/open-brain/actions/runs/34348556945), for the exact W7
baseline. Their SHA-256 values still match the CI manifests:

| Platform | Archive SHA-256 | Completed owner audit |
|---|---|---|
| Linux x86_64 | `0528020e12d439aa4737f52d804f95c10f4cbeb3b99e55745d4b7ce747d33f8a` | Five findings: three absolute-home-path, two private-ip-address |
| macOS arm64 | `acaf0415e495550711d6cffbc884b5144769606a5a5c8a6b95e560f2bcbbf202` | Four findings: two absolute-home-path, two private-ip-address |

Inspection on the macOS host took about 9.2 seconds for Linux and 6.4 seconds for macOS. No
`artifact-invalid`, resource-limit, worker-failure, or source-size finding occurred. Platform format
coverage comes from both real binaries and synthetic containers; the Linux worker's OS resource
limit has not yet been rerun in Linux CI for this feature branch.

The home-path findings are in both distributions' `direct_url.json` build metadata and, on Linux,
the generated sysconfig module. The address findings are in `ipaddress` and `urllib.request`.
These are reported policy findings, not evidence that private user notes were bundled. No matching
path, address, or private denylist term is included in this report.

## Verification

- Focused release and native-auditor regressions: 74 passed.
- `make verify`: lint, types, distribution builds passed; 3,655 tests passed and 5 filesystem tests
  skipped in 105.80 seconds.
- `make homebrew-smoke`: fresh macOS arm64 build, strict signature verification, native product
  checks, isolated Homebrew install, capture/search/import/MCP/export journey, and teardown passed.
  No pre-existing product installation was present.
- `make audit` and `make audit-history`, with the unmodified canonical denylist, passed from an
  isolated single-branch clone of the implementation commit.
- All six freshly built wheels and source distributions passed the artifact inspector.
- The real owner CLI invocation against both CI archives exited 1 with exactly the nine expected
  content findings and no source findings.
- `git diff --check` and `actionlint .github/workflows/ci.yml`: passed.

The 50 new synthetic cases cover large safe binaries, nested owner/generic canaries, Unicode
normalization, passive code handling, invalid marshal, decompression, table boundaries, links,
metadata allocation, hidden gaps/padding, worker errors, redaction, and the actual owner CLI route.
No binary fixtures or private data were committed.

The freshly built local macOS archive also completed inspection in about 6.3 seconds, with the same
four generic findings and no owner-term, parser, or limit findings. Its SHA-256 is
`7997b775748fb95f82528f9cbf97a41825083752d740a4520313e66e3b4d111e`. Passing native/Homebrew
checks does not override these content findings.

## Remaining release work

Remove build-machine paths from distributable metadata and resolve the standard-library address
findings through a separately reviewed content-policy decision. Run the artifact-aware owner audit
again on both final platform archives before accepting a combined publication manifest. The readiness
report's repository-settings and tap blockers remain outside this implementation.

No push, PR, merge, release publication, repository-setting change, or tap update was performed.
