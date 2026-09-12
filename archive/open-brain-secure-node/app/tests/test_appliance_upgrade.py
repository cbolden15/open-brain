from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from open_brain_engine.engine import BackupReceipt, CaptureAction, TextPayload, open_local_engine
from open_brain_engine.storage.operational import LockBusyError

from open_brain.profile import open_existing_single_user_local
from open_brain.services.appliance_init import initialize_appliance
from open_brain.services.appliance_lifecycle import (
    ApplianceLifecycleError,
    ApplianceLifecycleService,
    ArtifactCandidate,
    ArtifactCompatibilityReceipt,
    ArtifactLifecyclePort,
    ArtifactRemovalReceipt,
    ArtifactRollbackReceipt,
    ArtifactSwitchReceipt,
    LifecycleMigrationReceipt,
    OwnerLifecycleRequest,
)
from open_brain.services.appliance_recovery import (
    ApplianceBackupResult,
    ApplianceReplacementPreflight,
)


def test_upgrade_orders_verified_recovery_migrations_activation_restart_and_doctor(
    tmp_path: Path,
) -> None:
    root = _appliance_root(tmp_path)
    canonical_before = _portable_bytes(root)
    calls: list[str] = []
    artifact_port = _RecordingArtifactLifecyclePort(
        calls=calls,
        active_candidate_id="candidate_current-v1",
    )
    supervisor = _RecordingSupervisor(calls)
    service = ApplianceLifecycleService(
        root,
        recovery=_RecordingRecovery(calls),
        artifact_port=artifact_port,
        supervisor=supervisor,
        migrations=(
            _migration(calls, component="engine", from_version="1.0.0", to_version="1.1.0"),
            _migration(calls, component="app", from_version="1.0.0", to_version="1.1.0"),
        ),
        doctor_reader=_healthy_doctor(calls),
    )
    request = OwnerLifecycleRequest(
        request_id="upgrade_123e4567-e89b-42d3-a456-426614174301",
        requested_at="2026-09-01T12:00:00Z",
    )
    candidate = ArtifactCandidate(
        candidate_id="candidate_source-checkout-v110",
        version="1.1.0",
    )

    receipt = service.upgrade(
        owner_request=request,
        candidate=candidate,
        backup_destination=tmp_path / "backup",
        disposable_root=tmp_path / "upgrade-preflight",
    )
    replayed = service.upgrade(
        owner_request=request,
        candidate=candidate,
        backup_destination=tmp_path / "backup",
        disposable_root=tmp_path / "upgrade-preflight",
    )

    assert receipt.status == "upgraded"
    assert replayed.status == "replayed"
    assert receipt.backup_id == "backup_123e4567-e89b-42d3-a456-426614174301"
    assert receipt.preflight_state == "ready"
    assert receipt.doctor_state == "healthy"
    assert receipt.prior_candidate_id == "candidate_current-v1"
    assert receipt.active_candidate_id == candidate.candidate_id
    assert [migration.component for migration in receipt.migrations] == ["engine", "app"]
    assert calls == [
        "compatibility",
        "quiesce",
        "backup",
        "preflight",
        "migrate:engine",
        "migrate:app",
        "activate",
        "resume",
        "status",
        "doctor",
    ]
    assert artifact_port.rollback_count == 0
    assert supervisor.remove_count == 0
    assert _portable_bytes(root) == canonical_before

    with pytest.raises(ValueError, match="conflicting appliance lifecycle request"):
        service.upgrade(
            owner_request=request,
            candidate=ArtifactCandidate(
                candidate_id="candidate_source-checkout-v120",
                version="1.2.0",
            ),
            backup_destination=tmp_path / "backup",
            disposable_root=tmp_path / "upgrade-preflight",
        )


