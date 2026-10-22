# Automatic harness memory

This implementation adds a foreground memory bridge and optional lifecycle
adapters for Codex, Claude Code and Pi. Reusable setup and source resolution are
implemented; native acceptance is still in progress. Tests use disposable Brains and synthetic
native events; they do not establish that an installed harness captures or
receives memory during real inference.

The public package owns message normalization, useful-memory selection, source
citations, write-time tiers, the retry spool, retrieval and the adapter assets. Your local
configuration owns the Brain destination, source rules, exclusions and provider
consent. Private notes, configuration and transcript files belong outside the
repository.

Hermes authored capture and provider-bound recall use this same bridge; see
[the Hermes host contract](hermes-memory.md). Hermes has no native installation
asset and can never be declared local-only.

## Foreground commands

`open-brain harness` supports `capture`, `enqueue`, `drain`, `status`, `recall`,
`search`, `source`, `mcp` and `setup`. See `docs/harness-setup.md` for installation.
Every command takes an absolute `--config` path and a fixed `--client` identity:
`codex`, `claude-code`, `pi` or `hermes`. Capture is Hermes-only; setup and MCP
support the existing native clients. Capture, enqueue, recall and search read
bounded JSON from stdin. Source accepts a returned capture ID and resolves
retained context under current policy. MCP exposes search and source tools over inherited stdio.
Results are JSON; failures contain closed metadata codes.

Enqueue screens the event and appends it to a small retry spool without opening
the destination Brain, then returns `queued`. Hermes `capture` delivers inline
and spools only the requests whose delivery failed or was unavailable. Drain
opens an existing, identity-matched Brain and replays the oldest spooled lines,
at most 16 per run, through the normal engine journal. A delivered line is
removed; a failed line moves behind the others so it cannot starve later
captures, and nothing is discarded. A line refused 16 times is parked: it stays
in the spool and in the refused count but is no longer retried. Each line
records when it was enqueued, and a replay never overwrites a source revision
observed after it, so a stale retry cannot undo a later confidentiality marking. There are no receipts: delivery IDs make a
replayed line a downstream no-op. Status works while the Brain is unavailable
and counts pending lines and their latest refused or unavailable attempts.

The spool is one append-only JSONL file, mode `0600`, in the configured `outbox`
directory, mode `0700`. Each change writes a temporary sibling, fsyncs it,
renames it over the spool and fsyncs the directory, so a crash leaves either the
old or the new file. Opening the spool deletes any temporary sibling a crash
left behind, since it can hold source text. Earlier releases kept a `requests/` and `receipts/` custody
tree in the same directory; the spool ignores it.

The bridge configuration has this shape. Each path is absolute; the directory
must be owner-only mode `0700` and the file mode `0600`.

```json
{
  "schema_version": "open-brain-harness-bridge.v1",
  "brain_root": "/absolute/private/brain",
  "brain_id": "THE_EXISTING_BRAIN_ID",
  "policy": "/absolute/private/harness/policy.json",
  "consent_state": "/absolute/private/harness/provider-consent.json",
  "outbox": "/absolute/private/harness/outbox",
  "local_only_clients": {
    "pi": {"model_endpoint": "http://127.0.0.1:11434/v1"}
  }
}
```

`local_only_clients` is optional; files without it load unchanged. The Brain ID
must come from the existing destination. The bridge refuses a missing or
different destination before ordinary bootstrap. Provider consent uses the
existing `open-brain consent` commands and is bound to the Brain's current
issuer epoch. A source rule alone does not grant provider consent.

## Source policy and write-time tiers

The separate owner-only policy file has this shape:

```json
{
  "schema_version": "open-brain-harness-policy.v1",
  "enabled": true,
  "client_defaults": {"pi": "unknown", "codex": "personal"},
  "sources": [
    {
      "id": "example-project",
      "project_root": "/absolute/project",
      "tier": "work",
      "local_only": false
    }
  ],
  "exclusions": []
}
```

Every capture gets its tier when it is written. The most specific matching
source rule wins. Without one, the owner's `client_defaults` entry applies, then
the built-in default: `pi` and `hermes` are `unknown`, `claude-code` and `codex`
are `personal`. Any other client without an explicit owner default is
`unknown`. `client_defaults` is optional and may name `public`, `work`,
`personal` or `unknown`, never `secret`. Older v1 files that still carry a
`providers` map load as before; the map is ignored and grants nothing.

An authored line starting with `Confidential:`, `Private:` or `Local only:`, a
rule with `local_only: true`, or a host's `local_only: true` capture field
lowers the tier to `unknown`. A marking never raises a tier. A credential found
by the screen refuses the capture before anything is written, and so does a
source rule with tier `secret`. Exclusions and `enabled: false` apply before
transcript reads or Brain startup.

There is no session or fork confinement. The tier follows the event as written,
so a replay is tiered again under current policy. A change of tier or text is an
ordinary new source revision; message identity and delivery IDs stay stable.

