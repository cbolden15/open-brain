# Open Brain app package

Conventions that apply only when working under `packages/app`.

## Agent setup, space/inbox, review publication, legacy write recovery

Agent setup uses `agent setup` in the CLI and named plugin operations. The CLI supports separate
capture, search, content-read, history-read, inbox-read, organize, review-read, review-propose, and review-decide grants,
all off by default; desktop/plugin setup currently
exposes capture/search. `space` and `inbox` commands share `services/space_inbox.py` with explicitly
injected MCP callbacks. Routing changes assignment/search metadata, not publication or source trust.
Preview IDs bind configuration preimages and grants; only owned fragments may
be changed. Review CLI/MCP operations share `services/review_publication.py`; the engine owns
source-bound proposals and stable page revisions. See `docs/review-publication.md` and
`docs/records-and-history.md` and `docs/portable-brain-v4.md`. Use synthetic client profiles for tests. Existing Brain state requires schema 7 and
runtime session version 2; never weaken the compatibility checks to admit an older client. Owner-only legacy write
recovery is documented in `docs/managed-workspace-recovery.md`. Its read-only preview precedes
ordinary bootstrap; abandonment requires exclusive admission and never exposes general engine tasks.
