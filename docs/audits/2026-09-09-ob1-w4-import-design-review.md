# OB1-W4 import design review

Date: 2026-09-09

Scope: the five-minute Open Brain Markdown and Obsidian import design in
`docs/import.md`, with its privacy and threat-model changes. Secure Node, AIOS, W5 migrations, and
runtime implementation are outside this review.

## First review pass

The review command was:

```sh
<agent-config>/bin/doc-review \
  --doc <repo-root>/docs/import.md \
  --third-party deny
```

The first pass returned `REWORK`: 14 actionable findings and five informational findings. No
review lens failed. Deterministic repository evidence lookup passed. The optional model-refutation
step did not produce verification evidence, so that pass reported `verified: false`.

## Actionable finding dispositions

| # | Priority | Finding | Disposition |
|---:|:---:|---|---|
| 1 | P0 | One deterministic file failure could indefinitely block missing-path finalization for unrelated notes. | Accepted. Complete enumeration may finalize unrelated absence. Every failed or skipped known path counts as observed and preserves its own active revision. Only incomplete traversal, interruption, or fatal storage failure blocks finalization. |
| 2 | P0 | There was no recovery command for an accidental import. | Accepted as a pre-mutation prevention requirement. A new root receives an aggregate preview, immutable-retention warning, and explicit TTY or `--yes` confirmation before registration. The plan still defers destructive pruning, and W4 has no post-import purge. |
| 3 | P0 | Replacing a registered directory at the same path had no rebind workflow. | Resolved as an explicit 0.1.0 limit. The product-completion plan defers source-root relocation. A same-identity move must be restored to its registered path. A copy with a new identity can register at a new disjoint path. There is no rebind command. |
| 4 | P1 | The design lacked preview and atomic import modes. | The new-root aggregate preflight now provides preview and confirmation before mutation. The planned resumable one-file commit model remains non-atomic, with an explicit retention warning. |
| 5 | P1 | Concurrent imports and other writers were unspecified. | Accepted. The existing nonblocking single-writer lease excludes concurrent mutations and returns a stable retryable error. Import uses a lock-held internal capture primitive rather than reacquiring the non-reentrant lease. |
| 6 | P1 | Invalid filesystem names had no deterministic ordering or safe representation. | Accepted. Raw invalid names sort deterministically after valid paths and render only as stable run-local opaque tokens. Raw invalid bytes never reach output. |
| 7 | P1 | Fatal failures did not fit the completed-run JSON summary. | Accepted. Completed runs use the import summary. Fatal runs use the existing `error` envelope and human fatal errors go to stderr. |
| 8 | P1 | A 100-entry cap could hide every actionable failure. | Accepted. Failed outcomes fill the bounded list first in path order. Other outcomes follow. Counts and `entries_omitted` remain authoritative. |
| 9 | P1 | Large-vault confirmation omitted the observed scale and exceeded limits. | Accepted. The redacted failure includes observed counts, exact limits, exceeded field names, and the literal retry flag. It includes no path or content. |
| 10 | P2 | NUL-bearing text had no distinct result reason. | Accepted. It uses `invalid_content`; strict UTF-8 decode failures use `invalid_utf8`. |
| 11 | P2 | Long scans had no progress or interrupt contract. | Accepted. TTY human mode emits metadata-only progress to stderr at fixed intervals. JSON stays clean. Interrupt returns 130, preserves completed commits, and does not finalize missing paths. |
| 12 | P2 | A to B to A revision behavior was ambiguous. | Accepted. Revision lookup spans the file's complete history. Returning to A reactivates the existing A capture and creates no third capture. |
| 13 | P3 | A leading UTF-8 BOM could change title extraction. | Accepted. One leading BOM is ignored for title parsing only and remains in stored bytes. |
| 14 | P3 | The JSON example contradicted path-order rules. | Accepted. The example and ordering rule now agree. |

## Informational finding dispositions

| Finding | Disposition |
|---|---|
| Common macOS paths can contain symlink components. | The owner-selected root is resolved and independently descriptor-opened twice, then the pinned resolved tree is traversed without following links. |
| Imported capture privacy and intent were implicit. | The design fixes personal-local-only privacy, local-only authority, and hold intent. |
| The importer delivery namespace was not reserved. | The `markdown-import.` namespace requires both a pending reservation and the fixed importer context. Prefix alone never suppresses projection. |
| Plan authority was not linked. | The design now links the OB1 completion plan and five-minute acceptance contract. |
| A thematic break could be mistaken for unbounded frontmatter. | Frontmatter requires a closing delimiter within 100 logical lines and 65,536 bytes. Otherwise title parsing treats the text as normal Markdown. |

## External prerequisites

