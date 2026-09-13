from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine import (
    CaptureAction,
    TextPayload,
    acquire_daemon_authority,
    open_local_engine,
)

from open_brain.profile import open_existing_single_user_local
from open_brain.services.appliance_application import ApplianceApplication
from open_brain.services.appliance_daemon import (
    ApplianceDaemon,
    RecoveryControlRequest,
    request_recovery_job,
)
from open_brain.services.appliance_init import APPLIANCE_OWNER_CREDENTIAL, initialize_appliance
from open_brain.services.appliance_recovery import ApplianceRecoveryService
from open_brain.services.appliance_scheduler import (
    ApplianceJobResult,
    ApplianceScheduler,
    ApplianceSchedulerInterruptedError,
)

_ORIGINAL_SOCKET_BIND = socket.socket.bind
_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_SOCKET_LISTEN = socket.socket.listen


@pytest.fixture(autouse=True)
def allow_unix_domain_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def bind(
        self: socket.socket, address: str | bytes | tuple[Any, ...]
    ) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_BIND(self, address)
        raise AssertionError("network access is forbidden in the Phase 3 test suite")

    def connect(
        self: socket.socket, address: str | bytes | tuple[Any, ...]
    ) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_CONNECT(self, address)
        raise AssertionError("network access is forbidden in the Phase 3 test suite")

    def connect_ex(
        self: socket.socket, address: str | bytes | tuple[Any, ...]
    ) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_CONNECT_EX(self, address)
        raise AssertionError("network access is forbidden in the Phase 3 test suite")

    def listen(self: socket.socket, backlog: int = 0) -> object:
        if self.family == socket.AF_UNIX:
            return _ORIGINAL_SOCKET_LISTEN(self, backlog)
        raise AssertionError("network access is forbidden in the Phase 3 test suite")

    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "listen", listen)


@pytest.fixture
def short_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="ob-", dir=Path("/tmp").resolve()) as directory:
        yield Path(directory) / "brain"


def test_appliance_recovery_verifies_backup_restores_to_empty_root_and_generates_fresh_credential(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    profile = open_existing_single_user_local(root)
    with acquire_daemon_authority(profile) as authority:
        application = ApplianceApplication.open_mutating(root, authority=authority)
        assert application.mutations is not None
        space = application.mutations.inbox.spaces()[0]
        application.mutations.capture.accept(
            TextPayload("Portable restore token\n"),
            delivery_id="appliance.recovery.capture",
            action=CaptureAction.CANONICAL_NOTE,
            space_id=space.space_id,
        )
        scheduler = ApplianceScheduler(
            profile,
            handlers={"engine-recover": lambda _context: ApplianceJobResult.empty()},
            now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        )
        scheduler.run_due(now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC))
        recovery = application.recovery(scheduler=scheduler)
        portable = recovery.export_portable(
            tmp_path / "portable",
            export_id="export_123e4567-e89b-42d3-a456-4266141740c1",
        )
        backup = recovery.create_backup(
            tmp_path / "backup",
            backup_id="backup_123e4567-e89b-42d3-a456-4266141740c2",
        )
        (tmp_path / "restored").mkdir(mode=0o700)
        restored = recovery.restore_backup(tmp_path / "backup", tmp_path / "restored")

    source_credential = (root / APPLIANCE_OWNER_CREDENTIAL).read_text(encoding="utf-8")
    restored_credential = (tmp_path / "restored" / APPLIANCE_OWNER_CREDENTIAL).read_text(
        encoding="utf-8"
    )
    reopened = open_local_engine(open_existing_single_user_local(tmp_path / "restored"))
    result = reopened.retrieval.search("Portable restore token")[0]

    assert portable.status == "exported"
    assert backup.created.status == "created"
    assert backup.verified.status == "verified"
    assert restored.status == "restored"
    assert restored.credential_state == "created"
    assert restored.doctor_state == "healthy"
    assert restored.index_generation >= 1
    assert restored_credential != source_credential
    assert result.title == "Portable restore token"


