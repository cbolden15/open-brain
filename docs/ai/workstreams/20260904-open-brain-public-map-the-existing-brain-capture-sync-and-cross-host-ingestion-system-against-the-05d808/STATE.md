# Workstream State

- ID: `20260904-open-brain-public-map-the-existing-brain-capture-sync-and-cross-host-ingestion-system-against-the-05d808`
- Repo root: `<repo-root>`
- Remote identity SHA-256 fingerprint: `cb5e9cd7ac71c16e5109717b4bc07f01aed1bbda2b18a99bfbc76f7bd98245bc`
- Worktree: `<repo-root>`
- Branch: goal/open-brain-phase4-p4c
- Objective: Map the existing Brain capture, sync, and cross-host ingestion system against the public v0 appliance
- Created date: 2026-09-04

## Active milestone

- Status: complete; discovery, remote verification, synthesis, and independent review passed
- Starting head: `dd1c19f1e2cca4de3da779b5092747ab10d5b192`
- Objective: map existing capture producers, transports, stores, consumers, writer ownership, and
  cross-host synchronization against the public v0 appliance.
- Allowed writes: this workstream's bounded state and handoff only; private worker reports remain
  outside the repository.
- Stop condition: no service, configuration, content, synchronization, credential, publication,
  deployment, or repository source mutation.
- Worker budget: 0 active of 6; 5 total of 12 after the discovery and review waves.

## Safety state

- The public v0 daemon remains isolated outside the synchronized Brain tree.
- The temporary synchronized placement was removed, the daemon was restarted from its original
  root, and the captured test note was verified.
- Syncthing records the former sensitive runtime path as deleted. Bounded checks after peer
  reconnection found no live or versioned remote copy, so no transfer was observed.
- The public repository source remains unchanged; only this bounded workstream packet is present.

## Findings

- The established multi-root Brain and the public one-root v0 appliance are separate running
  systems with no installed or selected adapter between them.
- The established flow uses agent/CLI capture, typed events, project hooks/scanners, remote mobile
  ingress, and direct media summarization with host-specific writer ownership and synchronization.
- The media service currently writes direct notes into the established saved-content tree; its
  older private bridge spool is inactive.
- The retained private canonical writer and remote ingress bridges remain distinct from the public
  v0 daemon.
- The recommended integration is a versioned capture-only bridge with explicit privacy isolation,
  separate canonical-page migration, stable replay identities, and a host-local durable outbox.

## Verification

- Four independent discovery reports were reconciled.
- Metadata-only remote checks verified both infrastructure hosts after connectivity was restored.
- One independent reviewer returned five findings; all five corrections passed its focused recheck.
- `make verify` passed with Ruff, strict MyPy across 549 source files, 3,252 tests, six rebuilt
  distribution artifacts, and artifact-policy verification.
- The detailed map and worker evidence remain private outside the public repository.
