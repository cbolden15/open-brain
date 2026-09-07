# ADR 0012: Open Brain and Secure Node product split

Status: accepted

Date: 2026-09-07

## Context

The completed public product foundation and the Secure Node protocol work were being presented as
one default installation. That made advanced encrypted custody, grants, compartments, fencing,
daemon ownership, and recovery controls prerequisites for a person who only wanted local capture,
search, and export.

The repository already contains useful implementation for both concerns. Discarding it would add
risk without improving the default experience. Keeping its current packaging would make Secure
Node's complexity and native dependencies the base product.

## Decision

Open Brain is the default five-minute local product. It has one local user, one local Brain,
automatic private data-directory setup, SQLite-backed capture and search, and full Portable Brain
export. It requires no daemon, service, Docker, certificate, grant, key-custody flow, storage-root
decision, or manual database setup. It does not claim application-level encryption.

Secure Node is the opt-in advanced profile. The work previously called the M1 Reference Node,
including completed W0 and the in-progress W1 semantic kernel, belongs to Secure Node. Stable M1
identifiers remain as historical aliases, while current planning uses `SN1-W*` names.

The selected near-term packaging is `open-brain` for the default and
`open-brain[secure-node]` for the advanced profile. The engine mirrors that split with a
`secure-node` extra. A separate `open-brain-secure-node` distribution is the stronger isolation
option and must be reconsidered if extras cannot enforce dependency, CLI, support, or release
boundaries cleanly.

Portable Brain v1 is the shared minimum record and export contract. Secure Node protocol records
must represent that shared semantic inventory losslessly. BrainPack v2 may add Secure Node custody,
authorization, receipt, and purge metadata, but cannot replace the shared core with an incompatible
model. Default-to-Secure-Node upgrade occurs through validated export and import, never by treating
the default SQLite files as Secure Node storage.

## Superseded decisions

This ADR supersedes only these earlier product-boundary clauses:

- the v0.5 requirement that the default product install and run one supervised daemon;
- the M1 plan's rule that a plain top-level `open-brain` install include the full Reference Node;
- ADR 0010's packaging statement that the top-level application automatically selects the engine
  `node` extra; and
- any wording that attributes Secure Node encrypted-custody guarantees to the default Open Brain
  profile.

The protocol, schema, cryptography, authorization, information-flow, purge, fencing, and
compatibility evidence from ADRs 0001 through 0011 remains accepted for Secure Node. Portable Brain
v1, stable identities, provenance, review history, and open export remain shared product-family
authority.

## Consequences

The current code is intentionally transitional. Plain `open-brain` still selects advanced
dependencies, stateful mutation still routes through the appliance daemon, and the CLI still asks
for `OPEN_BRAIN_ROOT`. Those are implementation gaps scheduled in the product roadmap, not reasons
to weaken this decision.

Secure Node W1 remained paused and unverified throughout the documentation gate. Its uncommitted
files are preserved and remain paused for a separate implementation continuation. No Secure Node
W2 work or default-product implementation began inside the documentation gate.

This preserves the existing engineering work while preventing Secure Node complexity from becoming
the default OSS experience.