## Reads

The harness does not filter by tier itself. Cloud clients, which are Claude
Code, Codex, Hermes reading as a provider, and any unverified Pi, read through an
external-provider authority. Its tiers are the provider's durable consent tiers
within public, work and personal. The core's owner egress policy and that
consent then decide what is visible: a work record is readable only when the
owner policy shares work and the consent includes it. Unknown and secret records
are never readable this way. The bridge rechecks consent before releasing
content, so a revoked consent denies the read immediately.

Guardrail 1 governs unshared tiers. A client may read `unknown`, or a tier the
egress policy does not share, only when the bridge config declares it in
`local_only_clients` and the bridge verifies the declaration on that read. Only
`pi` can be declared; declaring any other client makes the configuration
invalid. The bridge re-reads its config on every invocation and checks that the
declared `model_endpoint` is an `http` or `https` URL without credentials whose
host is `127.0.0.1`, `::1` or `localhost`. Any other value, or no declaration,
fails closed to the cloud ceiling. A verified Pi reads every tier except
`secret`. This check binds the owner's declaration of Pi's model endpoint; it
does not inspect Pi's own routing at run time.

A request's `local_only: true` field grants nothing. From an unverified client
it returns `requires_local_only` with `local_only_host_required` and no content.
Even a verified Pi never receives a `secret` record, including secret records
that releases before Policy B wrote into per-project local spaces.

Exclusions are owner revocations at read time as well as at capture. Every
reader loses a source rule's space when the rule's root overlaps an exclusion,
and an unmapped project's space when that exact project path is excluded.
Unmapped projects nested below an excluded directory have spaces named by an
opaque path digest that the bridge cannot map back. To remove those, or anything
captured under a source rule you later deleted, use the owner purge commands:
`open-brain source withdraw` for a harness source and `open-brain retract` for
a capture (see `docs/owner-retraction.md`).

## Lifecycle adapters

The optional Node asset `assets/harness-hook.mjs` provides Codex and Claude Code
Stop capture and UserPromptSubmit recall. Its command pins the runtime,
configuration, client and event. It preserves the original native session
metadata. `OPEN_BRAIN_HARNESS_DISABLED=1` opts out before any bridge invocation.

Capture has a two-second outer deadline. After durable enqueue, the integration
may start one detached foreground drain command with the CLI's sixty-second
deadline. This is optional integration scheduling: installing the core does not
install a daemon, recurring worker, listener or service. Later capture hooks
provide another delivery attempt. A `queued` result means the event is in the
local spool; it does not mean the destination has saved or indexed the content.

Deadline cancellation escapes normal operational error handlers and stops the
drain before it rewrites the spool, so every line is kept for the next run.

Recall has a three-second outer deadline and returns at most five complete rows
within eight KiB of additional context. Oversized rows are omitted visibly.
Content is labelled untrusted reference data and keeps its source citation.
Successful empty retrieval differs from unavailable or refused retrieval.

The optional Pi asset `assets/pi-memory.mjs` exports
`createPiMemoryExtension`. Its host supplies the bridge, SDK session manager,
project, fixed provider identity, search schema and local-only enforcement
capabilities. These host capabilities do not widen the bridge's read ceiling;
only guardrail 1 does.
The extension captures authored branch entries with stable native IDs and
available header/entry timestamps. It recalls before agent start and exposes
`brain_search_page` and, when supplied by the bridge, `brain_read_source`.

## Current selection and identity limits

Selection is conservative and extractive. It recognizes specified English
patterns for facts, decisions and preferences, plus requested complete recipes.
Authorship is retained; assistant guesses and inferred preferences are excluded.
Selection is not factual verification or general semantic understanding.
Current recall excludes explicitly superseded decisions; source reads label
retained history as current, superseded or unknown. The projection recognizes
specified replacement wording within the same project and source order. It
refuses incomplete currentness scans. A local lexical planner expands supported
task paraphrases, such as meal planning, without cloud extraction or embedding.

The normalizer accepts at most 16,384 messages and eight MiB of authored text
from the currently named transcript or SDK branch. Pi enqueue JSON also has a
nine-MiB transport ceiling. Oversized events fail visibly. Spooled chunks keep
each selected candidate within the existing 32-candidate and 128-KiB input
bounds; each chunk is delivered or retried on its own. Source
messages are bounded to sixteen KiB and candidates to eight KiB. Unsupported
wording and oversized candidates remain selection limits.

Claude message UUIDs and Pi branch entry IDs provide native identity. Codex's
fallback hashes authored role/text with occurrence order, independently of
physical transcript lines. It survives inserted tool metadata and rotation that
retains authored messages. Fork and parent lineage is not tracked, and parent
transcript bodies are never mined. Actual installed capture, deadlines and
cross-harness retrieval remain acceptance work.
