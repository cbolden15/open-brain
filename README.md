# Open Brain

Open Brain is a local-first capture and search tool with full portable export. The default product is
for one local user and one local Brain. Its target experience is one-command installation, automatic
private data-directory setup, direct SQLite-backed capture and search, and no required daemon,
Docker, certificate, grant, storage-root decision, or manual database setup.

Secure Node is the opt-in advanced profile. It owns the encrypted custody, compartments,
authorization, receipts, fencing, certified purge, recovery, service, and multi-client work formerly
called the M1 Reference Node. The products share record identities and Portable Brain data so a
default Brain can upgrade without an in-place database migration.

The accepted boundary is in [the product-family contract](docs/product-family.md), with milestones in
[the product roadmap](docs/plans/product-roadmap.md). This preserves the existing engineering work
while preventing Secure Node complexity from becoming the default OSS experience.

## Principles

- Preserve the owner's one-line reason for saving something as first-class provenance.
- Route intent through a closed enum: `reference`, `idea`, `action_candidate`, or `hold`.
- Require review before third-party content can become an owner-authored idea or action.
- Default unknown or personal content to local-only handling.
- Keep private content, credentials, host configuration, and runtime state outside the repository.
- Maintain one canonical application repository rather than public and private forks.

## Development

Requirements: Python 3.14 and [uv](https://docs.astral.sh/uv/).

### Target installation

The default installation target is one command on a clean supported macOS or Linux machine. The
five-minute command and release test are specified in
[the five-minute acceptance test](docs/acceptance/five-minute-install.md). The small base package,
automatic local bootstrap, native artifact, and checksum-verifying installer are implemented and
locally verified. No release artifact has been published yet, and capture, search, export, status,
and doctor remain in `OB1-W1`.

The current macOS source and local artifact steps are in
[the macOS installation guide](docs/install-macos.md).

### Current source-checkout Open Brain

The default command creates one private local Brain automatically and reuses its stable identity.
It ignores the retained Secure Node `OPEN_BRAIN_ROOT` setting.

```bash
uv sync --frozen --package open-brain --no-dev
uv run --frozen --package open-brain --no-dev open-brain init --json
```

On macOS, state is created under
`$HOME/Library/Application Support/open-brain/brain`. On Linux, it is created under
`${XDG_DATA_HOME:-$HOME/.local/share}/open-brain/brain`.

### Secure Node precursor

The retained appliance remains available only through the explicit Secure Node extra and command.
It requires an explicit Brain root and runs a daemon:

```bash
export OPEN_BRAIN_ROOT="$HOME/open-brain-secure-node"
uv run --package open-brain --extra secure-node open-brain-secure-node init --json
uv run --package open-brain --extra secure-node open-brain-secure-node daemon
```

Use `open-brain-secure-node` for its retained owner CLI families. Use
`open-brain-secure-node-mcp` for its scoped MCP process.

```bash
uv run --package open-brain --extra secure-node open-brain-secure-node spaces create "Projects" --delivery=setup-projects --json
uv run --package open-brain --extra secure-node open-brain-secure-node capture quick text "Review the roadmap" --delivery=capture-roadmap --json
uv run --package open-brain --extra secure-node open-brain-secure-node inbox list --json
uv run --package open-brain --extra secure-node open-brain-secure-node query roadmap --json
```

Canonical text capture requires the `space_id` returned by `spaces create`, because Portable Brain
canonical-page frontmatter always carries a stable space identity:

```bash
uv run --package open-brain --extra secure-node open-brain-secure-node capture canonical text "Project context" \
  --delivery=capture-project-context \
  --space=space_REPLACE_WITH_RETURNED_ID \
  --json
```

Every mutating command requires a caller-supplied delivery ID. Repeating the same request
with the same delivery ID returns the existing identifiers. Reusing a delivery ID for a
different request fails closed and records metadata-only quarantine evidence.

### Verify the repository

```bash
uv sync --group dev
uv run open-brain --version
uv run ruff check .
uv run mypy
uv run pytest -q
uv run python -m build
```

Release auditing requires a local, uncommitted private denylist:

```bash
PRIVATE_DENYLIST=/path/to/private-denylist.txt make audit
```

The denylist contains one private term per line. Blank lines and lines beginning with `#` are ignored. Release mode refuses to pass without it.

## Status

`OB1-W0` implements the product boundary, automatic private-directory bootstrap, and unpublished
base-native installer path. Plain `open-brain` installs no Secure Node dependency and starts no
daemon. The full five-minute journey is not ready until `OB1-W1` adds capture, search, export,
status, and doctor and `OB1-W2` passes the published clean-host matrix.

The retained appliance implementation supports one local Brain root, stable portable identities,
typed capture, spaces, inbox routing, sibling review proposals, terminal decisions, canonical
Markdown publication, direct-edit reconciliation, lexical retrieval, immutable backup, disposable
restore, and distinct Portable export/import. The CLI, authenticated HTTP/share boundary, local UI,
scoped MCP, and public-job sinks use bounded capabilities over the same engine task objects. With
no model configured, captures remain usable and report `pending_enrichment`.

This is pre-alpha software. Phase 2 implements engine-level Portable Brain validation, export,
clean-root import, and disposable index rebuild. Export and import preserve portable identities,
history, routing, and exact source bytes while excluding operational state such as credentials,
databases, leases, runtime files, and indexes. The retained appliance profile uses provider `none` and
loads no connectors. A retained synthetic `JOB-029` proof exercises the internal seam only with
an absolute private configuration reference, capture-only authority, and egress enabled; host
evidence binds accepted captures to checkpoint advancement.

The public result projection exposes opaque IDs, bounded provenance, and safe titles/excerpts,
not raw or encoded protected references, absolute paths, credentials, storage-derived slugs and
paths, or bare SHA-256 tokens.
The retained Phase 3 work also defines source-checkout upgrade, rollback, and data-preserving uninstall through an
injected artifact lifecycle port, with launchd/systemd adapter evidence on Linux and macOS CI. The
default source-checkout effect remains unavailable. P4-W5 adds an unpublished frozen composition
with a manifest-bound native adapter, active-daemon quiescence, rollback restoration, and confined
managed cleanup. The native build reads an isolated archive of the named Git tree, rejects
replacement refs and external attributes, and compares every extracted blob and mode with the raw
no-replace tree. It admits only tracked package resources and records the source-tree digest.
Launchd upgrades unload the KeepAlive job before offline work and bootstrap it again afterward.
Those artifacts remain unpublished Secure Node precursor evidence. Publishing remains a separate
owner-authorized step.
Predecessor modules remain retained legacy compatibility code and are excluded from the default
application path.

## License

Apache License 2.0. See `LICENSE` and `NOTICE`.
