from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import TextPayload
from open_brain_engine.engine.contracts import LocalEngineContext
from open_brain_engine.engine.issuer_state import (
    derive_legacy_bindings,
    legacy_binding_manifest_sha256,
    synthetic_cutover_manifest,
)
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.portable_v5_evidence import (
    normalized_authoritative_search_rows,
    normalized_capture_rows,
    serialize_portable_v5_state,
    verify_portable_v5_semantic_state,
)
from open_brain_engine.engine.privacy_projection import (
    effective_privacy_json,
    project_retained_privacy_evidence,
)
from open_brain_engine.engine.privacy_repairs import (
    PrivacyRepairReceipt,
    PrivacyRepairRequest,
    privacy_repair_request_sha256,
)
from open_brain_engine.portable.v5 import (
    EFFECTIVE_PRIVACY_PATH,
    ISSUER_MIGRATION_PATH,
    LEGACY_BINDINGS_PATH,
)
from open_brain_engine.providers.base import ProviderMode

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA user_version = 9;
        CREATE TABLE captures (
            delivery_id TEXT PRIMARY KEY,
            capture_id TEXT UNIQUE,
            privacy_json TEXT,
            canonical_path TEXT,
            page_id TEXT,
            publication_id TEXT,
            payload_family TEXT,
            payload_json BLOB,
            search_text TEXT,
            title TEXT,
            source_origin TEXT,
            source_reference TEXT,
            provenance_json TEXT,
            actor_id TEXT,
            role_claim_json TEXT,
            space_id TEXT,
            accepted_at TEXT,
            action TEXT,
            publication_path TEXT
        );
        CREATE TABLE logical_sources (
            source_id TEXT PRIMARY KEY, head_capture_id TEXT, historical_only INTEGER,
            space_id TEXT, route_version INTEGER, head_version INTEGER,
            lifecycle TEXT, availability TEXT
        );
        CREATE TABLE source_revisions (
            capture_id TEXT PRIMARY KEY, source_id TEXT, sequence INTEGER,
            predecessor_capture_id TEXT, source_path TEXT, source_sha256 TEXT,
            source_bytes BLOB, recorded_at TEXT, diagnostic TEXT
        );
        CREATE TABLE canonical_revision_members (
            revision_id TEXT, page_id TEXT, publication_id TEXT, ordinal INTEGER,
            capture_id TEXT
        );
        CREATE TABLE source_revision_privacy (
            capture_id TEXT PRIMARY KEY, effective_privacy_json TEXT
        );
        CREATE TABLE canonical_revision_privacy (
            revision_id TEXT PRIMARY KEY, effective_privacy_json TEXT
        );
        CREATE TABLE privacy_invalid_evidence (
            target_kind TEXT, target_id TEXT, invalid_reason TEXT,
            invalid_evidence_sha256 TEXT,
            PRIMARY KEY (target_kind, target_id, invalid_evidence_sha256)
        );
        CREATE TABLE privacy_repair_ledger (
            repair_id TEXT PRIMARY KEY, repair_sequence INTEGER UNIQUE,
            target_kind TEXT, target_id TEXT, invalid_evidence_sha256 TEXT,
            owner_actor_id TEXT, replacement_privacy_json TEXT,
            operation_id TEXT UNIQUE, request_sha256 TEXT, issuer_epoch INTEGER,
            receipt_json TEXT, recorded_at TEXT, supersedes_repair_id TEXT
        );
        CREATE TABLE search_documents (
            result_id TEXT PRIMARY KEY, capture_id TEXT, record_type TEXT,
            payload_family TEXT, space_id TEXT, title TEXT, body TEXT,
            trust TEXT, provenance_json TEXT, canonical_path TEXT, updated_at TEXT,
            effective_tier TEXT, effective_cloud INTEGER,
            effective_external_egress INTEGER, invalid_evidence_reason TEXT,
            invalid_evidence_sha256 TEXT, applied_repair_id TEXT,
            applied_repair_sequence INTEGER
        );
        CREATE TABLE markdown_import_revisions (
            delivery_id TEXT, file_id TEXT, revision_id TEXT, capture_id TEXT
        );
        CREATE TABLE markdown_import_files (
            file_id TEXT, active_revision_id TEXT
        );
        CREATE TABLE review_page_heads (
            page_id TEXT, publication_id TEXT, proposal_id TEXT, capture_id TEXT,
            canonical_path TEXT
        );
        CREATE TABLE decisions (
            canonical_path TEXT, page_id TEXT, publication_id TEXT, proposal_id TEXT,
            publication_path TEXT, outcome TEXT, stage INTEGER
        );
        CREATE TABLE proposals (proposal_id TEXT, capture_id TEXT);
        CREATE TABLE review_sources (
            proposal_id TEXT, capture_id TEXT, ordinal INTEGER
        );
        CREATE TABLE revision_relationships (
            relationship_id TEXT, left_record_id TEXT, left_revision_id TEXT,
            right_record_id TEXT, right_revision_id TEXT, kind TEXT,
            decision TEXT, version INTEGER
        );
        CREATE TABLE relationship_decisions (
            decision_id TEXT, relationship_id TEXT, sequence INTEGER,
            decision TEXT, relationship_version INTEGER, recorded_at TEXT,
            actor_id TEXT
        );
        CREATE TABLE brain_identity (
            singleton INTEGER, tenant_id TEXT, brain_id TEXT, issuer_epoch INTEGER,
            legacy_issuer_epoch INTEGER, recorded_at TEXT
        );
        CREATE TABLE issuer_migration_marker (
            singleton INTEGER, source_manifest_bytes BLOB,
            source_manifest_sha256 TEXT, brain_id TEXT,
            legacy_issuer_epoch INTEGER, current_issuer_epoch INTEGER,
            legacy_binding_manifest_sha256 TEXT, recorded_at TEXT
        );
        CREATE TABLE legacy_issuer_bindings (
            artifact_path TEXT, jsonl_ordinal INTEGER, payload_sha256 TEXT,
            issuer_epoch INTEGER
        );
        """
    )
    connection.execute(
        "INSERT INTO brain_identity VALUES (1,?,?,?,?,?)",
        (
            TENANT_ID,
            derive_brain_id(TENANT_ID),
            1,
            None,
            "2026-09-20T12:00:00.000000Z",
        ),
    )
    return connection


def _decision() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.WORK,
        reason=PrivacyReason.POLICY_WORK,
        policy_version="privacy-v1",
        authority=Authority(cloud=True, external_egress=True),
    )


def _add_source(
    connection: sqlite3.Connection,
    *,
    suffix: str,
    privacy: str | int | bytes | None,
) -> tuple[str, str]:
    capture_id = f"capture_{suffix}"
    source_id = f"source_{suffix}"
    payload = portable_canonical_json_bytes({"capture_id": capture_id})
    digest = sha256(payload).hexdigest()
    connection.execute(
        "INSERT INTO captures (delivery_id,capture_id,privacy_json,canonical_path,page_id,"
        "publication_id,payload_family,payload_json,search_text,title,source_origin,"
        "source_reference,provenance_json,actor_id,role_claim_json,space_id,accepted_at,"
        "action,publication_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"delivery.{suffix}", capture_id, privacy, None, None, None, "text", payload,
            suffix, suffix.title(), "owner", f"synthetic:{suffix}", "{}", "actor_owner",
            "{}", None, "2026-09-20T12:00:00Z", "inbox", None,
        ),
    )
    connection.execute(
        "INSERT INTO source_revisions VALUES (?,?,?,?,?,?,?,?,?)",
        (
            capture_id,
            source_id,
            1,
            None,
            f"sources/captures/{capture_id}.json",
            digest,
            payload,
            "2026-09-20T12:00:00Z",
            None,
        ),
    )
    connection.execute(
        "INSERT INTO logical_sources VALUES (?,?,0,NULL,0,1,'active','available')",
        (source_id, capture_id),
    )
    stored_privacy = connection.execute(
        "SELECT privacy_json FROM captures WHERE capture_id=?", (capture_id,)
    ).fetchone()[0]
    evidence = project_retained_privacy_evidence(stored_privacy)
    connection.execute(
        "INSERT INTO source_revision_privacy VALUES (?,?)",
        (capture_id, effective_privacy_json(evidence)),
    )
    if evidence.invalid_reason is not None:
        connection.execute(
            "INSERT INTO privacy_invalid_evidence VALUES (?,?,?,?)",
            (
                "source_revision",
                capture_id,
                evidence.invalid_reason.value,
                evidence.invalid_evidence_sha256,
            ),
        )
        connection.execute(
            "INSERT INTO privacy_invalid_evidence VALUES (?,?,?,?)",
            (
                "search_document",
                capture_id,
                evidence.invalid_reason.value,
                evidence.invalid_evidence_sha256,
            ),
        )
    connection.execute(
        "INSERT INTO search_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            capture_id,
            capture_id,
            "source",
            "text",
            None,
            suffix.title(),
            suffix,
            "owner",
            portable_canonical_json_bytes({"capture_id": capture_id}).decode("utf-8"),
            None,
            "2026-09-20T12:00:00Z",
            evidence.tier.value,
            int(evidence.authority.cloud),
            int(evidence.authority.external_egress),
            None if evidence.invalid_reason is None else evidence.invalid_reason.value,
            evidence.invalid_evidence_sha256,
            None,
            None,
        ),
    )
    return capture_id, digest


def _add_canonical_head(connection: sqlite3.Connection, *, capture_id: str) -> tuple[str, str]:
    revision_id = "revision_current"
    page_id = "page_current"
    publication_id = "publication_current"
    connection.execute(
        "INSERT INTO canonical_revision_members VALUES (?,?,?,?,?)",
        (revision_id, page_id, publication_id, 0, capture_id),
    )
    privacy = connection.execute(
        "SELECT privacy_json FROM captures WHERE capture_id=?", (capture_id,)
    ).fetchone()[0]
    evidence = project_retained_privacy_evidence((privacy,))
    connection.execute(
        "INSERT INTO canonical_revision_privacy VALUES (?,?)",
        (revision_id, effective_privacy_json(evidence)),
    )
    connection.execute(
        "UPDATE captures SET canonical_path=?, page_id=?, publication_id=? "
        "WHERE capture_id=?",
        (f"content/spaces/notes/{page_id}.md", page_id, publication_id, capture_id),
    )
    connection.execute(
        "INSERT INTO search_documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            page_id,
            capture_id,
            "canonical",
            "text",
            None,
            "Current",
            "current",
            "owner",
            portable_canonical_json_bytes({"capture_id": capture_id}).decode("utf-8"),
            f"content/spaces/notes/{page_id}.md",
            "2026-09-20T12:00:00Z",
            evidence.tier.value,
            int(evidence.authority.cloud),
            int(evidence.authority.external_egress),
            None if evidence.invalid_reason is None else evidence.invalid_reason.value,
            evidence.invalid_evidence_sha256,
            None,
            None,
        ),
    )
    return revision_id, page_id


def test_fresh_state_sidecars_are_deterministic_and_preserve_relationship_presence() -> None:
    connection = _connection()
    stored = portable_canonical_json_bytes(_decision().to_dict()).decode("utf-8")
    text_id, _digest = _add_source(connection, suffix="text", privacy=stored)
    revision_id, page_id = _add_canonical_head(connection, capture_id=text_id)
    null_id, _digest = _add_source(connection, suffix="null", privacy=None)
    blob_id, _digest = _add_source(connection, suffix="blob", privacy=b"\x00\xff")
    connection.commit()
    connection.execute("BEGIN")

    absent = serialize_portable_v5_state(
        connection, tenant_id=TENANT_ID, relationship_sidecar_present=False
    )
    present = serialize_portable_v5_state(
        connection, tenant_id=TENANT_ID, relationship_sidecar_present=True
    )

    assert set(absent.sidecars) == {
        EFFECTIVE_PRIVACY_PATH,
        ISSUER_MIGRATION_PATH,
        LEGACY_BINDINGS_PATH,
    }
    assert absent.sidecars == serialize_portable_v5_state(
        connection, tenant_id=TENANT_ID, relationship_sidecar_present=False
    ).sidecars
    privacy = json.loads(absent.sidecars[EFFECTIVE_PRIVACY_PATH])
    retained = {row["capture_id"]: row["privacy_json"] for row in privacy["retained_privacy"]}
    assert retained[text_id]["storage_class"] == "text"
    assert retained[null_id] == {"storage_class": "null"}
    assert retained[blob_id]["storage_class"] == "blob"
    assert {
        (row["target_kind"], row["target_id"])
        for row in privacy["base_projections"]
    } == {
        ("source_revision", text_id),
        ("source_revision", null_id),
        ("source_revision", blob_id),
        ("canonical_revision", revision_id),
    }
    assert {
        (row["result_id"], row["capture_id"], row["record_type"])
        for row in privacy["resolved_search"]
    } == {
        (text_id, text_id, "source"),
        (null_id, null_id, "source"),
        (blob_id, blob_id, "source"),
        (page_id, text_id, "canonical"),
    }
    assert absent.semantic_state["relationship_evidence_present"] is False
    assert absent.semantic_state["relationship_evidence"] is None
    assert present.semantic_state["relationship_evidence_present"] is True
    assert present.semantic_state["relationship_evidence"] == {
        "schema_version": 1,
        "relationships": [],
        "decisions": [],
    }
    assert present.semantic_state_sha256 != absent.semantic_state_sha256


def test_sparse_repair_sequences_and_drift_verification() -> None:
    connection = _connection()
    capture_id, _digest = _add_source(connection, suffix="repair", privacy=None)
    base = project_retained_privacy_evidence(None)
    assert base.invalid_evidence_sha256 is not None
    previous: str | None = None
    for sequence in (2, 5):
        request = PrivacyRepairRequest(
            target_kind="source_revision",
            target_id=capture_id,
            invalid_evidence_sha256=base.invalid_evidence_sha256,
            replacement=_decision(),
            operation_id=f"repair.operation.{sequence}",
            supersedes_repair_id=previous,
        )
        repair_id = f"repair_{sequence}"
        receipt = PrivacyRepairReceipt(
            repair_id=repair_id,
            repair_sequence=sequence,
            target_kind=request.target_kind,
            target_id=request.target_id,
            invalid_evidence_sha256=request.invalid_evidence_sha256,
            owner_actor_id="actor_owner",
            issuer_epoch=1,
            replacement=_decision(),
            operation_id=request.operation_id,
            request_sha256=privacy_repair_request_sha256(request),
            supersedes_repair_id=previous,
            recorded_at=f"2026-09-20T12:00:0{sequence}.000000Z",
        )
        connection.execute(
            "INSERT INTO privacy_repair_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                repair_id,
                sequence,
                request.target_kind,
                request.target_id,
                request.invalid_evidence_sha256,
                receipt.owner_actor_id,
                portable_canonical_json_bytes(_decision().to_dict()).decode("utf-8"),
                request.operation_id,
                receipt.request_sha256,
                1,
                receipt.encode(),
                receipt.recorded_at,
                previous,
            ),
        )
        previous = repair_id
    connection.execute(
        "UPDATE search_documents SET effective_tier='work', effective_cloud=1, "
        "effective_external_egress=1, applied_repair_id='repair_5', "
        "applied_repair_sequence=5 WHERE result_id=?",
        (capture_id,),
    )
    connection.commit()
    connection.execute("BEGIN")

    state = serialize_portable_v5_state(
        connection, tenant_id=TENANT_ID, relationship_sidecar_present=False
    )
    privacy = json.loads(state.sidecars[EFFECTIVE_PRIVACY_PATH])
    assert [row["repair_sequence"] for row in privacy["repairs"]] == [2, 5]
    assert privacy["repairs"] == [
        json.loads(row[0])
        for row in connection.execute(
            "SELECT receipt_json FROM privacy_repair_ledger ORDER BY repair_sequence"
        )
    ]
    assert verify_portable_v5_semantic_state(
        connection,
        tenant_id=TENANT_ID,
        relationship_sidecar_present=False,
        expected_sha256=state.semantic_state_sha256,
    ) == state
    with pytest.raises(ValueError, match="semantic state mismatch"):
        verify_portable_v5_semantic_state(
            connection,
            tenant_id=TENANT_ID,
            relationship_sidecar_present=False,
            expected_sha256="0" * 64,
        )
    connection.execute(
        "UPDATE source_revision_privacy SET effective_privacy_json='{}' WHERE capture_id=?",
        (capture_id,),
    )
    with pytest.raises(ValueError, match="base privacy projection"):
        verify_portable_v5_semantic_state(
            connection,
            tenant_id=TENANT_ID,
            relationship_sidecar_present=False,
            expected_sha256=state.semantic_state_sha256,
        )


def test_upgraded_issuer_sidecars_preserve_exact_manifest_and_bindings() -> None:
    connection = _connection()
    _add_source(connection, suffix="upgraded", privacy=None)
    historical_files = {
        "brain.toml": b"[brain]\n",
        "sources/captures/history.jsonl": b'{"a":1}\n\n{"b":2}\n',
    }
    manifest_bytes = portable_canonical_json_bytes(
        synthetic_cutover_manifest(historical_files, tenant_id=TENANT_ID)
    )
    bindings = derive_legacy_bindings(historical_files)
    connection.execute("DELETE FROM brain_identity")
    connection.execute(
        "INSERT INTO brain_identity VALUES (1,?,?,?,?,?)",
        (
            TENANT_ID,
            derive_brain_id(TENANT_ID),
            2,
            1,
            "2026-09-20T12:00:00.000000Z",
        ),
    )
    connection.executemany(
        "INSERT INTO legacy_issuer_bindings VALUES (?,?,?,1)", bindings
    )
    connection.execute(
        "INSERT INTO issuer_migration_marker VALUES (1,?,?,?,?,?,?,?)",
        (
            manifest_bytes,
            sha256(manifest_bytes).hexdigest(),
            derive_brain_id(TENANT_ID),
            1,
            2,
            legacy_binding_manifest_sha256(bindings, issuer_epoch=1),
            "2026-09-20T12:00:01.000000Z",
        ),
    )
    connection.commit()
    connection.execute("BEGIN")

    state = serialize_portable_v5_state(
        connection, tenant_id=TENANT_ID, relationship_sidecar_present=False
    )

    issuer = json.loads(state.sidecars[ISSUER_MIGRATION_PATH])
    portable_bindings = json.loads(state.sidecars[LEGACY_BINDINGS_PATH])["bindings"]
    assert issuer["current_issuer_epoch"] == 2
    assert issuer["legacy_issuer_epoch"] == 1
    assert issuer["migration_marker"]["source_portable_manifest_sha256"] == sha256(
        manifest_bytes
    ).hexdigest()
    assert [row["jsonl_ordinal"] for row in portable_bindings] == [None, 0, 2]


def test_requires_active_schema_nine_snapshot_and_rejects_numeric_storage() -> None:
    connection = _connection()
    _add_source(connection, suffix="numeric", privacy=7)  # SQLite stores this as TEXT.
    connection.commit()
    with pytest.raises(ValueError, match="active transaction"):
        serialize_portable_v5_state(
            connection, tenant_id=TENANT_ID, relationship_sidecar_present=False
        )
    connection.execute("BEGIN")
    state = serialize_portable_v5_state(
        connection, tenant_id=TENANT_ID, relationship_sidecar_present=False
    )
    retained = json.loads(state.sidecars[EFFECTIVE_PRIVACY_PATH])["retained_privacy"]
    assert retained[0]["privacy_json"]["storage_class"] == "text"


def test_semantic_projection_commits_capture_and_authoritative_search_content() -> None:
    connection = _connection()
    capture_id, _digest = _add_source(connection, suffix="semantic", privacy=None)
    connection.execute(
        "UPDATE logical_sources SET space_id='space_current' WHERE head_capture_id=?",
        (capture_id,),
    )
    connection.execute(
        "UPDATE captures SET space_id='space_stale' WHERE capture_id=?", (capture_id,)
    )
    connection.execute(
        "UPDATE search_documents SET space_id='space_current' WHERE result_id=?", (capture_id,)
    )
    connection.commit()
    connection.execute("BEGIN")

    initial = serialize_portable_v5_state(
        connection, tenant_id=TENANT_ID, relationship_sidecar_present=False
    )
    capture_rows = normalized_capture_rows(connection)
    search_rows = normalized_authoritative_search_rows(connection)
    assert tuple(capture_rows[0]) == (
        "capture_id",
        "payload_family",
        "payload_json",
        "search_text",
        "title",
        "source_origin",
        "source_reference",
        "provenance_json",
        "actor_id",
        "role_claim_json",
        "space_id",
        "accepted_at",
        "action",
        "canonical_path",
        "page_id",
        "publication_id",
        "publication_path",
    )
    assert capture_rows[0]["payload_json"] == {"capture_id": capture_id}
    assert capture_rows[0]["space_id"] == "space_stale"
    assert search_rows[0]["space_id"] == "space_current"
    assert initial.semantic_state["captures"] == capture_rows
    assert initial.semantic_state["authoritative_search"] == search_rows

    connection.execute(
        "UPDATE search_documents SET space_id='space_stale' WHERE result_id=?", (capture_id,)
    )
    with pytest.raises(ValueError, match="current route mismatch"):
        normalized_authoritative_search_rows(connection)
    connection.execute(
        "UPDATE search_documents SET space_id='space_current' WHERE result_id=?", (capture_id,)
    )

    connection.execute(
        "UPDATE captures SET payload_json=? WHERE capture_id=?",
        (portable_canonical_json_bytes({"capture_id": capture_id, "drift": True}), capture_id),
    )
    capture_drift = serialize_portable_v5_state(
        connection, tenant_id=TENANT_ID, relationship_sidecar_present=False
    )
    assert capture_drift.semantic_state_sha256 != initial.semantic_state_sha256
    with pytest.raises(ValueError, match="semantic state mismatch"):
        verify_portable_v5_semantic_state(
            connection,
            tenant_id=TENANT_ID,
            relationship_sidecar_present=False,
            expected_sha256=initial.semantic_state_sha256,
        )

    connection.execute(
        "UPDATE captures SET payload_json=?, search_text='paired drift' WHERE capture_id=?",
        (portable_canonical_json_bytes({"capture_id": capture_id}), capture_id),
    )
    connection.execute(
        "UPDATE search_documents SET body='paired drift' WHERE result_id=?", (capture_id,)
    )
    paired_drift = serialize_portable_v5_state(
        connection, tenant_id=TENANT_ID, relationship_sidecar_present=False
    )
    assert paired_drift.semantic_state_sha256 != initial.semantic_state_sha256

    connection.execute(
        "UPDATE captures SET search_text='semantic' WHERE capture_id=?", (capture_id,)
    )
    connection.execute(
        "UPDATE search_documents SET body='semantic', title='search drift' WHERE result_id=?",
        (capture_id,),
    )
    search_drift = serialize_portable_v5_state(
        connection, tenant_id=TENANT_ID, relationship_sidecar_present=False
    )
    assert search_drift.semantic_state_sha256 != initial.semantic_state_sha256


def test_serializes_the_real_schema_nine_fresh_brain(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    root.mkdir(mode=0o700)
    (root / ".open-brain").mkdir(mode=0o700)
    profile = LocalEngineContext(
        root=root,
        root_identity=(root.stat().st_dev, root.stat().st_ino),
        tenant_id=TENANT_ID,
        owner_actor_id="actor_00000000-0000-4000-8000-000000000101",
        owner_role_claim={
            "actor_id": "actor_00000000-0000-4000-8000-000000000101",
            "capabilities": ["owner"],
            "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000102",
            "role_id": "role_00000000-0000-4000-8000-000000000103",
            "tenant_id": TENANT_ID,
        },
        provider_mode=ProviderMode.NONE,
        starter_spaces=("Notes",),
    )
    engine = BrainEngine.open(profile)
    receipt = engine.capture.accept(
        TextPayload("portable state"),
        delivery_id="portable-state.real-schema",
        space_id=engine.inbox.spaces()[0].space_id,
    )
    with open_local_database_read_only(profile) as connection:
        if not connection.in_transaction:
            connection.execute("BEGIN")
        state = serialize_portable_v5_state(
            connection, tenant_id=TENANT_ID, relationship_sidecar_present=False
        )
    privacy = json.loads(state.sidecars[EFFECTIVE_PRIVACY_PATH])
    assert [(row["result_id"], row["record_type"]) for row in privacy["resolved_search"]] == [
        (receipt.capture_id, "source")
    ]


@pytest.mark.parametrize("record_type,repaired", [("source", False), ("source", True),
                                                 ("canonical", False)])
@pytest.mark.parametrize("mutation", ["missing", "wrong_reason"])
def test_export_requires_agreeing_current_search_invalid_marker(
    record_type: str, repaired: bool, mutation: str,
) -> None:
    from open_brain_engine.engine.privacy_repairs import _append_repair_row

    connection = _connection()
    capture_id, _digest = _add_source(connection, suffix="invalid-marker", privacy=None)
    result_id = capture_id
    base = project_retained_privacy_evidence(None)
    assert base.invalid_evidence_sha256 is not None
    if record_type == "canonical":
        revision_id, result_id = _add_canonical_head(connection, capture_id=capture_id)
        connection.executemany(
            "INSERT INTO privacy_invalid_evidence VALUES (?,?,?,?)",
            [(kind, target, "missing", base.invalid_evidence_sha256)
             for kind, target in [("canonical_revision", revision_id),
                                  ("search_document", result_id)]],
        )
    if repaired:
        request = PrivacyRepairRequest(
            "source_revision", capture_id, base.invalid_evidence_sha256,
            _decision(), "repair.current-marker")
        receipt = PrivacyRepairReceipt(
            "repair_current", 1, "source_revision", capture_id, base.invalid_evidence_sha256,
            "actor_owner", 1, _decision(), request.operation_id,
            privacy_repair_request_sha256(request), None, "2026-09-20T12:00:00Z")
        _append_repair_row(connection, receipt=receipt)
        connection.execute(
            "UPDATE search_documents SET effective_tier='work', effective_cloud=1, "
            "effective_external_egress=1, applied_repair_id=?, applied_repair_sequence=1 "
            "WHERE result_id=?", (receipt.repair_id, result_id))
    # Preserve a separate older marker while corrupting only the current lineage.
    connection.execute(
        "INSERT INTO privacy_invalid_evidence VALUES ('search_document',?,'malformed',?)",
        (result_id, "0" * 64))
    before = serialize_portable_v5_state(
        connection, tenant_id=TENANT_ID, relationship_sidecar_present=False)
    assert any(row["invalid_evidence_sha256"] == "0" * 64 for row in json.loads(
        before.sidecars[EFFECTIVE_PRIVACY_PATH])["invalid_evidence"])
    if mutation == "missing":
        connection.execute(
            "DELETE FROM privacy_invalid_evidence WHERE target_kind='search_document' "
            "AND target_id=? AND invalid_evidence_sha256=?",
            (result_id, base.invalid_evidence_sha256))
    else:
        connection.execute(
            "UPDATE privacy_invalid_evidence SET invalid_reason='malformed' "
            "WHERE target_kind='search_document' AND target_id=? AND invalid_evidence_sha256=?",
            (result_id, base.invalid_evidence_sha256))
    with pytest.raises(ValueError, match="search.*marker"):
        serialize_portable_v5_state(
            connection, tenant_id=TENANT_ID, relationship_sidecar_present=False)
