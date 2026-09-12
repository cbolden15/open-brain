from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from open_brain_engine.engine import BackupReceipt

from open_brain.services.appliance_lifecycle import (
    ApplianceLifecycleService,
    ArtifactCandidate,
    ArtifactCompatibilityReceipt,
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
from open_brain.services.native_artifacts import (
    NATIVE_ARTIFACT_KIND,
    NativeArtifactError,
    NativeArtifactLifecycleAdapter,
    NativeArtifactManifest,
    native_platform_tag,
)


def test_native_artifact_kind_is_valid_across_the_lifecycle_contract() -> None:
    candidate = ArtifactCandidate(
        candidate_id="candidate_native-v1",
        version="0.1.0",
        artifact_kind=NATIVE_ARTIFACT_KIND,
    )

    assert ArtifactCompatibilityReceipt(
        candidate_id=candidate.candidate_id,
        artifact_kind=candidate.artifact_kind,
        current_version="0.1.0",
        target_version=candidate.version,
        status="compatible",
    ).artifact_kind == NATIVE_ARTIFACT_KIND
    assert ArtifactSwitchReceipt(
        candidate_id=candidate.candidate_id,
        artifact_kind=candidate.artifact_kind,
        active_candidate_id=candidate.candidate_id,
        status="activated",
    ).artifact_kind == NATIVE_ARTIFACT_KIND
    assert ArtifactRollbackReceipt(
        candidate_id=candidate.candidate_id,
        artifact_kind=candidate.artifact_kind,
        active_candidate_id=None,
        status="rolled_back",
    ).artifact_kind == NATIVE_ARTIFACT_KIND
    assert ArtifactRemovalReceipt(
        artifact_kind=candidate.artifact_kind,
        removed_candidate_id=candidate.candidate_id,
        status="removed",
    ).artifact_kind == NATIVE_ARTIFACT_KIND

    with pytest.raises(ValueError, match="artifact candidate"):
        ArtifactCandidate(
            candidate_id="candidate_native-v1",
            version="0.1.0",
            artifact_kind="native-onefile",
        )


def test_native_adapter_activates_reopens_and_rolls_back_exact_candidates(
    tmp_path: Path,
) -> None:
    install_root = _install_root(tmp_path)
    current = _write_candidate(
        install_root,
        candidate_id="candidate_native-v1",
        version="0.1.0",
    )
    target = _write_candidate(
        install_root,
        candidate_id="candidate_native-v2",
        version="0.2.0",
    )
    adapter = NativeArtifactLifecycleAdapter(
        install_root=install_root,
        current_version="0.1.0",
    )
    current_candidate = ArtifactCandidate(
        candidate_id=current.name,
        version="0.1.0",
        artifact_kind=NATIVE_ARTIFACT_KIND,
    )
    target_candidate = ArtifactCandidate(
        candidate_id=target.name,
        version="0.2.0",
        artifact_kind=NATIVE_ARTIFACT_KIND,
    )

    compatibility = adapter.compatibility_preflight(current_candidate)
    activated = adapter.activate(current_candidate)

    assert compatibility.status == "compatible"
    assert compatibility.current_version == "0.1.0"
    assert activated.active_candidate_id == current.name
    assert adapter.active_candidate_id == current.name
    assert (install_root / "current").is_symlink()
    assert (install_root / "current").readlink() == Path("candidates") / current.name

    reopened = NativeArtifactLifecycleAdapter(
        install_root=install_root,
        current_version="0.1.0",
    )
    assert reopened.active_candidate_id == current.name
    assert reopened.compatibility_preflight(target_candidate).status == "compatible"
    assert reopened.activate(target_candidate).active_candidate_id == target.name
    rollback = reopened.rollback(target_candidate, prior_candidate_id=current.name)

    assert rollback.active_candidate_id == current.name
    assert reopened.active_candidate_id == current.name
    assert (install_root / "current").readlink() == Path("candidates") / current.name


def test_native_adapter_rolls_back_when_the_active_candidate_is_corrupt(
    tmp_path: Path,
) -> None:
    install_root = _install_root(tmp_path)
    prior = _write_candidate(
        install_root,
        candidate_id="candidate_native-prior",
        version="0.1.0",
    )
    failed = _write_candidate(
        install_root,
        candidate_id="candidate_native-failed",
        version="0.2.0",
    )
    adapter = NativeArtifactLifecycleAdapter(
        install_root=install_root,
        current_version="0.1.0",
    )
    prior_candidate = ArtifactCandidate(prior.name, "0.1.0", NATIVE_ARTIFACT_KIND)
    failed_candidate = ArtifactCandidate(failed.name, "0.2.0", NATIVE_ARTIFACT_KIND)
    adapter.activate(prior_candidate)
    adapter.activate(failed_candidate)
    (failed / "_internal/resource.json").write_text("corrupt", encoding="utf-8")

    reopened = NativeArtifactLifecycleAdapter(
        install_root=install_root,
        current_version="0.1.0",
    )
    receipt = reopened.rollback(failed_candidate, prior_candidate_id=prior.name)

    assert receipt.active_candidate_id == prior.name
    assert reopened.active_candidate_id == prior.name
    assert (install_root / "current").readlink() == Path("candidates") / prior.name


def test_native_activation_preserves_unowned_temporary_name_collisions(
    tmp_path: Path,
) -> None:
    install_root = _install_root(tmp_path)
    candidate_path = _write_candidate(
        install_root,
        candidate_id="candidate_native-v1",
        version="0.1.0",
    )
    collision = install_root / f".current-{os.getpid()}.tmp"
    collision.write_text("owner state", encoding="utf-8")
    adapter = NativeArtifactLifecycleAdapter(
        install_root=install_root,
        current_version="0.1.0",
    )

    adapter.activate(
        ArtifactCandidate(candidate_path.name, "0.1.0", NATIVE_ARTIFACT_KIND)
    )

    assert collision.read_text(encoding="utf-8") == "owner state"


def test_native_adapter_rejects_tampering_and_escaping_symlinks_with_bounded_errors(
    tmp_path: Path,
) -> None:
    install_root = _install_root(tmp_path)
    candidate_path = _write_candidate(
        install_root,
        candidate_id="candidate_native-v1",
        version="0.1.0",
    )
    candidate = ArtifactCandidate(
        candidate_id=candidate_path.name,
        version="0.1.0",
        artifact_kind=NATIVE_ARTIFACT_KIND,
    )
    adapter = NativeArtifactLifecycleAdapter(
        install_root=install_root,
        current_version="0.1.0",
    )
    (candidate_path / "_internal/resource.json").write_text("tampered", encoding="utf-8")

    with pytest.raises(NativeArtifactError) as tampered:
        adapter.compatibility_preflight(candidate)

    assert str(tampered.value) == "native artifact operation failed"
    assert str(candidate_path) not in str(tampered.value)

    unsafe = install_root / "candidates/candidate_native-unsafe"
    unsafe.mkdir()
    (unsafe / "open-brain").write_bytes(b"executable")
    (unsafe / "open-brain").chmod(0o755)
    (unsafe / "escape").symlink_to("../../outside")

    with pytest.raises(NativeArtifactError, match="operation failed"):
        NativeArtifactManifest.create(
            unsafe,
            candidate_id=unsafe.name,
            version="0.1.0",
            platform_tag=native_platform_tag(),
        )


def test_native_manifest_supports_cross_target_audit_but_not_cross_target_activation(
    tmp_path: Path,
) -> None:
    install_root = _install_root(tmp_path)
    candidate_path = install_root / "candidates/candidate_native-other-host"
    (candidate_path / "_internal").mkdir(parents=True)
    executable = candidate_path / "open-brain"
    executable.write_bytes(b"native executable")
    executable.chmod(0o755)
    other_platform = (
        "linux-x86_64" if native_platform_tag() == "macos-arm64" else "macos-arm64"
    )
    NativeArtifactManifest.create(
        candidate_path,
        candidate_id=candidate_path.name,
        version="0.1.0",
        platform_tag=other_platform,
    ).write(candidate_path)

    assert NativeArtifactManifest.load(candidate_path).platform_tag == other_platform
    adapter = NativeArtifactLifecycleAdapter(
        install_root=install_root,
        current_version="0.1.0",
    )
    with pytest.raises(NativeArtifactError, match="operation failed"):
        adapter.compatibility_preflight(
            ArtifactCandidate(
                candidate_id=candidate_path.name,
                version="0.1.0",
                artifact_kind=NATIVE_ARTIFACT_KIND,
            )
        )


def test_native_adapter_removes_only_enrolled_artifacts_and_preserves_unrelated_state(
    tmp_path: Path,
) -> None:
    install_root = _install_root(tmp_path)
    candidate_path = _write_candidate(
        install_root,
        candidate_id="candidate_native-v1",
        version="0.1.0",
    )
    stale_candidate = _write_candidate(
        install_root,
        candidate_id="candidate_native-stale",
        version="0.0.9",
    )
    unrelated = install_root / "owner-note.txt"
    unrelated.write_text("preserve", encoding="utf-8")
    adapter = NativeArtifactLifecycleAdapter(
        install_root=install_root,
        current_version="0.1.0",
    )
    candidate = ArtifactCandidate(
        candidate_id=candidate_path.name,
        version="0.1.0",
        artifact_kind=NATIVE_ARTIFACT_KIND,
    )
    adapter.activate(candidate)

    receipt = adapter.remove(current_candidate_id=candidate.candidate_id)

    assert receipt == ArtifactRemovalReceipt(
        artifact_kind=NATIVE_ARTIFACT_KIND,
        removed_candidate_id=candidate.candidate_id,
        status="removed",
    )
    assert adapter.active_candidate_id is None
    assert not (install_root / "current").exists()
    assert not candidate_path.exists()
    assert stale_candidate.is_dir()
    assert tuple((install_root / "candidates").iterdir()) == (stale_candidate,)
    assert unrelated.read_text(encoding="utf-8") == "preserve"


def test_native_adapter_bootstraps_only_the_explicit_current_candidate(
    tmp_path: Path,
) -> None:
    install_root = _install_root(tmp_path)
    current = _write_candidate(
        install_root,
        candidate_id="candidate_native-current",
        version="0.1.0",
    )
    unrelated = _write_candidate(
        install_root,
        candidate_id="candidate_owner-unregistered",
        version="9.9.9",
    )
    (install_root / "current").symlink_to(Path("candidates") / current.name)

    adapter = NativeArtifactLifecycleAdapter(
        install_root=install_root,
        current_version="0.1.0",
    )
    receipt = adapter.remove(current_candidate_id=current.name)

    assert receipt.removed_candidate_id == current.name
    assert not current.exists()
    assert unrelated.is_dir()


def test_native_adapter_removes_corrupt_managed_trees_but_preserves_unregistered_state(
    tmp_path: Path,
) -> None:
    install_root = _install_root(tmp_path)
    prior = _write_candidate(
        install_root,
        candidate_id="candidate_native-prior",
        version="0.1.0",
    )
    failed = _write_candidate(
        install_root,
        candidate_id="candidate_native-failed",
        version="0.2.0",
    )
    adapter = NativeArtifactLifecycleAdapter(
        install_root=install_root,
        current_version="0.1.0",
    )
    prior_candidate = ArtifactCandidate(prior.name, "0.1.0", NATIVE_ARTIFACT_KIND)
    failed_candidate = ArtifactCandidate(failed.name, "0.2.0", NATIVE_ARTIFACT_KIND)
    adapter.activate(prior_candidate)
    adapter.activate(failed_candidate)
    (failed / "_internal/resource.json").write_text("corrupt", encoding="utf-8")
    adapter.rollback(failed_candidate, prior_candidate_id=prior.name)
    unregistered = _write_candidate(
        install_root,
        candidate_id="candidate_owner-unregistered",
        version="9.9.9",
    )
    owner_note = install_root / "owner-note.txt"
    owner_note.write_text("preserve", encoding="utf-8")

    reopened = NativeArtifactLifecycleAdapter(
        install_root=install_root,
        current_version="0.1.0",
    )
    receipt = reopened.remove(current_candidate_id=prior.name)

    assert receipt.removed_candidate_id == prior.name
    assert not prior.exists()
    assert not failed.exists()
    assert unregistered.is_dir()
    assert owner_note.read_text(encoding="utf-8") == "preserve"


def test_native_adapter_binds_to_upgrade_and_uninstall_with_clean_scoped_residue(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "brain").resolve()
    root.mkdir()
    (root / ".open-brain").mkdir()
    owner_data = root / "owner-data.txt"
    owner_data.write_text("preserve", encoding="utf-8")
    install_root = _install_root(tmp_path)
    current_path = _write_candidate(
        install_root,
        candidate_id="candidate_native-v010",
        version="0.1.0",
    )
    target_path = _write_candidate(
        install_root,
        candidate_id="candidate_native-v020",
        version="0.2.0",
    )
    adapter = NativeArtifactLifecycleAdapter(
        install_root=install_root,
        current_version="0.1.0",
    )
    adapter.activate(
        ArtifactCandidate(current_path.name, "0.1.0", NATIVE_ARTIFACT_KIND)
    )
    supervisor = _LifecycleSupervisor()
    recovery = _LifecycleRecovery()
    service = ApplianceLifecycleService(
        root,
        recovery=recovery,
        artifact_port=adapter,
        supervisor=supervisor,
        migrations=(
            _lifecycle_migration("engine"),
            _lifecycle_migration("app"),
        ),
        doctor_reader=lambda: {"state": "healthy"},
    )
    candidate = ArtifactCandidate(
        target_path.name,
        "0.2.0",
        NATIVE_ARTIFACT_KIND,
    )

    upgraded = service.upgrade(
        owner_request=OwnerLifecycleRequest(
            request_id="upgrade_123e4567-e89b-42d3-a456-426614174510",
            requested_at="2026-09-02T12:00:00Z",
        ),
        candidate=candidate,
        backup_destination=(tmp_path / "backup").resolve(),
        disposable_root=(tmp_path / "restore-preflight").resolve(),
    )
    uninstalled = service.uninstall(
        owner_request=OwnerLifecycleRequest(
            request_id="uninstall_123e4567-e89b-42d3-a456-426614174511",
            requested_at="2026-09-02T12:01:00Z",
        )
    )

    assert upgraded.status == "upgraded"
    assert upgraded.prior_candidate_id == current_path.name
    assert upgraded.active_candidate_id == target_path.name
    assert uninstalled.status == "uninstalled"
    assert uninstalled.brain_root_state == "preserved"
    assert supervisor.calls == ["stop", "restart", "status", "stop", "remove"]
    assert recovery.calls == ["backup", "preflight"]
    assert owner_data.read_text(encoding="utf-8") == "preserve"
    assert not (install_root / "current").exists()
    assert tuple((install_root / "candidates").iterdir()) == ()


def test_native_supervisor_commands_launch_the_frozen_executable_directly(
    tmp_path: Path,
) -> None:
    from open_brain.services.appliance_supervisors import LaunchdSupervisor, SystemdSupervisor

    root = (tmp_path / "brain").resolve()
    executable = (tmp_path / "install/current/open-brain").resolve()
    launchd = LaunchdSupervisor(
        root=root,
        checkout_root=None,
        python_executable=str(executable),
        unit_directory=(tmp_path / "LaunchAgents").resolve(),
        user_id=501,
        runtime_kind=NATIVE_ARTIFACT_KIND,
    )
    systemd = SystemdSupervisor(
        root=root,
        checkout_root=None,
        python_executable=str(executable),
        unit_directory=(tmp_path / "systemd").resolve(),
        runtime_kind=NATIVE_ARTIFACT_KIND,
    )

    import plistlib

    launchd_value = plistlib.loads(launchd.render().encode("utf-8"))
    systemd_value = systemd.render()

    assert launchd_value["ProgramArguments"] == [
        str(executable),
        "__appliance-daemon",
        "--root",
        str(root),
    ]
    assert f"ExecStart={executable} __appliance-daemon --root {root}" in systemd_value
    assert " -m " not in systemd_value
    assert "PYTHONPATH" not in systemd_value


def _install_root(tmp_path: Path) -> Path:
    root = (tmp_path / "install").resolve()
    (root / "candidates").mkdir(parents=True)
    return root


def _write_candidate(
    install_root: Path,
    *,
    candidate_id: str,
    version: str,
) -> Path:
    candidate = install_root / "candidates" / candidate_id
    (candidate / "_internal").mkdir(parents=True)
    executable = candidate / "open-brain"
    executable.write_bytes(b"native executable")
    executable.chmod(0o755)
    (candidate / "_internal/resource.json").write_text('{"status":"ok"}\n', encoding="utf-8")
    (candidate / "_internal/resource-link.json").symlink_to("resource.json")
    NativeArtifactManifest.create(
        candidate,
        candidate_id=candidate_id,
        version=version,
        platform_tag=native_platform_tag(),
    ).write(candidate)
    return candidate


class _LifecycleRecovery:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._backup_id: str | None = None

    def create_backup(self, destination: Path, *, backup_id: str) -> ApplianceBackupResult:
        assert destination.is_absolute()
        self.calls.append("backup")
        self._backup_id = backup_id
        return ApplianceBackupResult(
            created=_backup_receipt(backup_id, status="created"),
            verified=_backup_receipt(backup_id, status="verified"),
        )

    def preflight_replacement(
        self,
        source: Path,
        disposable_root: Path,
    ) -> ApplianceReplacementPreflight:
        assert source.is_absolute()
        assert disposable_root.is_absolute()
        assert self._backup_id is not None
        self.calls.append("preflight")
        return ApplianceReplacementPreflight(
            status="ready",
            backup_id=self._backup_id,
            manifest_digest_sha256="a" * 64,
            credential_state="created",
            doctor_state="healthy",
            index_generation=1,
        )


class _LifecycleSupervisor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def restart(self) -> None:
        self.calls.append("restart")

    def quiesce(self) -> None:
        self.calls.append("stop")

    def resume(self) -> None:
        self.calls.append("restart")

    def status(self) -> str:
        self.calls.append("status")
        return "active"

    def stop(self) -> None:
        self.calls.append("stop")

    def remove(self) -> None:
        self.calls.append("remove")


def _backup_receipt(backup_id: str, *, status: str) -> BackupReceipt:
    return BackupReceipt(
        backup_id=backup_id,
        created_at="2026-09-02T12:00:00Z",
        manifest_digest_sha256="a" * 64,
        status=status,
        portable_files=1,
        sqlite_snapshots=1,
        app_state_files=1,
    )


def _lifecycle_migration(
    component: str,
) -> Callable[[ArtifactCandidate], LifecycleMigrationReceipt]:
    def migrate(_candidate: ArtifactCandidate) -> LifecycleMigrationReceipt:
        return LifecycleMigrationReceipt(
            component=component,
            from_version="0.1.0",
            to_version="0.2.0",
            status="applied",
        )

    migrate.__name__ = f"migrate:{component}"
    return migrate
