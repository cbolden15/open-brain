from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from open_brain_engine.engine import LockScope

from open_brain_legacy._compat.open_brain.config import AppConfig, ConfigError

from .doctor import DoctorProbe, ProbeReading
from .models import DeploymentTarget, OperationsValidationError


class WriterRecordView(Protocol):
    @property
    def identity_id(self) -> str: ...

    @property
    def generation(self) -> int: ...

    @property
    def recorded_at(self) -> datetime: ...


class PendingQueueSnapshotView(Protocol):
    @property
    def pending_count(self) -> int: ...

    @property
    def malformed_count(self) -> int: ...

    @property
    def oldest_captured_at(self) -> datetime | None: ...


class HeldLeaseView(Protocol):
    @property
    def scope(self) -> LockScope: ...

    @property
    def acquired_at(self) -> datetime | None: ...


class LockStateSnapshotView(Protocol):
    @property
    def held_count(self) -> int: ...

    @property
    def malformed_count(self) -> int: ...

    @property
    def held_leases(self) -> tuple[HeldLeaseView, ...]: ...


ConfigurationLoader = Callable[[], AppConfig]
WriterRecordReader = Callable[[], WriterRecordView | None]
PendingQueueReader = Callable[[], PendingQueueSnapshotView]
LockStateReader = Callable[[], LockStateSnapshotView]
Clock = Callable[[], datetime]
_IDENTITY = re.compile(r"[a-z][a-z0-9-]{0,63}")


@dataclass(frozen=True, slots=True)
class SchemaSnapshot:
    capture_version: int
    expected_capture_version: int
    review_version: int
    expected_review_version: int
    capture_valid: bool = True
    review_valid: bool = True

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.capture_version,
                self.expected_capture_version,
                self.review_version,
                self.expected_review_version,
            )
        ):
            raise OperationsValidationError("invalid schema snapshot")
        if (
            self.expected_capture_version < 1
            or self.expected_review_version < 1
            or type(self.capture_valid) is not bool
            or type(self.review_valid) is not bool
        ):
            raise OperationsValidationError("invalid schema snapshot")


@dataclass(frozen=True, slots=True)
class StaleReferenceSnapshot:
    reference_count: int
    stale_count: int

    def __post_init__(self) -> None:
        if (
            type(self.reference_count) is not int
            or self.reference_count < 0
            or type(self.stale_count) is not int
            or not 0 <= self.stale_count <= self.reference_count
        ):
            raise OperationsValidationError("invalid stale-reference snapshot")


@dataclass(frozen=True, slots=True)
class BackupProfileEvidence:
    profile: str
    latest_created_at: datetime

    def __post_init__(self) -> None:
        if self.profile not in {"capture", "full", "personal", "runtime-state"} or not _is_aware(
            self.latest_created_at
        ):
            raise OperationsValidationError("invalid backup profile evidence")


@dataclass(frozen=True, slots=True)
class BackupEvidenceSnapshot:
    manifest_count: int
    malformed_count: int
    profiles: tuple[BackupProfileEvidence, ...]

    def __post_init__(self) -> None:
        if (
            type(self.manifest_count) is not int
            or self.manifest_count < 0
            or type(self.malformed_count) is not int
            or self.malformed_count < 0
            or not isinstance(self.profiles, tuple)
            or any(
                not isinstance(profile, BackupProfileEvidence) for profile in self.profiles
            )
            or [profile.profile for profile in self.profiles]
            != sorted({profile.profile for profile in self.profiles})
            or len(self.profiles) > self.manifest_count
        ):
            raise OperationsValidationError("invalid backup evidence snapshot")


def configuration_probe(loader: ConfigurationLoader) -> DoctorProbe:
    """Build a probe that validates one explicit configuration load attempt."""
    if not callable(loader):
        raise OperationsValidationError("invalid configuration probe loader")

    def probe(_: float) -> ProbeReading:
        try:
            config = loader()
        except ConfigError:
            return ProbeReading.unhealthy()
        if not isinstance(config, AppConfig):
            return ProbeReading.unhealthy()
        return ProbeReading.healthy()

    return probe


def optional_provider_probe(config: AppConfig) -> DoctorProbe:
    """Treat the intentionally disabled optional provider as a healthy absence."""
    if not isinstance(config, AppConfig):
        raise OperationsValidationError("invalid optional-provider probe configuration")

    def probe(_: float) -> ProbeReading:
        if config.provider == "local" and not config.cloud_enabled:
            return ProbeReading.healthy(count=0)
        return ProbeReading.unavailable()

    return probe


