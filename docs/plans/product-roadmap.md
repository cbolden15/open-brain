# Open Brain and Secure Node roadmap

- Status: Accepted product split; `OB1-W0` complete; `OB1-W1` active; `CORE-W0` paused after
  implementation review
- Date: 2026-09-07
- Product authority: [`../product-family.md`](../product-family.md)
- Acceptance authority: [`../acceptance/five-minute-install.md`](../acceptance/five-minute-install.md)

## Current state

The product split is a naming and delivery-boundary change, not a restart. Completed protocol,
schema, crypto-compatibility, and architecture work is retained as Secure Node work. The
semantic-kernel tree is complete and frozen at its accepted commit. `PF-W0` preserved its
then-uncommitted files without changing them; a later, separate continuation finished and verified
that work as `SN1-W1`.

`SN1-W1` is complete after ADR 0013's pre-release schema and lifecycle correction, focused and
repository-wide gates, and a fresh independent `READY` review with no findings. The first
`CORE-W0` implementation is preserved at commit `10d0846`, but its final review found three gaps.
That workstream is paused with `SN1-W2` still gated. `OB1-W0` completed on the separate local branch
`goal/open-brain-five-minute-install` at implementation commit `ebef782`. Its small base dependency
graph, direct private bootstrap, Python 3.14 native artifact, and checksum-verifying installer passed
the full and focused repository gates plus an independent `READY` review with no P0 through P2
findings. The artifacts remain unpublished. `OB1-W1` now has a local implementation candidate under
[`2026-09-07-ob1-w1-direct-local-journey.md`](2026-09-07-ob1-w1-direct-local-journey.md). Its focused
preflight, full verification, and native macOS artifact journey passed at its first implementation
commit. The first closure review returned two P1 findings for parser redaction and daemon ownership;
both fixes passed refreshed focused, full, and native gates. The second review confirmed those fixes
and found one P1: runtime evidence was observed only after profile compilation could change a partial
root. The tests-first read-only preflight correction now passes focused, full, and native gates.
The third review found a narrower identity-loss case for held daemon authority and one P2 covering
two stale contributor-guide references. Root-level lease inspection and guide corrections now pass
refreshed focused, full, and native gates. Fresh independent review remains.

## Milestones

| Milestone | Outcome | Workstreams | Gate |
|---|---|---|---|
| `PF-W0` Product split | One authoritative product contract, naming system, packaging direction, and acceptance test | Documentation only | All current public authority points at the split and no implementation begins |
| `CORE-W0` Shared portability | Freeze the lossless mapping between Portable Brain v1 shared records and Secure Node protocol records | Shared model, pure inbound adapter, schemas, conformance fixtures | The complete engine-produced fixture validates, maps through the in-memory Secure Node kernel, and reconstructs every manifest-declared byte without identity drift |
| `OB1` Five-minute Open Brain | Ship the default local product | `OB1-W0` packaging/bootstrap; `OB1-W1` direct capture/search/export; `OB1-W2` clean-host release proof | The exact 300-second macOS/Linux test passes from published artifacts |
| `SN1` Secure Node | Finish the advanced encrypted and authorized node formerly called M1 | `SN1-W0` through `SN1-W8` | Secure Node conformance and independent review pass without changing the Open Brain default |
| `UP1` Upgrade proof | Prove a real default-to-Secure-Node transition | `UP1-W0` export/import fixture; `UP1-W1` clean-host upgrade rehearsal | Source remains readable; target preserves the shared inventory and records new security policy honestly |

`CORE-W0` may be implemented alongside the product tracks only after `PF-W0` closes. It must close
before `SN1-W2` persists a second canonical representation or `OB1-W2` claims release readiness.
That second dependency is deliberate: the first public default release will not claim portability
while its documented Secure Node upgrade mapping is undefined. `CORE-W0` proves the pure mapping;
the durable fresh-node import belongs to `UP1-W0` after the required Secure Node work exists.

If `CORE-W0` finds that either frozen contract cannot represent a shared family without loss, work
stops at a new reviewed ADR. That ADR may amend pre-release Portable Brain v1 or the Secure Node
envelope, must list the affected schemas, fixtures, evidence, and downstream workstreams, and must
receive an independent `READY` review before mapping implementation resumes.

## Secure Node workstream mapping

Stable `M1-W*` identifiers remain in historical commits, receipts, manifests, and filenames. Current
work uses the following names:

| Previous ID | Current name | State |
|---|---|---|
| `M1-W0` | `SN1-W0`: Secure Node executable protocol freeze | Complete and independently accepted |
| `M1-W1` | `SN1-W1`: Secure Node pure semantic kernel | Complete and independently accepted |
| `M1-W2` | `SN1-W2`: transactional encrypted ledger and blobs | Not started |
| `M1-W3` | `SN1-W3`: capability authorization, request binding, and fencing | Not started |
| `M1-W4` | `SN1-W4`: commit, changes, and inspect | Not started |
| `M1-W5` | `SN1-W5`: compartment-safe search and projections | Not started |
| `M1-W6` | `SN1-W6`: provenance-closed certified purge | Not started |
| `M1-W7` | `SN1-W7`: Secure Node setup, clients, and service operation | Not started |
| `M1-W8` | `SN1-W8`: compatibility, conformance, and closure | Not started |

The active implementation plan remains at
[`2026-09-04-m1-semantic-kernel-node.md`](2026-09-04-m1-semantic-kernel-node.md) so historical links
and evidence do not break. Its title and current terminology identify it as Secure Node M1.

