"""The explicit engine-owned chained local migration coordinator."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    BrainEngine,
    LocalEngineContext,
    StateSchemaUnavailableError,
    TextPayload,
    local_migration,
    local_schema,
)
from open_brain_engine.engine.lifecycle_migration import migrate_saved_lifecycle
from open_brain_engine.engine.local_schema import inspect_phase1_state
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.privacy_migration import JOURNAL as PRIVACY_JOURNAL
from open_brain_engine.engine.privacy_migration import migrate_privacy
from open_brain_engine.engine.runtime_admission import (
    HeldRuntimeAdmission,
    exclusive_runtime_admission,
    hold_runtime_registry,
)
from open_brain_engine.engine.source_lifecycle_contracts import SourceInspectRequest
from open_brain_engine.engine.source_migration import JOURNAL as SOURCE_JOURNAL
from open_brain_engine.engine.source_migration import migrate_sources
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, RecordReadRequest, T03Error

from open_brain.local_data import select_local_root
from open_brain.profile import compile_single_user_local
from open_brain.services import local_runtime_session
from open_brain.services.local_bootstrap import open_local_brain
from open_brain.services.local_runtime_session import LocalRuntimeCompatibilityError

_STATE_DATABASE = ".open-brain/state/phase1.sqlite3"


def _clock() -> datetime:
    return datetime.now(UTC)


def _user_version(profile_root: Path) -> int:
    with sqlite3.connect(profile_root / _STATE_DATABASE) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def _journal_stage(profile_root: Path, relative: str) -> str | None:
    path = profile_root / relative
    if not path.exists():
        return None
    return str(json.loads(path.read_bytes())["stage"])


def _legacy_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: int
) -> LocalEngineContext:
    """One Brain created and written under a historical schema runtime."""
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    with monkeypatch.context() as legacy:
        legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", version)
        legacy.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:version])
        engine = BrainEngine.open(profile)
        engine.capture.accept(
            TextPayload(f"synthetic schema-{version} evidence"),
            delivery_id=f"legacy.{version}",
            space_id=engine.inbox.spaces()[0].space_id,
        )
    assert inspect_phase1_state(profile) == local_schema.SchemaState("supported_old", version)
    return profile


def _schema_seven_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> LocalEngineContext:
    from packages.app.tests.unit.engine.test_privacy_migration import use_schema_seven_runtime

    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    with monkeypatch.context() as legacy:
        use_schema_seven_runtime(legacy)
        engine = BrainEngine.open(profile)
        engine.capture.accept(
            TextPayload("synthetic schema-7 evidence"),
            delivery_id="legacy.seven",
            space_id=engine.inbox.spaces()[0].space_id,
        )
    assert inspect_phase1_state(profile) == local_schema.SchemaState("supported_old", 7)
    return profile


def _schema_eight_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> LocalEngineContext:
    from packages.app.tests.unit.engine.test_privacy_migration import use_schema_eight_runtime

    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    with monkeypatch.context() as legacy:
        use_schema_eight_runtime(legacy)
        engine = BrainEngine.open(profile)
        engine.capture.accept(
            TextPayload("synthetic schema-8 evidence"),
            delivery_id="legacy.eight",
            space_id=engine.inbox.spaces()[0].space_id,
        )
    assert inspect_phase1_state(profile) == local_schema.SchemaState("supported_old", 8)
    return profile


def _schema_ten_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LocalEngineContext, str, str]:
    """Create real schema-10 evidence, including a historically retired source."""
    from open_brain_engine.engine import paging

    checksums = Path(__file__).parents[5] / "tests/fixtures/local-schema/catalog-checksums.json"
    frozen = json.loads(checksums.read_bytes())
    assert all(migration.checksum == frozen[migration.name] for migration in LOCAL_MIGRATIONS[:10])
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Notes",))
    with monkeypatch.context() as historical:
        historical.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 10)
        historical.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:10])
        historical.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 10)
        historical.setattr(local_runtime_session, "RUNTIME_SESSION_VERSION", 5)
        engine = BrainEngine.open(profile)
        active = engine.capture.accept(
            TextPayload("synthetic schema-ten active evidence 漢字🙂"),
            delivery_id="legacy.ten.active",
            privacy_tier=PrivacyTier.WORK,
        )
        retired = engine.capture.accept(
            TextPayload("synthetic schema-ten retired evidence café Ω"),
            delivery_id="legacy.ten.retired",
        )
        with sqlite3.connect(profile.root / _STATE_DATABASE) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (10,)
            assert connection.execute("SELECT * FROM runtime_compatibility").fetchall() == [
                (1, 5, 10)
            ]
            assert connection.execute(
                "SELECT name FROM sqlite_master WHERE name='source_lifecycle_state'"
            ).fetchall() == []
            source_id = connection.execute(
                "SELECT source_id FROM source_revisions WHERE capture_id=?", (retired.capture_id,)
            ).fetchone()[0]
            # The historical schema retained retired state but had no withdrawal
            # receipt operation. Seed this legacy fixture before its migration.
            connection.execute(
                "UPDATE logical_sources SET lifecycle='retired',availability='missing' "
                "WHERE source_id=?",
                (source_id,),
            )
        # Complete the historical runtime's ordinary sidecar/projection recovery
        # before freezing the fixture's evidence for the schema-11 cutover.
        BrainEngine.open(profile)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("supported_old", 10)
    return profile, active.capture_id, retired.capture_id


def _schema_ten_evidence(profile: LocalEngineContext) -> dict[str, tuple[tuple[object, ...], ...]]:
    tables = (
        "captures",
        "logical_sources",
        "source_revisions",
        "source_aliases",
        "source_namespaces",
        "source_intakes",
        "source_revision_privacy",
        "canonical_revision_privacy",
        "privacy_invalid_evidence",
        "privacy_repair_ledger",
        "brain_identity",
        "legacy_issuer_bindings",
        "issuer_migration_marker",
    )
    with sqlite3.connect(profile.root / _STATE_DATABASE) as connection:
        evidence = {
            table: tuple(
                tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            )
            for table in tables
        }
        evidence["schema_migrations"] = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM schema_migrations WHERE version<=10 ORDER BY version"
            )
        )
    assert len(evidence["captures"]) == len(evidence["source_revisions"]) == 2
    assert len(evidence["source_revision_privacy"]) == 2
    assert len(evidence["brain_identity"]) == 1
    assert len(evidence["schema_migrations"]) == 10
    return evidence


def _vault_files(profile: LocalEngineContext) -> dict[str, bytes]:
    """Independently enumerate the confined cutover inventory the contract names."""
    root = Path(profile.root)
    files: dict[str, bytes] = {}
    if (root / "brain.toml").is_file():
        files["brain.toml"] = (root / "brain.toml").read_bytes()
    for prefix in ("content", "history", "sources"):
        for path in sorted((root / prefix).rglob("*")):
            if path.is_file():
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _issuer_state(profile: LocalEngineContext) -> tuple[object, ...]:
    with sqlite3.connect(Path(profile.root) / _STATE_DATABASE) as connection:
        marker = connection.execute(
            "SELECT source_manifest_bytes, source_manifest_sha256, brain_id, "
            "legacy_issuer_epoch, current_issuer_epoch, legacy_binding_manifest_sha256 "
            "FROM issuer_migration_marker"
        ).fetchone()
        bindings = tuple(
            connection.execute(
                "SELECT artifact_path, jsonl_ordinal, payload_sha256, issuer_epoch "
                "FROM legacy_issuer_bindings ORDER BY artifact_path, jsonl_ordinal"
            )
        )
    return marker, bindings


@contextmanager
def _counting_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[LocalEngineContext]]:
    """Count coordinator admission acquisitions while delegating to the real proof."""
    acquisitions: list[LocalEngineContext] = []
    real = exclusive_runtime_admission

    @contextmanager
    def counting(profile: LocalEngineContext) -> Iterator[object]:
        acquisitions.append(profile)
        with real(profile) as proof:
            yield proof

    monkeypatch.setattr(local_migration, "exclusive_runtime_admission", counting)
    yield acquisitions


def test_coordinator_is_a_no_op_for_absent_and_current_state(tmp_path: Path) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    absent = compile_single_user_local(tmp_path / "absent")
    coordinate(absent)
    assert not (absent.root / _STATE_DATABASE).exists()
    assert not (absent.root / ".open-brain/runtime-sessions").exists()

    current = compile_single_user_local(tmp_path / "current")
    BrainEngine.open(current)
    coordinate(current)
    assert inspect_phase1_state(current) == local_schema.SchemaState("current", 11)
    assert _journal_stage(current.root, SOURCE_JOURNAL) is None
    assert _journal_stage(current.root, PRIVACY_JOURNAL) is None


def test_coordinator_chains_schema_five_and_six_to_current_eleven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    for version in (5, 6):
        profile = _legacy_brain(tmp_path / f"v{version}", monkeypatch, version)
        coordinate(profile, clock=_clock)
        assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 11)
        assert _journal_stage(profile.root, SOURCE_JOURNAL) == "complete"
        assert _journal_stage(profile.root, PRIVACY_JOURNAL) == "complete"
        # The ordinary opener works unchanged once the chain has ended at current.
        reopened = BrainEngine.open(profile)
        assert reopened.retrieval.search("synthetic")[0].result_id


def test_coordinator_migrates_schema_seven_to_eleven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    profile = _schema_seven_brain(tmp_path, monkeypatch)
    coordinate(profile, clock=_clock)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 11)
    assert _journal_stage(profile.root, SOURCE_JOURNAL) is None
    assert _journal_stage(profile.root, PRIVACY_JOURNAL) == "complete"


def test_coordinator_resumes_a_pending_source_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    profile = _legacy_brain(tmp_path, monkeypatch, 6)

    class Crash(RuntimeError):
        pass

    def checkpoint(stage: str) -> None:
        if stage == "sidecars_durable":
            raise Crash

    with exclusive_runtime_admission(profile) as admission, pytest.raises(Crash):
        migrate_sources(profile, admission=admission, clock=_clock, checkpoint=checkpoint)
    assert _journal_stage(profile.root, SOURCE_JOURNAL) == "journal_durable"

    coordinate(profile, clock=_clock)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 11)
    assert _journal_stage(profile.root, SOURCE_JOURNAL) == "complete"
    assert _journal_stage(profile.root, PRIVACY_JOURNAL) == "complete"


def test_coordinator_resumes_a_pending_privacy_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    profile = _schema_seven_brain(tmp_path, monkeypatch)

    class Crash(RuntimeError):
        pass

    def checkpoint(stage: str) -> None:
        if stage == "schema_committed":
            raise Crash

    with exclusive_runtime_admission(profile) as admission, pytest.raises(Crash):
        migrate_privacy(profile, admission=admission, clock=_clock, checkpoint=checkpoint)
    assert _journal_stage(profile.root, PRIVACY_JOURNAL) != "complete"

    coordinate(profile, clock=_clock)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 11)
    assert _journal_stage(profile.root, PRIVACY_JOURNAL) == "complete"


def test_coordinator_resumes_after_a_crash_between_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    profile = _legacy_brain(tmp_path, monkeypatch, 6)

    class Crash(RuntimeError):
        pass

    fired: list[str] = []

    def checkpoint(stage: str) -> None:
        # The source migration reaches "complete" exactly once, before the
        # privacy phase starts; crashing there leaves the chain half-finished.
        if stage == "complete" and "source" not in fired:
            fired.append("source")
            raise Crash

    with pytest.raises(Crash):
        coordinate(profile, clock=_clock, checkpoint=checkpoint)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("supported_old", 7)
    assert _journal_stage(profile.root, SOURCE_JOURNAL) == "complete"
    assert _journal_stage(profile.root, PRIVACY_JOURNAL) is None

    coordinate(profile, clock=_clock)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 11)
    assert _journal_stage(profile.root, SOURCE_JOURNAL) == "complete"
    assert _journal_stage(profile.root, PRIVACY_JOURNAL) == "complete"


def test_coordinator_refuses_invalid_and_newer_state(tmp_path: Path) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    newer = compile_single_user_local(tmp_path / "newer")
    BrainEngine.open(newer)
    with sqlite3.connect(newer.root / _STATE_DATABASE) as connection:
        connection.execute("PRAGMA user_version=12")
    assert inspect_phase1_state(newer).state == "newer"
    with pytest.raises(StateSchemaUnavailableError, match="newer"):
        coordinate(newer)

    invalid = compile_single_user_local(tmp_path / "invalid")
    BrainEngine.open(invalid)
    with sqlite3.connect(invalid.root / _STATE_DATABASE) as connection:
        connection.execute("DELETE FROM schema_migrations WHERE version=9")
    assert inspect_phase1_state(invalid).state == "invalid"
    with pytest.raises(StateSchemaUnavailableError, match="invalid"):
        coordinate(invalid)


def test_one_exclusive_admission_spans_every_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    profile = _legacy_brain(tmp_path, monkeypatch, 6)
    with (
        _counting_admission(monkeypatch) as acquisitions,
        exclusive_runtime_admission(profile),
    ):
        coordinate(profile, clock=_clock)
    # The chain reused the already-live root-bound proof and acquired once.
    assert len(acquisitions) == 1
    assert acquisitions[0] is profile
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 11)


def test_product_bootstrap_migrates_old_state_only_after_peers_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selection = select_local_root(
        data_dir=str(tmp_path / "brain"),
        environment={"HOME": str(tmp_path)},
        platform_name="darwin",
    )
    probe = lambda _path, platform_name: "apfs" if platform_name == "darwin" else "ext4"  # noqa: E731

    profile = compile_single_user_local(selection.brain_root, starter_spaces=("Notes",))
    with monkeypatch.context() as legacy:
        legacy.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 5)
        legacy.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:5])
        engine = BrainEngine.open(profile)
        engine.capture.accept(
            TextPayload("synthetic bootstrap evidence"),
            delivery_id="bootstrap.five",
            space_id=engine.inbox.spaces()[0].space_id,
        )
    assert inspect_phase1_state(profile).state == "supported_old"

    # Materialize the registry, then hold one live peer session marker.
    with exclusive_runtime_admission(profile):
        pass
    registry = profile.root / ".open-brain/runtime-sessions/registry.lock"
    peer = registry.parent / "session-11111111111111111111111111111111.lock"
    descriptor = os.open(peer, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            pytest.raises(LocalRuntimeCompatibilityError, match="exclusive runtime"),
            open_local_brain(selection, filesystem_type_probe=probe),
        ):
            pytest.fail("a live peer admitted a migrating bootstrap")
        assert _user_version(profile.root) == 5
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        peer.unlink()

    with (
        _counting_admission(monkeypatch) as acquisitions,
        open_local_brain(selection, filesystem_type_probe=probe) as session,
    ):
        session.tasks.capture.accept(
            TextPayload("synthetic post-migration capture"),
            delivery_id="bootstrap.after",
        )
    assert _user_version(profile.root) == 11
    assert _journal_stage(profile.root, SOURCE_JOURNAL) == "complete"
    assert _journal_stage(profile.root, PRIVACY_JOURNAL) == "complete"
    assert len(acquisitions) == 1


def test_direct_engine_open_never_migrates_an_old_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _legacy_brain(tmp_path / "five", monkeypatch, 5)
    with pytest.raises(StateSchemaUnavailableError, match="source migration"):
        BrainEngine.open(old)
    assert _user_version(old.root) == 5

    seven = _schema_seven_brain(tmp_path / "seven", monkeypatch)
    with pytest.raises(StateSchemaUnavailableError, match="privacy migration"):
        BrainEngine.open(seven)
    assert _user_version(seven.root) == 7
    assert _journal_stage(seven.root, PRIVACY_JOURNAL) is None

    eight = _schema_eight_brain(tmp_path / "eight", monkeypatch)
    with pytest.raises(StateSchemaUnavailableError, match="issuer migration"):
        BrainEngine.open(eight)
    assert _user_version(eight.root) == 8


def _assert_exact_migrated_issuer_evidence(profile: LocalEngineContext) -> None:
    from hashlib import sha256

    from open_brain_engine.core.access_contracts import derive_brain_id
    from open_brain_engine.core.ids import portable_canonical_json_bytes
    from open_brain_engine.engine.issuer_state import (
        derive_legacy_bindings,
        legacy_binding_manifest_sha256,
        synthetic_cutover_manifest,
    )

    files = _vault_files(profile)
    expected_manifest = synthetic_cutover_manifest(files, tenant_id=profile.tenant_id)
    expected_bindings = derive_legacy_bindings(files)
    with sqlite3.connect(Path(profile.root) / _STATE_DATABASE) as connection:
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT tenant_id, brain_id, issuer_epoch, legacy_issuer_epoch "
                "FROM brain_identity"
            )
        ] == [(profile.tenant_id, derive_brain_id(profile.tenant_id), 2, 1)]
        marker = connection.execute(
            "SELECT source_manifest_bytes, source_manifest_sha256, brain_id, "
            "legacy_issuer_epoch, current_issuer_epoch, legacy_binding_manifest_sha256 "
            "FROM issuer_migration_marker"
        ).fetchone()
        assert marker is not None
        manifest_bytes, manifest_sha, marker_brain, legacy_epoch, current_epoch, binding_sha = (
            tuple(marker)
        )
        assert manifest_bytes == portable_canonical_json_bytes(expected_manifest)
        assert manifest_sha == sha256(manifest_bytes).hexdigest()
        assert (marker_brain, legacy_epoch, current_epoch) == (
            derive_brain_id(profile.tenant_id),
            1,
            2,
        )
        assert binding_sha == legacy_binding_manifest_sha256(
            expected_bindings, issuer_epoch=1
        )
        assert [
            tuple(row)
            for row in connection.execute(
                "SELECT artifact_path, jsonl_ordinal, payload_sha256, issuer_epoch "
                "FROM legacy_issuer_bindings ORDER BY artifact_path, jsonl_ordinal"
            )
        ] == [(path, ordinal, digest, 1) for path, ordinal, digest in expected_bindings]


def test_coordinator_migrates_schema_eight_to_eleven_with_exact_issuer_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    profile = _schema_eight_brain(tmp_path, monkeypatch)
    coordinate(profile, clock=_clock)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 11)
    _assert_exact_migrated_issuer_evidence(profile)
    # The ordinary opener works unchanged once the issuer phase has ended at current.
    reopened = BrainEngine.open(profile)
    assert reopened.retrieval.search("synthetic")[0].result_id


def test_issuer_migration_is_atomic_across_a_crash_and_idempotent_on_reentry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate
    from open_brain_engine.engine.issuer_state import migrate_issuer
    from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission

    profile = _schema_eight_brain(tmp_path, monkeypatch)

    class Crash(RuntimeError):
        pass

    def checkpoint(stage: str) -> None:
        if stage == "issuer_evidence_durable":
            raise Crash

    with pytest.raises(Crash):
        coordinate(profile, clock=_clock, checkpoint=checkpoint)
    # The crash left schema eight with no partial issuer state behind.
    assert inspect_phase1_state(profile) == local_schema.SchemaState("supported_old", 8)
    with sqlite3.connect(Path(profile.root) / _STATE_DATABASE) as connection:
        absent = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table in ("brain_identity", "legacy_issuer_bindings", "issuer_migration_marker"):
            assert table not in absent

    coordinate(profile, clock=_clock)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 11)
    _assert_exact_migrated_issuer_evidence(profile)

    # Re-entry on committed schema nine verifies the stored evidence and is a no-op.
    with sqlite3.connect(Path(profile.root) / _STATE_DATABASE) as connection:
        before = connection.execute(
            "SELECT source_manifest_sha256, recorded_at FROM issuer_migration_marker"
        ).fetchone()
    with exclusive_runtime_admission(profile) as admission:
        migrate_issuer(profile, admission=admission, clock=_clock)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 11)
    with sqlite3.connect(Path(profile.root) / _STATE_DATABASE) as connection:
        assert (
            tuple(
                connection.execute(
                    "SELECT source_manifest_sha256, recorded_at FROM issuer_migration_marker"
                ).fetchone()
            )
            == tuple(before)
        )
    _assert_exact_migrated_issuer_evidence(profile)


@pytest.mark.parametrize(
    ("stage", "expected_version"),
    [
        ("saved_lifecycle_preflight", 10),
        ("saved_lifecycle_schema_applied", 10),
        ("saved_lifecycle_complete", 11),
    ],
)
def test_schema_ten_lifecycle_migration_faults_preserve_exact_legacy_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, expected_version: int
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    profile, active, retired = _schema_ten_brain(tmp_path, monkeypatch)
    evidence, files = _schema_ten_evidence(profile), _vault_files(profile)
    with pytest.raises(StateSchemaUnavailableError, match="saved lifecycle migration"):
        BrainEngine.open(profile)
    assert _schema_ten_evidence(profile) == evidence
    observed = []

    def checkpoint(actual: str) -> None:
        observed.append(actual)
        if actual == stage:
            raise RuntimeError("synthetic lifecycle migration crash")

    with (
        exclusive_runtime_admission(profile) as admission,
        pytest.raises(RuntimeError, match="synthetic lifecycle migration crash"),
    ):
        migrate_saved_lifecycle(profile, admission=admission, clock=_clock, checkpoint=checkpoint)
    assert observed[-1] == stage
    assert _user_version(profile.root) == expected_version
    assert _schema_ten_evidence(profile) == evidence
    assert _vault_files(profile) == files
    with sqlite3.connect(profile.root / _STATE_DATABASE) as connection:
        lifecycle_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                "('source_lifecycle_state','source_lifecycle_operations','managed_source_deliveries')"
            )
        }
        assert len(lifecycle_tables) == (0 if expected_version == 10 else 3)
        assert connection.execute("SELECT * FROM runtime_compatibility").fetchall() == [
            (1, 5, 10) if expected_version == 10 else (1, 6, 11)
        ]

    coordinate(profile, clock=_clock)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 11)
    assert _schema_ten_evidence(profile) == evidence
    assert _vault_files(profile) == files
    with sqlite3.connect(profile.root / _STATE_DATABASE) as connection:
        assert connection.execute("SELECT * FROM runtime_compatibility").fetchall() == [(1, 6, 11)]
        assert connection.execute(
            "SELECT source_id,lifecycle_version FROM source_lifecycle_state ORDER BY source_id"
        ).fetchall() == connection.execute(
            "SELECT source_id,0 FROM logical_sources ORDER BY source_id"
        ).fetchall()
        for table in ("source_lifecycle_operations", "managed_source_deliveries"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        ledger = connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
        assert len(ledger) == 11
        assert tuple(ledger[-1][:3]) == (
            LOCAL_MIGRATIONS[10].version,
            LOCAL_MIGRATIONS[10].name,
            LOCAL_MIGRATIONS[10].checksum,
        )

    with exclusive_runtime_admission(profile) as admission:
        migrate_saved_lifecycle(
            profile,
            admission=admission,
            clock=_clock,
            checkpoint=lambda _stage: pytest.fail("committed lifecycle migration reran"),
        )
    coordinate(profile, clock=_clock)
    engine = BrainEngine.open(profile)
    owner = EffectiveAuthority("synthetic-owner", "migration-owner", frozenset(), None, owner=True)
    for capture_id, lifecycle, availability, body in (
        (active, "active", "available", "synthetic schema-ten active evidence 漢字🙂"),
        (retired, "retired", "missing", "synthetic schema-ten retired evidence café Ω"),
    ):
        with sqlite3.connect(profile.root / _STATE_DATABASE) as connection:
            source_id = connection.execute(
                "SELECT source_id FROM source_revisions WHERE capture_id=?", (capture_id,)
            ).fetchone()[0]
        inspection = engine.sources.inspect(
            SourceInspectRequest(source_id=source_id), authority=owner
        )
        assert (inspection.lifecycle, inspection.availability) == (lifecycle, availability)
        assert inspection.lifecycle_version == 0
        assert inspection.withdrawal_receipt is None
        response = engine.history.read_history(
            RecordReadRequest(record_id=capture_id, expected_revision_id=capture_id),
            authority=owner,
        ).to_wire()
        assert response["content"] == {"kind": "untrusted_text", "text": body}
    assert engine.retrieval.fetch(active) is not None
    assert engine.retrieval.fetch(retired) is None
    assert _schema_ten_evidence(profile) == evidence
    assert _vault_files(profile) == files
    with sqlite3.connect(profile.root / _STATE_DATABASE) as connection:
        assert (
            connection.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
            == ledger
        )


@pytest.mark.parametrize("proof_kind", ["foreign-root", "expired", "unlocked"])
def test_schema_ten_lifecycle_migration_requires_live_root_bound_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proof_kind: str
) -> None:
    from open_brain_engine.engine import lifecycle_migration

    profile, _, _ = _schema_ten_brain(tmp_path, monkeypatch)
    evidence, files = _schema_ten_evidence(profile), _vault_files(profile)
    monkeypatch.setattr(
        lifecycle_migration,
        "connect_database",
        lambda **_kwargs: pytest.fail("invalid admission reached database writes"),
    )
    if proof_kind == "foreign-root":
        another = compile_single_user_local(tmp_path / "another")
        with exclusive_runtime_admission(another) as proof, pytest.raises(T03Error):
            migrate_saved_lifecycle(profile, admission=proof, clock=_clock)
    else:
        with exclusive_runtime_admission(profile) as proof:
            pass
        if proof_kind == "expired":
            with pytest.raises(T03Error):
                migrate_saved_lifecycle(profile, admission=proof, clock=_clock)
        else:
            descriptor = os.open(
                profile.root / ".open-brain/runtime-sessions/registry.lock", os.O_RDWR
            )
            try:
                with pytest.raises(T03Error):
                    migrate_saved_lifecycle(
                        profile, admission=HeldRuntimeAdmission(profile, descriptor), clock=_clock
                    )
            finally:
                os.close(descriptor)
    assert _user_version(profile.root) == 10
    assert _schema_ten_evidence(profile) == evidence
    assert _vault_files(profile) == files


def test_schema_ten_lifecycle_migration_refuses_live_peer_before_database_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate
    from open_brain_engine.engine import lifecycle_migration

    profile, _, _ = _schema_ten_brain(tmp_path, monkeypatch)
    evidence, files = _schema_ten_evidence(profile), _vault_files(profile)
    with exclusive_runtime_admission(profile):
        pass
    registry = profile.root / ".open-brain/runtime-sessions/registry.lock"
    descriptor = os.open(registry, os.O_RDWR)
    directory = os.open(registry.parent, os.O_RDONLY | os.O_DIRECTORY)
    peer = registry.parent / "session-11111111111111111111111111111111.lock"
    session = os.open(peer, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(session, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with monkeypatch.context() as blocked:
            blocked.setattr(
                lifecycle_migration,
                "connect_database",
                lambda **_kwargs: pytest.fail("live peer reached lifecycle database writes"),
            )
            with hold_runtime_registry(profile, directory, descriptor) as proof:
                assert proof.live_peer_count == 1
                with pytest.raises(T03Error, match="^operation_pending$"):
                    migrate_saved_lifecycle(profile, admission=proof, clock=_clock)
                with pytest.raises(T03Error, match="^operation_pending$"):
                    coordinate(profile, clock=_clock)
        assert _user_version(profile.root) == 10
        assert _schema_ten_evidence(profile) == evidence
        assert _vault_files(profile) == files
    finally:
        fcntl.flock(session, fcntl.LOCK_UN)
        for handle in (session, directory, descriptor):
            os.close(handle)
        peer.unlink()
    coordinate(profile, clock=_clock)
    assert _user_version(profile.root) == 11
    assert _schema_ten_evidence(profile) == evidence


@pytest.mark.parametrize("opener", ["engine", "bootstrap"])
def test_historical_runtime_five_refuses_schema_eleven_before_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, opener: str
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    profile, _, _ = _schema_ten_brain(tmp_path, monkeypatch)
    coordinate(profile, clock=_clock)
    evidence, files = _schema_ten_evidence(profile), _vault_files(profile)
    database_bytes = (profile.root / _STATE_DATABASE).read_bytes()
    with monkeypatch.context() as historical:
        historical.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 10)
        historical.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:10])
        historical.setattr(local_runtime_session, "RUNTIME_SESSION_VERSION", 5)
        with pytest.raises(StateSchemaUnavailableError, match="newer"):
            if opener == "engine":
                BrainEngine.open(profile)
            else:
                selection = select_local_root(
                    data_dir=str(profile.root),
                    environment={"HOME": str(tmp_path)},
                    platform_name="darwin",
                )
                with open_local_brain(
                    selection, filesystem_type_probe=lambda *_args, **_kwargs: "apfs"
                ):
                    pytest.fail("historical runtime-five bootstrap entered schema eleven")
    assert _user_version(profile.root) == 11
    assert (profile.root / _STATE_DATABASE).read_bytes() == database_bytes
    assert _schema_ten_evidence(profile) == evidence
    assert _vault_files(profile) == files
