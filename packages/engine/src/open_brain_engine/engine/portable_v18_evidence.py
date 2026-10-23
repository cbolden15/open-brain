"""Portable18 evidence: exact Portable17 state plus follow-up items, evidence and decisions."""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from types import MappingProxyType

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.portable.followups_v18 import (
    FOLLOWUPS_PATH,
    followups_sidecar,
    validate_followups_evidence,
)

from .contracts import LocalEngineContext
from .followup_contracts import EvidenceRef
from .followup_visibility import evidence_ref, evidence_retracted
from .portable_v5_evidence import PortableV5StateEvidence
from .portable_v17_evidence import serialize_portable_v17_state

_ITEM_COLUMNS = (
    "item_id, kind, project, summary, owner, owner_name, owner_confirmed, due, due_evidence_id, "
    "state, snoozed_until, snooze_return_state, ticket_key, ticket_url, privacy_tier, "
    "superseded_by, resolution, version, created_at, updated_at, evidence_retracted, "
    "new_evidence_since_decision"
)
_EVIDENCE_COLUMNS = (
    "evidence_id, item_id, ordinal, role, capture_id, revision_id, quote_start, quote_end, "
    "quote_sha256, recorded_at"
)
_DECISION_COLUMNS = (
    "decision_id, item_id, sequence, operation_id, request_sha256, principal_id, action, "
    "prior_state, new_state, prior_version, new_version, evidence_ids_json, recorded_at"
)


def _tables_present(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='followup_items'"
        ).fetchone()
        is not None
    )


def followup_portable_rows(
    connection: sqlite3.Connection,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    """Items with retraction applied, evidence positions and hashes, and the decision log."""
    if not _tables_present(connection):
        return [], [], []
    rows = connection.execute(
        f"SELECT {_EVIDENCE_COLUMNS} FROM followup_evidence ORDER BY evidence_id"
    ).fetchall()
    refs: dict[str, list[EvidenceRef]] = {}
    for row in rows:
        refs.setdefault(row["item_id"], []).append(evidence_ref(row))
    evidence: list[dict[str, object]] = [dict(row) for row in rows]
    items: list[dict[str, object]] = []
    for row in connection.execute(f"SELECT {_ITEM_COLUMNS} FROM followup_items ORDER BY item_id"):
        retracted = bool(row["evidence_retracted"]) or evidence_retracted(
            connection, refs.get(row["item_id"], ())
        )
        items.append(
            {
                **dict(row),
                "summary": None if retracted else row["summary"],
                "owner_confirmed": bool(row["owner_confirmed"]),
                "new_evidence_since_decision": bool(row["new_evidence_since_decision"]),
                "evidence_retracted": retracted,
            }
        )
    decisions: list[dict[str, object]] = [
        {
            **{key: value for key, value in dict(row).items() if key != "evidence_ids_json"},
            "evidence_ids": json.loads(row["evidence_ids_json"]),
        }
        for row in connection.execute(
            f"SELECT {_DECISION_COLUMNS} FROM followup_decisions ORDER BY item_id, sequence"
        )
    ]
    return items, evidence, decisions


def serialize_portable_v18_state(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    *,
    relationship_sidecar_present: bool,
) -> PortableV5StateEvidence:
    base = serialize_portable_v17_state(
        connection,
        profile,
        relationship_sidecar_present=relationship_sidecar_present,
        _expected_schema=23,
    )
    raw = followups_sidecar(*followup_portable_rows(connection))
    semantic = {
        **base.semantic_state,
        "followups": validate_followups_evidence({FOLLOWUPS_PATH: raw}),
    }
    return PortableV5StateEvidence(
        MappingProxyType({**base.sidecars, FOLLOWUPS_PATH: raw}),
        MappingProxyType(semantic),
        sha256(canonical(semantic)).hexdigest(),
    )


__all__ = ["followup_portable_rows", "serialize_portable_v18_state"]
