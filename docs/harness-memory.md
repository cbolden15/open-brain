# Automatic harness memory

This implementation adds a foreground memory bridge and optional lifecycle
adapters for Codex, Claude Code and Pi. Installer wiring and native acceptance
are still in progress. The current tests use disposable Brains and synthetic
native events; they do not establish that an installed harness captures or
receives memory during real inference.

The public package owns message normalization, useful-memory selection, source
citations, custody, replay, retrieval and the adapter assets. Your local
configuration owns the Brain destination, source rules, exclusions and provider
consent. Private notes, configuration and transcript files belong outside the
repository.

## Foreground commands

`open-brain harness` supports `enqueue`, `drain`, `status`, `recall` and `search`.
Every command takes an absolute `--config` path and a fixed `--client` identity:
`codex`, `claude-code` or `pi`. Enqueue, recall and search read bounded JSON from
stdin. Results are JSON; failures contain closed metadata codes.

Enqueue acknowledges durable local custody without opening the destination
Brain. Drain opens an existing, identity-matched Brain and submits source
revisions through the normal engine journal. A request remains pending until its
revision and routing are acknowledged. Delivered requests and receipts remain
retained for source tracing. Status works while the Brain is unavailable and
distinguishes pending delivery, completed delivery and recorded failures.
Counts are partial when `complete` is false.

The bridge configuration has this shape. Each path is absolute; the directory
must be owner-only mode `0700` and the file mode `0600`.

```json
{
  "schema_version": "open-brain-harness-bridge.v1",
  "brain_root": "/absolute/private/brain",
  "brain_id": "THE_EXISTING_BRAIN_ID",
  "policy": "/absolute/private/harness/policy.json",
  "consent_state": "/absolute/private/harness/provider-consent.json",
  "outbox": "/absolute/private/harness/outbox"
}
```

The Brain ID must come from the existing destination. The bridge refuses a
missing or different destination before ordinary bootstrap. Provider consent
uses the existing `open-brain consent` commands and is bound to the Brain's
current issuer epoch. A source rule alone does not grant provider consent.

## Source policy

The separate owner-only policy file has this shape:

```json
{
  "schema_version": "open-brain-harness-policy.v1",
  "enabled": true,
  "providers": {
    "codex": ["personal", "work"],
    "claude-code": ["personal", "work"],
    "pi": ["personal", "work"]
  },
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

The most specific project rule wins. Unmapped projects are local and unknown.
An authored line starting with `Confidential:`, `Private:` or `Local only:` can
narrow a source to local-only. It cannot grant cloud access. Credentials cause
capture refusal before outbox persistence. Exclusions and `enabled: false`
apply before transcript reads or Brain startup.

A host's boolean `local_only: true` capture field also narrows storage, even
when the project rule permits sharing. Custody retains this restriction and an
unmarked replay cannot widen it. The field grants no authority to read private
memory; that still requires trusted host enforcement.

Cloud retrieval intersects current policy, durable provider consent, engine
privacy and source spaces before matching or ranking. It rechecks authorization
before returning content. A later confidentiality marking creates a restrictive
source revision. A retained local revision cannot become shared through replay
of an earlier unmarked event. Pending source narrowing blocks cloud reads until
its restrictive delivery is acknowledged. Overlapping exclusions and restrictive
nested rules currently withhold the whole enclosing source space.

The final bridge release check also revalidates durable provider consent after
runtime cleanup and pending-request inspection. Changed or unavailable consent
withholds the results.

The subprocess CLI cannot turn a caller's `local_only` field into host authority.
Unknown or confidential retrieval requires a trusted Pi host that enforces local
transport across inference, classifiers, retries, summaries and inherited
context. The Pi extension accepts explicit host enforcement capabilities; a
generic private flag is insufficient. Native enforcement still needs verification.

## Lifecycle adapters

The optional Node asset `assets/harness-hook.mjs` provides Codex and Claude Code
Stop capture and UserPromptSubmit recall. Its command pins the runtime,
configuration, client and event. It preserves the original native session
metadata. `OPEN_BRAIN_HARNESS_DISABLED=1` opts out before any bridge invocation.

Capture has a two-second outer deadline. After durable enqueue, the integration
may start one detached foreground drain command with the CLI's sixty-second
deadline. This is optional integration scheduling: installing the core does not
install a daemon, recurring worker, listener or service. Later capture hooks
provide another delivery attempt. Queue acknowledgements mean local custody;
they do not mean the destination has saved or indexed the content.

Drain tries new requests before retries, then retries the oldest attempt. A
retained failure cannot indefinitely block later healthy requests. Deadline
cancellation escapes normal operational error handlers and stops the drain.

Recall has a three-second outer deadline and returns at most five complete rows
within eight KiB of additional context. Oversized rows are omitted visibly.
Content is labelled untrusted reference data and keeps its source citation.
Successful empty retrieval differs from unavailable or refused retrieval.

The optional Pi asset `assets/pi-memory.mjs` exports
`createPiMemoryExtension`. Its host supplies the bridge, SDK session manager,
project, fixed provider identity, search schema and local-only enforcement
capabilities. It captures authored branch entries with their stable native IDs,
recalls before agent start and exposes `brain_search_page`. Synthetic fixtures
cover confinement handshakes and refusal; the host must prove actual enforcement.

## Current selection and continuity limits

Selection is conservative and extractive. It recognizes specified English
patterns for facts, decisions and preferences, plus requested complete recipes.
Authorship is retained; assistant guesses and inferred preferences are excluded.
Selection is not factual verification or general semantic understanding.
Changed decisions currently remain historical statements until currentness
projection is implemented.

Inputs are bounded to 256 messages and 128 KiB. Oversized native events fail
visibly rather than discarding an earlier confidentiality marking. Selection has
separate message and output bounds; unsupported wording and oversized candidates
are omitted. Incremental capture for longer sessions remains acceptance work.

Claude message UUIDs and Pi branch entry IDs provide native identity. Codex's
fallback hashes authored role/text with occurrence order, independently of
physical transcript lines. It survives inserted tool metadata and rotation that
retains authored messages. Dropping repeated identical messages or changing fork
lineage still needs a continuity map. Detailed policy-aware source resolution,
current decisions and native cross-harness acceptance remain required work.
