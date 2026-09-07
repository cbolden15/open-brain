# Open Brain and Secure Node roadmap

- Status: Accepted product split; `PF-W0` and `SN1-W1` complete; `CORE-W0` next
- Date: 2026-09-07
- Product authority: [`../product-family.md`](../product-family.md)
- Acceptance authority: [`../acceptance/five-minute-install.md`](../acceptance/five-minute-install.md)

## Current state

The product split is a naming and delivery-boundary change, not a restart. Completed protocol,
schema, crypto-compatibility, and architecture work is retained as Secure Node work. The uncommitted
semantic-kernel files were retained without modification throughout `PF-W0` and resumed only after
that gate closed.

`SN1-W1` is complete after ADR 0013's pre-release schema and lifecycle correction, focused and
repository-wide gates, and a fresh independent `READY` review with no findings. `CORE-W0` is the
next required milestone; no `SN1-W2` persistence work may start until its shared mapping gate
closes.

## Milestones

| Milestone | Outcome | Workstreams | Gate |
|---|---|---|---|
| `PF-W0` Product split | One authoritative product contract, naming system, packaging direction, and acceptance test | Documentation only | All current public authority points at the split and no implementation begins |
| `CORE-W0` Shared portability | Freeze the lossless mapping between Portable Brain v1 shared records and Secure Node protocol records | Schemas, adapters, conformance fixtures | A default export imports into a fresh Secure Node without identity or byte drift |
| `OB1` Five-minute Open Brain | Ship the default local product | `OB1-W1` packaging/bootstrap; `OB1-W2` direct capture/search/export; `OB1-W3` clean-host release proof | The exact 300-second macOS/Linux test passes from published artifacts |
| `SN1` Secure Node | Finish the advanced encrypted and authorized node formerly called M1 | `SN1-W0` through `SN1-W8` | Secure Node conformance and independent review pass without changing the Open Brain default |
| `UP1` Upgrade proof | Prove a real default-to-Secure-Node transition | `UP1-W0` export/import fixture; `UP1-W1` clean-host upgrade rehearsal | Source remains readable; target preserves the shared inventory and records new security policy honestly |

`CORE-W0` may be implemented alongside the product tracks only after `PF-W0` closes. It must close
before `SN1-W2` persists a second canonical representation or `OB1-W3` claims release readiness.

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

### `OB1-W1`: base packaging and automatic bootstrap

- Make `open-brain` the small default dependency closure.
- Move all Secure Node dependencies behind `open-brain[secure-node]` and
  `open-brain-engine[secure-node]`.
- Add the versioned one-line installer for the supported host matrix.
- Resolve and create the platform-local Brain root automatically on first stateful command.
- Create one owner and one Brain idempotently, with owner-only filesystem permissions.

Exit: a base install imports and reports help with no Secure Node dependency import, prompt, config,
daemon, listener, or service effect.

### `OB1-W2`: direct capture, search, and export

- Provide direct `open-brain capture`, `open-brain search`, and `open-brain export` commands.
- Use the existing engine and Portable Brain semantics behind a direct single-process composition.
- Keep SQLite setup internal and automatic.
- Make provider `none` and no egress the complete default path.
- Validate each full export before reporting success.

Exit: a first capture is durable, immediately searchable, and present in a verified Portable Brain
export without starting the appliance daemon.

### `OB1-W3`: clean-host and release proof

- Run the exact five-minute test on every supported clean macOS and Linux host.
- Prove no Secure Node dependencies or claims enter the base artifact.
- Prove reinstall and first-use bootstrap are idempotent.
- Publish versioned artifacts and installation documentation from the same source identity.

Exit: `OB-INSTALL-*`, `OB-DATA-*`, `OB-OPS-01`, and `OB-PRIVACY-01` all pass.

## Upgrade workstreams

### `UP1-W0`: conformance fixture

Export a populated default Brain containing every shared record family. Import it into a fresh
Secure Node with an explicit target compartment and custody policy. Compare stable IDs, canonical
bytes, source bytes, timestamps, spaces, provenance, and review outcomes.

### `UP1-W1`: user-facing upgrade rehearsal

Run the export/import path from released artifacts on a clean host. Keep the source intact, verify
the Secure Node target, search both sides for the same fixture, and prove the target reports only
post-import encryption coverage.

## Current conflicts and disposition

| Current item | Conflict with split | Disposition |
|---|---|---|
| `packages/app/pyproject.toml` makes `open-brain` depend on `open-brain-engine[node]`, Starlette, and Uvicorn | Yes. Plain install currently selects Secure Node dependencies. | Change in `OB1-W1`; do not edit during this documentation gate. |
| Installed mutating CLI routes through the appliance daemon and requires `OPEN_BRAIN_ROOT` | Yes. The default must run directly and choose its data directory automatically. | Change in `OB1-W1` and `OB1-W2`. |
| Existing source/wheel install guide initializes a root and starts a daemon | Yes as a default-product guide. | Retain as transitional Secure Node precursor evidence; replace the quickstart in `OB1`. |
| `SN1-W0` protocol, crypto, grant, receipt, fencing, purge, and recovery contracts | No after reclassification. | Preserve unchanged as Secure Node authority. |
| `SN1-W1` semantic-kernel implementation | No direct conflict. It contains advanced compartment and ciphertext state by design. | Preserve as completed Secure Node work; do not route it through the default Open Brain command path. |
| Portable Brain v1 and Secure Node BrainPack v2 currently lack an executable lossless mapping | Compatibility gap, not discarded work. | Close in `CORE-W0` before Secure Node persistence or a public upgrade claim. |

## Documentation gate result

`PF-W0` completed on 2026-09-07. The product contract, active Secure Node plan, architecture and
privacy boundaries, historical-contract notices, README, exact acceptance test, and governed
workstream state now agree. The gate used changed-document link validation, diff-integrity checks,
and the focused source-traceability and architecture baseline; it did not claim that the paused
`SN1-W1` tree or the five-minute product implementation had passed full verification.

Closing `PF-W0` permits a separate implementation continuation. It does not itself authorize
`SN1-W2`, publication, push, deployment, or live migration. `CORE-W0` must close before `SN1-W2`.