- Run the private owner-only current-tree and history audits at the frozen W4 head before merge.
- Run filesystem safety tests on Linux CI and the case-alias test on a case-insensitive macOS volume.
- Treat NFS, FUSE, synchronized trees, and hostile same-user mutation as unsupported snapshot
  guarantees unless a later filesystem conformance design proves them.
- Preserve the READY documentation contract during implementation and rerun the post-execution audit
  before merge.

## Second review pass

The same command returned `REWORK` with 11 actionable findings and one informational finding.
Feasibility, coherence, product, and design completed. Security and adversarial timed out, so this
pass was partial and reported `verified: false`.

| # | Priority | Finding | Disposition |
|---:|:---:|---|---|
| 1 | P0 | Accidental imports still had no safe lifecycle path. | Added mandatory aggregate preview, retention warning, and confirmation before registering a new root. Destructive post-import purge remains deferred by the governing plan. |
| 2 | P0 | A zero-file run could report `status=imported`. | Completed scans now use neutral `status=completed`, expose `selected`, and warn when it is zero. |
| 3 | P1 | Withheld missing finalization was invisible. | Completed summaries expose `missing_finalized=true` only after commit. Every incomplete, fatal, or interrupted envelope exposes `false`. |
| 4 | P1 | Root overlap was checked only during registration. | Every invocation rechecks the Brain and every reachable registered root before scan or mutation. Potential lexical overlap with an unreachable canonical root fails closed. |
| 5 | P1 | Overlap and changed-root errors were dead ends. | Both now provide exact bounded recovery guidance and safe conflict kind or opaque root ID, never a stored path. |
| 6 | P1 | Moved-root behavior contradicted unique identity. | Same-identity relocation is refused and must be restored. Only a copy or recreation with a new identity may become a new root. |
| 7 | P1 | Interrupt output was not closed. | SIGINT and `KeyboardInterrupt` now have exact JSON and human messages, `status=interrupted`, `missing_finalized=false`, and exit 130. |
| 8 | P1 | The entry-list cap was ambiguous. | The shared cap is exactly 100 total entries. Failures consume it first, and the remainder formula is fixed. |
| 9 | P2 | Registration order conflicted with write-free preflight. | The operation order now completes enumeration and aggregate gates before registration or any import mutation. |
| 10 | P2 | Title sanitation did not define replacement semantics. | C0, C1, and `Cf` characters become ASCII spaces before whitespace collapse, trim, and 200-code-point truncation. |
| 11 | P2 | Human output silently hid truncation. | Human output now prints an exact singular or plural omitted-result line. |

The informational lease-contention finding is also resolved: lock acquisition is nonblocking, waits
zero seconds, and returns exit 75 with `local_operation_busy`.

## Replacement security and adversarial review

Three concurrent read-only Codex reviews replaced the two timed-out lenses. They independently
confirmed the moved-root, output, and writer-contention changes and found these additional gaps:

- The public capture wrapper would reacquire the non-reentrant writer lease. Import now requires the
  existing engine-private lock-held submission path and a one-acquisition regression test.
- Full rederivation could resurrect a durable capture whose revision attachment was interrupted.
  Rederivation now joins on import delivery ID, including pending reservations, and projects only the
  exact active revision.
- The source reference could exceed the existing 65,536-character capture contract. W4 now fixes the
  percent-encoding algorithm and the resulting 65,456-character relative-path bound before
  reservation.
- One resolve followed by one open left an ordinary alias replacement race. The root is resolved and
  independently descriptor-opened twice, then revalidated before registration and finalization.
- An unreachable stored root could conceal a canonical ancestor or descendant. Such potential
  overlap now fails closed.

These replacement reviews returned no unresolved P0-P2 finding after the dispositions above. They
made no repository edits.

## Final review pass

The third formal `doc-review` invocation completed feasibility and security, and its adversarial
fallback completed. Product, design, and coherence timed out. The workflow then ended with
`DOC_REVIEW_SYNTHESIS_FAILED`, so it produced no verdict. This is recorded as a tool failure, not a
successful gate.

Per the owner instruction for timed-out lenses, a dedicated Codex synthesis judge reviewed the
current import, privacy, threat, plan, acceptance, and implementation-interface documents. Its first
pass found three remaining issues:

- interruption could arrive after missing-path commit but be reported as unfinalized;
- the governing plan called all file-swap races skips while the detailed design called a selected
  file changed during reading a failure;
- the large-vault JSON example omitted the mandatory `missing_finalized=false` detail.

After those fixes, the judge found one remaining P2: SIGINT during preflight lacked a safe point
before root registration. The final revision checks for interruption during enumeration and
confirmation, before registration and revision reservation, after each durable file outcome, and
before finalization. Once finalization starts, the command crosses an explicit completion boundary
and reports the committed result.

Final Codex gate verdict: `READY`.

Remaining P0-P2 findings: none.

Remaining P3 or informational findings: none.

Runtime implementation may begin. W5 remains blocked until W4 implementation and verification are
complete.
