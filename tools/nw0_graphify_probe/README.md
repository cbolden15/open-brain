# NW0 Graphify native proof

This experiment makes the reviewed Markdown helper reproducible on macOS arm64 and Linux x86_64.
It does not add Graphify to the application, alter its dependencies, or install an Obsidian plugin.

Run from the repository root after building the base executable:

```sh
make native
uv run --frozen --python 3.14 --no-dev --group native-build python -m tools.nw0_graphify_probe.run
```

The output directory must be new. Use `--output build/nw0-graphify-next` for another run so failed
evidence is preserved. `--base` selects an existing native base executable. Build commands have a ten-minute
supervision limit, a 4 MiB combined log cap, and process-group termination on failure; each native test has a separate bounded input, output, and process deadline.
Only dependency installation contacts the package index. The proof makes zero model calls.

## Inputs and maintained patch

`requirements.txt` pins the upstream wheel hashes for Graphify 0.9.57, PyYAML 6.0.3,
markdown-it-py 4.0.0, mdurl 0.1.2, and mdit-py-plugins 0.5.0. PyYAML is frozen with its pure Python
implementation. Wheels install without transitive dependency resolution into the new proof output
directory. The project's native-build group supplies the pinned PyInstaller toolchain.

`patch-manifest.json` checks upstream preimages before applying four maintained overrides and checks
the resulting seven-module Graphify source closure. The overrides make language exports lazy,
separate pure metadata/discovery helpers, and supply a selected-root Markdown extraction path using
the bounded parser. New modules have a null upstream preimage. Every override carries a modification
notice. Graphify's Apache 2.0 license, retained MIT license, and upstream notice are in `licenses/`;
distribution license resources are also preserved in the native archive.

The upstream source pin is `3f82bf7f837a07fb0f7668fbdbd5662801906942`, previously checked against the
0.9.57 wheel in NW0-B1. The maintained patch is permitted by the upstream-first adoption decision.
It is not represented as an upstream release or an accepted upstream contribution.

`payload/*.py.txt` and `overrides/**/*.py.txt` are experimental source inputs, materialized as Python
only inside the build directory. They stay outside the shipping package and strict application type
surface. The typed coordinator is checked by normal repository lint and mypy. The materialized helper
and parser run their own contract tests, followed by tests against the frozen executable.

## Evidence and limits

The semantic fixture contains synthetic selected notes, an excluded canary, and expected explicit
links. The cache fixture retains only synthetic bodies and Boolean target-presence observations
from Obsidian 1.12.7. It covers 58 cases: 56 matching cases and two explicitly unsupported cases.
It does not contain raw vault metadata, local paths, credentials, or private observation receipts.
Embeds and links share a deduplicated presence edge here; occurrence types and offsets are not proven.

The runtime checks cover selected IDs, deterministic output, incremental explicit links, unresolved
and ambiguous targets, malformed requests, byte limits, process timeout, frontmatter, and the cache
fixture. Each invocation uses a fresh temporary directory and an empty test home. These portable
checks do not enforce an OS sandbox and do not prove arbitrary-code confinement.

The audit requires the exact Graphify module closure, excludes advanced product and parser modules,
validates native architecture/signature, and inspects the packaged artifact with the existing bounded
content auditor. The helper envelope has exactly one `open-brain-graphify` executable and four
fixed legal files, each capped at 64 KiB. The base archive contract and all native scan limits stay
unchanged. The source-only frontmatter diagnostic is absent from the frozen helper.
It checks archive reproducibility from the same executable, not bit-identical
rebuilds. CI runs generic content rules. The owner's private denylist audit remains a separate gate
before using downloaded artifacts as final publication evidence.

Only the archive and path-free verification reports are uploaded under the dedicated `nw0-graphify`
artifact name. Build logs and staged inputs remain local to the runner. This is packaging and runtime
evidence, not controlled cold-start timing, a Homebrew paired-resource test, shipping compatibility
negotiation, provider passage, or a completed NW0 gate.
