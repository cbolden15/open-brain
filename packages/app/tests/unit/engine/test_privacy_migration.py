"""Schema-8 effective-privacy migration: catalog, classification, and backfill."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import DecisionOutcome, ProposalDraft, TextPayload, local_schema
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema import (
    PHASE1_STATE_DATABASE,
    inspect_phase1_state,
    open_local_database,
    open_local_database_read_only,
)
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.privacy_projection import (
    effective_privacy_json,
    project_retained_privacy_evidence,
)
from open_brain_engine.storage.migrations import SchemaError
from open_brain_engine.storage.sqlite import connect_database_read_only

from open_brain.profile import SingleUserLocalProfile, compile_single_user_local

PRIVACY_TABLES = (
    "source_revision_privacy",
    "canonical_revision_privacy",
    "privacy_invalid_evidence",
    "privacy_repair_ledger",
)

ISSUER_TABLES = (
    "brain_identity",
    "legacy_issuer_bindings",
    "issuer_migration_marker",
)

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"


def use_schema_seven_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 7)
    monkeypatch.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:7])


def use_schema_eight_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the schema-8-era runtime so the 7-to-8 coordinator still owns its catalog."""
    from open_brain_engine.engine import privacy_migration

    monkeypatch.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 8)
    monkeypatch.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:8])
    monkeypatch.setattr(privacy_migration, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:8])


def _privacy(
    tier: PrivacyTier,
    *,
    cloud: bool = False,
    external_egress: bool = False,
    confirmation_ref: str | None = None,
) -> PrivacyDecision:
    reasons = {
        PrivacyTier.PUBLIC: PrivacyReason.POLICY_PUBLIC,
        PrivacyTier.WORK: PrivacyReason.POLICY_WORK,
        PrivacyTier.PERSONAL: PrivacyReason.PERSONAL_CONFIRMED,
        PrivacyTier.SECRET: PrivacyReason.SECRET_DETECTED,
    }
    return PrivacyDecision.create(
        tier=tier,
        reason=reasons[tier],
        policy_version="privacy-v1",
        authority=Authority(cloud=cloud, external_egress=external_egress),
        confirmation_ref=confirmation_ref,
    )


def _stored(decision: PrivacyDecision) -> str:
    return json.dumps(decision.to_dict())


