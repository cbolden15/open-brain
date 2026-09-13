"""Fail-closed machine-readable evidence for starting stabilization at day 0."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import NoReturn


class DayZeroEvidenceError(ValueError):
    """The day-0 baseline is incomplete, stale, contradictory, or unbound."""


class JobBindingState(StrEnum):
    HEALTHY = "healthy"
    MANUAL_READY = "manual-ready"


class WriterSurface(StrEnum):
    CAPTURE_INGRESS = "capture-ingress"
    CONTENT = "content"
    INDEX = "index"
    STATE = "state"
    BACKUP = "backup"


class RecoveryRepository(StrEnum):
    PRIMARY = "primary"
    REPLICA = "replica"


class DayZeroCheckName(StrEnum):
    HEALTH = "health"
    QUEUE = "queue"
    REVIEW_APPROVE = "review-approve"
    REVIEW_REJECT = "review-reject"
    REVIEW_EDIT = "review-edit"
    REVIEW_ARCHIVE = "review-archive"
    LEDGER_PUBLICATION = "ledger-publication"
    BACKUPS = "backups"
    REDACTION_PRIVACY = "redaction-privacy"
    NIGHTLY_CYCLE = "nightly-cycle"
    CAPTURE_INTEGRITY = "capture-integrity"
    CLI_QUERY = "cli-query"
    MCP_QUERY = "mcp-query"
    MCP_FEEDBACK = "mcp-feedback"
    UI_READ = "ui-read"
    INGRESS_SHARE = "ingress-share"
    INGRESS_RAW = "ingress-raw"
    INGRESS_PLAYLIST = "ingress-playlist"
    INGRESS_SOCIAL_WEB = "ingress-social-web"
    SCHEDULED_SERVICES = "scheduled-services"
    DISPOSABLE_RESTORE = "disposable-restore"
    SYNCTHING_INTEGRITY = "syncthing-integrity"
    EXECUTABLE_REFERENCE_SCAN = "executable-reference-scan"


EXPECTED_JOB_IDS = tuple(f"JOB-{index:03d}" for index in range(1, 31))
EXPECTED_WRITER_SURFACES = tuple(WriterSurface)
DAY_ZERO_CHECKS = tuple(DayZeroCheckName)
_MANUAL_JOB_IDS = frozenset({"JOB-006", "JOB-009", "JOB-024"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_MAXIMUM_EVIDENCE_AGE = timedelta(hours=4)


@dataclass(frozen=True, slots=True)
class JobBindingEvidence:
    job_id: str
    state: JobBindingState
    evidence_digest_sha256: str


@dataclass(frozen=True, slots=True)
class WriterOwnershipEvidence:
    surface: WriterSurface
    owner_count: int
    evidence_digest_sha256: str


@dataclass(frozen=True, slots=True)
class RecoveryPointEvidence:
    repository: RecoveryRepository
    snapshot_identity_sha256: str
    restore_receipt_digest_sha256: str
    independently_verified: bool


@dataclass(frozen=True, slots=True)
class DayZeroCheck:
    name: DayZeroCheckName
    observed_at: datetime
    evidence_digest_sha256: str


@dataclass(frozen=True, slots=True)
class DayZeroBaseline:
    schema_version: int
    source_commit_sha: str
    agent_config_commit_sha: str
    source_artifact_digest_sha256: str
    sdist_digest_sha256: str
    wheel_digest_sha256: str
    installed_wheel_digest_sha256: str
    open_brain_config_digest_sha256: str
    agent_config_digest_sha256: str
    service_inventory_digest_sha256: str
    helper_digest_sha256: str
    job_bindings: tuple[JobBindingEvidence, ...]
    predecessor_active_count: int
    predecessor_loaded_count: int
    failed_service_count: int
    duplicate_owner_count: int
    missing_owner_count: int
    undrained_queue_count: int
    stale_lease_count: int
    writer_generation: int
    writers: tuple[WriterOwnershipEvidence, ...]
    recovery_points: tuple[RecoveryPointEvidence, ...]
    checks: tuple[DayZeroCheck, ...]
    rollback_available: bool
    rollback_activated: bool
    stabilization_started_at: datetime

    @property
    def digest_sha256(self) -> str:
        validate_day_zero_baseline(self)
        return sha256(_canonical_bytes(self)).hexdigest()


def validate_day_zero_baseline(value: DayZeroBaseline) -> None:
    """Require exact direct evidence for a valid, but not completed, seven-day window."""
    if type(value) is not DayZeroBaseline or value.schema_version != 1:
        _fail("invalid-day-zero-baseline")
    _require_commit(value.source_commit_sha, "invalid-day-zero-source-commit")
    _require_commit(value.agent_config_commit_sha, "invalid-day-zero-agent-config-commit")
    for digest, code in (
        (value.source_artifact_digest_sha256, "invalid-day-zero-source-artifact"),
        (value.sdist_digest_sha256, "invalid-day-zero-sdist"),
        (value.wheel_digest_sha256, "invalid-day-zero-wheel"),
        (value.installed_wheel_digest_sha256, "invalid-day-zero-installed-wheel"),
        (value.open_brain_config_digest_sha256, "invalid-day-zero-open-brain-config"),
        (value.agent_config_digest_sha256, "invalid-day-zero-agent-config"),
        (value.service_inventory_digest_sha256, "invalid-day-zero-service-inventory"),
        (value.helper_digest_sha256, "invalid-day-zero-helper"),
    ):
        _require_digest(digest, code)
    if value.installed_wheel_digest_sha256 != value.wheel_digest_sha256:
        _fail("installed-wheel-mismatch")
    _validate_counts(value)
    _validate_jobs(value.job_bindings)
    _validate_writers(value.writers)
    _validate_recovery(value.recovery_points)
    started_at = _utc(value.stabilization_started_at, "invalid-stabilization-start")
    _validate_checks(value.checks, started_at)
    if value.rollback_available is not True:
        _fail("day-zero-rollback-unavailable")
    if value.rollback_activated is not False:
        _fail("day-zero-rollback-activated")


def _validate_counts(value: DayZeroBaseline) -> None:
    service_counts = (
        value.predecessor_active_count,
        value.predecessor_loaded_count,
        value.failed_service_count,
        value.duplicate_owner_count,
        value.missing_owner_count,
    )
    runtime_counts = (value.undrained_queue_count, value.stale_lease_count)
    if any(type(count) is not int or count < 0 for count in (*service_counts, *runtime_counts)):
        _fail("invalid-day-zero-count")
    if value.predecessor_active_count or value.predecessor_loaded_count:
        _fail("predecessor-service-active")
    if any(service_counts):
        _fail("day-zero-service-inventory-unhealthy")
    if any(runtime_counts):
        _fail("day-zero-runtime-state-unhealthy")
    if type(value.writer_generation) is not int or value.writer_generation < 1:
        _fail("invalid-day-zero-writer-generation")


def _validate_jobs(values: tuple[JobBindingEvidence, ...]) -> None:
    if (
        not isinstance(values, tuple)
        or tuple(value.job_id for value in values) != EXPECTED_JOB_IDS
    ):
        _fail("day-zero-job-inventory-mismatch")
    for value in values:
        if type(value) is not JobBindingEvidence or type(value.state) is not JobBindingState:
            _fail("invalid-day-zero-job-binding")
        expected = (
            JobBindingState.MANUAL_READY
            if value.job_id in _MANUAL_JOB_IDS
            else JobBindingState.HEALTHY
        )
        if value.state is not expected:
            _fail("day-zero-job-state-mismatch")
        _require_digest(value.evidence_digest_sha256, "invalid-day-zero-job-evidence")


def _validate_writers(values: tuple[WriterOwnershipEvidence, ...]) -> None:
    if (
        not isinstance(values, tuple)
        or tuple(value.surface for value in values) != EXPECTED_WRITER_SURFACES
    ):
        _fail("day-zero-writer-inventory-mismatch")
    for value in values:
        if (
            type(value) is not WriterOwnershipEvidence
            or type(value.surface) is not WriterSurface
            or value.owner_count != 1
            or isinstance(value.owner_count, bool)
        ):
            _fail("day-zero-writer-owner-mismatch")
        _require_digest(value.evidence_digest_sha256, "invalid-day-zero-writer-evidence")


def _validate_recovery(values: tuple[RecoveryPointEvidence, ...]) -> None:
    if (
        not isinstance(values, tuple)
        or tuple(value.repository for value in values) != tuple(RecoveryRepository)
    ):
        _fail("day-zero-recovery-inventory-mismatch")
    snapshots: set[str] = set()
    for value in values:
        if (
            type(value) is not RecoveryPointEvidence
            or type(value.repository) is not RecoveryRepository
        ):
            _fail("invalid-day-zero-recovery-point")
        _require_digest(value.snapshot_identity_sha256, "invalid-day-zero-snapshot")
        _require_digest(value.restore_receipt_digest_sha256, "invalid-day-zero-restore")
        if value.independently_verified is not True:
            _fail("day-zero-recovery-not-verified")
        snapshots.add(value.snapshot_identity_sha256)
    if len(snapshots) != len(RecoveryRepository):
        _fail("day-zero-recovery-repositories-not-distinct")


def _validate_checks(values: tuple[DayZeroCheck, ...], started_at: datetime) -> None:
    if (
        not isinstance(values, tuple)
        or tuple(value.name for value in values) != DAY_ZERO_CHECKS
    ):
        _fail("day-zero-check-inventory-mismatch")
    for value in values:
        if type(value) is not DayZeroCheck or type(value.name) is not DayZeroCheckName:
            _fail("invalid-day-zero-check")
        observed_at = _utc(value.observed_at, "invalid-day-zero-check-time")
        if observed_at > started_at:
            _fail("day-zero-check-after-start")
        if started_at - observed_at > _MAXIMUM_EVIDENCE_AGE:
            _fail("day-zero-check-stale")
        _require_digest(value.evidence_digest_sha256, "invalid-day-zero-check-evidence")


def _canonical_bytes(value: DayZeroBaseline) -> bytes:
    payload = {
        "schema_version": value.schema_version,
        "source_commit_sha": value.source_commit_sha,
        "agent_config_commit_sha": value.agent_config_commit_sha,
        "source_artifact_digest_sha256": value.source_artifact_digest_sha256,
        "sdist_digest_sha256": value.sdist_digest_sha256,
        "wheel_digest_sha256": value.wheel_digest_sha256,
        "installed_wheel_digest_sha256": value.installed_wheel_digest_sha256,
        "open_brain_config_digest_sha256": value.open_brain_config_digest_sha256,
        "agent_config_digest_sha256": value.agent_config_digest_sha256,
        "service_inventory_digest_sha256": value.service_inventory_digest_sha256,
        "helper_digest_sha256": value.helper_digest_sha256,
        "job_bindings": [
            {
                "job_id": item.job_id,
                "state": item.state.value,
                "evidence_digest_sha256": item.evidence_digest_sha256,
            }
            for item in value.job_bindings
        ],
        "predecessor_active_count": value.predecessor_active_count,
        "predecessor_loaded_count": value.predecessor_loaded_count,
        "failed_service_count": value.failed_service_count,
        "duplicate_owner_count": value.duplicate_owner_count,
        "missing_owner_count": value.missing_owner_count,
        "undrained_queue_count": value.undrained_queue_count,
        "stale_lease_count": value.stale_lease_count,
        "writer_generation": value.writer_generation,
        "writers": [
            {
                "surface": item.surface.value,
                "owner_count": item.owner_count,
                "evidence_digest_sha256": item.evidence_digest_sha256,
            }
            for item in value.writers
        ],
        "recovery_points": [
            {
                "repository": item.repository.value,
                "snapshot_identity_sha256": item.snapshot_identity_sha256,
                "restore_receipt_digest_sha256": item.restore_receipt_digest_sha256,
                "independently_verified": item.independently_verified,
            }
            for item in value.recovery_points
        ],
        "checks": [
            {
                "name": item.name.value,
                "observed_at": _timestamp(item.observed_at),
                "evidence_digest_sha256": item.evidence_digest_sha256,
            }
            for item in value.checks
        ],
        "rollback_available": value.rollback_available,
        "rollback_activated": value.rollback_activated,
        "stabilization_started_at": _timestamp(value.stabilization_started_at),
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _require_digest(value: object, code: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(code)


def _require_commit(value: object, code: str) -> None:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        _fail(code)


def _utc(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _fail(code)
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value, "invalid-day-zero-timestamp").strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fail(code: str) -> NoReturn:
    raise DayZeroEvidenceError(code)
