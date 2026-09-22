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
from open_brain_engine.engine import (
    BrainEngine,
    LocalEngineContext,
    StateSchemaUnavailableError,
    TextPayload,
    local_migration,
    local_schema,
)
from open_brain_engine.engine.local_schema import inspect_phase1_state
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.privacy_migration import JOURNAL as PRIVACY_JOURNAL
from open_brain_engine.engine.privacy_migration import migrate_privacy
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.source_migration import JOURNAL as SOURCE_JOURNAL
from open_brain_engine.engine.source_migration import migrate_sources

from open_brain.local_data import select_local_root
from open_brain.profile import compile_single_user_local
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
    assert inspect_phase1_state(current) == local_schema.SchemaState("current", 10)
    assert _journal_stage(current.root, SOURCE_JOURNAL) is None
    assert _journal_stage(current.root, PRIVACY_JOURNAL) is None


def test_coordinator_chains_schema_five_and_six_to_current_ten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    for version in (5, 6):
        profile = _legacy_brain(tmp_path / f"v{version}", monkeypatch, version)
        coordinate(profile, clock=_clock)
        assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 10)
        assert _journal_stage(profile.root, SOURCE_JOURNAL) == "complete"
        assert _journal_stage(profile.root, PRIVACY_JOURNAL) == "complete"
        # The ordinary opener works unchanged once the chain has ended at current.
        reopened = BrainEngine.open(profile)
        assert reopened.retrieval.search("synthetic")[0].result_id


def test_coordinator_migrates_schema_seven_to_ten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    profile = _schema_seven_brain(tmp_path, monkeypatch)
    coordinate(profile, clock=_clock)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 10)
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
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 10)
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
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 10)
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
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 10)
    assert _journal_stage(profile.root, SOURCE_JOURNAL) == "complete"
    assert _journal_stage(profile.root, PRIVACY_JOURNAL) == "complete"


def test_coordinator_refuses_invalid_and_newer_state(tmp_path: Path) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    newer = compile_single_user_local(tmp_path / "newer")
    BrainEngine.open(newer)
    with sqlite3.connect(newer.root / _STATE_DATABASE) as connection:
        connection.execute("PRAGMA user_version=11")
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
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 10)


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
    assert _user_version(profile.root) == 10
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


def test_coordinator_migrates_schema_eight_to_ten_with_exact_issuer_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import coordinate_local_migration as coordinate

    profile = _schema_eight_brain(tmp_path, monkeypatch)
    coordinate(profile, clock=_clock)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 10)
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
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 10)
    _assert_exact_migrated_issuer_evidence(profile)

    # Re-entry on committed schema nine verifies the stored evidence and is a no-op.
    with sqlite3.connect(Path(profile.root) / _STATE_DATABASE) as connection:
        before = connection.execute(
            "SELECT source_manifest_sha256, recorded_at FROM issuer_migration_marker"
        ).fetchone()
    with exclusive_runtime_admission(profile) as admission:
        migrate_issuer(profile, admission=admission, clock=_clock)
    assert inspect_phase1_state(profile) == local_schema.SchemaState("current", 10)
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
