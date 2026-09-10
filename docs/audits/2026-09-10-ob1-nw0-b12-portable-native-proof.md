# NW0-B12 portable native packaging proof

Status: corrected macOS proof passes; native Linux CI remains pending. NW0 remains open.

The helper source, synthetic fixtures, hash-pinned wheel inputs, and maintained Graphify patch now
live in `tools/nw0_graphify_probe`. The same command builds and checks both supported native targets.
CI publishes the helper archive and two bounded verification reports separately from base releases.
The shipping application and its dependencies are unchanged.

## Evidence on this Mac

| Check | Result |
|---|---|
| Materialized helper/parser contracts | 10 grouped tests pass |
| Source-only frontmatter diagnostic | 20 checks pass |
| Corrected frozen helper | 84 runtime checks pass |
| Native identity | arm64, ad-hoc signature verification passes |
| Archive | 9,529,914 bytes; reproducible from identical executable bytes |
| Content inspection | Generic and authoritative owner-private source/artifact audits pass |
| Full project verification | 3,698 tests pass, 5 skip; lint, strict types and distribution builds pass |
| Required native rebuild and Homebrew smoke | Pass, including capture/search/import/MCP/export journey |
| Independent review | Scoped pass after correction; 12 focused supervisor/envelope tests pass |

Corrected helper SHA-256:
`77655ec1eaf14e6dada29aa21e60154188d01d4467f8d674afa014dfe20258b0`.
Corrected archive SHA-256:
`75ca9d637239fd4c0842e8e9768671705f4b0505c86901b56366d14bf4c9410d`.
These identify this build only. CI produces fresh platform receipts and hashes.

## Corrections retained from independent review

The initial experimental entrypoint exposed a frontmatter diagnostic accepting a directory. That
route bypassed the selected-body protocol and was removed from the frozen helper. The diagnostic
runs only from staged test source. Native checks now reject the old route and exercise frontmatter
through the normal request protocol; the frozen closure excludes the diagnostic module.

The build coordinator now caps combined logs at 4 MiB and kills its owned process group on failure
or timeout, including when a parent exits while a child holds its output pipe. Regression tests
exercise log overflow, nonzero exits, and a delayed descendant write after parent exit.

The base archive writer assumed an executable named `open-brain`. The helper now has its own exact
five-member envelope: `open-brain-graphify` and four fixed legal files. The native auditor recognizes
only this named proof envelope, caps each legal file at 64 KiB, and retains its existing bounded
native scanner. Tests reject missing, extra, duplicate, symbolic-link, oversized, and wrong-name
members and prove that private-term scanning reaches both executable constants and legal text.
The base one-executable contract, generic archive caps, and reviewed content exceptions are unchanged.

Initial artifacts and failed checks remain in private workstream evidence. None is promoted as the
corrected candidate. Graphify's Apache 2.0 license, earlier MIT text, upstream notice, and a separate
modification notice accompany the binary; dependency notices also remain embedded as runtime metadata.

## Scope and next gate

The 58 synthetic Obsidian cache cases retain 56 matching target-presence observations and two
explicitly unsupported cases. They do not prove full Markdown grammar or per-occurrence provenance.
Portable runtime checks use isolated homes and temporary roots but do not enforce an OS sandbox.
There are zero model calls. This proof does not pass controlled cold startup, paired Homebrew helper
installation, shipping protocol compatibility, a real provider, or desktop integration.

The reviewed A10 private identity proof adds eight passing tests for rename/restart identity,
unconfirmed missing notes, external edits, unsafe paths, exclusions, bounds and rollback. Its
observation interface remains trusted and it does not authorize filesystem write-back. C12 found no
supported mechanism in the inspected Claude client sources/docs that closes C7. D9 confirms UTM's
disposable-start control; the required x86_64 desktop guest and journey remain unproven.

The owner authorized continued effort beyond previous ceilings and necessary publication. All
prior effort remains charged; product call/byte/time, synthetic-data and consent controls remain.
Next: require macOS/Linux CI on the exact published commit and independently audit downloaded
artifacts before relying on their native platform evidence. C7 remains closed and NW1 remains gated.