def test_upgrade_quiesces_an_active_daemon_before_recovery_and_restores_it_on_failure(
    tmp_path: Path,
) -> None:
    root = _appliance_root(tmp_path)
    calls: list[str] = []
    daemon = {"active": True}

    class ActiveSupervisor(_RecordingSupervisor):
        def quiesce(self) -> None:
            calls.append("quiesce")
            daemon["active"] = False

        def resume(self) -> None:
            calls.append("resume")
            daemon["active"] = True

    class OfflineRecovery(_RecordingRecovery):
        def create_backup(
            self,
            destination: Path,
            *,
            backup_id: str,
        ) -> ApplianceBackupResult:
            calls.append("backup")
            assert not daemon["active"]
            raise RuntimeError("synthetic backup failure")

    service = ApplianceLifecycleService(
        root,
        recovery=OfflineRecovery(calls),
        artifact_port=_RecordingArtifactLifecyclePort(
            calls=calls,
            active_candidate_id="candidate_current-v1",
        ),
        supervisor=ActiveSupervisor(calls),
        migrations=(
            _migration(calls, component="engine", from_version="1.0.0", to_version="1.1.0"),
            _migration(calls, component="app", from_version="1.0.0", to_version="1.1.0"),
        ),
        doctor_reader=_healthy_doctor(calls),
    )

    with pytest.raises(ApplianceLifecycleError) as error:
        service.upgrade(
            owner_request=OwnerLifecycleRequest(
                request_id="upgrade_123e4567-e89b-42d3-a456-426614174326",
                requested_at="2026-09-01T12:26:00Z",
            ),
            candidate=ArtifactCandidate(
                candidate_id="candidate_source-checkout-v110",
                version="1.1.0",
            ),
            backup_destination=tmp_path / "backup-active-daemon",
            disposable_root=tmp_path / "preflight-active-daemon",
        )

    assert error.value.receipt.failure_stage == "backup"
    assert error.value.receipt.daemon_restore_state == "restored"
    assert daemon["active"] is True
    assert calls == ["compatibility", "quiesce", "backup", "resume", "status"]


def test_upgrade_replays_durably_across_service_restart_and_rejects_conflict(
    tmp_path: Path,
) -> None:
    root = _appliance_root(tmp_path)
    calls: list[str] = []
    artifact_port = _RecordingArtifactLifecyclePort(
        calls=calls,
        active_candidate_id="candidate_current-v1",
    )
    supervisor = _RecordingSupervisor(calls)
    migrations = (
        _migration(calls, component="engine", from_version="1.0.0", to_version="1.1.0"),
        _migration(calls, component="app", from_version="1.0.0", to_version="1.1.0"),
    )
    request = OwnerLifecycleRequest(
        request_id="upgrade_123e4567-e89b-42d3-a456-426614174320",
        requested_at="2026-09-01T12:20:00Z",
    )
    candidate = ArtifactCandidate(
        candidate_id="candidate_source-checkout-v110",
        version="1.1.0",
    )
    first = ApplianceLifecycleService(
        root,
        recovery=_RecordingRecovery(calls),
        artifact_port=artifact_port,
        supervisor=supervisor,
        migrations=migrations,
        doctor_reader=_healthy_doctor(calls),
    )
    first.upgrade(
        owner_request=request,
        candidate=candidate,
        backup_destination=tmp_path / "backup",
        disposable_root=tmp_path / "preflight",
    )
    completed_calls = tuple(calls)

    restarted = ApplianceLifecycleService(
        root,
        recovery=_RecordingRecovery(calls),
        artifact_port=artifact_port,
        supervisor=supervisor,
        migrations=migrations,
        doctor_reader=_healthy_doctor(calls),
    )
    replayed = restarted.upgrade(
        owner_request=request,
        candidate=candidate,
        backup_destination=tmp_path / "backup",
        disposable_root=tmp_path / "preflight",
    )

    assert replayed.status == "replayed"
    assert tuple(calls) == completed_calls
    with pytest.raises(ValueError, match="conflicting appliance lifecycle request"):
        restarted.upgrade(
            owner_request=request,
            candidate=ArtifactCandidate(
                candidate_id="candidate_source-checkout-v120",
                version="1.2.0",
            ),
            backup_destination=tmp_path / "backup",
            disposable_root=tmp_path / "preflight",
        )


