"""Schema23 follow-up store: items, immutable evidence and an append-only decision log."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Final

from open_brain_engine.storage.migrations import _migration, apply_migrations
from open_brain_engine.storage.sqlite import connect_database

if TYPE_CHECKING:
    from .contracts import LocalEngineContext
    from .runtime_admission import HeldRuntimeAdmission

FOLLOWUP_MAX_ITEMS: Final = 10_000

_STATES = "('suggested','confirmed','snoozed','dismissed','resolved','superseded')"
_DATE = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"

FOLLOWUP_SCHEMA: tuple[str, ...] = (
    f"""CREATE TABLE followup_items (
 item_id TEXT PRIMARY KEY CHECK(length(item_id) BETWEEN 1 AND 128),
 kind TEXT NOT NULL CHECK(kind IN ('commitment','question')),
 project TEXT CHECK(project IS NULL OR length(project) BETWEEN 1 AND 128),
 summary TEXT CHECK(summary IS NULL OR length(summary) BETWEEN 1 AND 280),
 owner TEXT NOT NULL CHECK(owner IN ('self','other','unknown')),
 owner_name TEXT CHECK((owner='other')=(owner_name IS NOT NULL)
   AND (owner_name IS NULL OR length(owner_name) BETWEEN 1 AND 128)),
 owner_confirmed INTEGER NOT NULL CHECK(owner_confirmed IN (0,1)),
 due TEXT CHECK(due IS NULL OR due {_DATE}),
 due_evidence_id TEXT CHECK((due IS NULL)=(due_evidence_id IS NULL)
   AND (due_evidence_id IS NULL OR length(due_evidence_id) BETWEEN 1 AND 128)),
 state TEXT NOT NULL CHECK(state IN {_STATES}),
 snoozed_until TEXT CHECK((state='snoozed')=(snoozed_until IS NOT NULL)
   AND (snoozed_until IS NULL OR snoozed_until {_DATE})),
 snooze_return_state TEXT CHECK((state='snoozed')=(snooze_return_state IS NOT NULL)
   AND (snooze_return_state IS NULL OR snooze_return_state IN ('suggested','confirmed'))),
 ticket_key TEXT CHECK(ticket_key IS NULL OR length(ticket_key) BETWEEN 1 AND 200),
 ticket_url TEXT CHECK(ticket_url IS NULL
   OR (ticket_key IS NOT NULL AND length(ticket_url) BETWEEN 1 AND 2048)),
 privacy_tier TEXT NOT NULL CHECK(privacy_tier IN ('public','work','personal','secret','unknown')),
 superseded_by TEXT REFERENCES followup_items(item_id)
   CHECK((state='superseded')=(superseded_by IS NOT NULL)),
 resolution TEXT CHECK((state='resolved')=(resolution IS NOT NULL)
   AND (resolution IS NULL OR resolution IN ('evidence','owner_statement'))),
 evidence_retracted INTEGER NOT NULL DEFAULT 0 CHECK(evidence_retracted IN (0,1)),
 new_evidence_since_decision INTEGER NOT NULL DEFAULT 0
   CHECK(new_evidence_since_decision IN (0,1)),
 version INTEGER NOT NULL CHECK(version>=1),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 CHECK(summary IS NOT NULL OR evidence_retracted=1)
)""",
    "CREATE INDEX followup_items_state_due_idx ON followup_items(state, due)",
    "CREATE INDEX followup_items_project_idx ON followup_items(project)",
    """CREATE TABLE followup_evidence (
 evidence_id TEXT PRIMARY KEY CHECK(length(evidence_id) BETWEEN 1 AND 128),
 item_id TEXT NOT NULL REFERENCES followup_items(item_id),
 ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 49),
 role TEXT NOT NULL CHECK(role IN ('origin','supporting','correction','resolution')),
 capture_id TEXT NOT NULL CHECK(length(capture_id) BETWEEN 1 AND 128),
 revision_id TEXT NOT NULL CHECK(length(revision_id) BETWEEN 1 AND 128),
 quote_start INTEGER NOT NULL CHECK(quote_start>=0),
 quote_end INTEGER NOT NULL CHECK(quote_end>quote_start),
 quote_sha256 TEXT NOT NULL CHECK(length(quote_sha256)=64),
 recorded_at TEXT NOT NULL,
 UNIQUE(item_id, ordinal)
)""",
    """CREATE UNIQUE INDEX followup_evidence_one_origin_idx
ON followup_evidence(item_id) WHERE role='origin'""",
    """CREATE UNIQUE INDEX followup_evidence_origin_quote_idx
ON followup_evidence(capture_id, revision_id, quote_start, quote_end) WHERE role='origin'""",
    """CREATE INDEX followup_evidence_revision_idx
ON followup_evidence(capture_id, revision_id)""",
    f"""CREATE TABLE followup_decisions (
 decision_id TEXT PRIMARY KEY CHECK(length(decision_id) BETWEEN 1 AND 128),
 item_id TEXT NOT NULL REFERENCES followup_items(item_id),
 sequence INTEGER NOT NULL CHECK(sequence>=1),
 operation_id TEXT NOT NULL UNIQUE CHECK(length(operation_id) BETWEEN 1 AND 128),
 request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
 principal_id TEXT NOT NULL CHECK(length(principal_id) BETWEEN 1 AND 256),
 action TEXT NOT NULL CHECK(action IN ('propose','add_evidence','confirm','dismiss','snooze',
   'correct','link','resolve','reopen','supersede')),
 prior_state TEXT CHECK(prior_state IS NULL OR prior_state IN {_STATES}),
 new_state TEXT NOT NULL CHECK(new_state IN {_STATES}),
 prior_version INTEGER NOT NULL CHECK(prior_version>=0),
 new_version INTEGER NOT NULL CHECK(new_version=prior_version+1),
 evidence_ids_json TEXT NOT NULL,
 receipt_json TEXT,
 recorded_at TEXT NOT NULL,
 UNIQUE(item_id, sequence)
)""",
    f"""CREATE TRIGGER followup_items_capacity BEFORE INSERT ON followup_items