def writer_ownership_probe(
    *,
    host_identity: str | None,
    reader: WriterRecordReader,
) -> DoctorProbe:
    """Observe canonical-writer designation without designating the current host."""
    if (
        host_identity is not None
        and (not isinstance(host_identity, str) or _IDENTITY.fullmatch(host_identity) is None)
        or not callable(reader)
    ):
        raise OperationsValidationError("invalid writer ownership probe configuration")

    def probe(_: float) -> ProbeReading:
        if host_identity is None:
            return ProbeReading.unhealthy(count=0)
        try:
            record = reader()
        except Exception:
            return ProbeReading.unhealthy(count=0)
        if (
            record is None
            or not _valid_writer_record(record)
            or record.identity_id != host_identity
        ):
            return ProbeReading.unhealthy(count=0)
        return ProbeReading.healthy(
            count=1,
            observed_at=record.recorded_at,
            target=DeploymentTarget.CANONICAL_WRITER,
        )

    return probe


def queue_age_probe(
    *,
    reader: PendingQueueReader,
    clock: Clock,
    stale_after_seconds: int,
) -> DoctorProbe:
    """Observe pending age and malformed records without transitioning queue state."""
    if (
        not callable(reader)
        or not callable(clock)
        or type(stale_after_seconds) is not int
        or not 1 <= stale_after_seconds <= 604_800
    ):
        raise OperationsValidationError("invalid queue age probe configuration")

    def probe(_: float) -> ProbeReading:
        try:
            snapshot = reader()
            observed_at = _utc(clock())
            if not _valid_queue_snapshot(snapshot):
                return ProbeReading.unavailable()
            oldest = snapshot.oldest_captured_at
            future_timestamp = oldest is not None and oldest > observed_at
            age_seconds = (
                None
                if oldest is None
                else max(0, int((observed_at - oldest).total_seconds()))
            )
            total_count = snapshot.pending_count + snapshot.malformed_count
            unhealthy = (
                snapshot.malformed_count > 0
                or future_timestamp
                or age_seconds is not None
                and age_seconds > stale_after_seconds
            )
            factory = ProbeReading.unhealthy if unhealthy else ProbeReading.healthy
            return factory(
                count=total_count,
                age_seconds=age_seconds,
                observed_at=observed_at,
            )
        except Exception:
            return ProbeReading.unavailable()

    return probe


def lock_state_probe(
    *,
    reader: LockStateReader,
    clock: Clock,
    stale_after_seconds: Mapping[LockScope, int],
) -> DoctorProbe:
    """Evaluate side-effect-free lock observations with per-scope stale thresholds."""
    expected_scopes = frozenset(
        {
            LockScope.SHARED_WRITER,
            LockScope.INDEX,
            LockScope.BACKUP_PROFILE,
            LockScope.INGRESS,
        }
    )
    if (
        not callable(reader)
        or not callable(clock)
        or not isinstance(stale_after_seconds, Mapping)
        or frozenset(stale_after_seconds) != expected_scopes
        or any(
            type(value) is not int or not 1 <= value <= 604_800
            for value in stale_after_seconds.values()
        )
    ):
        raise OperationsValidationError("invalid lock state probe configuration")
    thresholds = dict(stale_after_seconds)

    def probe(_: float) -> ProbeReading:
        try:
            snapshot = reader()
            observed_at = _utc(clock())
            if not _valid_lock_snapshot(snapshot):
                return ProbeReading.unavailable()
            maximum_age: int | None = None
            unhealthy = snapshot.malformed_count > 0
            for lease in snapshot.held_leases:
                if lease.acquired_at is None:
                    continue
                acquired_at = _utc(lease.acquired_at)
                future_timestamp = acquired_at > observed_at
                age_seconds = max(0, int((observed_at - acquired_at).total_seconds()))
                maximum_age = (
                    age_seconds if maximum_age is None else max(maximum_age, age_seconds)
                )
                if future_timestamp or age_seconds > thresholds[lease.scope]:
                    unhealthy = True
            factory = ProbeReading.unhealthy if unhealthy else ProbeReading.healthy
            return factory(
                count=snapshot.held_count,
                age_seconds=maximum_age,
                observed_at=observed_at,
            )
        except Exception:
            return ProbeReading.unavailable()

    return probe


def schema_probe(*, reader: Callable[[], SchemaSnapshot]) -> DoctorProbe:
    """Compare applied capture/review schemas through a read-only snapshot."""
    if not callable(reader):
        raise OperationsValidationError("invalid schema probe reader")

    def probe(_: float) -> ProbeReading:
        try:
            snapshot = reader()
        except Exception:
            return ProbeReading.unavailable()
        if not isinstance(snapshot, SchemaSnapshot):
            return ProbeReading.unavailable()
        mismatch_count = sum(
            (
                not snapshot.capture_valid
                or snapshot.capture_version != snapshot.expected_capture_version,
                not snapshot.review_valid
                or snapshot.review_version != snapshot.expected_review_version,
            )
        )
        factory = ProbeReading.unhealthy if mismatch_count else ProbeReading.healthy
        return factory(count=mismatch_count)

    return probe


