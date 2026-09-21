# Capture contract

The capture envelope preserves source, capture timestamp, normalized content type, immutable provenance, and the owner's one-line reason for saving an item.

Owner-authored captures require a non-empty one-line `capture_why`. Automated playlist captures may represent missing owner context explicitly as `automation_absent`; they are restricted to `hold` or `reference` and cannot become an idea or action candidate.

Intent is closed: `reference`, `idea`, `action_candidate`, or `hold`. Ideas and action candidates create review proposals only. Third-party content never silently creates a task.

Share intake accepts an owner-authored URL, reason, and optional shared text. Authentication, bounds, and JSON validation happen before durable queueing. Capture success requires immutable private raw persistence followed by either a durable private hold or receipt-bound event and distillation work. Capture creates no Markdown knowledge page or task.

Retries resume from durable boundaries. A capture with an existing extraction event retries only distillation queueing instead of fetching mutable source content again.

## Privacy tiers

Every capture carries one privacy tier from the closed set `public`, `work`, `personal`, `secret`, and `unknown`. The immutable privacy decision is recorded before persistence. Local owner capture and Markdown import keep their existing default decision unless the owner passes an explicit tier, and the owner-only explicit tier authority is never merged with the remote startup-policy tier set described below. See [privacy model](privacy-model.md).

## Admission limits

Admission is enforced in the engine's public capture boundary, before durable state exists. The limits are per process, because the core is one foreground process: the rate window, concurrency counter, and waiter count live in memory and recover in process. The CLI and MCP surfaces ship the compiled defaults; the limits are engine constructor configuration, not a flag a submitting client can change.

| Limit | Default |
|---|---|
| `max_envelope_bytes` | 8 MiB |
| `max_body_bytes` | 4 MiB |
| `max_batch_items` | 64 items |
| `max_batch_bytes` | 32 MiB |
| `requests_per_minute_per_principal` | 120 requests |
| `max_concurrent_admissions` | 8 |
| `max_writer_waiters` | 16 |
| `storage_high_free_bytes` | 2 GiB |
| `storage_critical_free_bytes` | 512 MiB |

Storage watermarks are absolute free bytes on the Brain root's filesystem, not usage ratios. A ratio threshold would refuse captures on an ordinary laptop whose large disk is mostly full while tens of gibibytes remain free, so both watermarks are byte floors. The probe runs before each submission's write path, so freeing space recovers capture without restarting anything.

## Admission results

Every refusal is one of nine stable named values carried by `CaptureAdmissionError`. A refusal leaves no capture row, source revision, blob, search document, or receipt.

| Result | Meaning | Retryable |
|---|---|---|
| `envelope_too_large` | The canonical request envelope exceeds `max_envelope_bytes` | No |
| `body_too_large` | The payload body exceeds `max_body_bytes` | No |
| `batch_too_large` | A submission batch exceeds the item or byte batch bounds | No |
| `rate_limited` | The principal exceeded `requests_per_minute_per_principal` in the sliding minute | Yes |
| `admission_busy` | The process is at `max_concurrent_admissions` for this submission path | Yes |
| `writer_queue_full` | The bounded writer-waiter queue is at `max_writer_waiters` | Yes |
| `storage_high` | Brain-root free bytes are below `storage_high_free_bytes` | Yes |
| `storage_critical` | Brain-root free bytes are below `storage_critical_free_bytes` | No, until space returns |
| `tier_not_permitted` | The requested tier is outside the trusted policy's allowed capture tiers | No |

Retryable means an identical retry may later be admitted unchanged. Recovery needs no process restart: the rate window ages out, the concurrency counter releases after each admission, and free space is re-probed per submission.

## Which gates apply to which path

Size checks (envelope, body, batch) and storage watermark checks apply to every submission path: owner capture, Markdown import, public-job, and destination-bound. The rate limit, concurrency cap, and bounded writer wait apply only to non-owner submissions, which today are the public-job and destination-bound paths.

Owner-originated capture and Markdown import are exempt from the rate, concurrency, and bounded-wait gates so existing local behavior is unchanged after upgrade. Owner paths keep their immediate busy behavior under writer contention instead of queueing behind remote submissions.

## Destination-bound submission

Destination-bound clients submit through one bounded operation governed by a trusted startup policy the owner selected at launch. The CLI command is `capture-submit` with `--policy`; the MCP session flag is `--allow-capture-submit`, which requires `--capture-policy`. Both read the same `launcher-policy.v1` JSON file, an exact schema with no extension fields, treated as trusted owner input, never as client input.

The policy is validated against this Brain's durable identity before any submission exists. A policy naming another Brain is refused with `destination_mismatch`, a superseded issuer epoch with `issuer_mismatch`, and an unexpected authorization generation with `stale_policy`. An `external_provider` egress mode fails closed with `consent_unavailable`, because the destination-bound path grants no egress authority.

The policy's `allowed_capture_tiers` set is the sole tier authority for this path. A request may select one tier inside that set. A missing tier becomes `unknown`, and it is accepted only when the set itself includes `unknown`. A tier outside the set is refused with `tier_not_permitted` before any engine call, leaving no partial state. No client flag can widen the set.

Non-owner admission gates apply: the principal is the tenant and actor pair from the policy identity, and rate, concurrency, and bounded writer waiting are enforced before the writer lease. Replay is idempotent on the delivery ID and request digest, and a reused delivery ID with different bytes conflicts rather than overwriting.

## Digest and receipt binding

The requested tier is inside the immutable request digest for the public-job and destination-bound paths only. The owner-path digest keeps its exact historical bytes, so existing local replay identity is unchanged. The destination-bound digest additionally binds the destination Brain ID and issuer epoch.

Every receipt binds `requested_tier` and `final_admitted_tier`. The two differ only when admission narrowed the tier at the canonical boundary. Destination-bound receipts additionally carry `delivery_id`, `request_sha256`, `destination_brain_id`, and `issuer_epoch`; every other path leaves those four unset so existing receipt bytes stay unchanged.

A synthetic destination-bound submission looks like:

```sh
open-brain capture-submit "synthetic destination text" \
  --policy /absolute/path/to/launcher-policy.json --privacy-tier work
```

The policy file is owner-prepared trusted input; the example text and any principal or provider names in examples are synthetic.
