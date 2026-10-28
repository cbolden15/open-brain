# Compact search performance measurements

Measured 2026-10-28 on macOS arm64 with Python 3.14.7. These are synthetic owner-local
read measurements of the implemented compact-search candidate, not installed-artifact
or real-source acceptance. Every fixture record matched the query, so counting had
to validate the entire corpus.

## Method

Construct 1,000- and 10,000-record temporary Brains outside the checkout. Extend one
valid owner capture into synthetic source/revision/privacy/index rows and retained
source files in a single fixture transaction, with distinct version-4 UUIDs and
recomputed receipt and source digests. Validate each retained source with the
Portable validator and run SQLite integrity checking. Fixture setup is excluded
from query timing; this does not measure capture throughput.

Run the real engine reader, authority checks, file-custody checks, record projector,
ranking, grouping and cursor code without mocking their results. Use the same
query and 20-reference limit for full and compact first pages. Compact requests use
title grouping and the default 4096-byte target. Measure both unique titles and
one title shared by every record. Assert exact corpus/group counts and count-only
empty results. The continuation uses the first compact response's real cursor.

Wall times below are single uninstrumented calls, not percentiles or guarantees.
Measure Python allocations separately on a replay of each 1,000-record request;
do not include allocation tracing in its reported latency. For the 10,000-record
case, record process maximum RSS without Python tracing. Encode the actual MCP
response, including its compact-only untrusted-text prefix, JSON escaping and
duplicated text/structured content, through `encoded_tool_response_size`.

## Query latency

| Matching records | Title shape | Full first page | Compact first page | Compact continuation | Count only |
|---|---|---:|---:|---:|---:|
| 1,000 | Unique | 0.100 s | 1.146 s | 1.143 s | 1.154 s |
| 1,000 | One shared title | 0.097 s | 1.135 s | 1.185 s | 1.161 s |
| 10,000 | Unique | 2.882 s | 14.756 s | 14.602 s | 14.599 s |
| 10,000 | One shared title | 3.042 s | 14.873 s | 14.615 s | 14.652 s |

Each compact continuation recomputes current exact eligibility and totals. Count
only saves response content, not that projection work. A smaller `limit` bounds
the output but does not avoid the full matching-set check. Narrow queries and
space/kind filters can reduce the number of matched projections to validate.

## Response size

All sizes are bytes. These are first-page results; continuations have comparable
sizes, with at most a small change for member offsets.

| Matching records | Title shape | Full MCP response | Compact JSON payload | Compact MCP response | Count-only MCP response | Compact references returned |
|---|---|---:|---:|---:|---:|---:|
| 1,000 | Unique | 42,896 | 3,966 | 8,459 | 685 | 9 |
| 1,000 | One shared title | 42,656 | 3,055 | 6,501 | 679 | 20 |
| 10,000 | Unique | 42,896 | 3,968 | 8,463 | 689 | 9 |
| 10,000 | One shared title | 42,656 | 3,057 | 6,505 | 681 | 20 |

The byte ceiling stops unique-title pages before their 20-reference ceiling.
Shared-title pages fit 20 references because they repeat the row metadata once.
In this fixture compact mode reduced first-page MCP bytes by about 80–85%.
Canonical payload size and final MCP size are different quantities; neither is
a promise about an unknown client's tokenizer or token cap.

## Memory and acceptance

The largest traced Python allocation peak for a compact request in the
1,000-record fixture was 415,903 bytes. SQLite uses a separate temporary relation
with a 2 MiB cache target and can spill to disk. The maximum process RSS observed
in the 10,000-record run was 76,496,896 bytes, about 73 MiB. Process RSS includes
imports, fixture construction and earlier operations; it is a whole-process high
water mark, not a per-request allocation delta. Python tracing excludes SQLite's
native allocations and the filesystem cache.

The measured implementation meets the response-size goal with bounded retained
metadata and no persistent index or migration. Its exact counts impose a visible
latency cost for broad queries. Retain that behavior explicitly rather than
reporting a truncated scan as an exact total. A future lower-latency counting
index or a distinct approximate-count mode needs its own contract and decision.