def test_upgrade_restart_after_activation_interruption_rolls_back_once_and_persists_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _appliance_root(tmp_path)
    calls: list[str] = []
    artifact_port = _RecordingArtifactLifecyclePort(
        calls=calls,
        active_candidate_id="candidate_current-v1",
    )
    supervisor = _RecordingSupervisor(calls)
    migrations = (
        _migration(calls, component="engine", from_version="1.0.0", to_version="1.1.0"),
        _migration(calls, component="app", from_version="1.0.0", to_version="1.1.0"),
    )
    candidate = ArtifactCandidate(
        candidate_id="candidate_source-checkout-v110",
        version="1.1.0",
    )
    request = OwnerLifecycleRequest(
        request_id="upgrade_123e4567-e89b-42d3-a456-426614174323",
        requested_at="2026-09-01T12:23:00Z",
    )

    def interrupt_after_activation(selected: ArtifactCandidate) -> ArtifactSwitchReceipt:
        calls.append("activate")
        artifact_port.active_candidate_id = selected.candidate_id
        raise KeyboardInterrupt

    monkeypatch.setattr(artifact_port, "activate", interrupt_after_activation)
    interrupted = ApplianceLifecycleService(
        root,
        recovery=_RecordingRecovery(calls),
        artifact_port=artifact_port,
        supervisor=supervisor,
        migrations=migrations,
        doctor_reader=_healthy_doctor(calls),
    )
    with pytest.raises(KeyboardInterrupt):
        interrupted.upgrade(
            owner_request=request,
            candidate=candidate,
            backup_destination=tmp_path / "backup-interrupted",
            disposable_root=tmp_path / "preflight-interrupted",
        )

    restarted = ApplianceLifecycleService(
        root,
        recovery=_RecordingRecovery(calls),
        artifact_port=artifact_port,
        supervisor=supervisor,
        migrations=migrations,
        doctor_reader=_healthy_doctor(calls),
    )
    with pytest.raises(ApplianceLifecycleError) as recovered:
        restarted.upgrade(
            owner_request=request,
            candidate=candidate,
            backup_destination=tmp_path / "backup-interrupted",
            disposable_root=tmp_path / "preflight-interrupted",
        )
    calls_after_recovery = tuple(calls)

    assert recovered.value.receipt.failure_stage == "interrupted"
    assert recovered.value.receipt.rollback_state == "rolled_back"
    assert artifact_port.active_candidate_id == "candidate_current-v1"
    assert artifact_port.rollback_count == 1
    with pytest.raises(ApplianceLifecycleError) as replayed:
        restarted.upgrade(
            owner_request=request,
            candidate=candidate,
            backup_destination=tmp_path / "backup-interrupted",
            disposable_root=tmp_path / "preflight-interrupted",
        )
    assert replayed.value.receipt == recovered.value.receipt
    assert tuple(calls) == calls_after_recovery


