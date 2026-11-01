# Changelog

## Unreleased

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