## Open Brain workstreams

### `OB1-W0`: base packaging and automatic bootstrap

- Make `open-brain` the small default dependency closure.
- Move all Secure Node dependencies behind `open-brain[secure-node]` and
  `open-brain-engine[secure-node]`.
- Add the versioned one-line installer for the supported host matrix.
- Resolve the platform-local application data home and create its `brain` child automatically on
  first stateful command. An absolute `--data-dir` override names that Brain root directly.
- Create one owner and one Brain idempotently, with owner-only filesystem permissions.
- Validate existing components without following symlinks, reject unsafe existing managed roots
  rather than repairing them, and reject the named network or synchronized locations before
  creating plaintext state. Revalidate root identity before the first identity and SQLite writes.
- At the end of this workstream, prove dependency isolation, CLI dispatch isolation, artifact-policy
  coverage, and version coordination. If any proof fails, invoke the accepted
  `open-brain-secure-node` distribution fallback before `OB1-W2`.

Exit: a base install imports and reports help with no Secure Node dependency import, prompt, config,
daemon, listener, or service effect.

### `OB1-W1`: direct capture, search, and export

- Provide direct `open-brain capture`, `open-brain search`, and `open-brain export` commands.
- Use the existing engine and Portable Brain semantics behind a direct single-process composition.
- Keep SQLite setup internal and automatic.
- Make provider `none` and no egress the complete default path.
- Validate each full export before reporting success.
- Implement daemonless `open-brain status --json` with the exact acceptance fields and direct
  `open-brain doctor --check private-data-directory|no-background-runtime|base-dependency-closure`
  checks used by the clean-host gate.

Exit: a first capture is durable, immediately searchable, and present in a verified Portable Brain
export without starting the appliance daemon. The exact status and doctor checks also pass without
opening a listener or importing Secure Node code.

### `OB1-W2`: clean-host and release proof

- Run the exact five-minute test on every supported clean macOS and Linux host.
- Prove no Secure Node dependencies or claims enter the base artifact.
- Prove reinstall and first-use bootstrap are idempotent.
- Publish versioned artifacts and installation documentation from the same source identity.

Exit: `OB-INSTALL-*`, `OB-DATA-*`, `OB-OPS-01`, and `OB-PRIVACY-01` all pass.

## Upgrade workstreams

### `UP1-W0`: conformance fixture

Export a populated default Brain containing every shared record family. Import it into a fresh
Secure Node with an explicit target compartment and custody policy. Compare stable IDs, canonical
bytes, source bytes, timestamps, spaces, provenance, and review outcomes. The engine fixture covers
every shared family, including families not created by the minimum default CLI. The released default
journey separately proves every family that its commands create. Import is all-or-nothing at one
receipt boundary: an abort leaves no addressable target Brain, and retrying the same import is
idempotent.

### `UP1-W1`: user-facing upgrade rehearsal

Run the export/import path from released artifacts on a clean host. Keep the source intact, verify
the Secure Node target, search both sides for the same fixture, and prove the target reports only
post-import encryption coverage. Create the plaintext export in an owner-only private location.
After verification, exercise and document the source-and-export decommission path, while warning
that retained plaintext copies remain outside Secure Node encryption and certified purge.

`UP1-W0` cannot start before `SN1-W3` closes because it needs durable encrypted storage and target
authorization. `UP1-W1` cannot start before the complete `SN1` gate and installable Secure Node
artifact exist. Both workstreams must rehearse an interrupted import, target quarantine or atomic
discard, and idempotent retry.

## Current conflicts and disposition

| Current item | Conflict with split | Disposition |
|---|---|---|
| `packages/app/pyproject.toml` made `open-brain` depend on `open-brain-engine[node]`, Starlette, and Uvicorn | Resolved. Plain install now selects only the base engine. | Completed in `OB1-W0`; the renamed dependencies require explicit `[secure-node]`. |
| Installed mutating CLI routed through the appliance daemon and required `OPEN_BRAIN_ROOT` | Resolved in the local implementation candidate. | `OB1-W0` routes the default to automatic bootstrap; `OB1-W1` uses direct engine tasks for capture, search, export, status, and doctor. |
| Existing source/wheel install guide initializes a root and starts a daemon | Yes as a default-product guide. | Retain as transitional Secure Node precursor evidence; replace the quickstart in `OB1`. |
| `SN1-W0` protocol, crypto, grant, receipt, fencing, purge, and recovery contracts | No after reclassification. | Preserve unchanged as Secure Node authority. |
| `SN1-W1` semantic-kernel implementation | No direct conflict. It contains advanced compartment and ciphertext state by design. | Preserve as completed Secure Node work; do not route it through the default Open Brain command path. |
| Portable Brain v1 and Secure Node BrainPack v2 currently lack an executable lossless mapping | Compatibility gap, not discarded work. | Close the pure inbound mapping in `CORE-W0`; prove durable import later in `UP1-W0`. |

## Documentation gate result

`PF-W0` completed on 2026-09-07. The product contract, active Secure Node plan, architecture and
privacy boundaries, historical-contract notices, README, exact acceptance test, and governed
workstream state now agree. The gate used changed-document link validation, diff-integrity checks,
and the focused source-traceability and architecture baseline; it did not claim that the paused
`SN1-W1` tree or the five-minute product implementation had passed full verification.

Closing `PF-W0` permitted the separate `SN1-W1` continuation that is now complete and independently
accepted. It did not authorize `SN1-W2`, publication, push, deployment, or live migration.
`CORE-W0` must close before `SN1-W2`.