def test_lifecycle_lease_rejects_concurrent_owner_requests_without_rolling_back_active_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _appliance_root(tmp_path)
    calls: list[str] = []
    entered = threading.Event()
    release = threading.Event()
    artifact_port = _RecordingArtifactLifecyclePort(
        calls=calls,
        active_candidate_id="candidate_current-v1",
    )
    original_preflight = artifact_port.compatibility_preflight

    def blocking_preflight(candidate: ArtifactCandidate) -> ArtifactCompatibilityReceipt:
        entered.set()
        if not release.wait(timeout=5):
            raise RuntimeError("synthetic lifecycle test timeout")
        return original_preflight(candidate)

    monkeypatch.setattr(artifact_port, "compatibility_preflight", blocking_preflight)
    migrations = (
        _migration(calls, component="engine", from_version="1.0.0", to_version="1.1.0"),
        _migration(calls, component="app", from_version="1.0.0", to_version="1.1.0"),
    )
    first = ApplianceLifecycleService(
        root,
        recovery=_RecordingRecovery(calls),
        artifact_port=artifact_port,
        supervisor=_RecordingSupervisor(calls),
        migrations=migrations,
        doctor_reader=_healthy_doctor(calls),
    )
    second = ApplianceLifecycleService(
        root,
        recovery=_RecordingRecovery(calls),
        artifact_port=artifact_port,
        supervisor=_RecordingSupervisor(calls),
        migrations=migrations,
        doctor_reader=_healthy_doctor(calls),
    )
    failures: list[BaseException] = []

    def run_first() -> None:
        try:
            first.upgrade(
                owner_request=OwnerLifecycleRequest(
                    request_id="upgrade_123e4567-e89b-42d3-a456-426614174324",
                    requested_at="2026-09-01T12:24:00Z",
                ),
                candidate=ArtifactCandidate(
                    candidate_id="candidate_source-checkout-v110",
                    version="1.1.0",
                ),
                backup_destination=tmp_path / "backup-active",
                disposable_root=tmp_path / "preflight-active",
            )
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(LockBusyError):
            second.upgrade(
                owner_request=OwnerLifecycleRequest(
                    request_id="upgrade_123e4567-e89b-42d3-a456-426614174325",
                    requested_at="2026-09-01T12:25:00Z",
                ),
                candidate=ArtifactCandidate(
                    candidate_id="candidate_source-checkout-v120",
                    version="1.2.0",
                ),
                backup_destination=tmp_path / "backup-concurrent",
                disposable_root=tmp_path / "preflight-concurrent",
            )
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert failures == []
    assert artifact_port.rollback_count == 0


def test_upgrade_rejects_incomplete_migrations_and_mismatched_recovery_evidence(
    tmp_path: Path,
) -> None:
    root = _appliance_root(tmp_path)
    candidate = ArtifactCandidate(
        candidate_id="candidate_source-checkout-v110",
        version="1.1.0",
    )
    mismatch_calls: list[str] = []
    mismatch_port = _RecordingArtifactLifecyclePort(
        calls=mismatch_calls,
        active_candidate_id="candidate_current-v1",
    )
    mismatch = ApplianceLifecycleService(
        root,
        recovery=_RecordingRecovery(
            mismatch_calls,
            preflight_backup_id="backup_123e4567-e89b-42d3-a456-426614174399",
        ),
        artifact_port=mismatch_port,
        supervisor=_RecordingSupervisor(mismatch_calls),
        migrations=(
            _migration(
                mismatch_calls,
                component="engine",
                from_version="1.0.0",
                to_version="1.1.0",
            ),
            _migration(
                mismatch_calls,
                component="app",
                from_version="1.0.0",
                to_version="1.1.0",
            ),
        ),
        doctor_reader=_healthy_doctor(mismatch_calls),
    )

    with pytest.raises(ApplianceLifecycleError) as mismatch_error:
        mismatch.upgrade(
            owner_request=OwnerLifecycleRequest(
                request_id="upgrade_123e4567-e89b-42d3-a456-426614174321",
                requested_at="2026-09-01T12:21:00Z",
            ),
            candidate=candidate,
            backup_destination=tmp_path / "backup-mismatch",
            disposable_root=tmp_path / "preflight-mismatch",
        )

    assert mismatch_error.value.receipt.failure_stage == "preflight"
    assert mismatch_error.value.receipt.daemon_restore_state == "restored"
    assert mismatch_calls == [
        "compatibility",
        "quiesce",
        "backup",
        "preflight",
        "resume",
        "status",
    ]

    migration_calls: list[str] = []
    migration_port = _RecordingArtifactLifecyclePort(
        calls=migration_calls,
        active_candidate_id="candidate_current-v1",
    )
    incomplete = ApplianceLifecycleService(
        root,
        recovery=_RecordingRecovery(migration_calls),
        artifact_port=migration_port,
        supervisor=_RecordingSupervisor(migration_calls),
        migrations=(
            _migration(
                migration_calls,
                component="engine",
                from_version="1.0.0",
                to_version="1.1.0",
            ),
        ),
        doctor_reader=_healthy_doctor(migration_calls),
    )

    with pytest.raises(ApplianceLifecycleError) as migration_error:
        incomplete.upgrade(
            owner_request=OwnerLifecycleRequest(
                request_id="upgrade_123e4567-e89b-42d3-a456-426614174322",
                requested_at="2026-09-01T12:22:00Z",
            ),
            candidate=candidate,
            backup_destination=tmp_path / "backup-migration",
            disposable_root=tmp_path / "preflight-migration",
        )

    assert migration_error.value.receipt.failure_stage == "migrations"
    assert migration_port.rollback_count == 1


