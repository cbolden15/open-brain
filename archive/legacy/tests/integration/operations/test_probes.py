from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from open_brain_engine.engine import LockScope
from open_brain_engine.storage.locks import HeldLeaseSnapshot, LockStateSnapshot
from open_brain_engine.storage.writer_record import CanonicalWriterRecord

from open_brain_legacy._compat.open_brain.config import AppConfig, ConfigError, RetainedRoots
from open_brain_legacy.capture.queue import PendingQueueSnapshot
from open_brain_legacy.operations.doctor import (
    DoctorRole,
    ProbeName,
    ProbeState,
    run_doctor,
)
from open_brain_legacy.operations.models import DeploymentTarget
from open_brain_legacy.operations.probes import (
    BackupEvidenceSnapshot,
    BackupProfileEvidence,
    SchemaSnapshot,
    StaleReferenceSnapshot,
    backup_evidence_probe,
    configuration_probe,
    lock_state_probe,
    queue_age_probe,
    schema_probe,
    stale_reference_probe,
    unavailable_probe,
    writer_ownership_probe,
)

_LOCK_THRESHOLDS = {
    LockScope.SHARED_WRITER: 60,
    LockScope.INDEX: 60,
    LockScope.BACKUP_PROFILE: 300,
    LockScope.INGRESS: 60,
}


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        roots=RetainedRoots(
            work=tmp_path / "work",
            personal=tmp_path / "personal",
            capture=tmp_path / "capture",
            saved_content=tmp_path / "saved-content",
            state=tmp_path / "state",
        ),
        backup=tmp_path / "backup",
        host_identity="synthetic-host",
    )


def test_configuration_probe_reports_loaded_and_invalid_configuration(tmp_path: Path) -> None:
    healthy = configuration_probe(lambda: _config(tmp_path))

    def invalid() -> AppConfig:
        raise ConfigError("synthetic invalid configuration")

    unhealthy = configuration_probe(invalid)

    assert healthy(1.0).state is ProbeState.HEALTHY
    assert unhealthy(1.0).state is ProbeState.UNHEALTHY


def test_configuration_probe_rejects_a_loader_returning_the_wrong_type() -> None:
    def wrong_type() -> AppConfig:
        return object()  # type: ignore[return-value]

    probe = configuration_probe(wrong_type)

    assert probe(1.0).state is ProbeState.UNHEALTHY


def test_writer_ownership_probe_rejects_a_different_canonical_writer() -> None:
    record = CanonicalWriterRecord.create(
        identity_id="other-writer",
        generation=4,
        recorded_at=datetime(2026, 8, 16, 20, 0, tzinfo=UTC),
    )
    probe = writer_ownership_probe(host_identity="synthetic-host", reader=lambda: record)

    reading = probe(1.0)

    assert reading.state is ProbeState.UNHEALTHY
    assert reading.count == 0
    assert reading.observed_at is None
    assert reading.target is None


def test_writer_ownership_probe_reports_missing_invalid_and_unconfigured_state() -> None:
    def invalid() -> CanonicalWriterRecord:
        raise ValueError("synthetic malformed record")

    missing = writer_ownership_probe(host_identity="synthetic-host", reader=lambda: None)
    malformed = writer_ownership_probe(host_identity="synthetic-host", reader=invalid)
    unconfigured = writer_ownership_probe(host_identity=None, reader=lambda: None)

    for reading in (missing(1.0), malformed(1.0), unconfigured(1.0)):
        assert reading.state is ProbeState.UNHEALTHY
        assert reading.count == 0
        assert reading.target is None


def test_writer_ownership_healthy_reading_survives_the_doctor_contract() -> None:
    record = CanonicalWriterRecord.create(
        identity_id="synthetic-host",
        generation=1,
        recorded_at=datetime(2026, 8, 16, 20, 0, tzinfo=UTC),
    )
    probes = {name: unavailable_probe() for name in ProbeName}
    probes[ProbeName.WRITER_OWNERSHIP] = writer_ownership_probe(
        host_identity="synthetic-host",
        reader=lambda: record,
    )

    result = run_doctor(
        role=DoctorRole.WRITER,
        probes=probes,
        timeout_seconds=1.0,
        strict=False,
    )
    check = next(check for check in result.checks if check.probe is ProbeName.WRITER_OWNERSHIP)

    assert check.state is ProbeState.HEALTHY
    assert check.count == 1
    assert check.target is DeploymentTarget.CANONICAL_WRITER


