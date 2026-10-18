# Current record characterization

Status: Phase 0 characterization evidence.

[`archive/legacy/tests/fixtures/phase0/current_records.json`](../archive/legacy/tests/fixtures/phase0/current_records.json)
records the current typed Python records that carry capture, privacy, provenance, redaction,
event, review, and ledger data. The focused contract test compares every recorded dataclass field
with the live class, so a current-format change cannot pass unnoticed.

These records remain valid current implementation formats. They are explicitly not Portable
Brain v1. Portable v1 has a separate schema and conformance suite because the current records do
not yet carry the complete tenant, actor, role, space, payload-family, or portable-history
contract.

Canonical Markdown and operational SQLite are characterized separately in the same fixture.
Markdown is durable owner-readable data. SQLite is operational state and is never a portable
source of truth.