def test_appliance_recovery_processes_portable_export_and_import_requests_through_scheduler(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    profile = open_existing_single_user_local(root)
    with acquire_daemon_authority(profile) as authority:
        application = ApplianceApplication.open_mutating(root, authority=authority)
        assert application.mutations is not None
        space = application.mutations.inbox.spaces()[0]
        application.mutations.capture.accept(
            TextPayload("Portable daemon replay token\n"),
            delivery_id="appliance.recovery.job.capture",
            action=CaptureAction.CANONICAL_NOTE,
            space_id=space.space_id,
        )
        holder: list[ApplianceRecoveryService] = []
        scheduler = ApplianceScheduler(
            profile,
            handlers={
                "portable-export": lambda context: holder[0].handle_job(
                    "portable-export", context
                ),
                "portable-import": lambda context: holder[0].handle_job(
                    "portable-import", context
                ),
            },
            now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        )
        recovery = application.recovery(scheduler=scheduler)
        holder.append(recovery)
        export_request = recovery.request_portable_export(
            tmp_path / "portable-job",
            export_id="export_123e4567-e89b-42d3-a456-4266141740d1",
        )

        first = scheduler.run_due(now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC))

        import_request = recovery.request_portable_import(
            tmp_path / "portable-job",
            tmp_path / "imported-job",
            import_id="import_123e4567-e89b-42d3-a456-4266141740d2",
        )
        second = scheduler.run_due(now=datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC))

    exported = tmp_path / "portable-job" / "portable-manifest.json"
    imported = open_local_engine(open_existing_single_user_local(tmp_path / "imported-job"))
    imported_result = imported.retrieval.search("Portable daemon replay token")[0]

    assert export_request.status == "scheduled"
    assert import_request.status == "scheduled"
    assert exported.is_file()
    assert ("portable-export", "completed") in [
        (receipt.job_name, receipt.status) for receipt in first
    ]
    assert ("portable-import", "completed") in [
        (receipt.job_name, receipt.status) for receipt in second
    ]
    assert (tmp_path / "imported-job" / APPLIANCE_OWNER_CREDENTIAL).is_file()
    assert imported_result.title == "Portable daemon replay token"


def test_appliance_recovery_preflights_replacement_only_through_disposable_restore(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    profile = open_existing_single_user_local(root)
    with acquire_daemon_authority(profile) as authority:
        application = ApplianceApplication.open_mutating(root, authority=authority)
        scheduler = ApplianceScheduler(
            profile,
            handlers={"engine-recover": lambda _context: ApplianceJobResult.empty()},
            now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        )
        scheduler.run_due(now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC))
        recovery = application.recovery(scheduler=scheduler)
        recovery.export_portable(
            tmp_path / "portable",
            export_id="export_123e4567-e89b-42d3-a456-4266141740e0",
        )
        backup = recovery.create_backup(
            tmp_path / "backup",
            backup_id="backup_123e4567-e89b-42d3-a456-4266141740e1",
        )
        disposable = tmp_path / "replacement-preflight"
        disposable.mkdir(mode=0o700)
        receipt = recovery.preflight_replacement(tmp_path / "backup", disposable)

    assert receipt.status == "ready"
    assert receipt.backup_id == backup.verified.backup_id
    assert receipt.manifest_digest_sha256 == backup.verified.manifest_digest_sha256
    assert receipt.doctor_state == "healthy"
    assert receipt.credential_state == "created"
    assert not hasattr(recovery, "replace_live")


def test_backup_job_replays_after_effect_before_request_and_scheduler_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    profile = open_existing_single_user_local(root)
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    destination = tmp_path / "backup-job"
    backup_id = "backup_123e4567-e89b-42d3-a456-4266141740e2"
    with acquire_daemon_authority(profile) as authority:
        application = ApplianceApplication.open_mutating(root, authority=authority)
        first_holder: list[ApplianceRecoveryService] = []
        first_scheduler = ApplianceScheduler(
            profile,
            handlers={
                "backup-create": lambda context: first_holder[0].handle_job(
                    "backup-create", context
                )
            },
            now=now,
        )
        first_recovery = application.recovery(scheduler=first_scheduler)
        first_holder.append(first_recovery)
        first_recovery.request_backup(destination, backup_id=backup_id)

        def interrupt_completion(*_args: object) -> None:
            raise ApplianceSchedulerInterruptedError

        monkeypatch.setattr(first_recovery, "_complete", interrupt_completion)
        with pytest.raises(ApplianceSchedulerInterruptedError):
            first_scheduler.run_due(now=now)
        assert (destination / "backup-manifest.json").is_file()

        second_holder: list[ApplianceRecoveryService] = []
        second_scheduler = ApplianceScheduler(
            profile,
            handlers={
                "backup-create": lambda context: second_holder[0].handle_job(
                    "backup-create", context
                )
            },
            now=now,
        )
        second_recovery = application.recovery(scheduler=second_scheduler)
        second_holder.append(second_recovery)
        receipts = second_scheduler.run_due(now=now)
        replayed = second_recovery.request_backup(destination, backup_id=backup_id)

    assert [(receipt.job_name, receipt.status) for receipt in receipts] == [
        ("backup-create", "completed")
    ]
    assert replayed.status == "completed"
    assert len(tuple(destination.glob("backup-manifest.json"))) == 1


