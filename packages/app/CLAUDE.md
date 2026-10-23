# Open Brain app package

Conventions that apply only when working under `packages/app`.

## Agent setup, space/inbox, review publication, legacy write recovery

`consumer-import` is an owner-local normalized stdin CLI for coding agents,
using existing-only bootstrap and the engine's consumer intake tasks. It adds
no MCP grant or parser activation; both production profiles remain inactive.
Commit/recover/halt require `--confirm`. See `docs/consumer-intake-agents.md`.

Agent setup uses `agent setup` in the CLI and named plugin operations. The CLI supports separate
capture, search, content-read, history-read, inbox-read, organize, review-read, review-propose, and review-decide grants,
all off by default; desktop/plugin setup currently
exposes capture/search. `space` and `inbox` commands share `services/space_inbox.py` with explicitly
injected MCP callbacks. Routing changes assignment/search metadata, not publication or source trust.
Preview IDs bind configuration preimages and grants; only owned fragments may
be changed. Review CLI/MCP operations share `services/review_publication.py`; the engine owns
source-bound proposals and stable page revisions. See `docs/review-publication.md` and
`docs/records-and-history.md` and `docs/portable-brain-v14.md`. Use synthetic client profiles for tests. Existing Brain state requires schema 23 and
runtime session version 16; never weaken the compatibility checks to admit an older client. Owner-only legacy write
recovery is documented in `docs/managed-workspace-recovery.md`. Its read-only preview precedes
ordinary bootstrap; abandonment requires exclusive admission and never exposes general engine tasks.
