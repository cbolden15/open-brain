from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from open_brain_engine.engine import CaptureAction, TextPayload, open_local_engine

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
    OwnerLifecycleRequest,
)
from open_brain.services.appliance_recovery import (
    ApplianceBackupResult,
    ApplianceReplacementPreflight,
)


def test_uninstall_requires_explicit_owner_request_orders_stop_remove_and_preserves_root(
    tmp_path: Path,
) -> None:
    root = _appliance_root(tmp_path)
    portable_before = _portable_bytes(root)
    calls: list[str] = []
    artifact_port = _RecordingArtifactLifecyclePort(
        calls=calls,
        active_candidate_id="candidate_current-v1",
    )
    supervisor = _RecordingSupervisor(calls)
    service = ApplianceLifecycleService(
        root,
        recovery=_UnusedRecovery(),
        artifact_port=artifact_port,
        supervisor=supervisor,
        migrations=(),
        doctor_reader=lambda: {"state": "healthy"},
    )

    with pytest.raises(ValueError, match="explicit owner request"):
        service.uninstall(owner_request=None)

    receipt = service.uninstall(
        owner_request=OwnerLifecycleRequest(
            request_id="uninstall_123e4567-e89b-42d3-a456-426614174401",
            requested_at="2026-09-01T13:00:00Z",
        )
    )
    replay = service.uninstall(
        owner_request=OwnerLifecycleRequest(
            request_id="uninstall_123e4567-e89b-42d3-a456-426614174401",
            requested_at="2026-09-01T13:00:00Z",
        )
    )

    assert receipt.status == "uninstalled"
    assert replay.status == "replayed"
    assert receipt.prior_candidate_id == "candidate_current-v1"
    assert receipt.brain_root_state == "preserved"
    assert calls == ["stop", "remove", "artifact-remove"]
    assert supervisor.remove_count == 1
    assert artifact_port.remove_count == 1
    assert artifact_port.active_candidate_id is None
    assert _portable_bytes(root) == portable_before
    assert "purge" not in inspect.signature(service.uninstall).parameters
    assert not hasattr(service, "purge")


def test_uninstall_reports_bounded_failures_without_deleting_brain_data(tmp_path: Path) -> None:
    root = _appliance_root(tmp_path / "private-uninstall-canary")
    portable_before = _portable_bytes(root)
    calls: list[str] = []
    service = ApplianceLifecycleService(
        root,
        recovery=_UnusedRecovery(),
        artifact_port=_RecordingArtifactLifecyclePort(
            calls=calls,
            active_candidate_id="candidate_current-v1",
            fail_remove=True,
        ),
        supervisor=_RecordingSupervisor(calls),
        migrations=(),
        doctor_reader=lambda: {"state": "healthy"},
    )

    with pytest.raises(ApplianceLifecycleError) as error:
        service.uninstall(
            owner_request=OwnerLifecycleRequest(
                request_id="uninstall_123e4567-e89b-42d3-a456-426614174402",
                requested_at="2026-09-01T13:01:00Z",
            )
        )

    rendered = json.dumps(error.value.receipt.to_dict(), sort_keys=True)
    assert error.value.receipt.failure_stage == "artifact-remove"
    assert calls == ["stop", "remove", "artifact-remove"]
    assert "private-uninstall-canary" not in rendered
    assert "private-uninstall-canary" not in str(error.value)
    assert _portable_bytes(root) == portable_before


def test_uninstall_replays_durably_across_service_restart_without_repeating_effects(
    tmp_path: Path,
) -> None:
    root = _appliance_root(tmp_path)
    calls: list[str] = []
    artifact_port = _RecordingArtifactLifecyclePort(
        calls=calls,
        active_candidate_id="candidate_current-v1",
    )
    supervisor = _RecordingSupervisor(calls)
    request = OwnerLifecycleRequest(
        request_id="uninstall_123e4567-e89b-42d3-a456-426614174403",
        requested_at="2026-09-01T13:02:00Z",
    )
    first = ApplianceLifecycleService(
        root,
        recovery=_UnusedRecovery(),
        artifact_port=artifact_port,
        supervisor=supervisor,
        migrations=(),
        doctor_reader=lambda: {"state": "healthy"},
    )
    first.uninstall(owner_request=request)
    completed_calls = tuple(calls)

    restarted = ApplianceLifecycleService(
        root,
        recovery=_UnusedRecovery(),
        artifact_port=artifact_port,
        supervisor=supervisor,
        migrations=(),
        doctor_reader=lambda: {"state": "healthy"},
    )
    replayed = restarted.uninstall(owner_request=request)

    assert replayed.status == "replayed"
    assert tuple(calls) == completed_calls


def _appliance_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    tasks = open_local_engine(open_existing_single_user_local(root))
    space = tasks.inbox.spaces()[0]
    tasks.capture.accept(
        TextPayload("Portable uninstall preservation text"),
        delivery_id="uninstall.preservation.capture",
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


class _UnusedRecovery:
    def create_backup(
        self,
        destination: Path,
        *,
        backup_id: str,
    ) -> ApplianceBackupResult:
        del destination, backup_id
        raise AssertionError("uninstall must not create a backup")

    def preflight_replacement(
        self,
        source: Path,
        disposable_root: Path,
    ) -> ApplianceReplacementPreflight:
        del source, disposable_root
        raise AssertionError("uninstall must not preflight replacement")


class _RecordingSupervisor:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.remove_count = 0

    def restart(self) -> None:
        self._calls.append("restart")

    def quiesce(self) -> None:
        self._calls.append("quiesce")

    def resume(self) -> None:
        self._calls.append("resume")

    def status(self) -> str:
        self._calls.append("status")
        return "active"

    def stop(self) -> None:
        self._calls.append("stop")

    def remove(self) -> None:
        self.remove_count += 1
        self._calls.append("remove")


class _RecordingArtifactLifecyclePort(ArtifactLifecyclePort):
    def __init__(
        self,
        *,
        calls: list[str],
        active_candidate_id: str | None,
        fail_remove: bool = False,
    ) -> None:
        self._calls = calls
        self.active_candidate_id = active_candidate_id
        self.fail_remove = fail_remove
        self.remove_count = 0

    def compatibility_preflight(self, candidate: ArtifactCandidate) -> ArtifactCompatibilityReceipt:
        return ArtifactCompatibilityReceipt(
            candidate_id=candidate.candidate_id,
            artifact_kind=candidate.artifact_kind,
            current_version="1.0.0",
            target_version=candidate.version,
            status="compatible",
        )

    def activate(self, candidate: ArtifactCandidate) -> ArtifactSwitchReceipt:
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
        self.active_candidate_id = prior_candidate_id
        return ArtifactRollbackReceipt(
            candidate_id=candidate.candidate_id,
            artifact_kind=candidate.artifact_kind,
            active_candidate_id=prior_candidate_id,
            status="rolled_back",
        )

    def remove(self, *, current_candidate_id: str | None = None) -> ArtifactRemovalReceipt:
        self.remove_count += 1
        self._calls.append("artifact-remove")
        if self.fail_remove:
            raise RuntimeError("private uninstall failure")
        self.active_candidate_id = None
        return ArtifactRemovalReceipt(
            artifact_kind="source-checkout",
            removed_candidate_id=current_candidate_id,
            status="removed",
        )