def test_recovery_completion_timestamp_never_precedes_the_durable_request(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root, starter_spaces=("Studio",))
    profile = open_existing_single_user_local(root)
    request_id = "backup_123e4567-e89b-42d3-a456-4266141740e3"
    with acquire_daemon_authority(profile) as authority:
        application = ApplianceApplication.open_mutating(root, authority=authority)
        holder: list[ApplianceRecoveryService] = []
        scheduler = ApplianceScheduler(
            profile,
            handlers={
                "backup-create": lambda context: holder[0].handle_job(
                    "backup-create", context
                )
            },
            now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        )
        handler = ApplianceRecoveryService(
            root,
            application,
            scheduler=scheduler,
            now=datetime(2026, 9, 1, 11, 0, tzinfo=UTC),
        )
        holder.append(handler)
        requester = ApplianceRecoveryService(
            root,
            application,
            scheduler=scheduler,
            now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        )
        requester.request_backup(tmp_path / "backup-clock", backup_id=request_id)
        scheduler.run_due(now=datetime(2026, 9, 1, 12, 0, tzinfo=UTC))

    record_path = (
        root
        / ".open-brain"
        / "state"
        / "appliance-recovery"
        / "backup-create"
        / f"{request_id}.json"
    )
    record = json.loads(record_path.read_bytes())

    assert record["completed_at"] >= record["requested_at"]


def test_recovery_control_envelopes_cover_backup_and_distinct_portable_jobs(
    tmp_path: Path,
) -> None:
    requests = (
        RecoveryControlRequest(
            operation="backup-create",
            request_id="backup_123e4567-e89b-42d3-a456-4266141740f1",
            destination=str(tmp_path / "backup"),
        ),
        RecoveryControlRequest(
            operation="portable-export",
            request_id="export_123e4567-e89b-42d3-a456-4266141740f2",
            destination=str(tmp_path / "portable"),
        ),
        RecoveryControlRequest(
            operation="portable-import",
            request_id="import_123e4567-e89b-42d3-a456-4266141740f3",
            source=str(tmp_path / "portable"),
            destination=str(tmp_path / "imported"),
        ),
    )

    for request in requests:
        assert RecoveryControlRequest.from_bytes(request.to_bytes()) == request


def test_daemon_control_schedules_and_executes_backup_on_its_shared_application(
    tmp_path: Path,
    short_root: Path,
) -> None:
    root = short_root
    initialize_appliance(root, starter_spaces=("Studio",))
    destination = tmp_path / "daemon-backup"
    with ApplianceDaemon(root) as daemon:
        thread = threading.Thread(target=daemon.serve_until_stopped)
        thread.start()
        try:
            receipt = request_recovery_job(
                root,
                RecoveryControlRequest(
                    operation="backup-create",
                    request_id="backup_123e4567-e89b-42d3-a456-4266141740f4",
                    destination=str(destination),
                ),
            )
            deadline = time.monotonic() + 5
            while not (destination / "backup-manifest.json").is_file():
                if time.monotonic() >= deadline:
                    raise AssertionError("daemon recovery job did not complete")
                time.sleep(0.01)
        finally:
            daemon.stop()
            thread.join(timeout=5)

    assert not thread.is_alive()
    assert receipt.operation == "backup-create"
    assert receipt.request_id == "backup_123e4567-e89b-42d3-a456-4266141740f4"
    assert receipt.status == "scheduled"
    assert str(destination) not in receipt.to_bytes().decode("utf-8")
