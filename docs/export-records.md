# Record export

`open-brain export-records` prints the current head captures that one external-provider launcher
policy may read, one JSON object per line. It is for handing captured text to a consumer outside
the Brain, such as a weekly digest job, without giving that consumer an MCP session or any write
grant.

The command only reads. It writes nothing to the Brain and holds no lock after it exits.

## Usage

```sh
open-brain export-records \
  --policy /abs/path/newsletter-policy.json \
  --consent-state /abs/path/consent.json \
  --since 2026-10-24 \
  [--until 2026-11-01] [--limit 2000] [--max-bytes-per-record 16384] \
  [--data-dir /abs/path/brain] [--json]
```

- `--since` is the first UTC day included. `--until` is the UTC day to stop before; without it the
  window ends at the moment the command starts. Both take `YYYY-MM-DD` only.
- `--limit` (1 to 10,000, default 2,000) is a ceiling, not a page size. If more records qualify,
  the command prints nothing and fails with `response_too_large`, so a short export is never
  mistaken for a complete one. Narrow the window and run it again.
- `--max-bytes-per-record` (1 to 1,048,576, default 16,384) cuts each record's text on a UTF-8
  character boundary and appends ` [truncated]`.
- `--json` changes only how errors are reported. Records are always JSON Lines. With `--json`, a
  failure prints one error object on stdout, so a consumer must check the exit code before
  treating any output as records.
- Every candidate in the window is read and checked, including ones that end up refused, while the
  command holds a shared read lease. Keep windows to days or weeks.

## Authority

The policy must be a `launcher-policy.v1` file with `egress_mode: external_provider`, read through
the same owner-only, no-follow path that `mcp --session-policy` uses. Its `consent_id` must name an
active grant in `--consent-state` for the same provider and authorization generation, and the
grant's tiers must cover the policy's `allowed_read_tiers`. The policy must hold `content-read`.

These are refused with exit 78 and print no records:

| Policy | Error code |
|---|---|
| `owner_local`, or `space_ids: []` | `invalid_policy` |
| no `--consent-state`, or no matching active consent | `consent_unavailable` |
| another Brain | `destination_mismatch` |
| another issuer epoch | `issuer_mismatch` |
| an older authorization generation, including a consent revoked or replaced after the policy was written | `stale_policy` |

A missing `--policy` or a malformed date is a usage error (exit 2). There is no fallback to owner
authority.

## What is exported

A record is exported when all of the following hold:

1. It is the head revision of an active, available, non-historical source.
2. It was accepted inside the window.
3. Its stored effective tier is one of the policy's read tiers. `secret` and `unknown` never
   qualify. `personal` qualifies only when the policy's read tiers and the consent grant both
   include it.
4. `RecordProjector.source` accepts it under the policy's authority. This repeats the tier check
   and adds the `external_egress` decision, the space scope, lifecycle and visibility, sharing
   eligibility, and a check of the source bytes against their recorded digest.

Step 3 runs in SQL and step 4 runs per record, so each check stands on its own. A record either one
refuses is skipped silently. So is a record whose source file is missing, damaged or replaced, so
one bad record does not block the rest of the window.

`external_egress` comes from the capture's own privacy decision or from the owner egress policy
(`egress-policy set --tier work`, for example). A work capture with neither is not exported, even
when the policy and consent allow the work tier.

## Output

Records are ordered by acceptance time, compared as times rather than text, then by capture id.
With an explicit `--until`, the same Brain, policy and window give the same bytes. Without it the
window ends when the command starts, so repeated runs can differ.

```json
{"captured_at":"2026-10-28T14:03:11Z","id":"capture_...","privacy_tier":"work","source":{"origin":"third_party","payload_family":"text","source_id":"source_..."},"text":"...","truncated":false}
```

`source` names the public origin, payload family and source id only. The raw source reference
(a file path or URL as captured) is never included.

The command collects and checks every record before printing any. A failure partway through prints
no records.
