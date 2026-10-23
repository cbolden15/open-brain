"""Restore Portable18 follow-up items, evidence positions and decisions into an empty store."""

from __future__ import annotations

import json
import sqlite3
from typing import ClassVar

from open_brain_engine.portable.followups_v18 import validate_followups_evidence
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v18_catalog import PORTABLE_V18_SCHEMA_CATALOG_DIGEST

from .contracts import LocalEngineContext
from .portable_v17_restore import V17RestoreBundle

_ITEM_INSERT = (
    "INSERT INTO followup_items (item_id, kind, project, summary, owner, owner_name, "
    "owner_confirmed, due, due_evidence_id, state, snoozed_until, snooze_return_state, "
    "ticket_key, ticket_url, privacy_tier, superseded_by, resolution, version, created_at, "
    "updated_at, evidence_retracted, new_evidence_since_decision) "
    "VALUES (:item_id, :kind, :project, :summary, :owner, :owner_name, :owner_confirmed, "
    ":due, :due_evidence_id, :state, :snoozed_until, :snooze_return_state, :ticket_key, "
    ":ticket_url, :privacy_tier, :superseded_by, :resolution, :version, :created_at, "
    ":updated_at, :evidence_retracted, :new_evidence_since_decision)"
)
_EVIDENCE_INSERT = (
    "INSERT INTO followup_evidence (evidence_id, item_id, ordinal, role, capture_id, "
    "revision_id, quote_start, quote_end, quote_sha256, recorded_at) VALUES (:evidence_id, "
    ":item_id, :ordinal, :role, :capture_id, :revision_id, :quote_start, :quote_end, "
    ":quote_sha256, :recorded_at)"
)
# receipt_json stays NULL on restored rows: replay of a pre-export operation_id returns the
# item's current state (followups._replay), and no withheld summary travels in the archive.
_DECISION_INSERT = (
    "INSERT INTO followup_decisions (decision_id, item_id, sequence, operation_id, "
    "request_sha256, principal_id, action, prior_state, new_state, prior_version, "
    "new_version, evidence_ids_json, recorded_at) VALUES (:decision_id, :item_id, :sequence, "
    ":operation_id, :request_sha256, :principal_id, :action, :prior_state, :new_state, "
    ":prior_version, :new_version, :evidence_ids_json, :recorded_at)"
)


def _supersede_order(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """Items ordered so every ``superseded_by`` target is inserted first (foreign keys on).

    The validator already refused unknown targets and cycles.
    """
    by_id = {row["item_id"]: row for row in items}
    placed: set[object] = set()
    ordered: list[dict[str, object]] = []
    for row in items:
        chain: list[dict[str, object]] = []
        walked: set[object] = set()
        current: dict[str, object] | None = row
        while current is not None and current["item_id"] not in placed:
            if current["item_id"] in walked:
                raise PortableValidationError("restored follow-ups hold a supersede cycle")
            walked.add(current["item_id"])
            chain.append(current)
            target = current["superseded_by"]
            current = None if target is None else by_id[target]
        for entry in reversed(chain):
            placed.add(entry["item_id"])
            ordered.append(entry)
    return ordered


def install_restored_followups(
    connection: sqlite3.Connection, evidence: dict[str, list[dict[str, object]]]
) -> None:
    """Install validated Portable follow-ups; the destination store must be empty."""
    for table in ("followup_items", "followup_evidence", "followup_decisions"):
        if connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            raise PortableValidationError("restored follow-ups must land in an empty store")
    for row in _supersede_order(evidence["items"]):
        connection.execute(
            _ITEM_INSERT,
            {
                **row,
                "owner_confirmed": int(bool(row["owner_confirmed"])),
                "evidence_retracted": int(bool(row["evidence_retracted"])),
                "new_evidence_since_decision": int(bool(row["new_evidence_since_decision"])),
            },
        )
    # Evidence and decisions follow their item rows.
    for row in evidence["evidence"]:
        connection.execute(_EVIDENCE_INSERT, row)
    for row in evidence["decisions"]:
        connection.execute(
            _DECISION_INSERT,
            {
                **{key: value for key, value in row.items() if key != "evidence_ids"},
                "evidence_ids_json": json.dumps(row["evidence_ids"], separators=(",", ":")),
            },
        )


class V18RestoreBundle(V17RestoreBundle):
    portable_version: ClassVar[int] = 18
    catalog_digest: ClassVar[str] = PORTABLE_V18_SCHEMA_CATALOG_DIGEST

    def install(self, connection: sqlite3.Connection, *, profile: LocalEngineContext) -> None:
        # Validate before any inherited destination write.
        evidence = validate_followups_evidence(self.snapshot.files)
        super().install(connection, profile=profile)
        install_restored_followups(connection, evidence)
        self.checkpoint("followups_restored")


__all__ = ["V18RestoreBundle", "install_restored_followups"]
