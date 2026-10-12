"""Read-only authorization checks for content released after SDK cleanup.

The caller must hold the canonical writer continuously from checkpoint capture
through validation and content construction. These functions acquire no leases,
initialize no engine, and never reopen the writer lock inode. Application policy
and provider consent remain the caller's responsibility.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .contracts import LocalEngineContext
from .historical_read_snapshot import historical_read_snapshot
from .historical_visibility import read_historical_registry
from .local_schema import open_local_database_read_only
from .paging import authority_binding, generations
from .records import RecordProjector
from .t03_contracts import EffectiveAuthority


@dataclass(frozen=True, slots=True)
class RetrievalReleaseCheckpoint:
    """One exact scoped authority and canonical retrieval state, without bodies."""

    profile: LocalEngineContext
    authority: EffectiveAuthority
    digest: str


def _checkpoint(
    connection: sqlite3.Connection, profile: LocalEngineContext, authority: EffectiveAuthority,
) -> RetrievalReleaseCheckpoint:
    state = {
        "authority": authority_binding(authority),
        # Full counters catch changes during multi-query reads and decision
        # currentness evaluation, including changes outside selected IDs.
        "canonical": generations(connection, profile=profile),
        "scoped": generations(connection, profile=profile, authority=authority),
        "identity": [dict(row) for row in connection.execute("SELECT * FROM brain_identity")],
        # Named spaces participate in the app's scoped authority selection.
        "spaces": [
            dict(row) for row in connection.execute("SELECT * FROM spaces ORDER BY space_id")
        ],
        "historical_registry": sha256(
            read_historical_registry(connection, profile).canonical_bytes()
        ).hexdigest(),
    }
    return RetrievalReleaseCheckpoint(
        profile, authority, sha256(portable_canonical_json_bytes(state)).hexdigest(),
    )


def retrieval_release_checkpoint(
    profile: LocalEngineContext, authority: EffectiveAuthority,
) -> RetrievalReleaseCheckpoint:
    """Bind state before a cached read, under the caller's held writer."""
    authority.require("content-read")
    connection = open_local_database_read_only(profile)
    try:
        with historical_read_snapshot(connection, profile):
            checkpoint = _checkpoint(connection, profile, authority)
        return checkpoint
    finally:
        connection.close()


def validate_retrieval_release(
    profile: LocalEngineContext,
    checkpoint: RetrievalReleaseCheckpoint,
    record_ids: tuple[str, ...],
) -> bool:
    """Recheck complete authority and each exact current source, purely read-only.

    The projector repeats lifecycle, route, head, effective privacy, sharing,
    historical authorization and retained-byte custody checks. A missing/damaged
    source or unavailable read-only state refuses the whole cached response.
    """
    try:
        if checkpoint.profile != profile:
            return False
        connection = open_local_database_read_only(profile)
        try:
            with historical_read_snapshot(connection, profile):
                if _checkpoint(connection, profile, checkpoint.authority) != checkpoint:
                    return False
                projector = RecordProjector(profile, connection, checkpoint.authority)
                for record_id in record_ids:
                    if not record_id.startswith("capture_"):
                        return False
                    projector.source(record_id, expected=record_id)
            return True
        finally:
            connection.close()
    except Exception:
        return False
