# Hermes memory adapter

Hermes can capture authored conversations and retrieve eligible Open Brain
memory through the existing foreground harness bridge. The adapter has no
Mattermost client, network listener, scheduler, or provider SDK. A trusted host
owns channel selection, canonical message identity, provider routing, deadlines,
and retries. These source tests do not establish installed or live acceptance.

## Capture contract

Use `client="hermes"` with `run_harness("enqueue", config_path, client, payload)`.
The host supplies an absolute, existing `project_path` representing its configured
source scope. The capture's tier comes from the matching source rule; without
one, Hermes captures are `unknown` unless the owner sets a Hermes entry in the
policy's `client_defaults`. A channel name or a model's claimed tier is not a
source rule.

```json
{
  "project_path": "/absolute/synthetic/source-scope",
  "session_id": "synthetic-channel-session",
  "messages": [
    {
      "role": "user",
      "text": "I prefer violet pencils.",
      "authored": true,
      "source_id": "mattermost:synthetic-post:user",
      "ordinal": 11,
      "timestamp": "2026-01-01T00:00:00Z"
    }
  ]
}
```

`messages` contains authored user or assistant text, stable canonical source IDs,
and deterministic nonnegative ordinals up to `2**53 - 1`. The host derives each
ID from a canonical Mattermost post or request, with separate role locators when
one request produces multiple authored entries. The adapter cannot verify an
external post exists. IDs and session IDs allow letters, digits, `.`, `_`, `:`,
and `-`, start with an alphanumeric character, and contain at most 256 characters.
IDs and ordinals must be unique within an event. Timestamp is optional; when
present it must be an ISO timestamp with timezone. Source IDs must be opaque
locators, not credentials or private configuration.

Tool and system records, plus entries marked `authored: false`, are omitted
entirely before capture. Hosts must mark injected reference context as unauthored.
The adapter retains Hermes attribution and canonical source IDs in citations;
it does not impersonate Pi. Existing clients retain their original citation
encoding, and older envelopes remain readable.

Excluded scopes and scopes whose rule tier is `secret` are refused. A local-only
rule, an authored confidentiality marking, or `local_only: true` lowers the tier
to `unknown`, so no cloud reader can see it. Credential screening covers every
retained authored message and refuses the capture before anything is written.
Parent and origin fields in a Hermes payload are ignored; no session or fork
lineage is tracked.

Enqueue appends the event to the destination-bound retry spool without opening
the Brain. `queued` means the event waits in the spool, not indexed memory.
`drain` opens only the existing identity-matched destination and submits normal
engine source revisions. A trusted relay may instead use
`run_harness("capture", ...)` or:

```sh
open-brain harness capture --client hermes --config /absolute/private/bridge.json --json
```

Capture reads the event from stdin and delivers it inline through the existing
engine journal. Only requests whose delivery failed or was unavailable are
appended to the spool, and the result is then `queued`. If the Brain cannot be
opened, the event is spooled and the result is `unavailable`. `acknowledged`
means canonical capture and routing completed; `success_empty` means
conservative selection found no useful memory. Replaying the same event returns
the same capture and delivery IDs. `queued`, `refused`, and `unavailable` must
remain visible to the host and must not be treated as canonical acknowledgement.
A missing or mismatched destination is never bootstrapped by this operation.

The selector keeps its existing conservative English facts, preferences,
decisions, and requested complete recipes. Authorship is evidence of source,
not factual verification. Unsupported wording can produce `success_empty`.

## Recall authority

Hermes recall, search, and source reads require trusted host arguments:

```python
run_harness(
    "recall", config_path, "hermes",
    {"project_path": str(scope), "query": "preferred editor"},
    provider_id="synthetic-provider",
    read_tiers=frozenset({PrivacyTier.WORK}),
)
```

The corresponding foreground CLI accepts `--provider-id` and one `--read-tier`
(`personal` or `work`) for Hermes reads. These flags are host configuration, so a
relay must pin them outside model-controlled JSON and deny arbitrary command
arguments. JSON fields named `provider_id` or `read_tiers` grant no authority.
A Hermes read without trusted arguments fails closed. Automatic recall keeps the
existing five-row and eight-KiB context bounds. Search and source have the same
authorization boundary.

For personal planning channels, the host passes only `PrivacyTier.PERSONAL`.
For work or operations channels, it passes only `PrivacyTier.WORK`. The configured
source scope must match that tier. The read uses an external-provider authority
whose tiers are this singleton within the provider's durable consent. The core's
owner egress policy then decides visibility before searching, ranking, counting,
or reading content. Source reads cannot fetch a capture from the other tier.
Consent is rechecked before release. An unknown provider, missing or revoked
consent, and an empty intersection are denied. Hermes can never be declared
local-only, so no payload flag or configuration lets it read `unknown` memory.

Provider identity is the actual recipient selected by the host, not `hermes`.
Durable consent must name that same provider. There is no
cross-provider fallback. Setup assets and stdio MCP installation continue to
support the existing native clients; Hermes requires its own trusted host.

## Replay and edits

Capture binds source text and authorship to stable source IDs within the
configured scope and session. Repeated delivery reuses engine revisions through
their delivery IDs. Moving a retained message's display ordinal preserves its
original citation ordinal. Every authored Hermes source is tracked, including
messages that produce no selected memory.

Changing text under a retained source ID records a new revision of that source,
tiered under current policy. A later confidentiality marking therefore lowers
the source to `unknown` and withdraws it from cloud reads. The adapter does not
retract facts selected from earlier text; the host decides whether an edit
needs explicit reconciliation.