def stale_reference_probe(
    *,
    reader: Callable[[], StaleReferenceSnapshot],
) -> DoctorProbe:
    """Report dangling published-document references from a bounded snapshot."""
    if not callable(reader):
        raise OperationsValidationError("invalid stale-reference probe reader")

    def probe(_: float) -> ProbeReading:
        try:
            snapshot = reader()
        except Exception:
            return ProbeReading.unavailable()
        if not isinstance(snapshot, StaleReferenceSnapshot):
            return ProbeReading.unavailable()
        factory = ProbeReading.unhealthy if snapshot.stale_count else ProbeReading.healthy
        return factory(count=snapshot.stale_count)

    return probe


def backup_evidence_probe(
    *,
    reader: Callable[[], BackupEvidenceSnapshot],
    clock: Clock,
    stale_after_seconds: int,
) -> DoctorProbe:
    """Report missing, malformed, future-dated, or stale published backup evidence."""
    if (
        not callable(reader)
        or not callable(clock)
        or type(stale_after_seconds) is not int
        or not 1 <= stale_after_seconds <= 604_800
    ):
        raise OperationsValidationError("invalid backup evidence probe configuration")

    def probe(_: float) -> ProbeReading:
        try:
            snapshot = reader()
            observed_at = _utc(clock())
        except Exception:
            return ProbeReading.unavailable()
        if not isinstance(snapshot, BackupEvidenceSnapshot):
            return ProbeReading.unavailable()
        expected_profiles = {"capture", "full", "personal", "runtime-state"}
        observed_profiles = {profile.profile for profile in snapshot.profiles}
        issue_count = snapshot.malformed_count + len(expected_profiles - observed_profiles)
        maximum_age: int | None = None
        for profile in snapshot.profiles:
            latest = _utc(profile.latest_created_at)
            future_timestamp = latest > observed_at
            age_seconds = max(0, int((observed_at - latest).total_seconds()))
            maximum_age = age_seconds if maximum_age is None else max(maximum_age, age_seconds)
            if future_timestamp or age_seconds > stale_after_seconds:
                issue_count += 1
        factory = ProbeReading.unhealthy if issue_count else ProbeReading.healthy
        return factory(
            count=issue_count,
            age_seconds=maximum_age,
            observed_at=observed_at,
        )

    return probe


def unavailable_probe() -> DoctorProbe:
    """Return an honest unavailable probe for infrastructure not implemented yet."""

    def probe(_: float) -> ProbeReading:
        return ProbeReading.unavailable()

    return probe


def _valid_writer_record(record: object) -> bool:
    identity_id = getattr(record, "identity_id", None)
    generation = getattr(record, "generation", None)
    recorded_at = getattr(record, "recorded_at", None)
    return (
        isinstance(identity_id, str)
        and _IDENTITY.fullmatch(identity_id) is not None
        and type(generation) is int
        and generation > 0
        and isinstance(recorded_at, datetime)
        and recorded_at.tzinfo is not None
        and recorded_at.utcoffset() is not None
    )


def _valid_queue_snapshot(snapshot: object) -> bool:
    pending_count = getattr(snapshot, "pending_count", None)
    malformed_count = getattr(snapshot, "malformed_count", None)
    oldest = getattr(snapshot, "oldest_captured_at", None)
    return (
        type(pending_count) is int
        and pending_count >= 0
        and type(malformed_count) is int
        and malformed_count >= 0
        and (oldest is None or _is_aware(oldest))
        and (pending_count == 0) is (oldest is None)
    )


def _valid_lock_snapshot(snapshot: object) -> bool:
    held_count = getattr(snapshot, "held_count", None)
    malformed_count = getattr(snapshot, "malformed_count", None)
    held_leases = getattr(snapshot, "held_leases", None)
    if (
        type(held_count) is not int
        or held_count < 0
        or type(malformed_count) is not int
        or malformed_count < 0
        or not isinstance(held_leases, tuple)
        or len(held_leases) != held_count
    ):
        return False
    for lease in held_leases:
        scope = getattr(lease, "scope", None)
        acquired_at = getattr(lease, "acquired_at", None)
        if (
            not isinstance(scope, LockScope)
            or scope is LockScope.NONE
            or acquired_at is not None
            and not _is_aware(acquired_at)
        ):
            return False
    return True


def _utc(value: datetime) -> datetime:
    if not _is_aware(value):
        raise ValueError("invalid probe timestamp")
    return value.astimezone(UTC)


def _is_aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )
