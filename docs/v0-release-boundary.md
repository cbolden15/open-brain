# Historical v0 appliance release-boundary evidence

Status: accepted historical Phase 0 evidence; default-product boundary superseded 2026-09-07

Current authority: [Open Brain product-family contract](product-family.md), version 0.6, revised
2026-09-07. The [v0.5 product contract](v0-product-contract.md),
[Option C architecture plan](plans/option-c-architecture.md), and
[proposed v0 system architecture](architecture/proposed-v0-system-architecture.md) remain
historical evidence for the completed appliance work. They do not make that appliance the default
product after the split.

## Change control

The v0.5 outcomes below remain fixed as historical acceptance evidence. Current implementation
must preserve that engineering for Secure Node while making plain Open Brain a direct local,
five-minute product with no required daemon. Portable Brain and shared record requirements remain
current across both products.

An unlisted feature, platform, payload, connector promise, hosted behavior, or
release-artifact change goes on the expansion backlog. It is not implemented,
advertised, or used to reinterpret a v0 exit gate until the owner accepts a contract
change. An accepted change updates the contract and names the required replacement
evidence before implementation begins.

The concrete backlog is maintained in [`docs/v0-expansion-backlog.md`](v0-expansion-backlog.md).
Anything not named in the approved contract or that backlog receives a new `EXP-*` row before
design or implementation starts.

Phase 0 does not move packages. If a release-boundary rule needs package movement to
be true, record that as Phase 1 work instead of weakening the rule or adding a broad
exception.

## Current namespace import evidence

`docs/v0-package-classification.json` classifies every immediate
`src/open_brain/` package directory and records its target owner, current mixed scope,
and extraction action. `tests/security/test_architecture_imports.py` AST-scans real
Python imports in those directories, verifies all current namespace endpoints have a
classification, and enforces the current safe engine scope: `core` cannot import an
app, connector, hosted, or legacy package.

This narrow scope is intentional evidence, not a claim that the whole current graph
already matches the target architecture. `capture`, `integrations`, `operations`,
`production`, `providers`, `release`, and `storage` remain explicitly mixed; `migrate`
and `parity` remain explicitly legacy. Their recorded Phase 1 extraction work is the
path to broader import enforcement.

The checker is static AST evidence for direct Python imports. Dynamic import targets
remain outside this Phase 0 proof and require review when the relevant package is
extracted.
