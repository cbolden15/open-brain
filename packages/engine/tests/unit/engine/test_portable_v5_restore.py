"""Clean-stage v5 restoration preserves immutable portable evidence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.engine.local_schema import _prepare_local_schema, classify_local_schema
from open_brain_engine.engine.portable_v5_restore import (
    ValidatedV5IssuerSeed,
    audit_restored_v5,
    restore_portable_v5_root,
)
from open_brain_engine.portable import v5
from open_brain_engine.portable.relationships_v1 import RELATIONSHIP_METADATA_PATH
from open_brain_engine.portable.v4 import SOURCE_METADATA_PATH
from open_brain_engine.portable.versioned import validated_portable_snapshot
from open_brain_engine.storage.sqlite import SchemaError

from packages.engine.tests.contract.test_portable_brain_v5 import _fixture as _contract_fixture
from packages.engine.tests.contract.test_portable_brain_v5 import _write

if TYPE_CHECKING:
    from open_brain_engine.engine.local import BrainEngine

TENANT = "tenant_123e4567-e89b-42d3-a456-426614174000"
NOW = "2026-09-20T12:00:00Z"


def _fixture(mutate: Callable[[BrainEngine], None] | None = None) -> dict[str, bytes]:
    from open_brain_engine.engine import CaptureAction, TextPayload
    from open_brain_engine.engine.local import BrainEngine
    from open_brain_engine.engine.materializer import _profile
    from open_brain_engine.engine.portability_ports import LocalTenantStorage
    from open_brain_engine.engine.portable_v5_evidence import serialize_portable_v5_state
    from open_brain_engine.portable.v1 import PortableSnapshot
    from open_brain_engine.storage.filesystem import capture_root_identity

    with TemporaryDirectory(prefix="v5-synthetic-") as directory:
        root = Path(directory).resolve()
        (root / ".open-brain").mkdir(mode=0o700)
        config = _contract_fixture()["brain.toml"]
        (root / "brain.toml").write_bytes(config)
        profile = _profile(
            root, PortableSnapshot(capture_root_identity(root), {}, {"brain.toml": config})
        )
        engine = BrainEngine.open(profile, clock=lambda: datetime(2026, 9, 20, 12, tzinfo=UTC))
        space = engine.inbox.create_space("Studio", delivery_id="synthetic.space")
        engine.capture.accept(
            TextPayload("Synthetic source one"),
            delivery_id="synthetic.first",
            space_id=space.space_id,
        )
        engine.capture.accept(
            TextPayload("Synthetic canonical source"),
            delivery_id="synthetic.second",
            space_id=space.space_id,
            action=CaptureAction.CANONICAL_NOTE,
        )
        if mutate is not None:
            mutate(engine)
        files = {
            path: payload
            for path, payload in LocalTenantStorage(
                root, profile.tenant_id, profile.root_identity
            ).portable_files()
            if path == "brain.toml" or path.startswith(("content/", "history/", "sources/"))
        }
        with engine._store.connect() as connection:
            evidence = serialize_portable_v5_state(
                connection,
                tenant_id=TENANT,
                relationship_sidecar_present=False,
            )
            files.update(evidence.sidecars)
        return files


def _seed() -> ValidatedV5IssuerSeed:
    return ValidatedV5IssuerSeed(
        migration_bytes=canonical(
            {
                "schema_version": 1,
                "tenant_id": TENANT,
                "brain_id": derive_brain_id(TENANT),
                "current_issuer_epoch": 1,
                "legacy_issuer_epoch": None,
                "identity_recorded_at": NOW,
                "migration_marker": None,
            }
        ),
        bindings_bytes=canonical({"schema_version": 1, "bindings": []}),
    )


def test_imported_issuer_only_seeds_genuinely_empty_schema() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    _prepare_local_schema(connection, True, True, tenant_id=TENANT, issuer_seed=_seed())
    assert classify_local_schema(connection).state == "current"
    assert tuple(connection.execute("SELECT * FROM brain_identity").fetchone()) == (
        1,
        TENANT,
        derive_brain_id(TENANT),
        1,
        None,
        NOW,
    )
    before = list(connection.iterdump())
    for setup_required in (True, False):
        with pytest.raises(SchemaError, match="empty"):
            _prepare_local_schema(
                connection, False, setup_required, tenant_id=TENANT, issuer_seed=_seed()
            )
        assert list(connection.iterdump()) == before
    connection.close()


def test_invalid_issuer_seed_rolls_back_schema_creation() -> None:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    with pytest.raises(SchemaError, match="migration failed"):
        _prepare_local_schema(
            connection,
            True,
            True,
            tenant_id="tenant_wrong",
            issuer_seed=_seed(),
        )
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
    connection.close()


def test_search_restore_uses_captured_markdown_after_stage_mutation(tmp_path: Path) -> None:
    from open_brain_engine.engine.local_schema import open_local_database_read_only
    from open_brain_engine.storage.markdown import parse_markdown, render_markdown

    files = _fixture()
    tmp_path.chmod(0o700)
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)
    page_path = next(path for path in files if "/notes/page_" in path)
    captured_page = parse_markdown(snapshot.files[page_path])
    mutated_bytes = render_markdown(
        fields={**captured_page.fields, "title": "Unvalidated stage title"},
        body="Unvalidated stage body canary\n",
    ).encode()
    (tmp_path / page_path).write_bytes(mutated_bytes)

    result = restore_portable_v5_root(
        tmp_path, snapshot=snapshot, expected_root_identity=snapshot.root_identity
    )

    with open_local_database_read_only(result.profile) as connection:
        row = connection.execute(
            "SELECT title,body FROM search_documents WHERE result_id=?",
            (captured_page.fields["page_id"],),
        ).fetchone()
        assert row is not None
        assert row["title"] == captured_page.fields["title"]
        assert captured_page.body.strip() in row["body"]
        assert "Unvalidated stage" not in row["body"]
    assert snapshot.files[page_path] == files[page_path]
    assert (tmp_path / page_path).read_bytes() == mutated_bytes


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("captures", "search_text", "UNVALIDATED SOURCE TEXT"),
        ("captures", "payload_json", b'{"family":"text","text":"UNVALIDATED PAYLOAD"}'),
        ("captures", "title", "UNVALIDATED SOURCE TITLE"),
        ("captures", "source_reference", "unvalidated-reference"),
        ("search_documents", "title", "UNVALIDATED SEARCH TITLE"),
        ("search_documents", "body", "UNVALIDATED SEARCH TEXT"),
        ("search_documents", "trust", "unverified"),
        ("search_documents", "updated_at", "2026-09-21T12:00:00Z"),
    ],
)
def test_snapshot_audit_rejects_content_drift_without_repair(
    tmp_path: Path,
    table: str,
    column: str,
    value: str | bytes,
) -> None:
    files = _fixture()
    tmp_path.chmod(0o700)
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)
    result = restore_portable_v5_root(
        tmp_path, snapshot=snapshot, expected_root_identity=snapshot.root_identity
    )
    database = tmp_path / ".open-brain/state/phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(f"UPDATE {table} SET {column}=?", (value,))
    with sqlite3.connect(database) as connection:
        before = list(connection.iterdump())
    with pytest.raises(ValueError):
        audit_restored_v5(result.profile, snapshot=snapshot)
    with sqlite3.connect(database) as connection:
        assert list(connection.iterdump()) == before


@pytest.mark.parametrize(
    "table,column", [("captures", "search_text"), ("search_documents", "body")]
)
def test_snapshot_audit_rejects_corruption_before_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    column: str,
) -> None:
    from collections.abc import Mapping

    from open_brain_engine.engine import portable_v5_restore
    from open_brain_engine.engine.contracts import LocalEngineContext

    files = _fixture()
    tmp_path.chmod(0o700)
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)
    original = portable_v5_restore._rederive_search

    def corrupt(
        connection: sqlite3.Connection,
        profile: LocalEngineContext,
        *,
        portable_files: Mapping[str, bytes],
    ) -> None:
        original(connection, profile, portable_files=portable_files)
        connection.execute(f"UPDATE {table} SET {column}='UNVALIDATED BASELINE'")

    monkeypatch.setattr(portable_v5_restore, "_rederive_search", corrupt)
    with pytest.raises(ValueError):
        restore_portable_v5_root(
            tmp_path, snapshot=snapshot, expected_root_identity=snapshot.root_identity
        )
    with sqlite3.connect(tmp_path / ".open-brain/state/phase1.sqlite3") as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM search_documents").fetchone()[0] == 0


def test_snapshot_content_audit_uses_current_source_route(tmp_path: Path) -> None:
    from open_brain_engine.engine.local_schema import open_local_database_read_only

    files = _fixture()
    metadata = json.loads(files[SOURCE_METADATA_PATH])
    source = metadata["sources"][0]
    source["space_id"] = None
    source["route_version"] += 1
    files[SOURCE_METADATA_PATH] = canonical(metadata)
    capture_id = source["head_capture_id"]
    historical = next(
        json.loads(payload)
        for path, payload in files.items()
        if path.startswith("sources/captures/") and json.loads(payload)["capture_id"] == capture_id
    )
    assert historical["space_id"] is not None
    tmp_path.chmod(0o700)
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)
    result = restore_portable_v5_root(
        tmp_path, snapshot=snapshot, expected_root_identity=snapshot.root_identity
    )
    audit_restored_v5(result.profile, snapshot=snapshot)
    with open_local_database_read_only(result.profile) as connection:
        assert (
            connection.execute(
                "SELECT space_id FROM captures WHERE capture_id=?", (capture_id,)
            ).fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                "SELECT space_id FROM search_documents WHERE result_id=?", (capture_id,)
            ).fetchone()[0]
            is None
        )


@pytest.mark.parametrize("relationships", [False, True])
def test_restore_fresh_snapshot_reopen_and_rederive(tmp_path: Path, relationships: bool) -> None:
    from open_brain_engine.engine.local import BrainEngine
    from open_brain_engine.engine.local_schema import open_local_database_read_only
    from open_brain_engine.engine.portable_index import rebuild_portable_index
    from open_brain_engine.engine.portable_v5_evidence import serialize_portable_v5_state
    from open_brain_engine.engine.reconciliation import rederive_live_search_projection

    files = _fixture()
    if relationships:
        files[RELATIONSHIP_METADATA_PATH] = canonical(
            {
                "schema_version": 1,
                "relationships": [],
                "decisions": [],
            }
        )
    tmp_path.chmod(0o700)
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)
    result = restore_portable_v5_root(
        tmp_path, snapshot=snapshot, expected_root_identity=snapshot.root_identity
    )
    with open_local_database_read_only(result.profile) as connection:
        assert (
            connection.execute("SELECT recorded_at FROM brain_identity").fetchone()[0]
            == (json.loads(files[v5.ISSUER_MIGRATION_PATH])["identity_recorded_at"])
        )
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == result.captures
        before = serialize_portable_v5_state(
            connection,
            tenant_id=TENANT,
            relationship_sidecar_present=relationships,
        ).semantic_state_sha256
    engine = BrainEngine.open(result.profile)
    rederive_live_search_projection(engine)
    audit_restored_v5(result.profile, snapshot=snapshot)
    assert (tmp_path / SOURCE_METADATA_PATH).read_bytes() == files[SOURCE_METADATA_PATH]
    assert (tmp_path / RELATIONSHIP_METADATA_PATH).exists() == relationships
    build = rebuild_portable_index(result.profile)
    index_path = tmp_path / ".open-brain/indexes/search.sqlite3"
    with sqlite3.connect(index_path) as index, engine._store.connect() as connection:
        assert index.execute(
            "SELECT result_id FROM search_documents ORDER BY result_id"
        ).fetchall() == [
            tuple(row)
            for row in connection.execute(
                "SELECT result_id FROM search_documents ORDER BY result_id"
            )
        ]
    index_path.unlink()
    rebuilt = rebuild_portable_index(result.profile)
    assert rebuilt.documents == build.documents
    with engine._store.connect() as connection:
        after = serialize_portable_v5_state(
            connection,
            tenant_id=TENANT,
            relationship_sidecar_present=relationships,
        ).semantic_state_sha256
    assert after == before
    with pytest.raises(SchemaError, match="empty"):
        restore_portable_v5_root(
            tmp_path,
            snapshot=snapshot,
            expected_root_identity=snapshot.root_identity,
        )
    audit_restored_v5(result.profile, snapshot=snapshot)


@pytest.mark.parametrize(
    "stage",
    [
        "base_materialized",
        "source_history_restored",
        "privacy_evidence_restored",
        "search_rederived",
    ],
)
def test_restore_transaction_rolls_back_all_base_rows(tmp_path: Path, stage: str) -> None:
    tmp_path.chmod(0o700)
    files = _fixture()
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)

    def fail(current: str) -> None:
        if current == stage:
            raise RuntimeError(stage)

    with pytest.raises(RuntimeError, match=stage):
        restore_portable_v5_root(
            tmp_path,
            snapshot=snapshot,
            expected_root_identity=snapshot.root_identity,
            checkpoint=fail,
        )
    with sqlite3.connect(tmp_path / ".open-brain/state/phase1.sqlite3") as connection:
        for table in (
            "captures",
            "logical_sources",
            "source_revisions",
            "source_revision_privacy",
            "privacy_invalid_evidence",
            "privacy_repair_ledger",
            "search_documents",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert (tmp_path / SOURCE_METADATA_PATH).read_bytes() == files[SOURCE_METADATA_PATH]


@pytest.mark.parametrize("value", [1, 1.25])
def test_schema9_numeric_privacy_affinity_and_atomic_refusal(
    tmp_path: Path, value: int | float
) -> None:
    from open_brain_engine.portable.v1 import PortableSnapshot
    from open_brain_engine.storage.filesystem import capture_root_identity

    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    _prepare_local_schema(connection, True, True, tenant_id=TENANT)
    # The exact canonical captures declaration determines the affinity.
    column = next(
        row for row in connection.execute("PRAGMA table_info(captures)") if row[1] == "privacy_json"
    )
    assert column[2] == "TEXT"
    connection.execute("CREATE TEMP TABLE affinity_probe (privacy_json TEXT)")
    connection.execute("INSERT INTO affinity_probe VALUES (?)", (value,))
    assert (
        connection.execute("SELECT typeof(privacy_json) FROM affinity_probe").fetchone()[0]
        == "text"
    )
    connection.close()
    files = _fixture()
    privacy = json.loads(files[v5.EFFECTIVE_PRIVACY_PATH])
    privacy["retained_privacy"][0]["privacy_json"] = v5.encode_retained_privacy_value(value)
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(privacy)
    snapshot = PortableSnapshot(capture_root_identity(tmp_path), {"schema_version": 5}, files)
    with pytest.raises(ValueError, match="numeric retained privacy.*TEXT affinity"):
        restore_portable_v5_root(
            tmp_path, snapshot=snapshot, expected_root_identity=snapshot.root_identity
        )
    assert not (tmp_path / ".open-brain").exists()


def _retain(engine: BrainEngine, value: str | bytes | None) -> None:
    from open_brain_engine.engine.reconciliation import rederive_live_search_projection

    with engine._store.transaction() as connection:
        connection.execute("UPDATE captures SET privacy_json=?", (value,))
        connection.execute("DELETE FROM source_revision_privacy")
        connection.execute("DELETE FROM canonical_revision_privacy")
    rederive_live_search_projection(engine)


@pytest.mark.parametrize("value", [None, "{broken", ' {"tier":"unknown"} ', b"\x00\xff"])
def test_exact_retained_values_and_markers_survive_reopen(
    tmp_path: Path,
    value: str | bytes | None,
) -> None:
    from open_brain_engine.engine.local_schema import open_local_database_read_only

    files = _fixture(lambda engine: _retain(engine, value))
    tmp_path.chmod(0o700)
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)
    result = restore_portable_v5_root(
        tmp_path,
        snapshot=snapshot,
        expected_root_identity=snapshot.root_identity,
    )
    with open_local_database_read_only(result.profile) as connection:
        rows = connection.execute(
            "SELECT typeof(privacy_json),privacy_json FROM captures"
        ).fetchall()
        for storage_class, actual in rows:
            assert storage_class == (
                "null" if value is None else "blob" if isinstance(value, bytes) else "text"
            )
            assert actual == value
    audit_restored_v5(result.profile, snapshot=snapshot)


def test_repairs_restore_exact_receipts_sparse_sequence_replay_and_append(tmp_path: Path) -> None:
    from open_brain_engine.core.models import PrivacyTier
    from open_brain_engine.engine.local import BrainEngine
    from open_brain_engine.engine.privacy_repairs import PrivacyRepairRequest
    from open_brain_engine.engine.t03_contracts import EffectiveAuthority

    from packages.engine.tests.unit.engine.test_privacy_repairs import _privacy

    def prepare(engine: BrainEngine) -> None:
        _retain(engine, None)
        authority = EffectiveAuthority(
            engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
        )
        with engine._store.connect() as connection:
            target = connection.execute(
                "SELECT capture_id FROM captures WHERE canonical_path IS NOT NULL"
            ).fetchone()[0]
            revision = connection.execute(
                "SELECT revision_id FROM canonical_revision_members"
            ).fetchone()[0]
            digest = connection.execute(
                "SELECT invalid_evidence_sha256 FROM privacy_invalid_evidence "
                "WHERE target_kind='source_revision' AND target_id=?",
                (target,),
            ).fetchone()[0]
        task = engine.tasks.privacy_repair
        assert task is not None
        task.repair_privacy(
            PrivacyRepairRequest(
                "canonical_revision",
                revision,
                digest,
                _privacy(PrivacyTier.PERSONAL),
                "restore.canonical",
            ),
            authority=authority,
        )
        first = task.repair_privacy(
            PrivacyRepairRequest(
                "source_revision",
                target,
                digest,
                _privacy(PrivacyTier.WORK),
                "restore.first",
            ),
            authority=authority,
        )
        task.repair_privacy(
            PrivacyRepairRequest(
                "source_revision",
                target,
                digest,
                _privacy(PrivacyTier.PUBLIC),
                "restore.second",
                first.repair_id,
            ),
            authority=authority,
        )

    files = _fixture(prepare)
    privacy = json.loads(files[v5.EFFECTIVE_PRIVACY_PATH])
    for row in privacy["repairs"]:
        row["repair_sequence"] *= 3
    for row in privacy["resolved_revisions"] + privacy["resolved_search"]:
        if row["applied_repair_sequence"] is not None:
            row["applied_repair_sequence"] *= 3
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(privacy)
    tmp_path.chmod(0o700)
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)
    result = restore_portable_v5_root(
        tmp_path,
        snapshot=snapshot,
        expected_root_identity=snapshot.root_identity,
    )
    engine = BrainEngine.open(result.profile)
    audit_restored_v5(result.profile, snapshot=snapshot)
    final = privacy["repairs"][-1]
    authority = EffectiveAuthority(
        engine.profile.owner_actor_id, "session", frozenset(), None, owner=True
    )
    request = PrivacyRepairRequest(
        final["target_kind"],
        final["target_id"],
        final["invalid_evidence_sha256"],
        final["replacement_privacy"],
        final["operation_id"],
        final["supersedes_repair_id"],
    )
    with engine._store.connect() as connection:
        before = connection.execute(
            "SELECT retrieval_generation FROM engine_generations"
        ).fetchone()[0]
    task = engine.tasks.privacy_repair
    assert task is not None
    assert task.repair_privacy(request, authority=authority).encode() == canonical(final).decode()
    with engine._store.connect() as connection:
        assert (
            connection.execute("SELECT retrieval_generation FROM engine_generations").fetchone()[0]
            == before
        )
    appended = task.repair_privacy(
        PrivacyRepairRequest(
            final["target_kind"],
            final["target_id"],
            final["invalid_evidence_sha256"],
            _privacy(PrivacyTier.WORK),
            "restore.new",
            final["repair_id"],
        ),
        authority=authority,
    )
    assert appended.repair_sequence == final["repair_sequence"] + 1


def test_upgraded_issuer_commitments_and_multirevision_source_restore(tmp_path: Path) -> None:
    import base64
    from hashlib import sha256

    from open_brain_engine.engine.issuer_state import (
        derive_legacy_bindings,
        synthetic_cutover_manifest,
    )
    from open_brain_engine.engine.local import BrainEngine
    from open_brain_engine.engine.reconciliation import rederive_live_search_projection

    files = _fixture()
    metadata = json.loads(files[SOURCE_METADATA_PATH])
    revisions = sorted(metadata["revisions"], key=lambda row: row["capture_id"])
    source = metadata["sources"][0]
    first, head = revisions
    source.update(head_capture_id=head["capture_id"], head_version=2, route_version=7)
    for index, row in enumerate(revisions, 1):
        row.update(
            source_id=source["source_id"],
            sequence=index,
            predecessor_capture_id=None if index == 1 else first["capture_id"],
        )
    metadata.update(sources=[source], revisions=revisions)
    files[SOURCE_METADATA_PATH] = canonical(metadata)
    privacy = json.loads(files[v5.EFFECTIVE_PRIVACY_PATH])
    privacy["resolved_search"] = [
        row for row in privacy["resolved_search"] if row["result_id"] != first["capture_id"]
    ]
    files[v5.EFFECTIVE_PRIVACY_PATH] = canonical(privacy)
    legacy_files = {key: value for key, value in files.items() if key not in v5.V5_SIDECAR_PATHS}
    manifest = canonical(synthetic_cutover_manifest(legacy_files, tenant_id=TENANT))
    bindings = [
        dict(artifact_path=path, jsonl_ordinal=ordinal, payload_sha256=digest, issuer_epoch=1)
        for path, ordinal, digest in derive_legacy_bindings(legacy_files)
    ]
    files[v5.LEGACY_BINDINGS_PATH] = canonical({"schema_version": 1, "bindings": bindings})
    issuer = json.loads(files[v5.ISSUER_MIGRATION_PATH])
    issuer.update(
        current_issuer_epoch=2,
        legacy_issuer_epoch=1,
        migration_marker={
            "source_portable_manifest_bytes_base64": base64.b64encode(manifest).decode(),
            "source_portable_manifest_sha256": sha256(manifest).hexdigest(),
            "brain_id": issuer["brain_id"],
            "designated_legacy_issuer_epoch": 1,
            "current_issuer_epoch": 2,
            "legacy_binding_manifest_sha256": sha256(canonical(bindings)).hexdigest(),
            "recorded_at": NOW,
        },
    )
    files[v5.ISSUER_MIGRATION_PATH] = canonical(issuer)
    tmp_path.chmod(0o700)
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)
    result = restore_portable_v5_root(
        tmp_path,
        snapshot=snapshot,
        expected_root_identity=snapshot.root_identity,
    )
    engine = BrainEngine.open(result.profile)
    rederive_live_search_projection(engine)
    audit_restored_v5(result.profile, snapshot=snapshot)
    with engine._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM logical_sources").fetchone()[0] == 1
        assert connection.execute("SELECT route_version FROM logical_sources").fetchone()[0] == 7
        assert (
            connection.execute(
                "SELECT source_manifest_bytes FROM issuer_migration_marker"
            ).fetchone()[0]
            == manifest
        )


def test_relationship_heads_and_decision_history_restore(tmp_path: Path) -> None:
    files = _fixture()
    metadata = json.loads(files[SOURCE_METADATA_PATH])
    captures = sorted(row["capture_id"] for row in metadata["revisions"])
    relationships = []
    decisions: list[dict[str, object]] = []
    for index, kind in enumerate(("duplicate_of", "contradicts"), 1):
        identity = f"relationship_00000000-0000-4000-8000-{index:012d}"
        relationships.append(
            {
                "relationship_id": identity,
                "left": {"record_id": captures[0], "revision_id": captures[0]},
                "right": {"record_id": captures[1], "revision_id": captures[1]},
                "kind": kind,
                "status": "removed" if index == 1 else "accepted",
                "version": 2 if index == 1 else 1,
            }
        )
        for version, choice in enumerate(("accept", "remove") if index == 1 else ("accept",), 1):
            sequence = len(decisions) + 1
            decisions.append(
                {
                    "decision_id": f"decision_00000000-0000-4000-8000-{sequence:012d}",
                    "relationship_id": identity,
                    "sequence": sequence,
                    "decision": choice,
                    "version": version,
                    "recorded_at": NOW,
                    "actor_id": "actor_123e4567-e89b-42d3-a456-426614174001",
                }
            )
    files[RELATIONSHIP_METADATA_PATH] = canonical(
        {
            "schema_version": 1,
            "relationships": relationships,
            "decisions": decisions,
        }
    )
    tmp_path.chmod(0o700)
    _write(tmp_path, files)
    snapshot = validated_portable_snapshot(tmp_path)
    result = restore_portable_v5_root(
        tmp_path,
        snapshot=snapshot,
        expected_root_identity=snapshot.root_identity,
    )
    audit_restored_v5(result.profile, snapshot=snapshot)
    assert (tmp_path / RELATIONSHIP_METADATA_PATH).read_bytes() == files[RELATIONSHIP_METADATA_PATH]