def test_upgrade_rejects_missing_owner_request_and_incompatible_candidates_without_forward_work(
    tmp_path: Path,
) -> None:
    root = _appliance_root(tmp_path)
    calls: list[str] = []
    artifact_port = _RecordingArtifactLifecyclePort(
        calls=calls,
        active_candidate_id="candidate_current-v1",
        compatibility_status="incompatible",
    )
    service = ApplianceLifecycleService(
        root,
        recovery=_RecordingRecovery(calls),
        artifact_port=artifact_port,
        supervisor=_RecordingSupervisor(calls),
        migrations=(),
        doctor_reader=lambda: {"state": "healthy"},
    )
    candidate = ArtifactCandidate(
        candidate_id="candidate_source-checkout-v110",
        version="1.1.0",
    )

    with pytest.raises(ValueError, match="explicit owner request"):
        service.upgrade(
            owner_request=None,
            candidate=candidate,
            backup_destination=tmp_path / "backup",
            disposable_root=tmp_path / "upgrade-preflight",
        )

    with pytest.raises(ApplianceLifecycleError) as error:
        service.upgrade(
            owner_request=OwnerLifecycleRequest(
                request_id="upgrade_123e4567-e89b-42d3-a456-426614174302",
                requested_at="2026-09-01T12:01:00Z",
            ),
            candidate=candidate,
            backup_destination=tmp_path / "backup",
            disposable_root=tmp_path / "upgrade-preflight",
        )

    assert error.value.receipt.failure_stage == "compatibility"
    assert error.value.receipt.rollback_state == "not_needed"
    assert calls == ["compatibility"]