class _SyntheticEvidence:
    """A schema-7 Brain carrying every retained-evidence class."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
        with monkeypatch.context() as legacy:
            use_schema_seven_runtime(legacy)
            engine = BrainEngine.open(self.profile)
            space_id = engine.inbox.spaces()[0].space_id
            self.personal = engine.capture.accept(
                TextPayload("synthetic personal evidence"),
                delivery_id="privacy.personal",
                space_id=space_id,
            )
            self.public = engine.capture.accept(
                TextPayload("synthetic public evidence"),
                delivery_id="privacy.public",
                space_id=space_id,
            )
            self.missing = engine.capture.accept(
                TextPayload("synthetic missing evidence"),
                delivery_id="privacy.missing",
                space_id=space_id,
            )
            self.malformed = engine.capture.accept(
                TextPayload("synthetic malformed evidence"),
                delivery_id="privacy.malformed",
                space_id=space_id,
            )
            proposal = engine.review.propose(
                (self.public.capture_id, self.personal.capture_id),
                (ProposalDraft("Synthetic privacy page", "Synthetic privacy body"),),
                delivery_id="privacy.proposal",
            )[0]
            decision = engine.review.decide(
                proposal.proposal_id,
                DecisionOutcome.APPROVED,
                delivery_id="privacy.decision",
                expected_review_digest=proposal.review_digest,
            )
            self.page_id = decision.page_id
            self.lone = engine.capture.accept(
                TextPayload("synthetic lone page evidence"),
                delivery_id="privacy.lone",
                space_id=space_id,
            )
            lone_proposal = engine.review.propose(
                (self.lone.capture_id,),
                (ProposalDraft("Synthetic lone page", "Synthetic lone body"),),
                delivery_id="privacy.lone.proposal",
            )[0]
            lone_decision = engine.review.decide(
                lone_proposal.proposal_id,
                DecisionOutcome.APPROVED,
                delivery_id="privacy.lone.decision",
                expected_review_digest=lone_proposal.review_digest,
            )
            self.lone_page_id = lone_decision.page_id
            connection = open_local_database(self.profile)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE captures SET privacy_json=? WHERE capture_id=?",
                    (
                        _stored(_privacy(PrivacyTier.PUBLIC, cloud=True, external_egress=True)),
                        self.public.capture_id,
                    ),
                )
                connection.execute(
                    "UPDATE captures SET privacy_json=NULL WHERE capture_id=?",
                    (self.missing.capture_id,),
                )
                connection.execute(
                    "UPDATE captures SET privacy_json='not json' WHERE capture_id=?",
                    (self.malformed.capture_id,),
                )
                # Graph mismatch: repoint one page head at an unrelated capture so the
                # current canonical search row no longer resolves its exact source set.
                connection.execute(
                    "UPDATE review_page_heads SET capture_id=? WHERE page_id=?",
                    (self.personal.capture_id, self.lone_page_id),
                )
                connection.execute("COMMIT")
            finally:
                connection.close()


def _read_only(profile: SingleUserLocalProfile) -> sqlite3.Connection:
    return connect_database_read_only(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    )


def test_privacy_migration_is_cataloged() -> None:
    from open_brain_engine.engine import local_schema_catalog

    assert [migration.name for migration in local_schema_catalog.LOCAL_MIGRATIONS][-4:] == [
        "immutable_source_history",
        "effective_privacy_projection",
        "issuer_identity_and_owner_repair",
        "durable_capture_ingestion_journal",
    ]
    assert local_schema_catalog.LOCAL_MIGRATIONS[9].version == 10
    assert len(local_schema_catalog.LOCAL_MIGRATIONS) == 10


def test_fresh_brain_is_schema_ten_current(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    BrainEngine.open(profile)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 10)
    with _read_only(profile) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT singleton, minimum_runtime_session_version, state_schema_version "
                "FROM runtime_compatibility"
            )
        ] == [(1, 5, 10)]
        # Fresh post-cutover state provisions identity atomically with the schema:
        # current epoch one, no legacy epoch, no bindings, and no migration marker.
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT tenant_id, brain_id, issuer_epoch, legacy_issuer_epoch FROM brain_identity"
            )
        ] == [(profile.tenant_id, derive_brain_id(profile.tenant_id), 1, None)]
        for table in ("legacy_issuer_bindings", "issuer_migration_marker"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        for table in PRIVACY_TABLES:
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM engine_generations").fetchone()[0] == 1
        columns = [row[1] for row in connection.execute("PRAGMA table_info(search_documents)")]
        for column in (
            "effective_tier",
            "effective_cloud",
            "effective_external_egress",
            "invalid_evidence_reason",
            "invalid_evidence_sha256",
            "applied_repair_id",
            "applied_repair_sequence",
        ):
            assert column in columns


def test_schema_eight_brain_classifies_supported_old_and_awaits_issuer_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as schema_eight:
        use_schema_eight_runtime(schema_eight)
        engine = BrainEngine.open(profile)
        accepted = engine.capture.accept(TextPayload("schema eight"), delivery_id="issuer.eight")
        assert accepted.capture_id
    assert inspect_phase1_state(profile) == local_schema.SchemaState("supported_old", 8)
    with pytest.raises(SchemaError, match="issuer migration requires exclusive admission"):
        open_local_database(profile, clock=lambda: datetime.now(UTC))
    with pytest.raises(SchemaError):
        open_local_database_read_only(profile)
    with open_local_database_read_only(profile, allow_old=True):
        pass


def test_schema_newer_than_ten_fails_closed(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    BrainEngine.open(profile)
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("PRAGMA user_version=11")
    finally:
        connection.close()
    assert inspect_phase1_state(profile) == local_schema.SchemaState("newer", 11)
    with pytest.raises(SchemaError, match="newer"):
        open_local_database(profile, clock=lambda: datetime.now(UTC))


def test_schema_nine_identity_evidence_is_durable_and_append_only(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    BrainEngine.open(profile)
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Fresh schema-nine state carries its derived identity and no legacy evidence.
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT tenant_id, brain_id, issuer_epoch, legacy_issuer_epoch FROM brain_identity"
            )
        ] == [(profile.tenant_id, derive_brain_id(profile.tenant_id), 1, None)]
        for table in ("legacy_issuer_bindings", "issuer_migration_marker"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        # Identity is singular by construction: a second row is rejected on the
        # singleton primary key, and any non-single row violates the table CHECK.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO brain_identity VALUES "
                f"(1, '{TENANT_ID}', 'brn-two', 2, 1, '2026-09-20T00:00:00Z')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO brain_identity VALUES "
                f"(2, '{TENANT_ID}x', 'brn-one', 1, 1, '2026-09-20T00:00:00Z')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="durable"):
            connection.execute("UPDATE brain_identity SET issuer_epoch = 3")
        with pytest.raises(sqlite3.IntegrityError, match="durable"):
            connection.execute("DELETE FROM brain_identity")
        # The marker retains the exact canonical source-manifest bytes with their
        # digest and binds the cutover epochs; the legacy epoch stays strictly lower.
        manifest_bytes = b'{"contract_version": "4"}'
        connection.execute(
            "INSERT INTO issuer_migration_marker VALUES "
            f"(1, ?, '{'d' * 64}', 'brn-one', 1, 2, '{'e' * 64}', '2026-09-20T00:00:00Z')",
            (manifest_bytes,),
        )
        stored = connection.execute(
            "SELECT source_manifest_bytes, source_manifest_sha256 FROM issuer_migration_marker"
        ).fetchone()
        assert tuple(stored) == (manifest_bytes, "d" * 64)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO issuer_migration_marker VALUES "
                f"(1, ?, '{'d' * 64}', 'brn-one', 2, 2, '{'e' * 64}', '2026-09-20T00:00:00Z')",
                (manifest_bytes,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE issuer_migration_marker SET current_issuer_epoch = 3")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM issuer_migration_marker")
        connection.execute(
            f"INSERT INTO legacy_issuer_bindings VALUES ('brain.toml', NULL, '{'f' * 64}', 1)"
        )
        connection.execute(
            "INSERT INTO legacy_issuer_bindings VALUES "
            f"('sources/logical-sources.jsonl', 0, '{'9' * 64}', 1)"
        )
        # Bindings are unique per artifact path and JSONL ordinal.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"INSERT INTO legacy_issuer_bindings VALUES ('brain.toml', NULL, '{'0' * 64}', 1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO legacy_issuer_bindings VALUES "
                f"('sources/logical-sources.jsonl', 0, '{'1' * 64}', 1)"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE legacy_issuer_bindings SET issuer_epoch = 2")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM legacy_issuer_bindings")
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_schema_nine_repair_ledger_enforces_owner_repair_contract(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    BrainEngine.open(profile)
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(privacy_repair_ledger)")]
        for column in (
            "repair_id",
            "repair_sequence",
            "target_kind",
            "target_id",
            "invalid_evidence_sha256",
            "owner_actor_id",
            "replacement_privacy_json",
            "operation_id",
            "request_sha256",
            "issuer_epoch",
            "receipt_json",
            "recorded_at",
            "supersedes_repair_id",
        ):
            assert column in columns
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO privacy_invalid_evidence VALUES "
            f"('source_revision', 'capture-1', 'missing', '{'a' * 64}')"
        )

        def append_repair(sequence: int, **overrides: object) -> None:
            row: dict[str, object] = {
                "repair_id": f"repair_{sequence}",
                "repair_sequence": sequence,
                "target_kind": "source_revision",
                "target_id": "capture-1",
                "invalid_evidence_sha256": "a" * 64,
                "owner_actor_id": "actor-1",
                "replacement_privacy_json": '{"tier":"work"}',
                "operation_id": f"privacy-repair.{sequence}",
                "request_sha256": "b" * 64,
                "issuer_epoch": 2,
                "receipt_json": '{"status":"committed"}',
                "recorded_at": "2026-09-20T00:00:00Z",
                "supersedes_repair_id": None,
            }
            row.update(overrides)
            marks = ", ".join("?" for _ in row)
            connection.execute(
                f"INSERT INTO privacy_repair_ledger ({', '.join(row)}) VALUES ({marks})",
                tuple(row.values()),
            )

        # Search documents stopped being repair targets in schema nine.
        with pytest.raises(sqlite3.IntegrityError):
            append_repair(11, target_kind="search_document", target_id="result-1")
        # Sequence, issuer epoch, and digests stay positive and exact.
        with pytest.raises(sqlite3.IntegrityError):
            append_repair(0)
        with pytest.raises(sqlite3.IntegrityError):
            append_repair(12, issuer_epoch=0)
        with pytest.raises(sqlite3.IntegrityError):
            append_repair(13, request_sha256="short")
        # The evidence triple must reference an append-only invalid marker.
        with pytest.raises(sqlite3.IntegrityError):
            append_repair(14, invalid_evidence_sha256="c" * 64)
        append_repair(41)
        with pytest.raises(sqlite3.IntegrityError):
            append_repair(42, repair_sequence=41)
        append_repair(43, supersedes_repair_id="repair_41")
        # Supersession never branches and operations never repeat.
        with pytest.raises(sqlite3.IntegrityError):
            append_repair(44, supersedes_repair_id="repair_41")
        with pytest.raises(sqlite3.IntegrityError):
            append_repair(45, operation_id="privacy-repair.43")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE privacy_repair_ledger SET owner_actor_id='actor-2'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM privacy_repair_ledger")
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_schema_nine_search_rows_carry_applied_repair_lineage(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    BrainEngine.open(profile)
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        digest = "a" * 64
        connection.execute(
            "INSERT INTO privacy_invalid_evidence VALUES "
            f"('source_revision', 'capture-1', 'missing', '{digest}')"
        )
        connection.execute(
            "INSERT INTO privacy_repair_ledger (repair_id, repair_sequence, target_kind, "
            "target_id, invalid_evidence_sha256, owner_actor_id, replacement_privacy_json, "
            "operation_id, request_sha256, issuer_epoch, receipt_json, recorded_at, "
            "supersedes_repair_id) VALUES "
            "('repair_r1', 41, 'source_revision', 'capture-1', ?, 'actor-1', "
            "'{\"tier\":\"work\"}', 'privacy-repair.r1', ?, 2, "
            "'{\"status\":\"committed\"}', '2026-09-20T00:00:00Z', NULL)",
            (digest, "b" * 64),
        )

        def insert_search(result_id: str, **overrides: object) -> None:
            row: dict[str, object] = {
                "result_id": result_id,
                "capture_id": "capture-1",
                "record_type": "note",
                "payload_family": "text",
                "space_id": None,
                "title": "synthetic title",
                "body": "synthetic body",
                "trust": "retained",
                "provenance_json": "{}",
                "canonical_path": None,
                "updated_at": "2026-09-20T00:00:00Z",
                "effective_tier": "unknown",
                "effective_cloud": 0,
                "effective_external_egress": 0,
                "invalid_evidence_reason": None,
                "invalid_evidence_sha256": None,
                "applied_repair_id": None,
                "applied_repair_sequence": None,
            }
            row.update(overrides)
            marks = ", ".join("?" for _ in row)
            connection.execute(
                f"INSERT INTO search_documents ({', '.join(row)}) VALUES ({marks})",
                tuple(row.values()),
            )

        # Unrepaired invalid lineage still fails closed to unknown and local-only.
        with pytest.raises(sqlite3.IntegrityError, match="failure closure"):
            insert_search(
                "result-unrepaired",
                invalid_evidence_reason="missing",
                invalid_evidence_sha256=digest,
                effective_tier="work",
            )
        # Applied repair identity and sequence are paired.
        with pytest.raises(sqlite3.IntegrityError, match="repair pairing"):
            insert_search(
                "result-unpaired",
                invalid_evidence_reason="missing",
                invalid_evidence_sha256=digest,
                applied_repair_id="repair_r1",
            )
        # An applied repair requires retained invalid lineage.
        with pytest.raises(sqlite3.IntegrityError, match="repair lineage"):
            insert_search(
                "result-no-lineage",
                applied_repair_id="repair_r1",
                applied_repair_sequence=41,
            )
        # The applied repair must resolve the exact invalid-evidence digest.
        with pytest.raises(sqlite3.IntegrityError, match="repair binding"):
            insert_search(
                "result-wrong-digest",
                invalid_evidence_reason="missing",
                invalid_evidence_sha256="b" * 64,
                applied_repair_id="repair_r1",
                applied_repair_sequence=41,
            )
        # A repaired row keeps its invalid lineage and carries the replacement value.
        insert_search(
            "result-repaired",
            invalid_evidence_reason="missing",
            invalid_evidence_sha256=digest,
            effective_tier="work",
            effective_cloud=1,
            effective_external_egress=1,
            applied_repair_id="repair_r1",
            applied_repair_sequence=41,
        )
        # A repaired secret still stays local-only.
        with pytest.raises(sqlite3.IntegrityError, match="authority"):
            insert_search(
                "result-secret",
                invalid_evidence_reason="missing",
                invalid_evidence_sha256=digest,
                effective_tier="secret",
                effective_cloud=1,
                applied_repair_id="repair_r1",
                applied_repair_sequence=41,
            )
        # Updates obey the same shape.
        with pytest.raises(sqlite3.IntegrityError, match="repair pairing"):
            connection.execute(
                "UPDATE search_documents SET applied_repair_id = NULL WHERE result_id = "
                "'result-repaired'"
            )
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_schema_seven_brain_classifies_supported_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    with monkeypatch.context() as legacy:
        use_schema_seven_runtime(legacy)
        engine = BrainEngine.open(profile)
        assert engine.capture.accept(TextPayload("legacy"), delivery_id="legacy.one").capture_id
    assert inspect_phase1_state(profile) == local_schema.SchemaState("supported_old", 7)
    with pytest.raises(SchemaError, match="privacy migration requires"):
        open_local_database(profile, clock=lambda: datetime.now(UTC))
    with pytest.raises(SchemaError):
        open_local_database_read_only(profile)
    with open_local_database_read_only(profile, allow_old=True):
        pass


def test_repair_ledger_is_append_only(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    BrainEngine.open(profile)
    with _read_only(profile) as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='privacy_repair_ledger'"
            )
        }
        assert triggers == {
            "privacy_repair_ledger_update_immutable",
            "privacy_repair_ledger_delete_immutable",
        }


def test_invalid_evidence_markers_are_append_only(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    BrainEngine.open(profile)
    with _read_only(profile) as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='privacy_invalid_evidence'"
            )
        }
        assert triggers == {
            "privacy_invalid_evidence_update_immutable",
            "privacy_invalid_evidence_delete_immutable",
        }
    # Retained markers are superseded through the repair ledger, never rewritten.
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO privacy_invalid_evidence VALUES ('search_document','probe','missing',?)",
            ("0" * 64,),
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE privacy_invalid_evidence SET invalid_reason='malformed' "
                "WHERE target_id='probe'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM privacy_invalid_evidence WHERE target_id='probe'")
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_privacy_migration_backfills_every_evidence_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import JOURNAL, migrate_privacy
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    with _read_only(profile) as connection:
        retained = {
            row[0]: row[1]
            for row in connection.execute("SELECT capture_id, privacy_json FROM captures")
        }
        members = [
            (row[0], row[1], row[2])
            for row in connection.execute(
                "SELECT revision_id, ordinal, capture_id FROM canonical_revision_members "
                "ORDER BY revision_id, ordinal"
            )
        ]
    public_stored = retained[evidence.public.capture_id]
    personal_stored = retained[evidence.personal.capture_id]
    lone_stored = retained[evidence.lone.capture_id]
    expected_missing = project_retained_privacy_evidence([None])
    expected_malformed = project_retained_privacy_evidence(["not json"])
    expected_page = project_retained_privacy_evidence([public_stored, personal_stored])
    expected_lone = project_retained_privacy_evidence(
        [lone_stored], caller_declared_inconsistent=True
    )

    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))

    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 8)
    assert json.loads((profile.root / JOURNAL).read_bytes())["stage"] == "complete"
    with _read_only(profile) as connection:
        # Immutable source evidence is untouched by the projection.
        assert {
            row[0]: row[1]
            for row in connection.execute("SELECT capture_id, privacy_json FROM captures")
        } == retained
        # Per-source-revision projections cover every revision exactly.
        assert {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT capture_id, effective_privacy_json FROM source_revision_privacy"
            )
        } == {
            capture_id: effective_privacy_json(project_retained_privacy_evidence([value]))
            for capture_id, value in retained.items()
        }
        # Per-canonical-revision projections aggregate each retained member set by ordinal.
        revisions: dict[str, list[str]] = {}
        for revision_id, _ordinal, capture_id in members:
            revisions.setdefault(revision_id, []).append(retained[capture_id])
        assert {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT revision_id, effective_privacy_json FROM canonical_revision_privacy"
            )
        } == {
            revision_id: effective_privacy_json(project_retained_privacy_evidence(values))
            for revision_id, values in revisions.items()
        }
        search = {
            row["result_id"]: row
            for row in connection.execute(
                "SELECT result_id, capture_id, record_type, effective_tier, effective_cloud, "
                "effective_external_egress, invalid_evidence_reason, invalid_evidence_sha256 "
                "FROM search_documents"
            )
        }
        source_rows = {
            row["capture_id"]: row for row in search.values() if row["record_type"] == "source"
        }
        assert source_rows[evidence.personal.capture_id]["effective_tier"] == "personal"
        assert source_rows[evidence.personal.capture_id]["effective_cloud"] == 0
        assert source_rows[evidence.public.capture_id]["effective_tier"] == "public"
        assert source_rows[evidence.public.capture_id]["effective_cloud"] == 1
        assert source_rows[evidence.public.capture_id]["effective_external_egress"] == 1
        assert source_rows[evidence.missing.capture_id]["effective_tier"] == "unknown"
        assert source_rows[evidence.missing.capture_id]["invalid_evidence_reason"] == "missing"
        assert (
            source_rows[evidence.missing.capture_id]["invalid_evidence_sha256"]
            == expected_missing.invalid_evidence_sha256
        )
        assert source_rows[evidence.malformed.capture_id]["effective_tier"] == "unknown"
        assert source_rows[evidence.malformed.capture_id]["invalid_evidence_reason"] == "malformed"
        # Derived canonical page: most restrictive tier plus intersected egress authority.
        page = search[evidence.page_id]
        assert page["record_type"] == "canonical"
        assert page["effective_tier"] == expected_page.tier.value
        assert page["effective_cloud"] == int(expected_page.authority.cloud)
        assert page["effective_external_egress"] == int(expected_page.authority.external_egress)
        assert page["invalid_evidence_reason"] is None
        # Graph mismatch is inconsistent for that row without blocking any other row.
        lone = search[evidence.lone_page_id]
        assert lone["effective_tier"] == "unknown"
        assert lone["invalid_evidence_reason"] == "inconsistent"
        assert lone["invalid_evidence_sha256"] == expected_lone.invalid_evidence_sha256
        # Markers exist exactly for the invalid targets, keyed by target kind and ID.
        markers = {
            (row[0], row[1], row[2], row[3])
            for row in connection.execute(
                "SELECT target_kind, target_id, invalid_reason, invalid_evidence_sha256 "
                "FROM privacy_invalid_evidence"
            )
        }
        assert markers == {
            (
                "source_revision",
                evidence.missing.capture_id,
                "missing",
                expected_missing.invalid_evidence_sha256,
            ),
            (
                "source_revision",
                evidence.malformed.capture_id,
                "malformed",
                expected_malformed.invalid_evidence_sha256,
            ),
            (
                "search_document",
                source_rows[evidence.missing.capture_id]["result_id"],
                "missing",
                expected_missing.invalid_evidence_sha256,
            ),
            (
                "search_document",
                source_rows[evidence.malformed.capture_id]["result_id"],
                "malformed",
                expected_malformed.invalid_evidence_sha256,
            ),
            (
                "search_document",
                evidence.lone_page_id,
                "inconsistent",
                expected_lone.invalid_evidence_sha256,
            ),
        }
        invalid_search = {
            result_id
            for result_id, row in search.items()
            if row["invalid_evidence_reason"] is not None
        }
        assert invalid_search == {
            target_id for kind, target_id, _, _ in markers if kind == "search_document"
        }
        assert connection.execute("SELECT count(*) FROM privacy_repair_ledger").fetchone()[0] == 0


def test_privacy_migration_pending_blocks_ordinary_opener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import JOURNAL, migrate_privacy
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile

    class Crash(RuntimeError):
        pass

    def checkpoint(stage: str) -> None:
        if stage == "schema_committed":
            raise Crash

    with exclusive_runtime_admission(profile) as admission, pytest.raises(Crash):
        migrate_privacy(
            profile, admission=admission, clock=lambda: datetime.now(UTC), checkpoint=checkpoint
        )
    assert json.loads((profile.root / JOURNAL).read_bytes())["stage"] != "complete"
    with pytest.raises(SchemaError, match="privacy migration is pending"):
        open_local_database(profile, clock=lambda: datetime.now(UTC))


@pytest.mark.parametrize(
    "crash_stage",
    ["exclusive_preflight", "journal_durable", "schema_committed", "validated", "complete"],
)
def test_privacy_migration_resumes_from_every_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash_stage: str
) -> None:
    from open_brain_engine.engine.privacy_migration import JOURNAL, migrate_privacy
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile

    class Crash(RuntimeError):
        pass

    def crash(stage: str) -> None:
        if stage == crash_stage:
            raise Crash

    with exclusive_runtime_admission(profile) as admission, pytest.raises(Crash):
        migrate_privacy(
            profile, admission=admission, clock=lambda: datetime.now(UTC), checkpoint=crash
        )

    # A crash at any checkpoint leaves recovery exactly one deterministic path:
    # re-run the same exclusive migration, which must finish without reapplying
    # a committed projection.
    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))

    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 8)
    assert json.loads((profile.root / JOURNAL).read_bytes())["stage"] == "complete"
    with _read_only(profile) as connection:
        assert (
            connection.execute("SELECT count(*) FROM source_revision_privacy").fetchone()[0]
            == (connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0])
        )
        canonical_count = connection.execute(
            "SELECT count(*) FROM canonical_revision_privacy"
        ).fetchone()[0]
        assert (
            canonical_count
            == connection.execute(
                "SELECT count(DISTINCT revision_id) FROM canonical_revision_members"
            ).fetchone()[0]
        )
        assert (
            connection.execute(
                """SELECT 1 FROM search_documents d
                WHERE (d.invalid_evidence_reason IS NOT NULL) != EXISTS (
                    SELECT 1 FROM privacy_invalid_evidence m
                    WHERE m.target_kind = 'search_document' AND m.target_id = d.result_id)
                LIMIT 1"""
            ).fetchone()
            is None
        )
        assert connection.execute("SELECT count(*) FROM privacy_repair_ledger").fetchone()[0] == 0
    # The completed journal unblocks the ordinary opener.
    open_local_database(profile, clock=lambda: datetime.now(UTC)).close()


def test_ordinary_engine_open_does_not_silently_migrate_v7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import JOURNAL

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    with pytest.raises(Exception, match="privacy migration"):
        BrainEngine.open(profile)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("supported_old", 7)
    assert not (profile.root / JOURNAL).exists()


def test_live_writes_keep_revision_privacy_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import migrate_privacy
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))

    # Post-migration live writes through the ordinary engine paths.
    engine = BrainEngine.open(profile)
    space_id = engine.inbox.spaces()[0].space_id
    fresh = engine.capture.accept(
        TextPayload("synthetic post-migration source"),
        delivery_id="privacy.drift.source",
        space_id=space_id,
    )
    proposal = engine.review.propose(
        (fresh.capture_id,),
        (ProposalDraft("Synthetic drift page", "Synthetic drift body"),),
        delivery_id="privacy.drift.proposal",
    )[0]
    decision = engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="privacy.drift.decision",
        expected_review_digest=proposal.review_digest,
    )

    with _read_only(profile) as connection:
        # Kimi P1 probe: revision counts must always equal their projection counts.
        assert (
            connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0]
            == (connection.execute("SELECT count(*) FROM source_revision_privacy").fetchone()[0])
        )
        assert (
            connection.execute(
                "SELECT count(DISTINCT revision_id) FROM canonical_revision_members"
            ).fetchone()[0]
            == connection.execute("SELECT count(*) FROM canonical_revision_privacy").fetchone()[0]
        )
        # Live source rows carry the exact complete effective JSON and lineage the
        # migration backfill encoding produces for the same retained evidence.
        stored = connection.execute(
            "SELECT privacy_json FROM captures WHERE capture_id = ?", (fresh.capture_id,)
        ).fetchone()[0]
        assert connection.execute(
            "SELECT effective_privacy_json FROM source_revision_privacy WHERE capture_id = ?",
            (fresh.capture_id,),
        ).fetchone()[0] == effective_privacy_json(project_retained_privacy_evidence([stored]))
        revision_id = connection.execute(
            "SELECT DISTINCT revision_id FROM canonical_revision_members WHERE page_id = ?",
            (decision.page_id,),
        ).fetchone()[0]
        values = [
            row[0]
            for row in connection.execute(
                "SELECT c.privacy_json FROM canonical_revision_members m "
                "LEFT JOIN captures c ON c.capture_id = m.capture_id "
                "WHERE m.revision_id = ? ORDER BY m.ordinal",
                (revision_id,),
            )
        ]
        assert connection.execute(
            "SELECT effective_privacy_json FROM canonical_revision_privacy WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()[0] == effective_privacy_json(project_retained_privacy_evidence(values))


def test_live_revision_registration_projects_invalid_evidence_with_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import migrate_privacy
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
    from open_brain_engine.engine.source_store import (
        register_completed_captures,
        register_publication_members,
    )

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))

    engine = BrainEngine.open(profile)
    space_id = engine.inbox.spaces()[0].space_id
    fresh = engine.capture.accept(
        TextPayload("synthetic invalid live source"),
        delivery_id="privacy.invalid.source",
        space_id=space_id,
    )
    proposal = engine.review.propose(
        (fresh.capture_id,),
        (ProposalDraft("Synthetic invalid page", "Synthetic invalid body"),),
        delivery_id="privacy.invalid.proposal",
    )[0]
    decision = engine.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="privacy.invalid.decision",
        expected_review_digest=proposal.review_digest,
    )

    # Retained evidence that is invalid at write time must still project through the
    # live registration sweep, fail closed, and append the exact invalid markers.
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE captures SET privacy_json=NULL WHERE capture_id = ?", (fresh.capture_id,)
        )
        connection.execute(
            "DELETE FROM source_revision_privacy WHERE capture_id = ?", (fresh.capture_id,)
        )
        revision_id = connection.execute(
            "SELECT DISTINCT revision_id FROM canonical_revision_members WHERE page_id = ?",
            (decision.page_id,),
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM canonical_revision_privacy WHERE revision_id = ?", (revision_id,)
        )
        register_completed_captures(connection, profile)
        register_publication_members(connection, profile)
        connection.execute("COMMIT")
    finally:
        connection.close()

    expected_missing = project_retained_privacy_evidence([None])
    with _read_only(profile) as connection:
        assert connection.execute(
            "SELECT effective_privacy_json FROM source_revision_privacy WHERE capture_id = ?",
            (fresh.capture_id,),
        ).fetchone()[0] == effective_privacy_json(expected_missing)
        assert connection.execute(
            "SELECT effective_privacy_json FROM canonical_revision_privacy WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()[0] == effective_privacy_json(expected_missing)
        for target_kind, target_id in (
            ("source_revision", fresh.capture_id),
            ("canonical_revision", revision_id),
        ):
            assert tuple(
                connection.execute(
                    "SELECT invalid_reason, invalid_evidence_sha256 "
                    "FROM privacy_invalid_evidence "
                    "WHERE target_kind = ? AND target_id = ?",
                    (target_kind, target_id),
                ).fetchone()
            ) == (
                "missing",
                expected_missing.invalid_evidence_sha256,
            )


def test_live_search_row_transitions_retain_invalid_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import migrate_privacy
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
    from open_brain_engine.engine.search_projection import upsert_search_document

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))

    engine = BrainEngine.open(profile)
    fresh = engine.capture.accept(
        TextPayload("synthetic live search lifecycle"),
        delivery_id="privacy.lifecycle.source",
    )
    with _read_only(profile) as connection:
        stored = connection.execute(
            "SELECT privacy_json FROM captures WHERE capture_id = ?", (fresh.capture_id,)
        ).fetchone()[0]
        title_row = connection.execute(
            "SELECT title, body, payload_family, space_id FROM search_documents "
            "WHERE result_id = ?",
            (fresh.capture_id,),
        ).fetchone()

    def live_upsert(connection: sqlite3.Connection) -> None:
        upsert_search_document(
            connection,
            result_id=fresh.capture_id,
            capture_id=fresh.capture_id,
            record_type="source",
            payload_family=title_row["payload_family"],
            space_id=title_row["space_id"],
            title=title_row["title"],
            body=title_row["body"],
            canonical_path=None,
            updated_at=datetime.now(UTC).isoformat(),
        )

    expected_missing = project_retained_privacy_evidence([None])
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE captures SET privacy_json=NULL WHERE capture_id = ?", (fresh.capture_id,)
        )
        # Live invalid projections append the search-document marker; a repeated
        # projection of the same target keeps one retained marker.
        live_upsert(connection)
        live_upsert(connection)
        assert tuple(
            connection.execute(
                "SELECT invalid_evidence_reason, invalid_evidence_sha256 "
                "FROM search_documents WHERE result_id = ?",
                (fresh.capture_id,),
            ).fetchone()
        ) == ("missing", expected_missing.invalid_evidence_sha256)
        assert (
            connection.execute(
                "SELECT count(*) FROM privacy_invalid_evidence "
                "WHERE target_kind='search_document' AND target_id = ?",
                (fresh.capture_id,),
            ).fetchone()[0]
            == 1
        )
        # A current row that heals keeps its retained invalid-evidence history.
        connection.execute(
            "UPDATE captures SET privacy_json=? WHERE capture_id = ?",
            (stored, fresh.capture_id),
        )
        live_upsert(connection)
        assert tuple(
            connection.execute(
                "SELECT invalid_evidence_reason, invalid_evidence_sha256 "
                "FROM search_documents WHERE result_id = ?",
                (fresh.capture_id,),
            ).fetchone()
        ) == (None, None)
        assert (
            connection.execute(
                "SELECT count(*) FROM privacy_invalid_evidence "
                "WHERE target_kind='search_document' AND target_id = ?",
                (fresh.capture_id,),
            ).fetchone()[0]
            == 1
        )
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_live_search_row_invalid_a_to_b_to_healed_retains_both_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import (
        _verify_privacy_projections,
        migrate_privacy,
    )
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
    from open_brain_engine.engine.search_projection import upsert_search_document

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))
    engine = BrainEngine.open(profile)
    fresh = engine.capture.accept(
        TextPayload("synthetic invalid-a invalid-b healed lifecycle"),
        delivery_id="privacy.abh.source",
    )
    with _read_only(profile) as connection:
        stored = connection.execute(
            "SELECT privacy_json FROM captures WHERE capture_id = ?", (fresh.capture_id,)
        ).fetchone()[0]
        title_row = connection.execute(
            "SELECT title, body, payload_family, space_id FROM search_documents "
            "WHERE result_id = ?",
            (fresh.capture_id,),
        ).fetchone()

    def live_upsert(connection: sqlite3.Connection) -> None:
        upsert_search_document(
            connection,
            result_id=fresh.capture_id,
            capture_id=fresh.capture_id,
            record_type="source",
            payload_family=title_row["payload_family"],
            space_id=title_row["space_id"],
            title=title_row["title"],
            body=title_row["body"],
            canonical_path=None,
            updated_at=datetime.now(UTC).isoformat(),
        )

    def search_markers(connection: sqlite3.Connection) -> set[tuple[str, str]]:
        return {
            (row[0], row[1])
            for row in connection.execute(
                "SELECT invalid_reason, invalid_evidence_sha256 "
                "FROM privacy_invalid_evidence "
                "WHERE target_kind='search_document' AND target_id = ?",
                (fresh.capture_id,),
            )
        }

    expected_missing = project_retained_privacy_evidence([None])
    expected_malformed = project_retained_privacy_evidence(["not json"])

    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Invalid-A: the current row fails closed and appends its marker; repeating
        # the same evidence stays idempotent.
        connection.execute(
            "UPDATE captures SET privacy_json=NULL WHERE capture_id = ?", (fresh.capture_id,)
        )
        live_upsert(connection)
        live_upsert(connection)
        assert tuple(
            connection.execute(
                "SELECT invalid_evidence_reason, invalid_evidence_sha256 "
                "FROM search_documents WHERE result_id = ?",
                (fresh.capture_id,),
            ).fetchone()
        ) == ("missing", expected_missing.invalid_evidence_sha256)
        assert search_markers(connection) == {("missing", expected_missing.invalid_evidence_sha256)}
        # Invalid-B: distinct evidence for the same mutable target must append a
        # second retained marker, not silently keep only the first.
        connection.execute(
            "UPDATE captures SET privacy_json='not json' WHERE capture_id = ?",
            (fresh.capture_id,),
        )
        live_upsert(connection)
        assert tuple(
            connection.execute(
                "SELECT invalid_evidence_reason, invalid_evidence_sha256 "
                "FROM search_documents WHERE result_id = ?",
                (fresh.capture_id,),
            ).fetchone()
        ) == ("malformed", expected_malformed.invalid_evidence_sha256)
        assert search_markers(connection) == {
            ("missing", expected_missing.invalid_evidence_sha256),
            ("malformed", expected_malformed.invalid_evidence_sha256),
        }
        _verify_privacy_projections(connection)
        # Healed: the current row clears while both distinct markers survive.
        connection.execute(
            "UPDATE captures SET privacy_json=? WHERE capture_id = ?",
            (stored, fresh.capture_id),
        )
        live_upsert(connection)
        assert tuple(
            connection.execute(
                "SELECT invalid_evidence_reason, invalid_evidence_sha256 "
                "FROM search_documents WHERE result_id = ?",
                (fresh.capture_id,),
            ).fetchone()
        ) == (None, None)
        assert search_markers(connection) == {
            ("missing", expected_missing.invalid_evidence_sha256),
            ("malformed", expected_malformed.invalid_evidence_sha256),
        }
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_verify_requires_exact_reason_and_digest_marker_for_current_search_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import (
        _verify_privacy_projections,
        migrate_privacy,
    )
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
    from open_brain_engine.engine.t03_contracts import T03Error

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))

    def rejects(statement: str, parameters: tuple[object, ...]) -> None:
        connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(statement, parameters)
            with pytest.raises(T03Error, match="operation_pending"):
                _verify_privacy_projections(connection)
            connection.execute("ROLLBACK")
        finally:
            connection.close()

    # A currently invalid search row whose retained marker disagrees on only the
    # reason, or only the digest, is drift: verification must fail closed.
    rejects(
        "UPDATE search_documents SET invalid_evidence_reason='malformed' WHERE result_id = ?",
        (evidence.missing.capture_id,),
    )
    rejects(
        "UPDATE search_documents SET invalid_evidence_sha256=? WHERE result_id = ?",
        ("e" * 64, evidence.missing.capture_id),
    )


def test_verify_rejects_extra_revision_markers_beyond_the_exact_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import (
        _verify_privacy_projections,
        migrate_privacy,
    )
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
    from open_brain_engine.engine.t03_contracts import T03Error

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))

    # An immutable revision is invalid with exactly one matching marker; a second
    # retained marker digest for the same target is an extra beyond the exact set.
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO privacy_invalid_evidence VALUES ('source_revision',?,'missing',?)",
            (evidence.missing.capture_id, "d" * 64),
        )
        with pytest.raises(T03Error, match="operation_pending"):
            _verify_privacy_projections(connection)
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_stale_complete_journal_resumes_migration_on_restored_v7_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.core.ids import portable_canonical_json_bytes
    from open_brain_engine.engine.privacy_migration import JOURNAL, migrate_privacy
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    # A Brain restored from backup to schema seven while its completed journal
    # survived must resume the migration, not treat the stale journal as proof.
    (profile.root / JOURNAL).write_bytes(
        portable_canonical_json_bytes(
            {
                "version": 1,
                "stage": "complete",
                "root_identity": list(profile.root_identity),
                "old_version": 7,
                "new_version": 8,
            }
        )
    )

    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))

    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 8)
    assert json.loads((profile.root / JOURNAL).read_bytes())["stage"] == "complete"
    with _read_only(profile) as connection:
        assert (
            connection.execute("SELECT count(*) FROM source_revisions").fetchone()[0]
            == (connection.execute("SELECT count(*) FROM source_revision_privacy").fetchone()[0])
        )
        assert (
            connection.execute(
                "SELECT count(DISTINCT revision_id) FROM canonical_revision_members"
            ).fetchone()[0]
            == connection.execute("SELECT count(*) FROM canonical_revision_privacy").fetchone()[0]
        )
    # The completed journal unblocks the ordinary opener.
    open_local_database(profile, clock=lambda: datetime.now(UTC)).close()


def test_verify_privacy_projections_requires_exact_revision_marker_agreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import (
        _verify_privacy_projections,
        migrate_privacy,
    )
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
    from open_brain_engine.engine.t03_contracts import T03Error

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))
    with _read_only(profile) as connection:
        page_revision = connection.execute(
            "SELECT revision_id FROM canonical_revision_members WHERE page_id = ?",
            (evidence.page_id,),
        ).fetchone()[0]
        missing_payload = json.loads(
            connection.execute(
                "SELECT effective_privacy_json FROM source_revision_privacy WHERE capture_id = ?",
                (evidence.missing.capture_id,),
            ).fetchone()[0]
        )
    tampered_reason = {**missing_payload, "invalid_reason": "malformed"}
    # The table CHECK admits any JSON object, so a payload that drops its digest
    # while keeping its reason is storable drift only verification can reject.
    unpaired_payload = {
        key: value for key, value in missing_payload.items() if key != "invalid_evidence_sha256"
    }

    def rejects(statement: str, parameters: tuple[object, ...]) -> None:
        connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(statement, parameters)
            with pytest.raises(T03Error, match="operation_pending"):
                _verify_privacy_projections(connection)
            connection.execute("ROLLBACK")
        finally:
            connection.close()

    # A revision-kind marker on a valid revision fails verification.
    rejects(
        "INSERT INTO privacy_invalid_evidence VALUES ('source_revision',?,'missing',?)",
        (evidence.personal.capture_id, "0" * 64),
    )
    # A canonical-revision marker on a valid revision fails verification.
    rejects(
        "INSERT INTO privacy_invalid_evidence VALUES ('canonical_revision',?,'missing',?)",
        (page_revision, "1" * 64),
    )
    # A revision payload with an unpaired reason and digest fails verification.
    rejects(
        "UPDATE source_revision_privacy SET effective_privacy_json=? WHERE capture_id = ?",
        (json.dumps(unpaired_payload), evidence.missing.capture_id),
    )
    # A well-formed payload whose marker disagrees on the exact reason fails.
    rejects(
        "UPDATE source_revision_privacy SET effective_privacy_json=? WHERE capture_id = ?",
        (json.dumps(tampered_reason), evidence.missing.capture_id),
    )


def test_verify_tolerates_retained_search_markers_for_healed_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import (
        _verify_privacy_projections,
        migrate_privacy,
    )
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))

    # A retained search-document marker for a currently valid row is retained
    # history, not drift: current rows carry current state, markers are append-only.
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO privacy_invalid_evidence VALUES ('search_document',?,'missing',?)",
            (evidence.personal.capture_id, "2" * 64),
        )
        _verify_privacy_projections(connection)
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_new_write_projection_is_byte_identical_to_migration_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import migrate_privacy
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
    from open_brain_engine.engine.search_projection import upsert_search_document

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))

    # A genuinely new capture on the migrated Brain projects through the same
    # retained-evidence path the backfill used.
    engine = BrainEngine.open(profile)
    fresh = engine.capture.accept(
        TextPayload("synthetic post-migration write"), delivery_id="privacy.fresh"
    )
    privacy_columns = (
        "effective_tier, effective_cloud, effective_external_egress, "
        "invalid_evidence_reason, invalid_evidence_sha256"
    )
    with _read_only(profile) as connection:
        stored = connection.execute(
            "SELECT privacy_json FROM captures WHERE capture_id = ?", (fresh.capture_id,)
        ).fetchone()[0]
        expected = project_retained_privacy_evidence([stored])
        row = connection.execute(
            f"SELECT {privacy_columns} FROM search_documents WHERE result_id = ?",
            (fresh.capture_id,),
        ).fetchone()
        assert tuple(row) == (
            expected.tier.value,
            int(expected.authority.cloud),
            int(expected.authority.external_egress),
            None,
            None,
        )
        # Re-running the live upsert over an already-migrated row must reproduce the
        # exact bytes the migration wrote, not a fresh re-derivation drift.
        migrated = connection.execute(
            f"SELECT {privacy_columns} FROM search_documents WHERE result_id = ?",
            (evidence.personal.capture_id,),
        ).fetchone()
        title = connection.execute(
            "SELECT title, body, payload_family, space_id FROM search_documents "
            "WHERE result_id = ?",
            (evidence.personal.capture_id,),
        ).fetchone()
    from open_brain_engine.engine.local_schema import open_local_database as _open

    connection = _open(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        upsert_search_document(
            connection,
            result_id=evidence.personal.capture_id,
            capture_id=evidence.personal.capture_id,
            record_type="source",
            payload_family=title["payload_family"],
            space_id=title["space_id"],
            title=title["title"],
            body=title["body"],
            canonical_path=None,
            updated_at=datetime.now(UTC).isoformat(),
        )
        assert tuple(
            connection.execute(
                f"SELECT {privacy_columns} FROM search_documents WHERE result_id = ?",
                (evidence.personal.capture_id,),
            ).fetchone()
        ) == tuple(migrated)
        connection.execute("ROLLBACK")
    finally:
        connection.close()


class _RepairedSchemaNineBrain:
    """A current schema-nine Brain holding one committed owner repair."""

    def __init__(self, tmp_path: Path) -> None:
        from open_brain_engine.engine.privacy_repairs import PrivacyRepairRequest
        from open_brain_engine.engine.reconciliation import rederive_live_search_projection
        from open_brain_engine.engine.source_store import (
            register_completed_captures,
            register_publication_members,
        )
        from open_brain_engine.engine.t03_contracts import EffectiveAuthority

        self.profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
        engine = BrainEngine.open(self.profile)
        space_id = engine.inbox.spaces()[0].space_id
        self.valid = engine.capture.accept(
            TextPayload("valid evidence"), delivery_id="repair.valid", space_id=space_id
        )
        self.broken = engine.capture.accept(
            TextPayload("evidence that will go missing"),
            delivery_id="repair.broken",
            space_id=space_id,
        )
        proposal = engine.review.propose(
            (self.valid.capture_id, self.broken.capture_id),
            (ProposalDraft("Repair page", "Repair body"),),
            delivery_id="repair.proposal",
        )[0]
        decision = engine.review.decide(
            proposal.proposal_id,
            DecisionOutcome.APPROVED,
            delivery_id="repair.decision",
            expected_review_digest=proposal.review_digest,
        )
        self.page_id = decision.page_id
        connection = open_local_database(self.profile, clock=lambda: datetime.now(UTC))
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE captures SET privacy_json=? WHERE capture_id=?",
                (
                    _stored(
                        _privacy(PrivacyTier.PERSONAL, confirmation_ref="confirmation://repair")
                    ),
                    self.valid.capture_id,
                ),
            )
            connection.execute(
                "UPDATE captures SET privacy_json=NULL WHERE capture_id=?",
                (self.broken.capture_id,),
            )
            connection.execute("DELETE FROM source_revision_privacy")
            connection.execute("DELETE FROM canonical_revision_privacy")
            connection.execute("COMMIT")
        finally:
            connection.close()
        connection = open_local_database(self.profile, clock=lambda: datetime.now(UTC))
        try:
            connection.execute("BEGIN IMMEDIATE")
            register_completed_captures(connection, self.profile)
            register_publication_members(connection, self.profile)
            connection.execute("COMMIT")
        finally:
            connection.close()
        rederive_live_search_projection(engine)
        with _read_only(self.profile) as connection:
            self.broken_digest = connection.execute(
                "SELECT invalid_evidence_sha256 FROM privacy_invalid_evidence "
                "WHERE target_kind='source_revision' AND target_id=?",
                (self.broken.capture_id,),
            ).fetchone()[0]
        repair_task = engine.tasks.privacy_repair
        assert repair_task is not None
        self.receipt = repair_task.repair_privacy(
            PrivacyRepairRequest(
                target_kind="source_revision",
                target_id=self.broken.capture_id,
                invalid_evidence_sha256=self.broken_digest,
                replacement=_privacy(PrivacyTier.WORK, cloud=True, external_egress=True),
                operation_id="privacy-repair.verify",
            ),
            authority=EffectiveAuthority(
                self.profile.owner_actor_id, "session", frozenset(), None, owner=True
            ),
        )


def test_verify_accepts_a_schema_nine_brain_with_committed_repairs(
    tmp_path: Path,
) -> None:
    from open_brain_engine.engine.privacy_migration import _verify_privacy_projections

    brain = _RepairedSchemaNineBrain(tmp_path)
    connection = open_local_database(brain.profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        _verify_privacy_projections(connection)
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_verify_detects_repaired_search_row_effective_value_drift(
    tmp_path: Path,
) -> None:
    from open_brain_engine.engine.privacy_migration import _verify_privacy_projections
    from open_brain_engine.engine.t03_contracts import T03Error

    brain = _RepairedSchemaNineBrain(tmp_path)
    connection = open_local_database(brain.profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        # The repaired row resolves to work with cloud authority; storing the
        # same tier without it is shape-legal drift only verification rejects.
        connection.execute(
            "UPDATE search_documents SET effective_cloud=0 WHERE result_id=?",
            (brain.broken.capture_id,),
        )
        with pytest.raises(T03Error, match="operation_pending"):
            _verify_privacy_projections(connection)
        # Restoring the resolved value must make the same ledger verify cleanly.
        connection.execute(
            "UPDATE search_documents SET effective_cloud=1 WHERE result_id=?",
            (brain.broken.capture_id,),
        )
        _verify_privacy_projections(connection)
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_schema_eight_migration_verifier_still_requires_an_empty_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine.privacy_migration import (
        _verify_privacy_projections,
        migrate_privacy,
    )
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
    from open_brain_engine.engine.t03_contracts import T03Error

    evidence = _SyntheticEvidence(tmp_path, monkeypatch)
    use_schema_eight_runtime(monkeypatch)
    profile = evidence.profile
    with exclusive_runtime_admission(profile) as admission:
        migrate_privacy(profile, admission=admission, clock=lambda: datetime.now(UTC))
    connection = open_local_database(profile, clock=lambda: datetime.now(UTC))
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert connection.execute("SELECT count(*) FROM privacy_repair_ledger").fetchone()[0] == 0
        # The schema-eight ledger shape predates sequences and receipts, but the
        # schema-eight verifier still refuses any ledger row at all.
        connection.execute(
            "INSERT INTO privacy_repair_ledger (repair_id, target_kind, target_id, "
            "invalid_evidence_sha256, owner_actor_id, replacement_privacy_json, "
            "operation_id, recorded_at, supersedes_repair_id) VALUES "
            "('repair_r1', 'source_revision', 'capture-1', ?, 'actor-1', "
            "'{\"tier\":\"work\"}', 'privacy-repair.r1', '2026-01-01T00:00:00Z', NULL)",
            ("3" * 64,),
        )
        with pytest.raises(T03Error, match="operation_pending"):
            _verify_privacy_projections(connection)
        connection.execute("ROLLBACK")
    finally:
        connection.close()