def test_unavailable_probe_carries_no_observation_fields() -> None:
    reading = unavailable_probe()(1.0)

    assert reading.state is ProbeState.UNAVAILABLE
    assert reading.count is None
    assert reading.age_seconds is None
    assert reading.observed_at is None
    assert reading.target is None


def test_queue_age_probe_reports_healthy_stale_and_malformed_snapshots() -> None:
    observed_at = datetime(2026, 8, 16, 20, 1, tzinfo=UTC)
    captured_at = observed_at - timedelta(seconds=30)
    healthy = queue_age_probe(
        reader=lambda: PendingQueueSnapshot(1, 0, captured_at),
        clock=lambda: observed_at,
        stale_after_seconds=60,
    )(1.0)
    stale = queue_age_probe(
        reader=lambda: PendingQueueSnapshot(1, 0, captured_at),
        clock=lambda: observed_at,
        stale_after_seconds=10,
    )(1.0)
    malformed = queue_age_probe(
        reader=lambda: PendingQueueSnapshot(0, 1, None),
        clock=lambda: observed_at,
        stale_after_seconds=60,
    )(1.0)

    assert healthy.state is ProbeState.HEALTHY
    assert healthy.count == 1
    assert healthy.age_seconds == 30
    assert healthy.observed_at == observed_at
    assert stale.state is ProbeState.UNHEALTHY
    assert stale.age_seconds == 30
    assert malformed.state is ProbeState.UNHEALTHY
    assert malformed.count == 1


def test_queue_age_probe_reports_reader_failure_as_unavailable() -> None:
    def failed() -> PendingQueueSnapshot:
        raise OSError("synthetic queue read failure")

    reading = queue_age_probe(
        reader=failed,
        clock=lambda: datetime(2026, 8, 16, 20, 1, tzinfo=UTC),
        stale_after_seconds=60,
    )(1.0)

    assert reading.state is ProbeState.UNAVAILABLE


def test_schema_probe_requires_both_applied_versions_to_match() -> None:
    healthy = schema_probe(reader=lambda: SchemaSnapshot(1, 1, 2, 2))(1.0)
    mismatched = schema_probe(reader=lambda: SchemaSnapshot(2, 1, 1, 2))(1.0)
    checksum_drift = schema_probe(
        reader=lambda: SchemaSnapshot(1, 1, 2, 2, capture_valid=False)
    )(1.0)

    assert healthy.state is ProbeState.HEALTHY
    assert healthy.count == 0
    assert mismatched.state is ProbeState.UNHEALTHY
    assert mismatched.count == 2
    assert checksum_drift.state is ProbeState.UNHEALTHY
    assert checksum_drift.count == 1


def test_schema_probe_reports_reader_failure_as_unavailable() -> None:
    def failed() -> SchemaSnapshot:
        raise OSError("synthetic schema read failure")

    assert schema_probe(reader=failed)(1.0).state is ProbeState.UNAVAILABLE


def test_stale_reference_probe_reports_verified_and_dangling_inventory() -> None:
    healthy = stale_reference_probe(reader=lambda: StaleReferenceSnapshot(4, 0))(1.0)
    stale = stale_reference_probe(reader=lambda: StaleReferenceSnapshot(4, 2))(1.0)

    assert healthy.state is ProbeState.HEALTHY
    assert healthy.count == 0
    assert stale.state is ProbeState.UNHEALTHY
    assert stale.count == 2


