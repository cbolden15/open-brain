# Backup and restore follow-up

Status: explicitly excluded from the current new-user functionality implementation program. No implementation or execution authorization is implied.

Repository: `.`.

Source finding: F3 in `docs/audits/2026-09-16-new-user-functional-assessment.md`, baseline `5cf081aa3d591e14b89245db290d4a186ff8156a`.

## Confirmed gap

The engine can import a clean Portable Brain, but a new installed user has no supported identity-preserving Portable restore workflow. The public `open-brain import` command imports Markdown. Pointing it at an exported Brain does not restore source identities, review/publication state and history; it can ingest canonical/space Markdown as new raw records. Documentation must not equate these operations.

## Future scope, requiring a separate plan

1. Define a supported owner restore command and fresh-target preflight, format/version validation, preserved identities/history and a complete success receipt.
2. Define conflict, occupied-target, partial/corrupt archive and interruption behavior before implementing anything destructive. Prefer a verified clean target with atomic promotion; do not overwrite a live Brain implicitly.
3. Assess backup creation/scheduling, retention, external destination and credential/encryption choices as separate requirements, not confirmed assessment defects.
4. Assess revision restore/revert separately from the read-only history and append-only correction work already in the active program.
5. Prove restored capture/search/publication/workspace behavior through the installed product on both supported platforms, including newer lifecycle/source metadata introduced by the active plan.

Existing exported artifacts may contain retired or subsequently forgotten information. The new live-store forget operation does not erase external archives. Any future restore design must explicitly address suppression/deletion policy, old identities, external copies and the risk of reintroducing previously forgotten material.

## Boundary with the active plan

The active plan preserves existing export validation, byte/history invariants and supported import tests. Internal crash-safe migration/purge recovery and existing managed-note reactivation are regression/correctness work, not a new backup/restore feature. Do not remove these tests because F3 is excluded. Do not make the active plan depend on completing this follow-up.

Next action when authorized: validate current Portable import/export contracts and write a separate restore threat model and acceptance matrix before coding.
