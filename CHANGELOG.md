# Changelog

## 0.2.3 - 2026-11-12

- Fixed: `open-brain export` failed with `local_operation_failed` (exit 78) on a Brain whose
  capture metadata came to more than 64 MiB. That is a few thousand text captures, because the
  archive keeps each capture's original payload and search text. The capture-metadata,
  capture-custody and historical-authority sidecars now share a 256 MiB limit, the same on
  export, verify and restore. The Portable format and its catalog digest are unchanged. An
  export at or below 64 MiB per sidecar is still readable by 0.2.2; a larger one needs 0.2.3.
- Fixed: `open-brain export --verify` on a Brain of about 6,000 files or more wrote and verified
  the export, then reported `local_operation_failed` (exit 78), because it read the export's
  manifest back with a 1 MiB limit. The manifest now shares the 256 MiB limit.
- An export over the limit now fails with `portable_export_too_large` (exit 78). The message and
  the JSON error name the sidecar, its size and the limit, and no export is written.
- If 0.1.x can't export your Brain before the 0.2.0 schema migration, copy the whole Brain
  directory while nothing writes to it instead. Use `sqlite3 <db> ".backup <copy>"` for
  `.open-brain/state/phase1.sqlite3` and `.open-brain/indexes/search.sqlite3`, and an ordinary
  copy for every other file.

## 0.2.2 - 2026-11-01

- `open-brain capture` takes `-` to read text from stdin, `--delivery-id` for an owner-chosen
  idempotency key (a retry with the same ID and text returns the original capture), and
  `--expect-brain-id`/`--expect-issuer-epoch` to refuse a Brain with a different identity
  (`destination_mismatch`, exit 78). `open-brain search` takes `-` and the same identity check.
  A forced-command capture server can now use the CLI instead of importing Open Brain.

## 0.2.1 - 2026-11-01

- Fixed: a store with an Obsidian vault or a Markdown import folder could not be opened, migrated, or
  exported once macOS gave that folder's volume a different device number, which can happen after a
  reboot. Every command failed with "Open Brain could not complete the local command", and
  re-importing reported `import_root_changed`. Opening the Brain now re-records the device number
  when the recorded path still opens to the same folder (same inode), before migration. A replaced
  folder is still refused. Affects 0.1.0 and 0.2.0; upgrading from 0.1.0 to 0.2.0 stopped after
  schema 6.

## 0.2.0 - 2026-11-01

- Local state schema 23 (follow-up items), runtime session 16 and Portable 18. Opening a schema 22
  store migrates it one way; take a verified Portable export with the 0.1.x runtime first.

- `open-brain export-records` prints the head captures an external-provider launcher policy and its
  consent may read, as JSON Lines. It checks tier in SQL and again per record through the record
  projector, honours `external_egress`, never prints raw source references, and fails without
  output when more records qualify than `--limit`. See `docs/export-records.md`.
- Corrected the 0.1.0 entry below: migrations do not back up existing stores. Take a verified
  Portable export with the current runtime before upgrading (`docs/schema-migrations.md`).

## 0.1.0 - 2026-09-13

- The base distribution is now strictly foreground-only. Secure Node and legacy implementations
  are preserved under `archive/` and are excluded from the workspace, builds, tests, and entry points.
- Local capture, SQLite search, status, and doctor commands run without a daemon or API key.
- Markdown import checks files before writing and supports repeat imports without duplicate records.
- Portable export preserves record bytes and can be verified independently.
- Versioned SQLite migrations reject unsupported schemas. They do not back up existing stores; a
  verified Portable export is the pre-upgrade backup.
- Local MCP capture and search use the same operations as the CLI.
- Contributors can run lint, type checking, tests, builds, and isolated Homebrew smoke checks with
  `make contributor-check` on macOS arm64 or Linux x86_64.
