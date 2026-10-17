# Offline semantic search

Offline semantic search finds records by meaning as well as by words. A search for "where
did we talk about shipping delays" can return a note that says "the courier was late". It
runs on your machine with a small embedding model that ships as an optional resource in the
installed base. Nothing is sent over the network, and searching never downloads a model.

It is optional and off by default. Until you enable it, search is lexical only and no
embedding helper ever starts.

## Commands

All four commands are owner-local CLI commands. No MCP tool, plugin operation or desktop
screen can change the policy.

```sh
open-brain embedding enable --component-id <64 lowercase hex>
open-brain embedding rebuild
open-brain embedding status --json
open-brain embedding disable
```

- `enable` records your consent for one installed component. Consent is checked again
  before any text is released to the helper.
- `rebuild` runs in the foreground and builds the vector cache. Ctrl-C cancels it at the
  next bounded check.
- `status` prints the policy and the health object described below.
- `disable` revokes consent. Revocation takes effect immediately; semantic results stop
  before the cache files are cleaned up.

## The cache is derived

The vectors live in a local cache next to the Brain. The cache is derived data:

- Deleting it loses no records. Lexical search keeps working.
- It is local only. It is not part of a Portable Brain export, and it is not synchronized.
- After restoring a Portable Brain on another machine or from a backup, the cache starts
  empty. Run `open-brain embedding rebuild` there under current consent.
- Editing, retracting or re-routing a record invalidates its vectors at the moment the
  change commits, even if cleanup of old files lags.
- Backups and history may keep old cache material. Open Brain makes no secure-erasure claim
  for the cache.

## Search modes

`search.page` accepts three modes.

- `lexical` is the default. It never starts the helper.
- `hybrid_preferred` uses semantic and lexical results together when the resource, consent
  and cache are all ready. Otherwise it returns lexical results with the `model_unavailable`
  warning and `mode_used: "lexical"`.
- `hybrid_required` returns `model_unavailable` instead of lexical results when the
  resource, consent or cache is not ready.

When semantic search runs, `mode_used` is `hybrid`. Results are ranked by `ob.hybrid-rrf.v2`:
semantic and lexical lists are fused with equal weights, and only the lexical tiers that rank
by relevance (title and phrase matches) feed the lexical list. The Obsidian plugin and the
desktop app show `mode_used` and any warning next to the result count.

A cursor from a lexical search cannot continue as hybrid, and a hybrid cursor does not fall
back to lexical. After a rebuild, model change or consent change, start a new first page.

## Health states

`open-brain status --json`, `open-brain doctor` and `open-brain embedding status --json`
include an `embedding_health` object with schema `ob.embedding-health.v1`. It is owner-local
and is never part of MCP, plugin or harness output. It contains no paths, counts, timings or
text.

| Key | Values |
|---|---|
| `resource_state` | `missing`, `untrusted`, `unsupported`, `ready` |
| `cache_state` | `absent`, `incomplete`, `stale`, `ready` |
| `consent_state` | `required`, `enabled`, `revoked` |
| `component_id` | Installed component ID, or null |
| `last_error` | One `ob.embedding-error.v1` category, or null |

`ready` for the resource means the installed metadata verified and the platform is
supported. It does not measure quality or speed. A cache is `stale` when it was built for a
different component than the installed one, or when it fails validation. It is `incomplete`
when no complete generation exists yet. Health reads metadata only; it never starts the
helper.

## Limits

- Records larger than the per-document limit (64 KiB of text) are excluded from the cache.
  The rebuild receipt reports how many were excluded as `excluded_oversize`. They stay
  searchable lexically.
- A cache holds at most 100,000 chunks. A Brain that would exceed this does not get a
  complete cache.
