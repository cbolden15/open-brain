# Open Brain documentation

Start with the [README](../README.md) for install and the MCP client examples, then
[first use](first-use.md). The [CLI reference](cli.md) lists every command, and the
[feature/version matrix](core-v01-features.md) separates what is implemented from what is accepted.

## Install and operate

- [Installation](install.md), [configuration](configuration.md) and [doctor checks](doctor.md)
- [Operations](operations.md), [schema migrations](schema-migrations.md) and
  [managed workspace recovery](managed-workspace-recovery.md)
- [Historical root checkpoints](historical-root-checkpoints.md)

## Capture, import and organize

- [Capture contract](capture-contract.md) and [import](import.md)
- [Spaces and inbox](spaces-inbox.md), [workspace content](workspace-content.md) and
  [review and publication](review-publication.md)
- [Conversation intake](consumer-intake.md) and [intake from coding agents](consumer-intake-agents.md)
- Sources: [Google Calendar](google-calendar.md), [iMessage](imessage.md) and the
  [integrations](integrations/core-sources.md) folder, including [Slack](integrations/slack.md)

## Search and records

- [Retrieval design](retrieval.md) and [records and history](records-and-history.md)
- [Offline semantic search](semantic-search.md): optional, local, off by default
- [Owner retraction](owner-retraction.md) and its [impact model](retraction-impact.md)

## Agents and MCP

- [Agent setup](agent-setup.md) for Claude Code and Codex
- [Harness setup](harness-setup.md), [harness memory](harness-memory.md),
  [Hermes memory](hermes-memory.md), [admitted local transactions](admitted-local-transactions.md)
  and [harness acceptance](harness-acceptance.md)

## Product, privacy and security

- [Product family](product-family.md), [architecture](architecture.md) and the
  [v0 product contract](v0-product-contract.md)
- [Privacy model](privacy-model.md) and [threat model](threat-model.md)
- [Release boundary](v0-release-boundary.md) and [expansion backlog](v0-expansion-backlog.md)
- [Artifact characterization](artifact-characterization.md) and
  [current record characterization](current-record-characterization.md)

## Portable Brain formats

Portable 16 is current (local schema 21, runtime session 16). Earlier formats stay documented as
predecessors.

| Format | Document |
|---|---|
| 1 to 4 | [v1](portable-brain-v1.md), [v2](portable-brain-v2.md), [v3](portable-brain-v3.md), [v4](portable-brain-v4.md) |
| 5 to 8 | [v5](portable-brain-v5.md), [v6](portable-brain-v6.md), [v7](portable-brain-v7.md), [v8](portable-brain-v8.md) |
| 9 to 12 | [v9](portable-brain-v9.md), [v10](portable-brain-v10.md), [v11](portable-brain-v11.md), [v12](portable-brain-v12.md) |
| 13 to 16 | [v13](portable-brain-v13.md), [v14](portable-brain-v14.md), [v15](portable-brain-v15.md), [v16](portable-brain-v16.md) |

Standalone clean restore accepts Portable 5 through 16; see [the CLI reference](cli.md).
