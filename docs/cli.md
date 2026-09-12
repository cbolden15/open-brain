# Command-line interface

`open-brain` is one foreground command. Stateful commands create or reopen the platform-local Brain,
perform the requested operation, close SQLite resources, and exit. No separate init, daemon, HTTP
server, service unit, container, or privileged setup is required.

## Commands

```text
open-brain init
open-brain capture TEXT
open-brain import ABSOLUTE_DIRECTORY [--yes] [--allow-large-vault]
open-brain search QUERY [--limit N]
open-brain export ABSOLUTE_DIRECTORY [--verify]
open-brain doctor
open-brain status
open-brain mcp --allow-capture|--allow-search
```

Commands that support machine output accept `--json`. The shared `--data-dir` option must be an
absolute path. Without it, the CLI uses the platform data directory. `OPEN_BRAIN_ROOT` is ignored.

## Status and doctor

Status reports profile `local`, storage `sqlite`, daemon false, and application encryption false.
Doctor checks the private data directory, foreground runtime boundary, base dependency closure, and
search index. Neither command probes or manages a service.

## MCP

`open-brain mcp` speaks MCP over inherited stdio. At least one of `--allow-capture` or
`--allow-search` is required. The process owns no listener and ends at EOF. Session limits bound
accidental loops; restarting the explicitly launched process resets them.

## Exit behavior

Invalid usage returns 2. Temporary SQLite writer contention returns 75 for capture, search, import,
or MCP work. A private-directory or operation failure returns 78 without exposing sensitive paths.
Interrupted Markdown import returns 130.

Secure Node and predecessor command families are historical source under `archive/`. They are not
installed commands and are not supported through the Open Brain executable.