@pytest.mark.parametrize(
    ("stage", "expected"),
    (
        (
            "engine",
            [
                "compatibility",
                "quiesce",
                "backup",
                "preflight",
                "migrate:engine",
                "rollback",
                "resume",
                "status",
            ],
        ),
        (
            "app",
            [
                "compatibility",
                "quiesce",
                "backup",
                "preflight",
                "migrate:engine",
                "migrate:app",
                "rollback",
                "resume",
                "status",
            ],
        ),
        (
            "activate",
            [
                "compatibility",
                "quiesce",
                "backup",
                "preflight",
                "migrate:engine",
                "migrate:app",
                "activate",
                "rollback",
                "resume",
                "status",
            ],
        ),
        (
            "restart",
            [
                "compatibility",
                "quiesce",
                "backup",
                "preflight",
                "migrate:engine",
                "migrate:app",
                "activate",
                "resume",
                "rollback",
                "resume",
                "status",
            ],
        ),
        (
            "doctor",
            [
                "compatibility",
                "quiesce",
                "backup",
                "preflight",
                "migrate:engine",
                "migrate:app",
                "activate",
                "resume",
                "status",
                "doctor",
                "rollback",
                "resume",
                "status",
            ],
        ),
    ),
)
def test_upgrade_rolls_back_once_for_each_forward_stage_and_preserves_prior_candidate(
    tmp_path: Path,
    stage: str,
    expected: list[str],
) -> None:
    root = _appliance_root(tmp_path)
    artifact_port = _RecordingArtifactLifecyclePort(
        calls=[],
        active_candidate_id="candidate_current-v1",
        fail_stage="activate" if stage == "activate" else None,
    )
    calls = artifact_port.calls
    supervisor = _RecordingSupervisor(calls, fail_stage="restart" if stage == "restart" else None)
    migrations = (
        _migration(
            calls,
            component="engine",
            from_version="1.0.0",
            to_version="1.1.0",
            fail=stage == "engine",
        ),
        _migration(
            calls,
            component="app",
            from_version="1.0.0",
            to_version="1.1.0",
            fail=stage == "app",
        ),
    )
    def healthy_doctor() -> dict[str, str]:
        calls.append("doctor")
        return {"state": "healthy"}

    def unhealthy_doctor() -> dict[str, str]:
        calls.append("doctor")
        return {"state": "needs_attention"}

    doctor_reader = unhealthy_doctor if stage == "doctor" else healthy_doctor
    service = ApplianceLifecycleService(
        root,
        recovery=_RecordingRecovery(calls),
        artifact_port=artifact_port,
        supervisor=supervisor,
        migrations=migrations,
        doctor_reader=doctor_reader,
    )

    with pytest.raises(ApplianceLifecycleError) as error:
        service.upgrade(
            owner_request=OwnerLifecycleRequest(
                request_id={
                    "engine": "upgrade_123e4567-e89b-42d3-a456-426614174311",
                    "app": "upgrade_123e4567-e89b-42d3-a456-426614174312",
                    "activate": "upgrade_123e4567-e89b-42d3-a456-426614174313",
                    "restart": "upgrade_123e4567-e89b-42d3-a456-426614174314",
                    "doctor": "upgrade_123e4567-e89b-42d3-a456-426614174315",
                }[stage],
                requested_at="2026-09-01T12:02:00Z",
            ),
            candidate=ArtifactCandidate(
                candidate_id="candidate_source-checkout-v110",
                version="1.1.0",
            ),
            backup_destination=tmp_path / "backup",
            disposable_root=tmp_path / "upgrade-preflight",
        )

    assert error.value.receipt.failure_stage == stage
    assert error.value.receipt.rollback_state == "rolled_back"
    assert error.value.receipt.daemon_restore_state == "restored"
    assert calls == expected
    assert artifact_port.rollback_count == 1
    assert artifact_port.active_candidate_id == "candidate_current-v1"


def test_upgrade_requires_verified_backup_preflight_and_bounded_rollback_failures(
    tmp_path: Path,
) -> None:
    root = _appliance_root(tmp_path / "bounded")
    calls: list[str] = []
    service = ApplianceLifecycleService(
        root,
        recovery=_RecordingRecovery(
            calls,
            verified_status="created",
            preflight_status="needs_attention",
        ),
        artifact_port=_RecordingArtifactLifecyclePort(
            calls=calls,
            active_candidate_id="candidate_current-v1",
            rollback_raises=True,
        ),
        supervisor=_RecordingSupervisor(calls),
        migrations=(
            _migration(calls, component="engine", from_version="1.0.0", to_version="1.1.0"),
        ),
        doctor_reader=_unhealthy_doctor(calls),
    )
    candidate = ArtifactCandidate(
        candidate_id="candidate_source-checkout-v110",
        version="1.1.0",
    )

    with pytest.raises(ApplianceLifecycleError) as backup_error:
        service.upgrade(
            owner_request=OwnerLifecycleRequest(
                request_id="upgrade_123e4567-e89b-42d3-a456-426614174303",
                requested_at="2026-09-01T12:03:00Z",
            ),
            candidate=candidate,
            backup_destination=tmp_path / "backup",
            disposable_root=tmp_path / "upgrade-preflight",
        )

    assert backup_error.value.receipt.failure_stage == "backup"
    assert backup_error.value.receipt.rollback_state == "not_needed"
    assert backup_error.value.receipt.daemon_restore_state == "restored"
    assert calls == ["compatibility", "quiesce", "backup", "resume", "status"]

    second_calls: list[str] = []
    second_port = _RecordingArtifactLifecyclePort(
        calls=second_calls,
        active_candidate_id="candidate_current-v1",
        rollback_raises=True,
    )
    second_root = _appliance_root(root.parent / "rollback-canary")
    second_service = ApplianceLifecycleService(
        second_root,
        recovery=_RecordingRecovery(second_calls),
        artifact_port=second_port,
        supervisor=_RecordingSupervisor(second_calls),
        migrations=(
            _migration(
                second_calls,
                component="engine",
                from_version="1.0.0",
                to_version="1.1.0",
            ),
            _migration(
                second_calls,
                component="app",
                from_version="1.0.0",
                to_version="1.1.0",
            ),
        ),
        doctor_reader=_unhealthy_doctor(second_calls),
    )

    with pytest.raises(ApplianceLifecycleError) as rollback_error:
        second_service.upgrade(
            owner_request=OwnerLifecycleRequest(
                request_id="upgrade_123e4567-e89b-42d3-a456-426614174304",
                requested_at="2026-09-01T12:04:00Z",
            ),
            candidate=candidate,
            backup_destination=tmp_path / "backup-second",
            disposable_root=tmp_path / "upgrade-preflight-second",
        )

    rendered = json.dumps(rollback_error.value.receipt.to_dict(), sort_keys=True)
    assert rollback_error.value.receipt.failure_stage == "doctor"
    assert rollback_error.value.receipt.rollback_state == "rollback_failed"
    assert second_port.active_candidate_id == "candidate_current-v1"
    assert "rollback-canary" not in rendered
    assert "private" not in rendered.casefold()
    assert "rollback-canary" not in str(rollback_error.value)


