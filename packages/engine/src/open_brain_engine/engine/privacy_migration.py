"""Journaled schema-8 privacy migration, called only under runtime writer admission."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import atomic_replace, read_confined
from open_brain_engine.storage.locks import FileLease, LockBusyError
from open_brain_engine.storage.migrations import apply_migrations
from open_brain_engine.storage.sqlite import connect_database

from .contracts import LocalEngineContext
from .local_schema import (
    PHASE1_STATE_DATABASE,
    SchemaState,
    _MigrationClock,
    classify_local_schema,
)
from .local_schema_catalog import LOCAL_MIGRATIONS
from .privacy_repairs import audit_privacy_repair_ledger
from .privacy_store import (
    record_invalid_evidence,
    write_canonical_revision_privacy,
    write_source_revision_privacy,
)
from .runtime_admission import HeldRuntimeAdmission
from .search_projection import project_search_privacy, write_search_privacy
from .t03_contracts import T03Error

JOURNAL = ".open-brain/state/privacy-migration.json"
NEW_STATE_SCHEMA_VERSION = 8
OLD_STATE_SCHEMA_VERSION = 7


def _reached_target(state: SchemaState) -> bool:
    """The privacy phase ends at exactly schema eight, current or supported-old.

    Schema eight is only labeled "current" while it is the newest catalog era;
    once a later era ships, a genuinely schema-eight database classifies as
    supported-old and this phase is still complete.
    """
    return (
        state.state in {"current", "supported_old"}
        and state.version == NEW_STATE_SCHEMA_VERSION
    )


def _read(profile: LocalEngineContext, relative: str) -> bytes | None:
    return read_confined(
        root=profile.root, relative=relative, expected_root_identity=profile.root_identity
    )


def _write(profile: LocalEngineContext, relative: str, payload: bytes) -> None:
    atomic_replace(
        root=profile.root,
        relative=relative,
        data=payload,
        expected_root_identity=profile.root_identity,
    )


def privacy_migration_pending(profile: LocalEngineContext) -> bool:
    payload = _read(profile, JOURNAL)
    if payload is None:
        return False
    try:
        return bool(json.loads(payload)["stage"] != "complete")
    except KeyError, ValueError, TypeError:
        raise T03Error("operation_pending") from None


def migrate_privacy(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _: None,
) -> None:
    admission.validate(profile)
    if admission.live_peer_count:
        raise T03Error("operation_pending")
    lease = FileLease(
        profile.root / ".open-brain",
        "privacy-migration",
        clock=clock,
        parent_root_identity=profile.root_identity,
    )
    try:
        with lease.acquire_shared_writer():
            _migrate_privacy(profile, admission=admission, clock=clock, checkpoint=checkpoint)
    except LockBusyError:
        raise T03Error("operation_pending") from None


def _migrate_privacy(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _: None,
) -> None:
    """Project retained privacy evidence once; never replay a committed projection."""
    admission.validate(profile)
    connection = connect_database(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    )
    try:
        raw = _read(profile, JOURNAL)
        if raw is None:
            state = classify_local_schema(connection)
            if state.state != "supported_old" or state.version != OLD_STATE_SCHEMA_VERSION:
                raise T03Error("operation_pending")
            checkpoint("exclusive_preflight")
            plan = {
                "version": 1,
                "stage": "journal_durable",
                "root_identity": list(profile.root_identity),
                "old_version": OLD_STATE_SCHEMA_VERSION,
                "new_version": NEW_STATE_SCHEMA_VERSION,
            }
            _write(profile, JOURNAL, portable_canonical_json_bytes(plan))
            checkpoint("journal_durable")
        else:
            plan = json.loads(raw)
            if (
                plan.get("version") != 1
                or plan.get("root_identity") != list(profile.root_identity)
                or plan.get("old_version") != OLD_STATE_SCHEMA_VERSION
                or plan.get("new_version") != NEW_STATE_SCHEMA_VERSION
            ):
                raise T03Error("operation_pending")
        state = classify_local_schema(connection)
        # A complete journal is honored only when the schema state itself is already
        # current: a Brain restored to schema seven under a surviving complete
        # journal must resume the migration instead of trusting the stale journal.
        if plan["stage"] == "complete" and _reached_target(state):
            return
        if state.state == "supported_old" and state.version == OLD_STATE_SCHEMA_VERSION:
            admission.validate(profile)
            connection.execute("BEGIN IMMEDIATE")
            apply_migrations(
                connection,
                clock=_MigrationClock(clock),
                migrations=LOCAL_MIGRATIONS[:NEW_STATE_SCHEMA_VERSION],
                schema_version=NEW_STATE_SCHEMA_VERSION,
            )
            _backfill_privacy_projections(connection)
            _verify_privacy_projections(connection)
            connection.execute("COMMIT")
        elif not _reached_target(state):
            # Crash recovery may only resume from the exact committed schema-eight
            # state or the untouched schema-seven state; anything else is pending.
            raise T03Error("operation_pending")
        checkpoint("schema_committed")
        state = classify_local_schema(connection)
        if not _reached_target(state):
            raise T03Error("operation_pending")
        _verify_privacy_projections(connection)
        checkpoint("validated")
        plan["stage"] = "complete"
        _write(profile, JOURNAL, portable_canonical_json_bytes(plan))
        checkpoint("complete")
    except ValueError, KeyError, TypeError, sqlite3.Error, OSError:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise T03Error("operation_pending") from None
    finally:
        connection.close()


def _backfill_privacy_projections(connection: sqlite3.Connection) -> None:
    """Project every retained revision and current search row deterministically."""
    for revision in list(
        connection.execute(
            "SELECT r.capture_id AS capture_id, c.privacy_json AS privacy_json "
            "FROM source_revisions r LEFT JOIN captures c ON c.capture_id = r.capture_id "
            "ORDER BY r.capture_id"
        )
    ):
        write_source_revision_privacy(
            connection,
            capture_id=revision["capture_id"],
            privacy_json=revision["privacy_json"],
        )
    for revision_id in list(
        connection.execute(
            "SELECT DISTINCT revision_id FROM canonical_revision_members ORDER BY revision_id"
        )
    ):
        values = [
            row[0]
            for row in connection.execute(
                "SELECT c.privacy_json FROM canonical_revision_members m "
                "LEFT JOIN captures c ON c.capture_id = m.capture_id "
                "WHERE m.revision_id = ? ORDER BY m.ordinal",
                (revision_id[0],),
            )
        ]
        write_canonical_revision_privacy(
            connection, revision_id=revision_id[0], values=values
        )
    for row in list(
        connection.execute(
            "SELECT result_id, capture_id, record_type FROM search_documents ORDER BY result_id"
        )
    ):
        evidence = project_search_privacy(
            connection,
            result_id=row["result_id"],
            capture_id=row["capture_id"],
            record_type=row["record_type"],
        )
        write_search_privacy(connection, result_id=row["result_id"], evidence=evidence)
        record_invalid_evidence(
            connection,
            target_kind="search_document",
            target_id=row["result_id"],
            evidence=evidence,
        )


def _verify_revision_marker_agreement(
    connection: sqlite3.Connection,
    *,
    table: str,
    key: str,
    target_kind: str,
) -> None:
    """Require exact marker agreement for one immutable revision kind.

    Every effective payload must decode, carry a paired reason and digest, and
    match its ``privacy_invalid_evidence`` markers as an exact set: zero markers
    when valid, exactly one matching marker in both fields when invalid, and no
    extras.
    """
    for row in connection.execute(
        f"SELECT {key} AS target_id, effective_privacy_json FROM {table}"  # noqa: S608
    ):
        try:
            payload = json.loads(row["effective_privacy_json"])
        except ValueError:
            raise T03Error("operation_pending") from None
        if not isinstance(payload, dict):
            raise T03Error("operation_pending")
        reason = payload.get("invalid_reason")
        digest = payload.get("invalid_evidence_sha256")
        if (reason is None) != (digest is None):
            raise T03Error("operation_pending")
        marker_set = {
            (marker[0], marker[1])
            for marker in connection.execute(
                "SELECT invalid_reason, invalid_evidence_sha256 "
                "FROM privacy_invalid_evidence "
                "WHERE target_kind = ? AND target_id = ?",
                (target_kind, row["target_id"]),
            )
        }
        if reason is None:
            if marker_set:
                raise T03Error("operation_pending")
        elif marker_set != {(reason, digest)}:
            raise T03Error("operation_pending")


def _verify_privacy_projections(connection: sqlite3.Connection) -> None:
    """Explicitly recheck coverage, shape, and marker agreement before finishing."""
    if connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0] != (
        connection.execute("SELECT count(*) FROM source_revision_privacy").fetchone()[0]
    ):
        raise T03Error("operation_pending")
    if connection.execute(
        "SELECT count(DISTINCT revision_id) FROM canonical_revision_members"
    ).fetchone()[0] != connection.execute(
        "SELECT count(*) FROM canonical_revision_privacy"
    ).fetchone()[0]:
        raise T03Error("operation_pending")
    _verify_revision_marker_agreement(
        connection, table="source_revision_privacy", key="capture_id",
        target_kind="source_revision",
    )
    _verify_revision_marker_agreement(
        connection, table="canonical_revision_privacy", key="revision_id",
        target_kind="canonical_revision",
    )
    if (
        connection.execute(
            """SELECT 1 FROM search_documents
        WHERE (invalid_evidence_reason IS NULL) != (invalid_evidence_sha256 IS NULL)
        OR effective_tier NOT IN ('public','work','personal','secret','unknown') LIMIT 1"""
        ).fetchone()
        is not None
    ):
        raise T03Error("operation_pending")
    # Current search rows carry current state while markers are append-only history:
    # every invalid current row must hold an exact reason-and-digest marker, while
    # older retained markers on invalid or healed rows are preserved evidence.
    if (
        connection.execute(
            """SELECT 1 FROM search_documents d
        WHERE d.invalid_evidence_reason IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM privacy_invalid_evidence m
            WHERE m.target_kind = 'search_document' AND m.target_id = d.result_id
              AND m.invalid_reason = d.invalid_evidence_reason
              AND m.invalid_evidence_sha256 = d.invalid_evidence_sha256) LIMIT 1"""
        ).fetchone()
        is not None
    ):
        raise T03Error("operation_pending")
    # The repair-aware ledger audit subsumes the empty-ledger rule: below schema
    # nine the migration still begins with no repairs, while a repaired Brain
    # verifies its full ledger, receipt, and projection agreement instead.
    audit_privacy_repair_ledger(connection)
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise T03Error("operation_pending")
