"""Engine-owned persistence for effective-privacy projections.

Migration backfill and live writer transactions share these paths so every
retained revision receives byte-identical effective privacy from the same
encoding, and every invalid projection appends an immutable marker.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from .privacy_projection import (
    RepairedPrivacyEvidence,
    RetainedPrivacyEvidence,
    RetainedPrivacyValue,
    effective_privacy_json,
    project_retained_privacy_evidence,
)

EFFECTIVE_PRIVACY_SCHEMA_VERSION = 8
OWNER_REPAIR_SCHEMA_VERSION = 9

__all__ = [
    "EFFECTIVE_PRIVACY_SCHEMA_VERSION",
    "OWNER_REPAIR_SCHEMA_VERSION",
    "effective_privacy_enabled",
    "owner_repair_projection_enabled",
    "record_invalid_evidence",
    "write_canonical_revision_privacy",
    "write_source_revision_privacy",
]


def effective_privacy_enabled(connection: sqlite3.Connection) -> bool:
    """Whether this database carries the schema-eight privacy projection tables."""
    return bool(
        connection.execute("PRAGMA user_version").fetchone()[0]
        >= EFFECTIVE_PRIVACY_SCHEMA_VERSION
    )


def owner_repair_projection_enabled(connection: sqlite3.Connection) -> bool:
    """Whether this database carries the schema-nine repair-aware projections."""
    return bool(
        connection.execute("PRAGMA user_version").fetchone()[0]
        >= OWNER_REPAIR_SCHEMA_VERSION
    )


def record_invalid_evidence(
    connection: sqlite3.Connection,
    *,
    target_kind: str,
    target_id: str,
    evidence: RetainedPrivacyEvidence | RepairedPrivacyEvidence,
) -> None:
    """Append one retained invalid-evidence marker for an invalid projection.

    Markers are retained history, never rewritten: identity is the target plus
    the invalid-evidence digest, which binds the reason, so only an identical
    marker event deduplicates and every distinct invalid transition of a
    mutable search target appends its own retained marker. A repaired evidence
    value retains its original invalid lineage, so its marker is the same.
    """
    if evidence.invalid_reason is None or evidence.invalid_evidence_sha256 is None:
        return
    connection.execute(
        "INSERT OR IGNORE INTO privacy_invalid_evidence VALUES (?,?,?,?)",
        (
            target_kind,
            target_id,
            evidence.invalid_reason.value,
            evidence.invalid_evidence_sha256,
        ),
    )


def write_source_revision_privacy(
    connection: sqlite3.Connection,
    *,
    capture_id: str,
    privacy_json: RetainedPrivacyValue,
) -> None:
    """Persist one immutable source revision's effective privacy from its capture evidence."""
    evidence = project_retained_privacy_evidence([privacy_json])
    connection.execute(
        "INSERT INTO source_revision_privacy VALUES (?,?)",
        (capture_id, effective_privacy_json(evidence)),
    )
    record_invalid_evidence(
        connection, target_kind="source_revision", target_id=capture_id, evidence=evidence
    )


def write_canonical_revision_privacy(
    connection: sqlite3.Connection,
    *,
    revision_id: str,
    values: Iterable[RetainedPrivacyValue],
) -> None:
    """Persist one canonical revision's effective privacy from its retained member set.

    ``values`` must be the retained member evidence in ordinal order so invalid
    digests stay byte-identical between migration backfill and live writes.
    """
    evidence = project_retained_privacy_evidence(tuple(values))
    connection.execute(
        "INSERT INTO canonical_revision_privacy VALUES (?,?)",
        (revision_id, effective_privacy_json(evidence)),
    )
    record_invalid_evidence(
        connection, target_kind="canonical_revision", target_id=revision_id, evidence=evidence
    )
