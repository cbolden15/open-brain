from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from open_brain_legacy.migrate import (
    MigrationBlockedError,
    MigrationError,
    MigrationState,
    StaleMigrationPlanError,
)
from open_brain_legacy.migrate.state import (
    AtomicStateArtifactPublisher,
    ConfinedStateArtifactReadBack,
    PythonSqliteSnapshotCapability,
    StateAdoptionManifest,
    StateAdoptionPlan,
    StateAdoptionReceipt,
    StateApplyCapabilities,
    StateArtifact,
    StateArtifactKind,
    StateArtifactPublisher,
    StateArtifactReadBack,
    StateAuthorityReceipt,
    StateCapabilityIssuer,
    StateFamily,
    StateFamilyManifest,
    StateJsonKeySpec,
    StateReadOnlySourceHandle,
    StateSqliteSnapshotCapability,
    StateSqliteSnapshotReceipt,
    StateSqliteSnapshotRequest,
    StateTargetState,
    canonical_state_manifest,
    validate_state_manifest,
)
from open_brain_legacy.migrate.state import (
    apply_state_adoption as _apply_state_adoption,
)
from open_brain_legacy.migrate.state import (
    plan_state_adoption as _plan_state_adoption,
)

_NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
_STATE_CONTROL = ".open-brain-state-adoption"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_CHECKOUT_PATHS = [
    str(_REPOSITORY_ROOT / "src"),
    str(_REPOSITORY_ROOT / "packages" / "engine" / "src"),
]
_PLAN_CONTEXTS: dict[str, StateCapabilityIssuer] = {}
_PLANS: dict[str, StateAdoptionPlan] = {}


def plan_state_adoption(
    *,
    manifest: StateAdoptionManifest,
    source_root: Path,
    target_root: Path,
    backup_root: Path,
    identities: object = None,
    sqlite_snapshot: StateSqliteSnapshotCapability | None = None,
) -> StateAdoptionPlan:
    assert manifest == canonical_state_manifest()
    assert identities is None
    issuer = StateCapabilityIssuer(clock=lambda: _NOW)
    capabilities = issuer.issue_plan_capabilities(
        source_root=source_root,
        target_root=target_root,
        backup_root=backup_root,
        expires_at=_NOW + timedelta(hours=1),
    )
    plan = _plan_state_adoption(
        issuer=issuer,
        capabilities=capabilities,
        sqlite_snapshot=sqlite_snapshot,
    )
    _PLAN_CONTEXTS[plan.fingerprint] = issuer
    _PLANS[plan.fingerprint] = plan
    return plan


def apply_state_adoption(
    *,
    plan: StateAdoptionPlan,
    capability: StateApplyCapabilities,
    restore_root: Path,
    sqlite_snapshot: StateSqliteSnapshotCapability | None = None,
    publisher: StateArtifactPublisher | None = None,
    read_back: StateArtifactReadBack | None = None,
) -> StateAdoptionReceipt:
    issuer = _PLAN_CONTEXTS[plan.fingerprint]
    return _apply_state_adoption(
        plan=plan,
        issuer=issuer,
        capabilities=capability,
        restore_root=restore_root,
        sqlite_snapshot=sqlite_snapshot,
        publisher=publisher,
        read_back=read_back,
    )


def _generic_manifest() -> StateAdoptionManifest:
    return StateAdoptionManifest(
        schema_version=1,
        families=tuple(
            StateFamilyManifest(
                family=family,
                artifacts=(
                    StateArtifact(
                        relative=PurePosixPath(f"{family.value}.json"),
                        kind=StateArtifactKind.JSON,
                        schema_version=1,
                        json_keys=(
                            StateJsonKeySpec(collection_path=("records",), key_fields=("id",)),
                        ),
                    ),
                ),
            )
            for family in StateFamily
        ),
    )


def _manifest() -> StateAdoptionManifest:
    return canonical_state_manifest()