WHEN (SELECT count(*) FROM followup_items) >= {FOLLOWUP_MAX_ITEMS}
BEGIN SELECT RAISE(ABORT, 'follow-up capacity reached'); END""",
    """CREATE TRIGGER followup_items_update_guard BEFORE UPDATE ON followup_items
WHEN NEW.item_id IS NOT OLD.item_id OR NEW.version <> OLD.version+1
 OR NEW.kind IS NOT OLD.kind OR NEW.privacy_tier IS NOT OLD.privacy_tier
 OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'follow-up items move one version at a time'); END""",
    """CREATE TRIGGER followup_items_delete_immutable BEFORE DELETE ON followup_items
BEGIN SELECT RAISE(ABORT, 'follow-up items are retained'); END""",
    """CREATE TRIGGER followup_evidence_update_immutable BEFORE UPDATE ON followup_evidence
BEGIN SELECT RAISE(ABORT, 'follow-up evidence is immutable'); END""",
    """CREATE TRIGGER followup_evidence_delete_immutable BEFORE DELETE ON followup_evidence
BEGIN SELECT RAISE(ABORT, 'follow-up evidence is immutable'); END""",
    """CREATE TRIGGER followup_decisions_update_immutable BEFORE UPDATE ON followup_decisions
BEGIN SELECT RAISE(ABORT, 'follow-up decisions are append-only'); END""",
    """CREATE TRIGGER followup_decisions_delete_immutable BEFORE DELETE ON followup_decisions
BEGIN SELECT RAISE(ABORT, 'follow-up decisions are append-only'); END""",
    "DROP TABLE runtime_compatibility",
    """CREATE TABLE runtime_compatibility (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 minimum_runtime_session_version INTEGER NOT NULL CHECK(minimum_runtime_session_version=16),
 state_schema_version INTEGER NOT NULL CHECK(state_schema_version=23)
)""",
    "INSERT INTO runtime_compatibility VALUES(1,16,23)",
)
FOLLOWUP_MIGRATION = _migration(23, "followup-items", FOLLOWUP_SCHEMA)


def followup_store_is_consistent(connection: sqlite3.Connection) -> bool:
    """Classifier check: one origin row per item and one decision row per version."""
    return (
        connection.execute(
            "SELECT 1 FROM followup_items AS item WHERE item.version <> ("
            " SELECT count(*) FROM followup_decisions AS decision"
            " WHERE decision.item_id=item.item_id"
            ") OR 1 <> ("
            " SELECT count(*) FROM followup_evidence AS evidence"
            " WHERE evidence.item_id=item.item_id AND evidence.role='origin'"
            ") LIMIT 1"
        ).fetchone()
        is None
    )


def migrate_followups(
    profile: LocalEngineContext,
    *,
    admission: HeldRuntimeAdmission,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> None:
    """Exclusively admit schema23 with an empty follow-up store, touching no other rows."""
    from .historical_recovery import require_historical_snapshot_settled
    from .local_schema import (
        PHASE1_STATE_DATABASE,
        LocalSchemaUpgradeCommittedError,
        _MigrationClock,
        _validate_upgrade_data,
        classify_local_schema,
    )
    from .local_schema_catalog import LOCAL_MIGRATIONS
    from .t03_contracts import T03Error

    admission.validate(profile)
    if admission.live_peer_count:
        raise T03Error("operation_pending")
    connection = connect_database(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    )
    committed = False
    try:
        state = classify_local_schema(connection)
        if state.state == "current" and state.version == 23:
            return
        if state.state != "supported_old" or state.version != 22:
            raise T03Error("operation_pending")
        checkpoint("followup_preflight")
        admission.validate(profile)
        connection.execute("BEGIN IMMEDIATE")
        if classify_local_schema(connection) != state:
            raise T03Error("operation_pending")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        apply_migrations(
            connection,
            clock=_MigrationClock(clock),
            migrations=LOCAL_MIGRATIONS[:23],
            schema_version=23,
        )
        upgraded = classify_local_schema(connection)
        if upgraded.state != "current" or upgraded.version != 23:
            raise T03Error("operation_pending")
        checkpoint("followup_before_commit")
        require_historical_snapshot_settled(connection, profile)
        _validate_upgrade_data(connection)
        admission.validate(profile)
        connection.commit()
        committed = True
        checkpoint("followup_after_commit")
    except BaseException as error:
        if connection.in_transaction:
            connection.rollback()
        if committed:
            raise LocalSchemaUpgradeCommittedError("follow-up migration committed") from error
        raise
    finally:
        connection.close()


__all__ = [
    "FOLLOWUP_MAX_ITEMS",
    "FOLLOWUP_MIGRATION",
    "FOLLOWUP_SCHEMA",
    "followup_store_is_consistent",
    "migrate_followups",
]