def test_backup_evidence_probe_reports_fresh_missing_stale_and_malformed() -> None:
    observed_at = datetime(2026, 8, 16, 20, 0, tzinfo=UTC)
    fresh_at = observed_at - timedelta(hours=2)
    stale_at = observed_at - timedelta(days=2)
    fresh_profiles = tuple(
        BackupProfileEvidence(profile, fresh_at)
        for profile in ("capture", "full", "personal", "runtime-state")
    )

    fresh = backup_evidence_probe(
        reader=lambda: BackupEvidenceSnapshot(4, 0, fresh_profiles),
        clock=lambda: observed_at,
        stale_after_seconds=86_400,
    )(1.0)
    missing = backup_evidence_probe(
        reader=lambda: BackupEvidenceSnapshot(0, 0, ()),
        clock=lambda: observed_at,
        stale_after_seconds=86_400,
    )(1.0)
    stale = backup_evidence_probe(
        reader=lambda: BackupEvidenceSnapshot(
            4,
            0,
            (
                BackupProfileEvidence("capture", stale_at),
                *fresh_profiles[1:],
            ),
        ),
        clock=lambda: observed_at,
        stale_after_seconds=86_400,
    )(1.0)
    malformed = backup_evidence_probe(
        reader=lambda: BackupEvidenceSnapshot(4, 1, fresh_profiles),
        clock=lambda: observed_at,
        stale_after_seconds=86_400,
    )(1.0)

    assert fresh.state is ProbeState.HEALTHY
    assert fresh.count == 0
    assert fresh.age_seconds == 7_200
    assert missing.state is ProbeState.UNHEALTHY
    assert stale.state is ProbeState.UNHEALTHY
    assert malformed.state is ProbeState.UNHEALTHY


def test_stale_reference_probe_reports_reader_failure_as_unavailable() -> None:
    def failed() -> StaleReferenceSnapshot:
        raise OSError("synthetic stale-reference read failure")

    assert stale_reference_probe(reader=failed)(1.0).state is ProbeState.UNAVAILABLE


def test_lock_state_probe_applies_scope_specific_stale_thresholds() -> None:
    observed_at = datetime(2026, 8, 16, 20, 5, tzinfo=UTC)
    fresh_lease = HeldLeaseSnapshot(
        LockScope.INDEX,
        "index",
        observed_at - timedelta(seconds=30),
    )
    stale_lease = HeldLeaseSnapshot(
        LockScope.INDEX,
        "index",
        observed_at - timedelta(seconds=90),
    )
    fresh = lock_state_probe(
        reader=lambda: LockStateSnapshot(1, 0, fresh_lease.acquired_at, (fresh_lease,)),
        clock=lambda: observed_at,
        stale_after_seconds=_LOCK_THRESHOLDS,
    )(1.0)
    stale = lock_state_probe(
        reader=lambda: LockStateSnapshot(1, 0, stale_lease.acquired_at, (stale_lease,)),
        clock=lambda: observed_at,
        stale_after_seconds=_LOCK_THRESHOLDS,
    )(1.0)

    assert fresh.state is ProbeState.HEALTHY
    assert fresh.count == 1
    assert fresh.age_seconds == 30
    assert stale.state is ProbeState.UNHEALTHY
    assert stale.age_seconds == 90


def test_lock_state_probe_accepts_held_descriptor_write_window_but_not_malformed_state() -> None:
    observed_at = datetime(2026, 8, 16, 20, 5, tzinfo=UTC)
    writing = HeldLeaseSnapshot(LockScope.INGRESS, "ingress", None)
    held_without_descriptor = lock_state_probe(
        reader=lambda: LockStateSnapshot(1, 0, None, (writing,)),
        clock=lambda: observed_at,
        stale_after_seconds=_LOCK_THRESHOLDS,
    )(1.0)
    malformed = lock_state_probe(
        reader=lambda: LockStateSnapshot(0, 1, None),
        clock=lambda: observed_at,
        stale_after_seconds=_LOCK_THRESHOLDS,
    )(1.0)

    assert held_without_descriptor.state is ProbeState.HEALTHY
    assert held_without_descriptor.count == 1
    assert held_without_descriptor.age_seconds is None
    assert malformed.state is ProbeState.UNHEALTHY


def test_lock_state_probe_reports_reader_failure_as_unavailable() -> None:
    def failed() -> LockStateSnapshot:
        raise OSError("synthetic lock inspection failure")

    reading = lock_state_probe(
        reader=failed,
        clock=lambda: datetime(2026, 8, 16, 20, 5, tzinfo=UTC),
        stale_after_seconds=_LOCK_THRESHOLDS,
    )(1.0)

    assert reading.state is ProbeState.UNAVAILABLE
