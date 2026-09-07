# Public CLI characterization

This document records the retained Phase 0 parser and scheduled-route boundary. The installed CLI
and MCP behavior belongs to the isolated app distribution; standalone HTTP and legacy bridge
scripts are not installed. The 31-family parser below remains compatibility evidence rather than
the default installed entry point.

The default installed entry point is the separate daemonless `open-brain` parser. Its bounded
surface is help, version, `init`, `capture`, `search`, `export`, `status`, and `doctor`. The five data
journey commands use the local engine directly with provider mode `none`; they do not route through
the retained parser, daemon, HTTP server, connector registry, or Secure Node wrapper.

The machine-readable source is [`tests/fixtures/phase0/public_cli.json`](../tests/fixtures/phase0/public_cli.json). The focused characterization test compares that fixture with the live parser registry, scheduled route registry, static `pyproject.toml` metadata, and stable exit-code constants.

## Parser surface

The retained parser exposes 31 top-level command families:

`backup`, `capture`, `close-day`, `config`, `cron`, `curation`, `dev`, `digest`, `doctor`, `explain`, `hooks`, `inbox`, `index`, `ledger`, `lifeos`, `lint`, `messages`, `migrate`, `now`, `okf`, `ops`, `proposals`, `query`, `registry`, `retention`, `review`, `share`, `social`, `spaces`, `status`, and `ui`.

The global parser options are `--version`, `--json`, `--dry-run`, `-h`, and `--help`. Help is inspected without loading configuration or creating content or state roots. `--version` prints `open-brain 0.1.0` and exits `0`; the focused test checks the output and exit code without loading application state.

The parser exposes families that are also used by scheduled routes. A parser-visible family is not evidence that every delegated subcommand grammar is implemented at the dispatcher boundary. In particular, the current characterization does not freeze command-specific help output.

The legacy `migrate` family remains parser-visible for compatibility, but default application composition deliberately does not register a `migrate` adapter. In the default route it returns the bounded `unavailable` failure envelope; predecessor-only `parity`, `shadow`, and `doctor --cutover` routes are not parser-reachable and are rejected as usage errors.

## Scheduled routes

The fixture records 30 scheduled routes, keyed by `JOB-001` through `JOB-030`. Each route records its parser path, literal options, and typed adapter family (`capture`, `writer`, or `optional`). Options are recorded as parser metadata, including configured-value placeholders already present in the static job catalog. No configuration values are read.

Several routes share a parser path and are distinguished by their options and job ID. This includes the `backup run`, `capture serve`, `doctor`, `index`, and `now check` families. The fixture preserves those distinctions.

| Job | Parser path | Options | Adapter |
|---|---|---|---|
| `JOB-001` | `doctor` | `--json --role=probe` | optional |
| `JOB-002` | `index` | `--check --json --read-only` | optional |
| `JOB-003` | `now check` | `--json --read-only` | optional |
| `JOB-004` | `backup sqlite` | `--dry-run --json --profile=local` | optional |
| `JOB-005` | `capture imessage-ingress` | `--append --json` | capture |
| `JOB-006` | `close-day prepare` | `--dry-run --json` | writer |
| `JOB-007` | `dev signals scan` | `--append-only --json` | writer |
| `JOB-008` | `lint` | `--json --scope=work --write-report` | writer |
| `JOB-009` | `hooks sync` | `--dry-run --json` | writer |
| `JOB-010` | `ledger run` | `--json --nightly` | writer |
| `JOB-011` | `backup run` | `--json --profile=capture` | writer |
| `JOB-012` | `curation run` | `--day=yesterday --json` | writer |
| `JOB-013` | `doctor` | `--json --role=writer` | optional |
| `JOB-014` | `backup run` | `--json --profile=full` | writer |
| `JOB-015` | `ops git-sync` | `--json` | writer |
| `JOB-016` | `index` | `--json --scope=all` | writer |
| `JOB-017` | `lifeos nudge midday` | `--date=CONFIGURED_DATE --json` | optional |
| `JOB-018` | `lifeos plan` | `--date=CONFIGURED_DATE --generic-titles --json` | optional |
| `JOB-019` | `lifeos reset` | `--date=CONFIGURED_DATE --json` | optional |
| `JOB-020` | `messages extract` | `--json --review-actions` | optional |
| `JOB-021` | `messages sync` | `--dry-run --json` | optional |
| `JOB-022` | `now build` | `--json --role=writer` | writer |
| `JOB-023` | `backup run` | `--json --profile=personal` | writer |
| `JOB-024` | `retention run` | `--dry-run --json` | optional |
| `JOB-025` | `backup run` | `--json --profile=runtime-state` | writer |
| `JOB-026` | `ui serve` | `--bind=CONFIGURED_PRIVATE_BIND --port=CONFIGURED_PORT` | optional |
| `JOB-027` | `capture serve` | `--bind=CONFIGURED_PRIVATE_BIND --port=CONFIGURED_PORT` | capture |
| `JOB-028` | `capture serve` | `--bind=CONFIGURED_PRIVATE_BIND --mode=ingress --port=CONFIGURED_PORT` | capture |
| `JOB-029` | `capture poll` | `--json --mode=ingress --source=youtube` | capture |
| `JOB-030` | `now check` | `--json --read-only` | optional |

## Current packaging and entry points

The app distribution is `open-brain` version `0.1.0`. Its installed scripts are:

- `open-brain = open_brain.services.local_entrypoints:run_cli`
- `open-brain-secure-node = open_brain.services.secure_node_entrypoints:run_cli`
- `open-brain-secure-node-mcp = open_brain.services.secure_node_entrypoints:run_mcp`

The app wheel depends unconditionally on exactly `open-brain-engine==0.1.0`. Wheel-only acceptance
verifies the local default in a base environment and the retained appliance in a separate
`secure-node` environment. Connector and legacy distributions remain absent in both.

Source characterization still reads static parser metadata. Phase 4 acceptance independently
checks installed distribution metadata, module origins, resources, and console scripts.

## Stable exit classes

Interactive CLI classes are `0` success, `1` failure, `2` usage, and `3` deferred. Scheduled application classes are `0` success, `1` failure, `75` lock held, and `78` configuration failure. The bounded union recorded by Phase 0 is therefore `0`, `1`, `2`, `3`, `75`, and `78`.

These are exit classes, not a promise that every command currently reaches every class. The focused characterization test locks the numeric mapping to the current `ExitCode`, scheduled `ExitClass`, and scheduled dispatch contract.

## Current appliance boundary

The appliance CLI adds `daemon`, `init`, `status`, supervisor, `upgrade`, and `uninstall` commands
around the six retained Phase 1 families. `daemon` runs the source/wheel process in the foreground.
Upgrade and uninstall require explicit owner confirmation
and an injected source-checkout lifecycle port; the default native-artifact effect fails closed.
Portable export/import remain distinct app/daemon recovery operations rather than aliases of backup
or lifecycle commands. The retained Phase 0 parser is characterization data, not an installed
entrypoint.

The same boundary applies to command-specific help snapshots and delegated `ledger`/`migrate` grammars: they remain concerns for later work rather than new Phase 0 behavior.
