# Changelog

## Unreleased

- Local capture, SQLite search, status, and doctor commands run without a daemon or API key.
- Markdown import checks files before writing and supports repeat imports without duplicate records.
- Portable export preserves record bytes and can be verified independently.
- Versioned SQLite migrations back up existing stores and reject unsupported schemas.
- Local MCP capture and search use the same operations as the CLI.
- Contributors can run lint, type checking, tests, builds, and isolated Homebrew smoke checks with
  `make contributor-check` on macOS arm64 or Linux x86_64.

The public Homebrew tap and first product release are not published yet.
