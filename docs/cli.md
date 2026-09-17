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
open-brain workspace setup|status|observe|refresh
open-brain workspace accept|materialize|deactivate|restore NOTE_ID
open-brain workspace resolve NOTE_ID --choice accepted|candidate
open-brain graph suggestions
open-brain graph accept SUGGESTION_ID
open-brain graph grant-consent|revoke-consent --provider PROVIDER --access-mode MODE
open-brain graph exclude|include SUBJECT --kind note|folder
open-brain export ABSOLUTE_DIRECTORY [--verify]
open-brain doctor
open-brain status
open-brain mcp --allow-capture|--allow-search|--allow-workspace-read|--allow-graph-refresh
```

Commands that support machine output accept `--json`. The shared `--data-dir` option must be an
absolute path. Without it, the CLI uses the platform data directory. `OPEN_BRAIN_ROOT` is ignored.

`workspace setup` creates or reopens the dedicated `Open Brain Vault` sibling beside the private
Brain directory. Explicit `workspace refresh` adds newly accepted canonical pages and advances
eligible existing notes to the current approved publication, keeping their registered paths.
Unchanged publications do not rewrite bodies; representative-capture metadata changes can create
an identical-body revision without a file write. Owner edits and links are retained; divergence
requires explicit conflict resolution and materialization. Missing, inactive, excluded, and already
conflicted notes are not overwritten. Retrying a refresh request finishes only its original bound
work. Observation, acceptance,
materialization, deactivation, restoration, conflict resolution, consent, exclusions, and suggested
link acceptance are explicit owner operations. State-only actions do not rewrite Markdown.

## Status and doctor

Status reports profile `local`, storage `sqlite`, daemon false, and application encryption false.
Doctor checks the private data directory, foreground runtime boundary, base dependency closure, and
search index. Neither command probes or manages a service.

## MCP

`open-brain mcp` speaks MCP over inherited stdio. At least one capability flag is required. Workspace
read exposes only path-free status and pending suggestions. Graph refresh uses the already configured
provider and current owner consent; it cannot select a provider, change consent or exclusions, accept
a link, resolve a conflict, or mutate a note. Until a provider adapter is configured, refresh returns
`provider_not_configured` without an inference attempt. The process owns no listener and ends at EOF.

Per process, workspace reads are limited to 500 calls and 16 MiB of serialized results. Graph refresh
is limited to 20 requests, 40 actual model attempts, and 1 MiB of selected note input. These limits are
in addition to the engine's durable per-workspace provider budget. Restarting an explicitly launched
MCP process resets only the process limits.

## Exit behavior

Invalid usage returns 2. Temporary SQLite writer contention returns 75 for capture, search, import,
or MCP work. A private-directory or operation failure returns 78 without exposing sensitive paths.
Interrupted Markdown import returns 130.

Secure Node and predecessor command families are historical source under `archive/`. They are not
installed commands and are not supported through the Open Brain executable.
