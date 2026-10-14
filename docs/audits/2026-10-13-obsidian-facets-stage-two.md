# Obsidian facets Stage 2 verification

Stage 2 B/U/P/V/T/I/R is implemented and verified locally. The reviewed code candidate is
`55c1ced42ebbeb7dd0ddbd82c70829448306d6f3`. Subsequent ledger commits record this result;
they do not change the verified implementation. No branch was pushed, published or deployed.
The standalone desktop application received no feature work. Its existing regression checks
remain part of the repository gate.

## Immutable inputs

The starting verified main commit was `23e7a2bb75567b5492c026ea76f16c463739245e`.
Schema 15/runtime 10/Portable 10 remain unchanged.

| Contract | Revision | Fixture-manifest SHA-256 |
|---|---|---|
| C0 | facets-c0-v4 | 95e85b535eeb4263a6e430970ecf1d49de8d0b72de7425e41be703897d93dbc5 |
| O0 | obsidian-o0-v1 | bee455084d682e8e717f5f40e1a5cfad82ada7546fdf6412552a8e4844a50d9f |

Neither fixture manifest nor the frozen facet wire definitions changed. Additive owner-local
bridge actions observe managed file identities and rebuild facet projections. The existing
publication listing accepts a query filter for bounded recovery inspection. Ordinary imported
body-write custody remains refused.

## Integrated checks

Checks ran in frozen verification and native worktrees, each with its own environment.

| Check | Result |
|---|---|
| `make verify` | Exit 0: ruff, mypy 618 files, Python 7074 passed/5 skipped, package builds, Obsidian 664 tests, desktop 259 TypeScript/30 Python/26 Rust tests and desktop build |
| `make native-audit homebrew-smoke` | Exit 0: native audit, actual installed Homebrew product journey and preservation of the existing product |
| Independent engine/transport followup | Python 29/29; client/actual-child TypeScript 20/20; real query-filter regression passed |
| Independent final review | Real-process fault reproductions fixed; publication/transport 63 tests and typecheck passed; focus/unload delta 6 focused tests passed |
| Repository hygiene | `git diff --check`, `actionlint .github/workflows/ci.yml` and gitleaks over all 19 implementation commits passed |

The five Python skips are filesystem capabilities on macOS: non-UTF-8 filenames and distinct
NFC/NFD names. The existing fork test emits a multithreaded-fork deprecation warning.

The first full run passed 7048 Python tests but its shared harness fixture exceeded the 3-second
recall deadline once, producing 26 setup errors. The unchanged fixture then passed 32 checks
separately. The final full gate passed after separating
the native build from the repository run. No timeout, benchmark threshold or product contract
was weakened. This fixture proves public-interface behavior, not native model inference.

## Acceptance coverage

| ID | Evidence |
|---|---|
| O01 | Old/partial capability client cases preserve legacy operations; real restricted server discovery and direct invocation enforce grants and scope. |
| O02 | Actual managed canonical resolution, explicit observation, edited identity/path/root refusals and classification without a managed source file. |
| O03 | Actual stdio vocabulary create/rename/archive and durable identity; vocabulary creation also completed in Obsidian. |
| O04 | Actual source/canonical assignments and stale-write refusal; editor interaction tests preserve cancelled/stale drafts. Both source and canonical classification completed in Obsidian. |
| O05 | Actual child restart, dropped and malformed postcommit responses, exact operation-ID retry and one durable change. |
| O06 | Real match-all/filter/cursor checks; bounded browser interactions; a saved source query was created and run in Obsidian. |
| O07 | Real cross-space frozen publication and zero/33-source refusals; explicit destination and actual inspection were exercised in Obsidian. |
| O08 | Real update retains destination/cumulative provenance and conflicts on relevant drift; page assignment remains separate from attachment. |
| O09 | Real committed publication/refresh-failure recovery and rejection; independent fresh-child/client recovery; actual Obsidian reload recovered the original page without another approval. |
| O10 | Actual filesystem removal/conflict previews; deterministic engine expiry/name cases; Obsidian showed the explicit Apollo removal and required confirmation. |
| O11 | Actual filesystem failure/retry commits once and preserves edits/indexes; Obsidian Apply returned classification version 2 and read-only history. |
| O12 | Real raw Unicode/oversize/strict parsing, shared quotas and pre-effect reservations; smaller negotiated transport limits. |
| O13 | Lifecycle/selection/focus interaction checks, actual host command/selection/keyboard focus, inherited dark theme and static UI without added motion. Automatic graph refresh no longer steals note focus. |
| O14 | Actual plugin mutations, full export/import into a fresh root, restored query/evidence equality, rebuilt facet metadata/indexes and refusal of old preview/body-write custody. |
| O15 | Actual Obsidian 1.14.4 synthetic-vault owner flow, capture/search/manual publication, retained conflict review/resolution, index opening and publication recovery after reopening the host. |

This is a bounded acceptance pass, not an exhaustive accessibility or every-platform audit.
Restricted-authority tests use the production byte-stream server with real engine state
in-process; they are not a separately launched restricted CLI policy session. UI mocks establish
interaction only. Persistence, authorization, publication recovery and restore claims above use
actual engine/transport/filesystem producers.

## Actual host receipt

The test used `/private/tmp/obsidian-stage-two/host/Open Brain Vault`, an explicit synthetic
Brain root and a wrapper selecting the built native executable. No provider, owner credentials,
existing owner vault installation or new network path was used. The owner explicitly approved
exiting Restricted mode for this synthetic vault.

Native SHA-256: `0b19cfe0b9cff3f00bc4dfcae241e84aa9eef82ad285bf6923e69b438042ab97`.
Plugin main.js SHA-256: `e6f60b38f2a30ae80b0dc84fba708bd172ee04be1602e6d88fb4793000a3f69a`.

The owner flow created Project/Apollo, classified an unmapped source, saved Apollo sources,
published Apollo overview across spaces, explicitly observed its file, assigned the canonical
page, reviewed a Markdown removal, applied it and read classification history. It also completed
ordinary capture/manual publication and resolved a real retained upstream/vault divergence.
The conflict fixture used actual engine publication and filesystem edits, without a mocked
conflict producer. After a fresh host window/client, recovery inspected the existing approved
proposal and reopened its original page. A final native inspection confirmed exactly one facet
proposal for the query, classification version 2, zero open conflicts and preserved owner bodies.

Host control became stale while editing synthetic Markdown. The test window was closed and
reopened through Obsidian's vault manager; malformed temporary frontmatter was refused before
mutation and repaired. The final removal bytes were prepared on disk, then previewed and applied
in the actual host. Host observations are kept separate from fault-injection subprocess evidence.

## Review findings resolved

Postcommit malformed successes and malformed error frames now remain uncertain rather than
being mistaken for definitive conflicts. Strict inspection retains original integer lexemes.
Publication startup reloads query-scoped proposals, actual inspections and history before a draft;
pending work blocks replacement, approval recovers by refresh, and additional pages require
explicit intent after complete proof. Recovery retains at most 100 proposal/history rows and
blocks when it cannot prove completion.

Reconciliation captures the active file before awaiting identity lookup, keeping its Edit note
action attached to that file. Automatic graph updates preserve focus, and unload during an
existing canvas ownership read prevents the subsequent write/open. Independent review found
no unresolved blocker within these scopes.

Private logs, fault scripts, host receipts and retained acceptance bytes are under
`/private/tmp/obsidian-stage-two`. Generated Rust targets and large test scratch data are
disposable after verification; source commits and acceptance evidence remain retained.