def _payload(
    family: StateFamily,
    artifact: StateArtifact | None = None,
    *,
    value: str = "synthetic",
) -> bytes:
    selected = _manifest().families[0].artifacts[0] if artifact is None else artifact
    key_spec = selected.json_keys[0]
    collection = key_spec.collection_path[0]
    key = key_spec.key_fields[0]
    identifiers = {
        "capture/queue.json": "queue-001",
        "capture/requested-videos.json": "video-001",
        "capture/context-sidecars.json": "capture-001",
        "providers/retrieval-metadata.json": "metadata-001",
        "recovery/backup-metadata.json": "backup-001",
    }
    return json.dumps(
        {
            collection: [{key: identifiers[str(selected.relative)], "value": value}],
            "schema_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_source(
    source: Path,
    manifest: StateAdoptionManifest,
    *,
    skip: frozenset[str] = frozenset(),
) -> dict[str, bytes]:
    expected: dict[str, bytes] = {}
    for family in manifest.families:
        for artifact in family.artifacts:
            if str(artifact.relative) in skip:
                continue
            path = source / artifact.relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if artifact.kind is StateArtifactKind.JSON:
                path.write_bytes(_payload(family.family, artifact))
            else:
                _write_sqlite_fixture(path, artifact)
            expected[str(artifact.relative)] = path.read_bytes()
    return expected


def _write_sqlite_fixture(path: Path, artifact: StateArtifact) -> None:
    assert artifact.sqlite is not None
    connection = sqlite3.connect(path)
    try:
        if artifact.sqlite.application_id is not None:
            connection.execute(f"PRAGMA application_id = {artifact.sqlite.application_id}")
            connection.execute(f"PRAGMA user_version = {artifact.schema_version}")
        relative = str(artifact.relative)
        if relative == "events/events.sqlite3":
            connection.execute(
                "CREATE TABLE events (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            connection.executemany(
                "INSERT INTO events VALUES (?, ?)",
                (("event-001", "checkpointed"), ("event-002", "pre-wal")),
            )
        elif relative == "review/review.sqlite3":
            connection.execute(
                "CREATE TABLE reviews (review_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE review_audit (audit_id TEXT PRIMARY KEY, review_id TEXT NOT NULL, "
                "payload TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO reviews VALUES ('review-001', 'review')")
            connection.execute(
                "INSERT INTO review_audit VALUES ('audit-001', 'review-001', 'audit')"
            )
        elif relative == "ledger/ledger.sqlite3":
            connection.execute(
                "CREATE TABLE ledger_rows (stage_digest TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO ledger_rows VALUES ('stage-001', 'ledger')")
        elif relative == "ledger/inflight.sqlite3":
            connection.execute("CREATE TABLE schema_metadata (version INTEGER NOT NULL)")
            connection.execute("INSERT INTO schema_metadata VALUES (1)")
            connection.execute(
                "CREATE TABLE inflight_journal (stage_digest TEXT PRIMARY KEY, "
                "payload TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO inflight_journal VALUES ('stage-002', 'inflight')"
            )
        else:
            raise AssertionError("unexpected canonical SQLite artifact")
        connection.commit()
    finally:
        connection.close()


class _QuiescedWalSource:
    def close(self) -> None:
        pass


def _wal_sqlite_source(
    source: Path,
) -> tuple[_QuiescedWalSource, StateAdoptionManifest]:
    manifest = _manifest()
    _write_source(source, manifest, skip=frozenset({"events/events.sqlite3"}))
    database = source / "events/events.sqlite3"
    database.parent.mkdir(parents=True, exist_ok=True)
    script = "\n".join(
        (
            "import os, sqlite3, sys",
            "connection = sqlite3.connect(sys.argv[1])",
            "assert connection.execute('PRAGMA journal_mode = WAL').fetchone()[0] == 'wal'",
            "connection.execute('PRAGMA wal_autocheckpoint = 0')",
            "connection.execute('PRAGMA application_id = 4242')",
            "connection.execute('PRAGMA user_version = 3')",
            "connection.execute('CREATE TABLE events "
            "(event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)')",
            "connection.execute(\"INSERT INTO events VALUES "
            "('event-001', 'checkpointed')\")",
            "connection.commit()",
            "connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')",
            "connection.execute(\"INSERT INTO events VALUES "
            "('event-002', 'wal-resident')\")",
            "connection.commit()",
            "os._exit(0)",
        )
    )
    subprocess.run([sys.executable, "-c", script, str(database)], check=True)
    for path in database.parent.iterdir():
        path.chmod(0o444)
    return _QuiescedWalSource(), manifest


class _ReadOnlySnapshotSpy:
    def __init__(self) -> None:
        self.saw_read_only_handle = False

    def snapshot(self, request: StateSqliteSnapshotRequest) -> StateSqliteSnapshotReceipt:
        self.saw_read_only_handle = isinstance(request.source, StateReadOnlySourceHandle)
        assert not isinstance(request.source, Path)
        return PythonSqliteSnapshotCapability().snapshot(request)


class _ForgedSnapshotProvider:
    def snapshot(self, request: StateSqliteSnapshotRequest) -> StateSqliteSnapshotReceipt:
        del request
        return StateSqliteSnapshotReceipt._issued(object())


class _CountingPublisher:
    def __init__(self) -> None:
        self.calls = 0
        self._delegate = AtomicStateArtifactPublisher()

    def publish(
        self, *, target_root: Path, relative: PurePosixPath, payload: bytes
    ) -> None:
        self.calls += 1
        self._delegate.publish(
            target_root=target_root,
            relative=relative,
            payload=payload,
        )


class _InterruptAfterFirstPublish(_CountingPublisher):
    def publish(
        self, *, target_root: Path, relative: PurePosixPath, payload: bytes
    ) -> None:
        super().publish(target_root=target_root, relative=relative, payload=payload)
        if self.calls == 1:
            raise KeyboardInterrupt("synthetic interruption after first JSON write")


class _FailSqliteReadBack:
    def __init__(self) -> None:
        self.sqlite_reads = 0
        self._delegate = ConfinedStateArtifactReadBack()

    def read(self, *, target_root: Path, relative: PurePosixPath) -> bytes | None:
        payload = self._delegate.read(target_root=target_root, relative=relative)
        if relative.suffix == ".sqlite3":
            self.sqlite_reads += 1
            return None
        return payload


def _identities() -> object:
    return None


def _apply_capability(
    plan_fingerprint: str, restore_root: Path
) -> StateApplyCapabilities:
    return _PLAN_CONTEXTS[plan_fingerprint].issue_apply_capabilities(
        plan=_PLANS[plan_fingerprint],
        restore_root=restore_root,
        expires_at=_NOW + timedelta(hours=1),
    )


def _crash_apply_worker(root_value: str, crash_point: str) -> None:
    root = Path(root_value)
    source = root / "source"
    target = root / "target"
    backups = root / "backups"
    restore = root / "restore"
    issuer = StateCapabilityIssuer(clock=lambda: _NOW)
    planning = issuer.issue_plan_capabilities(
        source_root=source,
        target_root=target,
        backup_root=backups,
        expires_at=_NOW + timedelta(hours=1),
    )
    plan = _plan_state_adoption(issuer=issuer, capabilities=planning)
    applying = issuer.issue_apply_capabilities(
        plan=plan,
        restore_root=restore,
        expires_at=_NOW + timedelta(hours=1),
    )

    def terminate(observed: str) -> None:
        if observed == crash_point:
            os._exit(86)

    _apply_state_adoption(
        plan=plan,
        issuer=issuer,
        capabilities=applying,
        restore_root=restore,
        crash_hook=terminate,
    )


def _direct_plan(
    source: Path, target: Path, backups: Path
) -> tuple[StateCapabilityIssuer, StateAdoptionPlan]:
    issuer = StateCapabilityIssuer(clock=lambda: _NOW)
    capabilities = issuer.issue_plan_capabilities(
        source_root=source,
        target_root=target,
        backup_root=backups,
        expires_at=_NOW + timedelta(hours=1),
    )
    return issuer, _plan_state_adoption(issuer=issuer, capabilities=capabilities)


def _committed_artifact_root(target: Path) -> Path:
    control = target / _STATE_CONTROL
    current = json.loads((control / "CURRENT").read_bytes())
    assert isinstance(current, dict)
    assert isinstance(current["generation"], str)
    return control / "generations" / current["generation"] / "artifacts"


def _assert_no_committed_target(target: Path) -> None:
    control = target / _STATE_CONTROL
    assert control.is_dir()
    assert not (control / "CURRENT").exists()
    assert not (control / "transaction.json").exists()
    assert not tuple((control / "generations").iterdir())
    assert not tuple((control / "staging").iterdir())


def test_state_manifest_is_versioned_and_covers_all_seven_families() -> None:
    manifest = _manifest()

    validate_state_manifest(manifest)

    assert {family.family for family in manifest.families} == set(StateFamily)
    assert len(manifest.families) == 7

    with pytest.raises(MigrationBlockedError, match="state manifest is incomplete"):
        validate_state_manifest(
            StateAdoptionManifest(schema_version=1, families=manifest.families[:-1])
        )
    with pytest.raises(MigrationBlockedError, match="state manifest version is unsupported"):
        validate_state_manifest(
            StateAdoptionManifest(schema_version=2, families=manifest.families)
        )


def test_canonical_manifest_rejects_missing_extra_misassigned_and_generic_artifacts() -> None:
    manifest = canonical_state_manifest()
    artifacts = {
        family.family: tuple((str(item.relative), item.kind) for item in family.artifacts)
        for family in manifest.families
    }

    assert artifacts == {
        StateFamily.QUEUE_REQUESTED_VIDEO: (
            ("capture/queue.json", StateArtifactKind.JSON),
            ("capture/requested-videos.json", StateArtifactKind.JSON),
        ),
        StateFamily.CAPTURE_CONTEXT: (
            ("capture/context-sidecars.json", StateArtifactKind.JSON),
        ),
        StateFamily.EVENT_LEDGERS: (
            ("events/events.sqlite3", StateArtifactKind.SQLITE),
        ),
        StateFamily.REVIEW_AUDIT: (
            ("review/review.sqlite3", StateArtifactKind.SQLITE),
        ),
        StateFamily.LEDGER_INFLIGHT: (
            ("ledger/ledger.sqlite3", StateArtifactKind.SQLITE),
            ("ledger/inflight.sqlite3", StateArtifactKind.SQLITE),
        ),
        StateFamily.PROVIDER_RETRIEVAL: (
            ("providers/retrieval-metadata.json", StateArtifactKind.JSON),
        ),
        StateFamily.RECOVERY_BACKUP: (
            ("recovery/backup-metadata.json", StateArtifactKind.JSON),
        ),
    }
    validate_state_manifest(manifest)

    missing = replace(
        manifest,
        families=(
            replace(manifest.families[0], artifacts=manifest.families[0].artifacts[:-1]),
            *manifest.families[1:],
        ),
    )
    extra = replace(
        manifest,
        families=(
            replace(
                manifest.families[0],
                artifacts=(
                    *manifest.families[0].artifacts,
                    manifest.families[1].artifacts[0],
                ),
            ),
            *manifest.families[1:],
        ),
    )
    misassigned = replace(
        manifest,
        families=(
            replace(manifest.families[0], artifacts=manifest.families[1].artifacts),
            replace(manifest.families[1], artifacts=manifest.families[0].artifacts),
            *manifest.families[2:],
        ),
    )
    generic = _generic_manifest()
    for invalid in (missing, extra, misassigned, generic):
        with pytest.raises(MigrationBlockedError, match="state manifest"):
            validate_state_manifest(invalid)


def test_state_plan_is_read_only_identity_bound_and_classifies_empty_or_exact_replay(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    source.mkdir()
    target.mkdir()
    backups.mkdir()
    manifest = _manifest()
    source_before = _write_source(source, manifest)

    empty_plan = plan_state_adoption(
        manifest=manifest,
        source_root=source,
        target_root=target,
        backup_root=backups,
        identities=_identities(),
    )

    assert empty_plan.target_state is StateTargetState.EMPTY
    assert empty_plan.copy_count == 9
    assert empty_plan.idempotency_key_count == 11
    assert empty_plan.source_snapshot_digest == sha256(
        json.dumps(
            {path: sha256(payload).hexdigest() for path, payload in sorted(source_before.items())},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    observed_source = {
        str(path.relative_to(source)): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    assert observed_source == source_before
    assert not tuple(target.iterdir())
    redacted = json.dumps(empty_plan.to_redacted_dict(), sort_keys=True)
    assert str(tmp_path) not in redacted
    assert "synthetic-source-v1" not in redacted
    assert "synthetic" not in redacted

    restore = tmp_path / "restore"
    restore.mkdir()
    apply_state_adoption(
        plan=empty_plan,
        capability=_apply_capability(empty_plan.fingerprint, restore),
        restore_root=restore,
    )

    replay_plan = plan_state_adoption(
        manifest=manifest,
        source_root=source,
        target_root=target,
        backup_root=backups,
        identities=_identities(),
    )

    assert replay_plan.target_state is StateTargetState.EXACT_REPLAY
    assert replay_plan.copy_count == 0
    assert replay_plan.source_snapshot_digest == empty_plan.source_snapshot_digest


def test_state_plan_rejects_unsafe_or_unlisted_artifacts_and_root_aliases(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    source.mkdir()
    target.mkdir()
    backups.mkdir()
    manifest = _manifest()
    _write_source(source, manifest)

    unsafe = StateAdoptionManifest(
        schema_version=1,
        families=(
            StateFamilyManifest(
                family=manifest.families[0].family,
                artifacts=(
                    StateArtifact(
                        relative=PurePosixPath("../escape.json"),
                        kind=StateArtifactKind.JSON,
                        schema_version=1,
                        json_keys=manifest.families[0].artifacts[0].json_keys,
                    ),
                ),
            ),
            *manifest.families[1:],
        ),
    )
    with pytest.raises(MigrationBlockedError, match="artifact path is unsafe"):
        validate_state_manifest(unsafe)

    with pytest.raises(MigrationBlockedError, match="state capability roots must be separate"):
        plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=source,
            backup_root=backups,
            identities=_identities(),
        )
    with pytest.raises(MigrationBlockedError, match="state capability roots must be separate"):
        plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=target,
            identities=_identities(),
        )
    (source / "unlisted.json").write_text("{}", encoding="utf-8")
    with pytest.raises(MigrationBlockedError, match="source does not match state manifest"):
        plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )


def test_state_plan_blocks_idempotency_conflicts_and_partial_targets(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    source.mkdir()
    target.mkdir()
    backups.mkdir()
    restore = tmp_path / "restore"
    restore.mkdir()
    manifest = _manifest()
    _write_source(source, manifest)
    first_family = manifest.families[0]
    first_relative = first_family.artifacts[0].relative
    initial = plan_state_adoption(
        manifest=manifest,
        source_root=source,
        target_root=target,
        backup_root=backups,
        identities=_identities(),
    )
    apply_state_adoption(
        plan=initial,
        capability=_apply_capability(initial.fingerprint, restore),
        restore_root=restore,
    )
    (source / first_relative).write_bytes(
        _payload(first_family.family, first_family.artifacts[0], value="conflict")
    )

    with pytest.raises(MigrationBlockedError, match="state idempotency conflict"):
        plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )

    (source / first_relative).write_bytes(
        _payload(first_family.family, first_family.artifacts[0])
    )
    (target / "uncommitted.json").write_bytes(b"{}")
    with pytest.raises(MigrationBlockedError, match="target contains uncommitted entries"):
        plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )

    (target / "uncommitted.json").unlink()
    duplicate_family = manifest.families[0]
    duplicate_relative = duplicate_family.artifacts[0].relative
    duplicate_payload = json.dumps(
        {
            "items": [
                {"item_id": "queue-001", "value": "first"},
                {"item_id": "queue-001", "value": "second"},
            ],
            "schema_version": 1,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    (source / duplicate_relative).write_bytes(duplicate_payload)
    with pytest.raises(MigrationBlockedError, match="source contains duplicate idempotency key"):
        plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )


def test_sqlite_snapshot_includes_wal_rows_without_copying_sidecars(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    source.mkdir()
    target.mkdir()
    backups.mkdir()
    _writer, manifest = _wal_sqlite_source(source)
    wal = source / "events/events.sqlite3-wal"
    shm = source / "events/events.sqlite3-shm"
    assert wal.exists() and wal.stat().st_size > 0
    assert shm.exists() and shm.stat().st_size > 0
    sidecars_before = {wal.name: wal.read_bytes(), shm.name: shm.read_bytes()}

    plan = plan_state_adoption(
        manifest=manifest,
        source_root=source,
        target_root=target,
        backup_root=backups,
        identities=_identities(),
        sqlite_snapshot=PythonSqliteSnapshotCapability(),
    )
    sidecars_after = {wal.name: wal.read_bytes(), shm.name: shm.read_bytes()}

    artifacts = [artifact for family in manifest.families for artifact in family.artifacts]
    sqlite_index = next(
        index
        for index, artifact in enumerate(artifacts)
        if artifact.kind is StateArtifactKind.SQLITE
    )
    evidence = plan.artifacts[sqlite_index]
    snapshot = sqlite3.connect(":memory:")
    try:
        snapshot.deserialize(plan.snapshot_payloads[sqlite_index])
        rows = snapshot.execute("SELECT event_id, payload FROM events ORDER BY event_id").fetchall()
    finally:
        snapshot.close()

    assert rows == [
        ("event-001", "checkpointed"),
        ("event-002", "wal-resident"),
    ]
    assert plan.ready
    assert evidence.kind is StateArtifactKind.SQLITE
    assert evidence.sqlite_integrity_check == "ok"
    assert evidence.sqlite_tables[0].row_count == 2
    assert evidence.sqlite_sidecars_observed == ("-shm", "-wal")
    assert not tuple(target.iterdir())
    assert sidecars_after == sidecars_before


def test_sqlite_snapshot_accepts_explicit_schema_version_and_rejects_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    source.mkdir()
    target.mkdir()
    backups.mkdir()
    manifest = _manifest()
    _write_source(source, manifest)

    plan = plan_state_adoption(
        manifest=manifest,
        source_root=source,
        target_root=target,
        backup_root=backups,
        identities=_identities(),
    )

    artifacts = [artifact for family in manifest.families for artifact in family.artifacts]
    inflight_index = next(
        index
        for index, artifact in enumerate(artifacts)
        if str(artifact.relative) == "ledger/inflight.sqlite3"
    )
    assert plan.artifacts[inflight_index].schema_version == 1
    database = sqlite3.connect(source / "ledger/inflight.sqlite3")
    try:
        database.execute("UPDATE schema_metadata SET version = 2")
        database.commit()
    finally:
        database.close()
    with pytest.raises(MigrationBlockedError, match="sqlite schema version mismatch"):
        plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )


@pytest.mark.parametrize(
    ("drift", "message"),
    [
        ("schema", "sqlite schema drift"),
        ("rows", "sqlite row reconciliation mismatch"),
        ("keys", "sqlite idempotency reconciliation mismatch"),
        ("application_id", "sqlite schema version mismatch"),
        ("user_version", "sqlite schema version mismatch"),
    ],
)
def test_sqlite_snapshot_rejects_schema_row_and_key_drift(
    tmp_path: Path, drift: str, message: str
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    source.mkdir()
    target.mkdir()
    backups.mkdir()
    manifest = _manifest()
    _write_source(source, manifest)
    database = sqlite3.connect(source / "events/events.sqlite3")
    try:
        if drift == "schema":
            database.execute("ALTER TABLE events ADD COLUMN drift TEXT")
        elif drift == "rows":
            database.execute("INSERT INTO events VALUES ('event-003', 'extra')")
        elif drift == "keys":
            database.execute(
                "UPDATE events SET event_id = 'event-drift' WHERE event_id = 'event-002'"
            )
        elif drift == "application_id":
            database.execute("PRAGMA application_id = 4243")
        else:
            database.execute("PRAGMA user_version = 4")
        database.commit()
    finally:
        database.close()
    with pytest.raises(MigrationBlockedError, match=message):
        plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )


def test_sqlite_snapshot_rejects_malformed_bytes_and_target_sidecars(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    source.mkdir()
    target.mkdir()
    backups.mkdir()
    restore = tmp_path / "restore"
    restore.mkdir()
    writer, manifest = _wal_sqlite_source(source)
    try:
        plan = plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )
        apply_state_adoption(
            plan=plan,
            capability=_apply_capability(plan.fingerprint, restore),
            restore_root=restore,
        )
        replay = plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )
        assert replay.target_state is StateTargetState.EXACT_REPLAY
        assert replay.ready
        artifact_root = _committed_artifact_root(target)
        (artifact_root / "events/events.sqlite3-wal").write_bytes(
            (source / "events/events.sqlite3-wal").read_bytes()
        )
        with pytest.raises(MigrationBlockedError, match="target contains sqlite sidecar"):
            plan_state_adoption(
                manifest=manifest,
                source_root=source,
                target_root=target,
                backup_root=backups,
                identities=_identities(),
            )
    finally:
        writer.close()

    shutil.rmtree(target)
    target.mkdir()
    malformed = source / "events/events.sqlite3"
    malformed.chmod(0o644)
    malformed.write_bytes(b"not a sqlite database")
    with pytest.raises(MigrationBlockedError, match="sqlite snapshot failed"):
        plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )


def test_sqlite_provider_receives_only_read_only_handle_and_forged_receipt_blocks(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    source.mkdir()
    target.mkdir()
    backups.mkdir()
    manifest = _manifest()
    _write_source(source, manifest)
    spy = _ReadOnlySnapshotSpy()

    plan_state_adoption(
        manifest=manifest,
        source_root=source,
        target_root=target,
        backup_root=backups,
        identities=_identities(),
        sqlite_snapshot=spy,
    )

    assert spy.saw_read_only_handle
    with pytest.raises(MigrationBlockedError, match="authority receipt is forged"):
        plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
            sqlite_snapshot=_ForgedSnapshotProvider(),
        )


def test_issuer_rejects_forged_expired_wrong_operation_and_wrong_root_authority(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    restore = tmp_path / "restore"
    for root in (source, target, backups, restore):
        root.mkdir()
    manifest = _manifest()
    _write_source(source, manifest)
    now = [_NOW]
    issuer = StateCapabilityIssuer(clock=lambda: now[0])
    plan_capabilities = issuer.issue_plan_capabilities(
        source_root=source,
        target_root=target,
        backup_root=backups,
        expires_at=_NOW + timedelta(hours=1),
    )
    expiring_plan_capabilities = issuer.issue_plan_capabilities(
        source_root=source,
        target_root=target,
        backup_root=backups,
        expires_at=_NOW + timedelta(minutes=5),
    )
    plan = _plan_state_adoption(
        issuer=issuer,
        capabilities=plan_capabilities,
    )
    apply_capabilities = issuer.issue_apply_capabilities(
        plan=plan,
        restore_root=restore,
        expires_at=_NOW + timedelta(minutes=30),
    )
    expiring_apply_capabilities = issuer.issue_apply_capabilities(
        plan=plan,
        restore_root=restore,
        expires_at=_NOW + timedelta(minutes=5),
    )

    now[0] = _NOW + timedelta(minutes=5)
    with pytest.raises(MigrationBlockedError, match="state capability is expired"):
        _plan_state_adoption(
            issuer=issuer,
            capabilities=expiring_plan_capabilities,
        )
    with pytest.raises(MigrationBlockedError, match="state capability is expired"):
        _apply_state_adoption(
            plan=plan,
            issuer=issuer,
            capabilities=expiring_apply_capabilities,
            restore_root=restore,
        )

    receipt = _apply_state_adoption(
        plan=plan,
        issuer=issuer,
        capabilities=apply_capabilities,
        restore_root=restore,
    )
    evidence = issuer.inspect_adoption_receipt(receipt)
    assert evidence.backup is not None

    forged_receipt = StateAuthorityReceipt._issued(object())
    with pytest.raises(MigrationBlockedError, match="state authority receipt is forged"):
        issuer.inspect_authority_receipt(forged_receipt, operation="backup")
    forged_adoption = StateAdoptionReceipt._issued(object())
    with pytest.raises(MigrationBlockedError, match="state authority receipt is forged"):
        issuer.inspect_adoption_receipt(forged_adoption)
    with pytest.raises(MigrationBlockedError, match="state receipt operation mismatch"):
        issuer.inspect_authority_receipt(evidence.backup, operation="restore")

    original_target = tmp_path / "original-target"
    target.rename(original_target)
    target.mkdir()
    with pytest.raises(MigrationBlockedError, match="state capability root mismatch"):
        issuer.inspect_adoption_receipt(receipt)
    target.rmdir()
    original_target.rename(target)

    now[0] = _NOW + timedelta(minutes=30)
    with pytest.raises(MigrationBlockedError, match="state authority receipt is expired"):
        issuer.inspect_adoption_receipt(receipt)


def test_state_apply_is_backup_first_restorable_and_second_apply_is_noop(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    restore = tmp_path / "restore"
    source.mkdir()
    target.mkdir()
    backups.mkdir()
    restore.mkdir()
    writer, manifest = _wal_sqlite_source(source)
    source_before = {
        str(path.relative_to(source)): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }

    try:
        plan = plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )
        applied = apply_state_adoption(
            plan=plan,
            capability=_apply_capability(plan.fingerprint, restore),
            restore_root=restore,
        )
        applied_evidence = _PLAN_CONTEXTS[plan.fingerprint].inspect_adoption_receipt(
            applied
        )
        assert applied_evidence.backup is not None
        assert applied_evidence.disposable_restore is not None
        backup_evidence = _PLAN_CONTEXTS[plan.fingerprint].inspect_authority_receipt(
            applied_evidence.backup, operation="backup"
        )
        restore_evidence = _PLAN_CONTEXTS[plan.fingerprint].inspect_authority_receipt(
            applied_evidence.disposable_restore, operation="restore"
        )
        backups_after_apply = tuple(backups.iterdir())
        replay = plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )
        noop_publisher = _CountingPublisher()
        noop = apply_state_adoption(
            plan=replay,
            capability=_apply_capability(replay.fingerprint, restore),
            restore_root=restore,
            publisher=noop_publisher,
        )
        noop_evidence = _PLAN_CONTEXTS[replay.fingerprint].inspect_adoption_receipt(noop)
        source_after = {
            str(path.relative_to(source)): path.read_bytes()
            for path in source.rglob("*")
            if path.is_file()
        }
    finally:
        writer.close()

    artifacts = [artifact for family in manifest.families for artifact in family.artifacts]
    assert applied_evidence.state is MigrationState.APPLIED
    assert applied_evidence.write_count == 9
    assert backup_evidence.tracked_count == 9
    assert backup_evidence.file_count == 0
    assert restore_evidence.restored_count == 0
    assert applied_evidence.duplicate_idempotency_keys == 0
    assert applied_evidence.duplicate_captures == 0
    assert source_after == source_before
    artifact_root = _committed_artifact_root(target)
    assert {
        str(artifact.relative): (artifact_root / artifact.relative).read_bytes()
        for artifact in artifacts
    } == dict(
        zip((str(artifact.relative) for artifact in artifacts), plan.snapshot_payloads, strict=True)
    )
    assert not (artifact_root / "events/events.sqlite3-wal").exists()
    assert not (artifact_root / "events/events.sqlite3-shm").exists()

    assert replay.target_state is StateTargetState.EXACT_REPLAY
    assert noop_evidence.state is MigrationState.NOOP
    assert noop_evidence.write_count == 0
    assert noop_evidence.backup is None
    assert noop_evidence.disposable_restore is None
    assert noop_evidence.duplicate_idempotency_keys == 0
    assert noop_evidence.duplicate_captures == 0
    assert noop_publisher.calls == 0
    assert tuple(backups.iterdir()) == backups_after_apply


def test_state_apply_interruption_rolls_back_and_same_plan_retries(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    restore = tmp_path / "restore"
    source.mkdir()
    target.mkdir()
    backups.mkdir()
    restore.mkdir()
    writer, manifest = _wal_sqlite_source(source)
    source_before = {
        str(path.relative_to(source)): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    publisher = _InterruptAfterFirstPublish()

    try:
        plan = plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )
        with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
            apply_state_adoption(
                plan=plan,
                capability=_apply_capability(plan.fingerprint, restore),
                restore_root=restore,
                publisher=publisher,
            )
        _assert_no_committed_target(target)
        assert not tuple(restore.iterdir())
        assert {
            str(path.relative_to(source)): path.read_bytes()
            for path in source.rglob("*")
            if path.is_file()
        } == source_before

        retried = apply_state_adoption(
            plan=plan,
            capability=_apply_capability(plan.fingerprint, restore),
            restore_root=restore,
        )
        retried_evidence = _PLAN_CONTEXTS[plan.fingerprint].inspect_adoption_receipt(
            retried
        )
    finally:
        writer.close()

    assert publisher.calls == 1
    assert retried_evidence.state is MigrationState.APPLIED
    assert retried_evidence.write_count == 9
    assert retried_evidence.duplicate_captures == 0
    assert len(tuple(backups.iterdir())) == 2


def test_state_apply_sqlite_read_back_failure_restores_exact_empty_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    restore = tmp_path / "restore"
    source.mkdir()
    target.mkdir()
    backups.mkdir()
    restore.mkdir()
    writer, manifest = _wal_sqlite_source(source)
    source_before = {
        str(path.relative_to(source)): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    reader = _FailSqliteReadBack()

    try:
        plan = plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )
        with pytest.raises(MigrationError, match="state adoption read-back mismatch"):
            apply_state_adoption(
                plan=plan,
                capability=_apply_capability(plan.fingerprint, restore),
                restore_root=restore,
                read_back=reader,
            )
        source_after = {
            str(path.relative_to(source)): path.read_bytes()
            for path in source.rglob("*")
            if path.is_file()
        }
    finally:
        writer.close()

    assert reader.sqlite_reads == 1
    assert source_after == source_before
    _assert_no_committed_target(target)
    assert not tuple(restore.iterdir())
    assert len(tuple(backups.iterdir())) == 1


def test_state_apply_rejects_invalid_capability_source_change_and_target_race(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    restore = tmp_path / "restore"
    source.mkdir()
    target.mkdir()
    backups.mkdir()
    restore.mkdir()
    writer, manifest = _wal_sqlite_source(source)

    try:
        plan = plan_state_adoption(
            manifest=manifest,
            source_root=source,
            target_root=target,
            backup_root=backups,
            identities=_identities(),
        )
        forged = StateApplyCapabilities._issued(object())
        with pytest.raises(MigrationBlockedError, match="state capability is forged"):
            apply_state_adoption(
                plan=plan,
                capability=forged,
                restore_root=restore,
            )
        with pytest.raises(MigrationBlockedError, match="capability operation mismatch"):
            apply_state_adoption(
                plan=plan,
                capability=cast(StateApplyCapabilities, plan.capabilities),
                restore_root=restore,
            )
        wrong_restore = tmp_path / "wrong-restore"
        wrong_restore.mkdir()
        root_bound = _apply_capability(plan.fingerprint, restore)
        with pytest.raises(MigrationBlockedError, match="state restore root mismatch"):
            apply_state_adoption(
                plan=plan,
                capability=root_bound,
                restore_root=wrong_restore,
            )

        queue = manifest.families[0]
        queue_relative = queue.artifacts[0].relative
        (source / queue_relative).write_bytes(_payload(queue.family, value="source-race"))
        with pytest.raises(StaleMigrationPlanError, match="state adoption plan is stale"):
            apply_state_adoption(
                plan=plan,
                capability=_apply_capability(plan.fingerprint, restore),
                restore_root=restore,
            )

        (source / queue_relative).write_bytes(_payload(queue.family))
        (target / queue_relative).parent.mkdir(parents=True, exist_ok=True)
        (target / queue_relative).write_bytes(plan.snapshot_payloads[0])
        with pytest.raises(StaleMigrationPlanError, match="state adoption plan is stale"):
            apply_state_adoption(
                plan=plan,
                capability=_apply_capability(plan.fingerprint, restore),
                restore_root=restore,
            )
    finally:
        writer.close()

    assert not tuple(backups.iterdir())
    assert not tuple(restore.iterdir())


def test_state_apply_publishes_one_reader_visible_generation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    restore = tmp_path / "restore"
    for root in (source, target, backups, restore):
        root.mkdir()
    manifest = _manifest()
    _write_source(source, manifest)
    issuer, plan = _direct_plan(source, target, backups)
    applying = issuer.issue_apply_capabilities(
        plan=plan,
        restore_root=restore,
        expires_at=_NOW + timedelta(hours=1),
    )

    receipt = _apply_state_adoption(
        plan=plan,
        issuer=issuer,
        capabilities=applying,
        restore_root=restore,
    )
    evidence = issuer.inspect_adoption_receipt(receipt)
    _replay_issuer, replay = _direct_plan(source, target, backups)

    assert evidence.state is MigrationState.APPLIED
    assert replay.target_state is StateTargetState.EXACT_REPLAY
    assert {path.name for path in target.iterdir()} == {_STATE_CONTROL}
    control = target / _STATE_CONTROL
    current = json.loads((control / "CURRENT").read_bytes())
    generation = control / "generations" / current["generation"]
    artifacts = [artifact for family in manifest.families for artifact in family.artifacts]
    assert {
        str(path.relative_to(generation / "artifacts"))
        for path in (generation / "artifacts").rglob("*")
        if path.is_file()
    } == {str(artifact.relative) for artifact in artifacts}
    assert all(not (target / artifact.relative).exists() for artifact in artifacts)


@pytest.mark.parametrize(
    ("crash_point", "expected_state"),
    (
        ("stage", StateTargetState.EMPTY),
        ("generation_rename", StateTargetState.EMPTY),
        ("pointer_publish", StateTargetState.EXACT_REPLAY),
        ("journal_cleanup", StateTargetState.EXACT_REPLAY),
    ),
)
def test_state_generation_recovery_after_process_termination(
    tmp_path: Path,
    crash_point: str,
    expected_state: StateTargetState,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    restore = tmp_path / "restore"
    for root in (source, target, backups, restore):
        root.mkdir()
    _write_source(source, _manifest())
    worker = (
        "import runpy, sys; "
        f"sys.path[:0] = {_SOURCE_CHECKOUT_PATHS!r}; "
        "namespace = runpy.run_path(sys.argv[1]); "
        "namespace['_crash_apply_worker'](sys.argv[2], sys.argv[3])"
    )

    completed = subprocess.run(
        [sys.executable, "-c", worker, __file__, str(tmp_path), crash_point],
        check=False,
    )
    assert completed.returncode == 86

    issuer, recovered = _direct_plan(source, target, backups)
    assert recovered.target_state is expected_state
    applying = issuer.issue_apply_capabilities(
        plan=recovered,
        restore_root=restore,
        expires_at=_NOW + timedelta(hours=1),
    )
    receipt = _apply_state_adoption(
        plan=recovered,
        issuer=issuer,
        capabilities=applying,
        restore_root=restore,
    )
    evidence = issuer.inspect_adoption_receipt(receipt)
    assert evidence.state is (
        MigrationState.APPLIED
        if expected_state is StateTargetState.EMPTY
        else MigrationState.NOOP
    )
    assert not (target / _STATE_CONTROL / "transaction.json").exists()
    _final_issuer, final = _direct_plan(source, target, backups)
    assert final.target_state is StateTargetState.EXACT_REPLAY


def test_target_lease_blocks_competing_apply_and_false_noop(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    restore = tmp_path / "restore"
    for root in (source, target, backups, restore):
        root.mkdir()
    _write_source(source, _manifest())
    issuer, initial = _direct_plan(source, target, backups)
    initial_apply = issuer.issue_apply_capabilities(
        plan=initial,
        restore_root=restore,
        expires_at=_NOW + timedelta(hours=1),
    )
    _apply_state_adoption(
        plan=initial,
        issuer=issuer,
        capabilities=initial_apply,
        restore_root=restore,
    )
    first_issuer, first = _direct_plan(source, target, backups)
    second_issuer, second = _direct_plan(source, target, backups)
    first_apply = first_issuer.issue_apply_capabilities(
        plan=first,
        restore_root=restore,
        expires_at=_NOW + timedelta(hours=1),
    )
    second_apply = second_issuer.issue_apply_capabilities(
        plan=second,
        restore_root=restore,
        expires_at=_NOW + timedelta(hours=1),
    )
    competitor_blocked = False

    def compete(point: str) -> None:
        nonlocal competitor_blocked
        if point != "before_noop_receipt":
            return
        with pytest.raises(MigrationBlockedError, match="target lease is unavailable"):
            _apply_state_adoption(
                plan=second,
                issuer=second_issuer,
                capabilities=second_apply,
                restore_root=restore,
            )
        competitor_blocked = True
        (target / "competitor.json").write_bytes(b"competitor")

    with pytest.raises(StaleMigrationPlanError, match="state target changed during no-op"):
        _apply_state_adoption(
            plan=first,
            issuer=first_issuer,
            capabilities=first_apply,
            restore_root=restore,
            crash_hook=compete,
        )

    assert competitor_blocked
    assert (target / "competitor.json").read_bytes() == b"competitor"


def test_current_publication_is_no_replace_under_competing_writer(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    restore = tmp_path / "restore"
    for root in (source, target, backups, restore):
        root.mkdir()
    _write_source(source, _manifest())
    issuer, plan = _direct_plan(source, target, backups)
    applying = issuer.issue_apply_capabilities(
        plan=plan,
        restore_root=restore,
        expires_at=_NOW + timedelta(hours=1),
    )
    competitor = b'{"generation":"competitor"}'

    def publish_competitor(point: str) -> None:
        if point == "before_pointer_publish":
            (target / _STATE_CONTROL / "CURRENT").write_bytes(competitor)

    with pytest.raises(StaleMigrationPlanError, match="CURRENT changed"):
        _apply_state_adoption(
            plan=plan,
            issuer=issuer,
            capabilities=applying,
            restore_root=restore,
            crash_hook=publish_competitor,
        )

    assert (target / _STATE_CONTROL / "CURRENT").read_bytes() == competitor


def test_generation_promotion_is_no_replace_under_competing_writer(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    backups = tmp_path / "backups"
    restore = tmp_path / "restore"
    for root in (source, target, backups, restore):
        root.mkdir()
    _write_source(source, _manifest())
    issuer, plan = _direct_plan(source, target, backups)
    applying = issuer.issue_apply_capabilities(
        plan=plan,
        restore_root=restore,
        expires_at=_NOW + timedelta(hours=1),
    )
    competitor_marker = b"competitor-generation"
    competitor_generation: Path | None = None

    def publish_competitor(point: str) -> None:
        nonlocal competitor_generation
        if point != "before_generation_rename":
            return
        control = target / _STATE_CONTROL
        journal = json.loads((control / "transaction.json").read_bytes())
        competitor_generation = control / "generations" / journal["generation"]
        competitor_generation.mkdir()
        (competitor_generation / "competitor").write_bytes(competitor_marker)

    with pytest.raises(StaleMigrationPlanError, match="generation already exists"):
        _apply_state_adoption(
            plan=plan,
            issuer=issuer,
            capabilities=applying,
            restore_root=restore,
            crash_hook=publish_competitor,
        )

    assert competitor_generation is not None
    assert (competitor_generation / "competitor").read_bytes() == competitor_marker
    assert not (target / _STATE_CONTROL / "CURRENT").exists()