def _appliance_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    tasks = open_local_engine(open_existing_single_user_local(root))
    space = tasks.inbox.spaces()[0]
    tasks.capture.accept(
        TextPayload("Portable upgrade preservation text"),
        delivery_id="upgrade.preservation.capture",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=space.space_id,
    )
    return root


def _portable_bytes(root: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for relative in ("brain.toml",):
        path = root / relative
        snapshot[relative] = path.read_bytes()
    for directory in ("content", "sources", "history"):
        for path in sorted((root / directory).rglob("*")):
            if path.is_file():
                snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


def _backup_receipt(*, status: str, backup_id: str) -> BackupReceipt:
    return BackupReceipt(
        backup_id=backup_id,
        created_at="2026-09-01T12:00:00Z",
        manifest_digest_sha256="a" * 64,
        status=status,
        portable_files=3,
        sqlite_snapshots=1,
        app_state_files=1,
    )


def _migration(
    calls: list[str],
    *,
    component: str,
    from_version: str,
    to_version: str,
    fail: bool = False,
) -> Callable[[ArtifactCandidate], LifecycleMigrationReceipt]:
    def run(_candidate: ArtifactCandidate) -> LifecycleMigrationReceipt:
        calls.append(f"migrate:{component}")
        if fail:
            raise RuntimeError(f"private {component} failure")
        return LifecycleMigrationReceipt(
            component=component,
            from_version=from_version,
            to_version=to_version,
            status="applied",
        )

    run.__name__ = f"migrate:{component}"
    return run


def _healthy_doctor(calls: list[str]) -> Callable[[], dict[str, str]]:
    def read() -> dict[str, str]:
        calls.append("doctor")
        return {"state": "healthy"}

    return read


def _unhealthy_doctor(calls: list[str]) -> Callable[[], dict[str, str]]:
    def read() -> dict[str, str]:
        calls.append("doctor")
        return {"state": "needs_attention"}

    return read


class _RecordingRecovery:
    def __init__(
        self,
        calls: list[str],
        *,
        verified_status: str = "verified",
        preflight_status: str = "ready",
        preflight_backup_id: str | None = None,
    ) -> None:
        self._calls = calls
        self._verified_status = verified_status
        self._preflight_status = preflight_status
        self._preflight_backup_id = preflight_backup_id
        self._created_backup_id: str | None = None

    def create_backup(self, destination: Path, *, backup_id: str) -> ApplianceBackupResult:
        self._calls.append("backup")
        assert destination.is_absolute()
        assert backup_id.startswith("backup_")
        self._created_backup_id = backup_id
        return ApplianceBackupResult(
            created=_backup_receipt(status="created", backup_id=backup_id),
            verified=_backup_receipt(status=self._verified_status, backup_id=backup_id),
        )

    def preflight_replacement(
        self,
        source: Path,
        disposable_root: Path,
    ) -> ApplianceReplacementPreflight:
        self._calls.append("preflight")
        assert source.is_absolute()
        assert disposable_root.is_absolute()
        backup_id = self._preflight_backup_id or self._created_backup_id
        assert backup_id is not None
        return ApplianceReplacementPreflight(
            status=self._preflight_status,
            backup_id=backup_id,
            manifest_digest_sha256="a" * 64,
            credential_state="created",
            doctor_state="healthy" if self._preflight_status == "ready" else "needs_attention",
            index_generation=1,
        )


class _RecordingSupervisor:
    def __init__(self, calls: list[str], fail_stage: str | None = None) -> None:
        self._calls = calls
        self._restart_failures = 1 if fail_stage == "restart" else 0
        self.remove_count = 0

    def restart(self) -> None:
        self._calls.append("restart")
        if self._restart_failures:
            self._restart_failures -= 1
            raise RuntimeError("private restart failure")

    def status(self) -> str:
        self._calls.append("status")
        return "active"

    def stop(self) -> None:
        self._calls.append("stop")

    def quiesce(self) -> None:
        self._calls.append("quiesce")

    def resume(self) -> None:
        self._calls.append("resume")
        if self._restart_failures:
            self._restart_failures -= 1
            raise RuntimeError("private restart failure")

    def remove(self) -> None:
        self.remove_count += 1
        self._calls.append("remove")


class _RecordingArtifactLifecyclePort(ArtifactLifecyclePort):
    def __init__(
        self,
        *,
        calls: list[str],
        active_candidate_id: str | None,
        compatibility_status: str = "compatible",
        fail_stage: str | None = None,
        rollback_raises: bool = False,
    ) -> None:
        self.calls = calls
        self.active_candidate_id = active_candidate_id
        self.compatibility_status = compatibility_status
        self.fail_stage = fail_stage
        self.rollback_raises = rollback_raises
        self.rollback_count = 0

    def compatibility_preflight(self, candidate: ArtifactCandidate) -> ArtifactCompatibilityReceipt:
        self.calls.append("compatibility")
        return ArtifactCompatibilityReceipt(
            candidate_id=candidate.candidate_id,
            artifact_kind=candidate.artifact_kind,
            current_version="1.0.0",
            target_version=candidate.version,
            status=self.compatibility_status,
        )

    def activate(self, candidate: ArtifactCandidate) -> ArtifactSwitchReceipt:
        self.calls.append("activate")
        if self.fail_stage == "activate":
            raise RuntimeError(f"private candidate path for {candidate.candidate_id}")
        self.active_candidate_id = candidate.candidate_id
        return ArtifactSwitchReceipt(
            candidate_id=candidate.candidate_id,
            artifact_kind=candidate.artifact_kind,
            active_candidate_id=candidate.candidate_id,
            status="activated",
        )

    def rollback(
        self,
        candidate: ArtifactCandidate,
        *,
        prior_candidate_id: str | None,
    ) -> ArtifactRollbackReceipt:
        self.rollback_count += 1
        self.calls.append("rollback")
        self.active_candidate_id = prior_candidate_id
        if self.rollback_raises:
            raise RuntimeError("private rollback failure")
        return ArtifactRollbackReceipt(
            candidate_id=candidate.candidate_id,
            artifact_kind=candidate.artifact_kind,
            active_candidate_id=prior_candidate_id,
            status="rolled_back",
        )

    def remove(self, *, current_candidate_id: str | None = None) -> ArtifactRemovalReceipt:
        self.calls.append("artifact-remove")
        return ArtifactRemovalReceipt(
            artifact_kind="source-checkout",
            removed_candidate_id=current_candidate_id,
            status="removed",
        )
