# Hermes memory adapter

Hermes can capture authored conversations and retrieve eligible Open Brain
memory through the existing foreground harness bridge. The adapter has no
Mattermost client, network listener, scheduler, or provider SDK. A trusted host
owns channel selection, canonical message identity, provider routing, deadlines,
and retries. These source tests do not establish installed or live acceptance.

## Capture contract

Use `client="hermes"` with `run_harness("enqueue", config_path, client, payload)`.
The host supplies an absolute, existing `project_path` representing its configured
source scope. A current explicit source rule must classify that scope as personal
or work and permit sharing. A channel name or a model's claimed tier is not a
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
entirely before custody. Hosts must mark injected reference context as unauthored.
The adapter retains Hermes attribution and canonical source IDs in citations;
it does not impersonate Pi. Existing clients retain their original citation
encoding, and older envelopes remain readable.

Unknown, secret, public, excluded, and local-only scopes are refused for Hermes.
Authored confidentiality markings and `local_only: true` also refuse capture.
Credential screening covers every retained authored message before persistence.
Refusals retain only denial metadata, with no refused message body. A later
restriction blocks earlier session material from cloud release; replay cannot
remove it. Parent and origin mappings supplied by a Hermes payload are refused.

Enqueue uses the existing destination-bound durable outbox without opening the
Brain. `queued` means local custody, not indexed memory. `drain` opens only the
existing identity-matched destination and submits normal engine source revisions.
A trusted relay may instead use `run_harness("capture", ...)` or:

```sh
open-brain harness capture --client hermes --config /absolute/private/bridge.json --json
```

Capture reads the event from stdin, enqueues it, and synchronously delivers only
its bounded requests through the existing engine journal and outbox receipts.
`acknowledged` means canonical capture and routing completed; `success_empty`
means conservative selection found no useful memory. Receipt replay returns the
same capture IDs. `queued`, `refused`, and `unavailable` must remain visible to
the host and must not be treated as canonical acknowledgement. A missing or
mismatched destination is never bootstrapped by this operation. Events with more
than sixteen custody chunks remain queued for later bounded drain commands.

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
source scope must match that tier. The engine intersects this singleton with
current provider policy and destination-bound durable provider consent before
searching, ranking, counting, or reading content. Source reads cannot fetch a
capture from the other tier. Policy and consent are rechecked before release.
An unknown provider, missing or revoked consent, and an empty intersection are
denied. Neither `local_only_host=True` nor any payload flag enables Hermes to
read unknown, confidential, or local-only memory.

Provider identity is the actual recipient selected by the host, not `hermes`.
The current policy and consent must grant that same provider. There is no
cross-provider fallback. Setup assets and stdio MCP installation continue to
support the existing native clients; Hermes requires its own trusted host.

## Replay and edits

The outbox binds source text and authorship to stable source IDs within the
configured scope and session. Repeated delivery reuses engine revisions and
receipts. Moving a retained message's display ordinal preserves its original
citation ordinal. Every authored Hermes source is tracked, including messages
that produce no selected memory.

Changing text under a retained source ID returns `lineage_conflict`, even when
the new text produces no selected memory. The adapter keeps the original accepted
source; this refusal does not reconcile the edited post or retract prior facts.
The host must surface the conflict for explicit reconciliation. It must not
invent a fresh ID merely to bypass edit detection. A confidentiality marking or
credential in a later edit latches a denial before any conflicting body can be
retained. Reusing canonical IDs across different sessions requires the host to
keep the original configured session identity; Hermes accepts no forged fork
lineage.
