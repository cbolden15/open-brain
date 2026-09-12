"""Pure metadata-only P7-W2 ordered cutover rehearsal contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256

from open_brain_legacy.parity.harness import ArtifactAttestationEvidence, EvidenceScope

from .cutover_doctor import (
    CutoverCheckState,
    CutoverDoctorOutcome,
    CutoverDoctorResult,
    CutoverProbeName,
    phase6_cutover_manifest,
)
from .models import OperationsValidationError

CUTOVER_REHEARSAL_VERSION = "phase7-wave2-rehearsal-v1"
GENESIS_RECEIPT_DIGEST_SHA256 = "0" * 64

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^run_[0-9a-f]{16,64}$")


class CutoverSurface(StrEnum):
    CLI_READS = "cli-reads"
    MCP_UI_READS = "mcp-ui-reads"
    IOS_RAW_CAPTURE = "ios-raw-capture"
    YOUTUBE_PLAYLIST = "youtube-playlist"
    SOCIAL_WEB_DRAIN = "social-web-drain"
    LEDGER_REVIEW_JOBS = "ledger-review-jobs"
    SCHEDULED_WRITERS = "scheduled-writers"
    RECOVERY_TOOLING = "recovery-tooling"


class WriterDisposition(StrEnum):
    NOT_APPLICABLE_READ_ONLY = "not-applicable-read-only"
    ONE_SYNTHETIC_WRITER = "one-synthetic-writer"
    NOT_APPLICABLE_TOOLING = "not-applicable-tooling"


@dataclass(frozen=True, slots=True)
class CutoverSurfaceSpec:
    ordinal: int
    surface: CutoverSurface
    label: str
    writer_disposition: WriterDisposition

    def to_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "surface": self.surface.value,
            "label": self.label,
            "writer_disposition": self.writer_disposition.value,
        }


CUTOVER_SURFACE_MANIFEST = (
    CutoverSurfaceSpec(
        1,
        CutoverSurface.CLI_READS,
        "read-only CLI status/doctor/query",
        WriterDisposition.NOT_APPLICABLE_READ_ONLY,
    ),
    CutoverSurfaceSpec(
        2,
        CutoverSurface.MCP_UI_READS,
        "MCP and UI reads",
        WriterDisposition.NOT_APPLICABLE_READ_ONLY,
    ),
    CutoverSurfaceSpec(
        3,
        CutoverSurface.IOS_RAW_CAPTURE,
        "iOS share endpoint and raw capture",
        WriterDisposition.ONE_SYNTHETIC_WRITER,
    ),
    CutoverSurfaceSpec(
        4,
        CutoverSurface.YOUTUBE_PLAYLIST,
        "YouTube playlist poller",
        WriterDisposition.ONE_SYNTHETIC_WRITER,
    ),
    CutoverSurfaceSpec(
        5,
        CutoverSurface.SOCIAL_WEB_DRAIN,
        "social/web drain",
        WriterDisposition.ONE_SYNTHETIC_WRITER,
    ),
    CutoverSurfaceSpec(
        6,
        CutoverSurface.LEDGER_REVIEW_JOBS,
        "ledger and review jobs",
        WriterDisposition.ONE_SYNTHETIC_WRITER,
    ),
    CutoverSurfaceSpec(
        7,
        CutoverSurface.SCHEDULED_WRITERS,
        "remaining scheduled writers",
        WriterDisposition.ONE_SYNTHETIC_WRITER,
    ),
    CutoverSurfaceSpec(
        8,
        CutoverSurface.RECOVERY_TOOLING,
        "backups, retention, and restore tooling",
        WriterDisposition.NOT_APPLICABLE_TOOLING,
    ),
)


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise OperationsValidationError("invalid canonical cutover value") from None


def _digest_value(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise OperationsValidationError(f"invalid {label}")
    return value


def _utc(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise OperationsValidationError(f"invalid {label}")
    normalized = value.astimezone(UTC)
    if normalized.utcoffset() != UTC.utcoffset(normalized):
        raise OperationsValidationError(f"invalid {label}")
    return normalized


def _timestamp(value: datetime) -> str:
    return _utc(value, "cutover timestamp").strftime("%Y-%m-%dT%H:%M:%S.%fZ")


CUTOVER_MANIFEST_DIGEST_SHA256 = _digest_value(
    {
        "manifest_version": CUTOVER_REHEARSAL_VERSION,
        "surfaces": [spec.to_dict() for spec in CUTOVER_SURFACE_MANIFEST],
    }
)


class RehearsalScope(StrEnum):
    SYNTHETIC_ONLY = "synthetic-rehearsal-only"


class OwnerGateState(StrEnum):
    NOT_PERFORMED_OWNER_GATED = "not-performed-owner-gated"


class SurfaceCheckOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class CutoverDiagnosticClass(StrEnum):
    NONE = "none"
    SYNTHETIC_CHECK_FAILED = "synthetic-check-failed"
    ROLLBACK_STAGE_FAILED = "rollback-stage-failed"
    CONTRADICTORY_EVIDENCE = "contradictory-evidence"
    OWNER_GATE_CLOSED = "owner-gate-closed"


class ForwardStage(StrEnum):
    SNAPSHOT = "snapshot"
    OLD_WRITER_DISPOSITION = "old-writer-disposition"
    NEW_SERVICE_ENABLED = "new-service-enabled"
    ONE_WRITER_PROOF = "one-writer-proof"
    SYNTHETIC_SMOKE = "synthetic-smoke"
    VERIFICATION = "verification"
    GREEN = "green"


FORWARD_STAGES = tuple(ForwardStage)


class RollbackTrigger(StrEnum):
    DATA_LOSS_OR_DUPLICATE_WRITE = "data-loss-or-duplicate-write"
    PRIVACY_TIER_MISMATCH = "privacy-tier-mismatch"
    UNREDACTED_LOG_RESIDUE = "unredacted-log-residue"
    QUEUE_NOT_DRAINABLE_OR_RECOVERABLE = "queue-not-drainable-or-recoverable"
    REVIEW_GATE_BYPASS = "review-gate-bypass"
    REQUIRED_HEALTH_RED = "required-health-red"


class RollbackStage(StrEnum):
    ROLLBACK_TRIGGERED = "rollback-triggered"
    NEW_SERVICE_DISABLED = "new-service-disabled"
    SNAPSHOT_RESTORE_DISPOSITION = "snapshot-restore-disposition"
    OLD_SERVICE_REENABLED_DISPOSITION = "old-service-reenabled-disposition"
    REDACTED_DIAGNOSTIC_PRESERVED = "redacted-diagnostic-preserved"
    ROLLBACK_VERIFIED = "rollback-verified"


ROLLBACK_STAGES = tuple(RollbackStage)


class RollbackDisposition(StrEnum):
    TRIGGER_RECORDED = "trigger-recorded"
    DISABLED_SYNTHETIC = "disabled-synthetic"
    RESTORED_SYNTHETIC = "restored-synthetic"
    NOT_REQUIRED = "not-required"
    REENABLED_SYNTHETIC = "reenabled-synthetic"
    NOT_APPLICABLE_READ_ONLY = "not-applicable-read-only"
    NOT_APPLICABLE_TOOLING = "not-applicable-tooling"
    PRESERVED_REDACTED = "preserved-redacted"
    VERIFIED_SYNTHETIC = "verified-synthetic"


class ReceiptChainKind(StrEnum):
    FORWARD = "forward"
    ROLLBACK = "rollback"


class ReceiptOutcome(StrEnum):
    PASSED = "passed"
    BLOCKED = "blocked"


class RehearsalOutcome(StrEnum):
    SYNTHETIC_GREEN = "synthetic-green"
    ROLLED_BACK_SYNTHETIC = "rolled-back-synthetic"
    BLOCKED = "blocked"
    ROLLBACK_BLOCKED = "rollback-blocked"


class NegativeCase(StrEnum):
    NONE = "none"
    SURFACE_NOT_REQUESTED = "surface-not-requested"
    WRITER_EVIDENCE_INVALID = "writer-evidence-invalid"
    SMOKE_FAILED_WITHOUT_TRIGGER = "smoke-failed-without-trigger"
    VERIFICATION_FAILED_WITHOUT_TRIGGER = "verification-failed-without-trigger"
    ROLLBACK_TRIGGER_MISSING = "rollback-trigger-missing"
    ROLLBACK_ATTEMPT_MISSING = "rollback-attempt-missing"
    ROLLBACK_STAGE_MISSING = "rollback-stage-missing"
    ROLLBACK_STAGE_FAILED = "rollback-stage-failed"
    ROLLBACK_STAGE_REORDERED = "rollback-stage-reordered"
    ROLLBACK_STAGE_DUPLICATED = "rollback-stage-duplicated"
    ROLLBACK_STAGE_AFTER_FAILURE = "rollback-stage-after-failure"
    LATER_SURFACE_AFTER_TRIGGER = "later-surface-after-trigger"
    INCOMPATIBLE_STATE_ON_NON_WRITER = "incompatible-state-on-non-writer"
    RESTORE_DISPOSITION_INVALID = "restore-disposition-invalid"
    REENABLE_DISPOSITION_INVALID = "reenable-disposition-invalid"


@dataclass(frozen=True, slots=True)
class RollbackStageAttempt:
    stage: RollbackStage
    outcome: SurfaceCheckOutcome
    disposition: RollbackDisposition
    diagnostic_class: CutoverDiagnosticClass = CutoverDiagnosticClass.NONE

    def __post_init__(self) -> None:
        if (
            not isinstance(self.stage, RollbackStage)
            or not isinstance(self.outcome, SurfaceCheckOutcome)
            or not isinstance(self.disposition, RollbackDisposition)
            or not isinstance(self.diagnostic_class, CutoverDiagnosticClass)
        ):
            raise OperationsValidationError("invalid rollback stage attempt")

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "outcome": self.outcome.value,
            "disposition": self.disposition.value,
            "diagnostic_class": self.diagnostic_class.value,
        }


@dataclass(frozen=True, slots=True)
class SyntheticRollbackAttempt:
    stages: tuple[RollbackStageAttempt, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.stages, tuple) or any(
            type(stage) is not RollbackStageAttempt for stage in self.stages
        ):
            raise OperationsValidationError("invalid rollback attempt")

    def to_dict(self) -> dict[str, object]:
        return {"stages": [stage.to_dict() for stage in self.stages]}


@dataclass(frozen=True, slots=True)
class SyntheticSurfaceScenario:
    surface: CutoverSurface
    ordinal: int
    attempt: int
    attempt_requested: bool
    writer_disposition: WriterDisposition
    writer_identity_digest_sha256: str | None
    legacy_writer_count: int
    smoke_outcome: SurfaceCheckOutcome
    verification_outcome: SurfaceCheckOutcome
    rollback_trigger: RollbackTrigger | None
    incompatible_state_written: bool
    diagnostic_class: CutoverDiagnosticClass
    rollback_attempt: SyntheticRollbackAttempt | None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.surface, CutoverSurface)
            or not isinstance(self.ordinal, int)
            or isinstance(self.ordinal, bool)
            or not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or self.attempt < 1
            or not isinstance(self.attempt_requested, bool)
            or not isinstance(self.writer_disposition, WriterDisposition)
            or not isinstance(self.legacy_writer_count, int)
            or isinstance(self.legacy_writer_count, bool)
            or self.legacy_writer_count < 0
            or not isinstance(self.smoke_outcome, SurfaceCheckOutcome)
            or not isinstance(self.verification_outcome, SurfaceCheckOutcome)
            or (
                self.rollback_trigger is not None
                and not isinstance(self.rollback_trigger, RollbackTrigger)
            )
            or not isinstance(self.incompatible_state_written, bool)
            or not isinstance(self.diagnostic_class, CutoverDiagnosticClass)
            or (
                self.rollback_attempt is not None
                and type(self.rollback_attempt) is not SyntheticRollbackAttempt
            )
        ):
            raise OperationsValidationError("invalid synthetic surface scenario")
        if self.writer_identity_digest_sha256 is not None:
            _require_digest(self.writer_identity_digest_sha256, "writer identity digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "surface": self.surface.value,
            "ordinal": self.ordinal,
            "attempt": self.attempt,
            "attempt_requested": self.attempt_requested,
            "writer_disposition": self.writer_disposition.value,
            "writer_identity_digest_sha256": self.writer_identity_digest_sha256,
            "legacy_writer_count": self.legacy_writer_count,
            "smoke_outcome": self.smoke_outcome.value,
            "verification_outcome": self.verification_outcome.value,
            "rollback_trigger": (
                None if self.rollback_trigger is None else self.rollback_trigger.value
            ),
            "incompatible_state_written": self.incompatible_state_written,
            "diagnostic_class": self.diagnostic_class.value,
            "rollback_attempt": (
                None if self.rollback_attempt is None else self.rollback_attempt.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class SyntheticCutoverScenario:
    run_id: str
    manifest_version: str
    manifest_digest_sha256: str
    input_digest_sha256: str
    snapshot_digest_sha256: str
    surfaces: tuple[SyntheticSurfaceScenario, ...]
    resume_from: CutoverSurface | None
    scope: RehearsalScope = RehearsalScope.SYNTHETIC_ONLY
    real_capture_gate: OwnerGateState = OwnerGateState.NOT_PERFORMED_OWNER_GATED
    live_transition_gate: OwnerGateState = OwnerGateState.NOT_PERFORMED_OWNER_GATED

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.run_id, str) or _RUN_ID.fullmatch(self.run_id) is None:
            raise OperationsValidationError("invalid cutover run ID")
        if (
            self.manifest_version != CUTOVER_REHEARSAL_VERSION
            or self.manifest_digest_sha256 != CUTOVER_MANIFEST_DIGEST_SHA256
        ):
            raise OperationsValidationError("invalid cutover manifest binding")
        _require_digest(self.input_digest_sha256, "cutover input digest")
        _require_digest(self.snapshot_digest_sha256, "cutover snapshot digest")
        if (
            not isinstance(self.surfaces, tuple)
            or len(self.surfaces) != len(CUTOVER_SURFACE_MANIFEST)
            or any(type(surface) is not SyntheticSurfaceScenario for surface in self.surfaces)
        ):
            raise OperationsValidationError("invalid cutover surface scenarios")
        for supplied, expected in zip(self.surfaces, CUTOVER_SURFACE_MANIFEST, strict=True):
            if supplied.surface is not expected.surface or supplied.ordinal != expected.ordinal:
                raise OperationsValidationError("invalid cutover surface order")
        if self.resume_from is not None and not isinstance(self.resume_from, CutoverSurface):
            raise OperationsValidationError("invalid cutover resume surface")
        if (
            self.scope is not RehearsalScope.SYNTHETIC_ONLY
            or self.real_capture_gate is not OwnerGateState.NOT_PERFORMED_OWNER_GATED
            or self.live_transition_gate is not OwnerGateState.NOT_PERFORMED_OWNER_GATED
        ):
            raise OperationsValidationError("cutover owner gate is not closed")

    def _binding_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "manifest_version": self.manifest_version,
            "manifest_digest_sha256": self.manifest_digest_sha256,
            "input_digest_sha256": self.input_digest_sha256,
            "snapshot_digest_sha256": self.snapshot_digest_sha256,
            "surfaces": [surface.to_dict() for surface in self.surfaces],
            "scope": self.scope.value,
            "real_capture_gate": self.real_capture_gate.value,
            "live_transition_gate": self.live_transition_gate.value,
        }

    @property
    def scenario_digest_sha256(self) -> str:
        # The resume cursor advances between invocations; the immutable plan does not.
        return _digest_value(self._binding_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            **self._binding_dict(),
            "resume_from": None if self.resume_from is None else self.resume_from.value,
            "scenario_digest_sha256": self.scenario_digest_sha256,
        }


@dataclass(frozen=True, slots=True)
class CutoverReceipt:
    schema_version: int
    manifest_version: str
    manifest_digest_sha256: str
    run_id: str
    surface: CutoverSurface
    ordinal: int
    attempt: int
    chain_kind: ReceiptChainKind
    stage: ForwardStage | RollbackStage
    receipt_id: str
    idempotency_key_sha256: str
    predecessor_receipt_digest_sha256: str
    receipt_digest_sha256: str
    input_digest_sha256: str
    snapshot_digest_sha256: str
    artifact_digest_sha256: str
    artifact_attestation_digest_sha256: str
    preflight_digest_sha256: str
    scenario_digest_sha256: str
    stage_payload_digest_sha256: str
    outcome: ReceiptOutcome
    writer_disposition: WriterDisposition | None
    rollback_disposition: RollbackDisposition | None
    diagnostic_class: CutoverDiagnosticClass
    evaluated_at: datetime
    scope: RehearsalScope
    real_capture_gate: OwnerGateState
    live_transition_gate: OwnerGateState
    redacted: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_version": self.manifest_version,
            "manifest_digest_sha256": self.manifest_digest_sha256,
            "run_id": self.run_id,
            "surface": self.surface.value,
            "ordinal": self.ordinal,
            "attempt": self.attempt,
            "chain_kind": self.chain_kind.value,
            "stage": self.stage.value,
            "receipt_id": self.receipt_id,
            "idempotency_key_sha256": self.idempotency_key_sha256,
            "predecessor_receipt_digest_sha256": self.predecessor_receipt_digest_sha256,
            "receipt_digest_sha256": self.receipt_digest_sha256,
            "input_digest_sha256": self.input_digest_sha256,
            "snapshot_digest_sha256": self.snapshot_digest_sha256,
            "artifact_digest_sha256": self.artifact_digest_sha256,
            "artifact_attestation_digest_sha256": (
                self.artifact_attestation_digest_sha256
            ),
            "preflight_digest_sha256": self.preflight_digest_sha256,
            "scenario_digest_sha256": self.scenario_digest_sha256,
            "stage_payload_digest_sha256": self.stage_payload_digest_sha256,
            "outcome": self.outcome.value,
            "writer_disposition": (
                None if self.writer_disposition is None else self.writer_disposition.value
            ),
            "rollback_disposition": (
                None if self.rollback_disposition is None else self.rollback_disposition.value
            ),
            "diagnostic_class": self.diagnostic_class.value,
            "evaluated_at": _timestamp(self.evaluated_at),
            "scope": self.scope.value,
            "real_capture_gate": self.real_capture_gate.value,
            "live_transition_gate": self.live_transition_gate.value,
            "redacted": self.redacted,
        }


@dataclass(frozen=True, slots=True)
class PriorReceiptLedger:
    receipts: tuple[CutoverReceipt, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.receipts, tuple) or any(
            type(receipt) is not CutoverReceipt for receipt in self.receipts
        ):
            raise OperationsValidationError("invalid prior receipt ledger")

    @classmethod
    def empty(cls) -> PriorReceiptLedger:
        return cls(())

    @property
    def ledger_digest_sha256(self) -> str:
        return _digest_value(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {"receipts": [receipt.to_dict() for receipt in self.receipts]}


@dataclass(frozen=True, slots=True, init=False)
class CutoverRehearsalResult:
    _outcome: RehearsalOutcome
    _receipts: tuple[CutoverReceipt, ...]
    _negative_case: NegativeCase
    _manifest_digest_sha256: str
    _preflight_digest_sha256: str
    _scenario_digest_sha256: str
    _prior_ledger_digest_sha256: str
    _artifact_digest_sha256: str
    _artifact_attestation_digest_sha256: str
    _evaluated_at: datetime
    _result_digest_sha256: str

    def __init__(self) -> None:
        raise TypeError("CutoverRehearsalResult is reducer-created")

    @classmethod
    def _create(
        cls,
        *,
        outcome: RehearsalOutcome,
        receipts: tuple[CutoverReceipt, ...],
        negative_case: NegativeCase,
        manifest_digest_sha256: str,
        preflight_digest_sha256: str,
        scenario_digest_sha256: str,
        prior_ledger_digest_sha256: str,
        artifact_digest_sha256: str,
        artifact_attestation_digest_sha256: str,
        evaluated_at: datetime,
    ) -> CutoverRehearsalResult:
        result = cls.__new__(cls)
        values = {
            "_outcome": outcome,
            "_receipts": receipts,
            "_negative_case": negative_case,
            "_manifest_digest_sha256": manifest_digest_sha256,
            "_preflight_digest_sha256": preflight_digest_sha256,
            "_scenario_digest_sha256": scenario_digest_sha256,
            "_prior_ledger_digest_sha256": prior_ledger_digest_sha256,
            "_artifact_digest_sha256": artifact_digest_sha256,
            "_artifact_attestation_digest_sha256": artifact_attestation_digest_sha256,
            "_evaluated_at": evaluated_at,
        }
        for name, value in values.items():
            object.__setattr__(result, name, value)
        object.__setattr__(result, "_result_digest_sha256", _digest_value(result._payload()))
        result._validate()
        return result

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "manifest_version": CUTOVER_REHEARSAL_VERSION,
            "manifest_digest_sha256": self._manifest_digest_sha256,
            "outcome": self._outcome.value,
            "resolved": self._outcome is RehearsalOutcome.SYNTHETIC_GREEN,
            "negative_case": self._negative_case.value,
            "receipts": [receipt.to_dict() for receipt in self._receipts],
            "preflight_digest_sha256": self._preflight_digest_sha256,
            "scenario_digest_sha256": self._scenario_digest_sha256,
            "prior_ledger_digest_sha256": self._prior_ledger_digest_sha256,
            "artifact_digest_sha256": self._artifact_digest_sha256,
            "artifact_attestation_digest_sha256": (
                self._artifact_attestation_digest_sha256
            ),
            "evaluated_at": _timestamp(self._evaluated_at),
            "scope": RehearsalScope.SYNTHETIC_ONLY.value,
            "real_capture_gate": OwnerGateState.NOT_PERFORMED_OWNER_GATED.value,
            "live_transition_gate": OwnerGateState.NOT_PERFORMED_OWNER_GATED.value,
            "production_cutover_gate": OwnerGateState.NOT_PERFORMED_OWNER_GATED.value,
            "redacted": True,
        }

    def _validate(self) -> None:
        if (
            not isinstance(self._outcome, RehearsalOutcome)
            or not isinstance(self._negative_case, NegativeCase)
            or not isinstance(self._receipts, tuple)
            or any(type(receipt) is not CutoverReceipt for receipt in self._receipts)
            or self._manifest_digest_sha256 != CUTOVER_MANIFEST_DIGEST_SHA256
        ):
            raise OperationsValidationError("invalid cutover rehearsal result")
        for value, label in (
            (self._preflight_digest_sha256, "preflight digest"),
            (self._scenario_digest_sha256, "scenario digest"),
            (self._prior_ledger_digest_sha256, "prior ledger digest"),
            (self._artifact_digest_sha256, "artifact digest"),
            (self._artifact_attestation_digest_sha256, "artifact attestation digest"),
            (self._result_digest_sha256, "result digest"),
        ):
            _require_digest(value, label)
        if self._result_digest_sha256 != _digest_value(self._payload()):
            raise OperationsValidationError("invalid cutover result digest")

    @property
    def outcome(self) -> RehearsalOutcome:
        self._validate()
        return self._outcome

    @property
    def resolved(self) -> bool:
        self._validate()
        return self._outcome is RehearsalOutcome.SYNTHETIC_GREEN

    @property
    def receipts(self) -> tuple[CutoverReceipt, ...]:
        self._validate()
        return self._receipts

    @property
    def negative_case(self) -> NegativeCase:
        self._validate()
        return self._negative_case

    @property
    def result_digest_sha256(self) -> str:
        self._validate()
        return self._result_digest_sha256

    @property
    def prior_ledger_digest_sha256(self) -> str:
        self._validate()
        return self._prior_ledger_digest_sha256

    def to_dict(self) -> dict[str, object]:
        self._validate()
        return {**self._payload(), "result_digest_sha256": self._result_digest_sha256}


@dataclass(frozen=True, slots=True)
class _Context:
    preflight_digest_sha256: str
    scenario_digest_sha256: str
    artifact_digest_sha256: str
    artifact_attestation_digest_sha256: str


def _artifact_dict(value: ArtifactAttestationEvidence) -> dict[str, object]:
    if type(value) is not ArtifactAttestationEvidence:
        raise OperationsValidationError("invalid artifact attestation evidence")
    try:
        evidence = value.to_dict()
        artifact = value.artifact.to_dict()
    except Exception:
        raise OperationsValidationError("invalid artifact attestation evidence") from None
    return {**evidence, "artifact": artifact}


def _validate_artifact_attestation(
    value: ArtifactAttestationEvidence,
    evaluated_at: datetime,
) -> None:
    _artifact_dict(value)
    attested_at = _utc(value.evaluated_at, "artifact attestation evaluation time")
    expires_at = _utc(value.expires_at, "artifact attestation expiry")
    if (
        value.scope is not EvidenceScope.SYNTHETIC
        or evaluated_at < attested_at
        or evaluated_at >= expires_at
    ):
        raise OperationsValidationError("artifact attestation is not current")


def _validate_preflight(value: CutoverDoctorResult) -> str:
    if type(value) is not CutoverDoctorResult:
        raise OperationsValidationError("invalid cutover preflight")
    manifest = phase6_cutover_manifest()
    if (
        value.schema_version != 2
        or value.manifest_version != manifest.schema_version
        or value.manifest_digest != manifest.manifest_digest
        or value.outcome is not CutoverDoctorOutcome.SYNTHETIC_READY
        or value.synthetic_ready is not True
        or value.cutover_ready is not False
        or value.exit_code != 0
        or tuple(check.probe for check in value.checks) != tuple(CutoverProbeName)
        or any(
            check.state is not CutoverCheckState.HEALTHY or check.findings
            for check in value.checks
        )
    ):
        raise OperationsValidationError("cutover preflight is not synthetic-ready")
    return _digest_value(value.to_dict())


def _idempotency_key(
    scenario: SyntheticCutoverScenario,
    surface: SyntheticSurfaceScenario,
    chain_kind: ReceiptChainKind,
    stage: ForwardStage | RollbackStage,
) -> str:
    return _digest_value(
        {
            "schema_version": 1,
            "manifest_version": CUTOVER_REHEARSAL_VERSION,
            "run_id": scenario.run_id,
            "surface": surface.surface.value,
            "ordinal": surface.ordinal,
            "attempt": surface.attempt,
            "chain_kind": chain_kind.value,
            "stage": stage.value,
        }
    )


def _receipt_digest(receipt: CutoverReceipt) -> str:
    payload = receipt.to_dict()
    payload.pop("receipt_digest_sha256")
    return _digest_value(payload)


def _new_receipt(
    *,
    scenario: SyntheticCutoverScenario,
    surface: SyntheticSurfaceScenario,
    context: _Context,
    chain_kind: ReceiptChainKind,
    stage: ForwardStage | RollbackStage,
    predecessor: str,
    outcome: ReceiptOutcome,
    evaluated_at: datetime,
    rollback_disposition: RollbackDisposition | None = None,
    rollback_attempt: RollbackStageAttempt | None = None,
) -> CutoverReceipt:
    idempotency_key = _idempotency_key(scenario, surface, chain_kind, stage)
    stage_payload = {
        "surface": surface.to_dict(),
        "chain_kind": chain_kind.value,
        "stage": stage.value,
        "rollback_attempt": (
            None if rollback_attempt is None else rollback_attempt.to_dict()
        ),
    }
    receipt = CutoverReceipt(
        schema_version=1,
        manifest_version=CUTOVER_REHEARSAL_VERSION,
        manifest_digest_sha256=CUTOVER_MANIFEST_DIGEST_SHA256,
        run_id=scenario.run_id,
        surface=surface.surface,
        ordinal=surface.ordinal,
        attempt=surface.attempt,
        chain_kind=chain_kind,
        stage=stage,
        receipt_id=f"rcpt_{idempotency_key[:32]}",
        idempotency_key_sha256=idempotency_key,
        predecessor_receipt_digest_sha256=predecessor,
        receipt_digest_sha256=GENESIS_RECEIPT_DIGEST_SHA256,
        input_digest_sha256=scenario.input_digest_sha256,
        snapshot_digest_sha256=scenario.snapshot_digest_sha256,
        artifact_digest_sha256=context.artifact_digest_sha256,
        artifact_attestation_digest_sha256=(
            context.artifact_attestation_digest_sha256
        ),
        preflight_digest_sha256=context.preflight_digest_sha256,
        scenario_digest_sha256=context.scenario_digest_sha256,
        stage_payload_digest_sha256=_digest_value(stage_payload),
        outcome=outcome,
        writer_disposition=(
            surface.writer_disposition if chain_kind is ReceiptChainKind.FORWARD else None
        ),
        rollback_disposition=rollback_disposition,
        diagnostic_class=surface.diagnostic_class,
        evaluated_at=evaluated_at,
        scope=RehearsalScope.SYNTHETIC_ONLY,
        real_capture_gate=OwnerGateState.NOT_PERFORMED_OWNER_GATED,
        live_transition_gate=OwnerGateState.NOT_PERFORMED_OWNER_GATED,
        redacted=True,
    )
    return replace(receipt, receipt_digest_sha256=_receipt_digest(receipt))


def _validate_receipt(
    receipt: CutoverReceipt,
    *,
    scenario: SyntheticCutoverScenario,
    context: _Context,
    predecessor: str,
) -> None:
    if not 1 <= receipt.ordinal <= len(scenario.surfaces):
        raise OperationsValidationError("invalid cutover receipt ordinal")
    surface = scenario.surfaces[receipt.ordinal - 1]
    expected_stage_type = (
        ForwardStage if receipt.chain_kind is ReceiptChainKind.FORWARD else RollbackStage
    )
    rollback_attempt = None
    if (
        receipt.chain_kind is ReceiptChainKind.ROLLBACK
        and surface.rollback_trigger is None
    ):
        raise OperationsValidationError("rollback receipt has no closed trigger")
    if (
        receipt.chain_kind is ReceiptChainKind.ROLLBACK
        and surface.rollback_attempt is not None
    ):
        rollback_attempt = next(
            (
                item
                for item in surface.rollback_attempt.stages
                if item.stage is receipt.stage
            ),
            None,
        )
    stage_payload_digest = _digest_value(
        {
            "surface": surface.to_dict(),
            "chain_kind": receipt.chain_kind.value,
            "stage": receipt.stage.value,
            "rollback_attempt": (
                None if rollback_attempt is None else rollback_attempt.to_dict()
            ),
        }
    )
    expected_outcome = ReceiptOutcome.PASSED
    expected_writer_disposition: WriterDisposition | None = None
    expected_rollback_disposition: RollbackDisposition | None = None
    if receipt.chain_kind is ReceiptChainKind.FORWARD:
        expected_writer_disposition = surface.writer_disposition
        if receipt.stage is ForwardStage.ONE_WRITER_PROOF:
            expected_outcome = (
                ReceiptOutcome.PASSED
                if _writer_evidence_valid(surface)
                else ReceiptOutcome.BLOCKED
            )
        elif receipt.stage is ForwardStage.SYNTHETIC_SMOKE:
            expected_outcome = (
                ReceiptOutcome.PASSED
                if surface.smoke_outcome is SurfaceCheckOutcome.PASSED
                else ReceiptOutcome.BLOCKED
            )
        elif receipt.stage is ForwardStage.VERIFICATION:
            expected_outcome = (
                ReceiptOutcome.PASSED
                if surface.verification_outcome is SurfaceCheckOutcome.PASSED
                else ReceiptOutcome.BLOCKED
            )
        elif receipt.stage is ForwardStage.GREEN:
            expected_outcome = (
                ReceiptOutcome.PASSED
                if _writer_evidence_valid(surface)
                and surface.smoke_outcome is SurfaceCheckOutcome.PASSED
                and surface.verification_outcome is SurfaceCheckOutcome.PASSED
                and surface.rollback_trigger is None
                else ReceiptOutcome.BLOCKED
            )
    elif rollback_attempt is not None:
        expected_rollback_disposition = rollback_attempt.disposition
        expected_outcome = (
            ReceiptOutcome.PASSED
            if rollback_attempt.outcome is SurfaceCheckOutcome.PASSED
            else ReceiptOutcome.BLOCKED
        )
    if (
        receipt.schema_version != 1
        or receipt.manifest_version != CUTOVER_REHEARSAL_VERSION
        or receipt.manifest_digest_sha256 != CUTOVER_MANIFEST_DIGEST_SHA256
        or receipt.run_id != scenario.run_id
        or not isinstance(receipt.stage, expected_stage_type)
        or receipt.receipt_id != f"rcpt_{receipt.idempotency_key_sha256[:32]}"
        or receipt.idempotency_key_sha256
        != _idempotency_key(
            scenario,
            surface,
            receipt.chain_kind,
            receipt.stage,
        )
        or receipt.predecessor_receipt_digest_sha256 != predecessor
        or receipt.input_digest_sha256 != scenario.input_digest_sha256
        or receipt.snapshot_digest_sha256 != scenario.snapshot_digest_sha256
        or receipt.artifact_digest_sha256 != context.artifact_digest_sha256
        or receipt.artifact_attestation_digest_sha256
        != context.artifact_attestation_digest_sha256
        or receipt.preflight_digest_sha256 != context.preflight_digest_sha256
        or receipt.scenario_digest_sha256 != context.scenario_digest_sha256
        or receipt.stage_payload_digest_sha256 != stage_payload_digest
        or receipt.outcome is not expected_outcome
        or receipt.writer_disposition is not expected_writer_disposition
        or receipt.rollback_disposition is not expected_rollback_disposition
        or receipt.diagnostic_class is not surface.diagnostic_class
        or receipt.scope is not RehearsalScope.SYNTHETIC_ONLY
        or receipt.real_capture_gate is not OwnerGateState.NOT_PERFORMED_OWNER_GATED
        or receipt.live_transition_gate is not OwnerGateState.NOT_PERFORMED_OWNER_GATED
        or receipt.redacted is not True
        or _utc(receipt.evaluated_at, "receipt evaluation time") != receipt.evaluated_at
        or receipt.receipt_digest_sha256 != _receipt_digest(receipt)
    ):
        raise OperationsValidationError("invalid or conflicting cutover receipt")


def _writer_evidence_valid(surface: SyntheticSurfaceScenario) -> bool:
    expected = CUTOVER_SURFACE_MANIFEST[surface.ordinal - 1].writer_disposition
    if surface.writer_disposition is not expected:
        return False
    if expected is WriterDisposition.ONE_SYNTHETIC_WRITER:
        return (
            surface.writer_identity_digest_sha256 is not None
            and surface.legacy_writer_count == 0
        )
    return surface.writer_identity_digest_sha256 is None and surface.legacy_writer_count == 0


def _expected_restore_disposition(
    surface: SyntheticSurfaceScenario,
) -> RollbackDisposition:
    if surface.writer_disposition is not WriterDisposition.ONE_SYNTHETIC_WRITER:
        return RollbackDisposition.NOT_REQUIRED
    if surface.incompatible_state_written:
        return RollbackDisposition.RESTORED_SYNTHETIC
    return RollbackDisposition.NOT_REQUIRED


def _expected_reenable_disposition(
    surface: SyntheticSurfaceScenario,
) -> RollbackDisposition:
    if surface.writer_disposition is WriterDisposition.ONE_SYNTHETIC_WRITER:
        return RollbackDisposition.REENABLED_SYNTHETIC
    if surface.writer_disposition is WriterDisposition.NOT_APPLICABLE_READ_ONLY:
        return RollbackDisposition.NOT_APPLICABLE_READ_ONLY
    return RollbackDisposition.NOT_APPLICABLE_TOOLING


def _expected_rollback_disposition(
    surface: SyntheticSurfaceScenario,
    stage: RollbackStage,
) -> RollbackDisposition:
    if stage is RollbackStage.ROLLBACK_TRIGGERED:
        return RollbackDisposition.TRIGGER_RECORDED
    if stage is RollbackStage.NEW_SERVICE_DISABLED:
        return RollbackDisposition.DISABLED_SYNTHETIC
    if stage is RollbackStage.SNAPSHOT_RESTORE_DISPOSITION:
        return _expected_restore_disposition(surface)
    if stage is RollbackStage.OLD_SERVICE_REENABLED_DISPOSITION:
        return _expected_reenable_disposition(surface)
    if stage is RollbackStage.REDACTED_DIAGNOSTIC_PRESERVED:
        return RollbackDisposition.PRESERVED_REDACTED
    return RollbackDisposition.VERIFIED_SYNTHETIC


def _prior_position(
    receipts: tuple[CutoverReceipt, ...],
) -> tuple[int, int, bool]:
    surface_index = 0
    forward_index = 0
    rollback_index = 0
    in_rollback = False
    for receipt in receipts:
        if surface_index >= len(CUTOVER_SURFACE_MANIFEST):
            raise OperationsValidationError("receipt exists after terminal cutover state")
        expected_surface = CUTOVER_SURFACE_MANIFEST[surface_index]
        if (
            receipt.surface is not expected_surface.surface
            or receipt.ordinal != expected_surface.ordinal
        ):
            raise OperationsValidationError("invalid prior receipt surface order")
        if receipt.chain_kind is ReceiptChainKind.FORWARD and not in_rollback:
            if receipt.stage is not FORWARD_STAGES[forward_index]:
                raise OperationsValidationError("invalid prior forward receipt order")
            forward_index += 1
            if receipt.stage is ForwardStage.GREEN:
                surface_index += 1
                forward_index = 0
        elif receipt.chain_kind is ReceiptChainKind.ROLLBACK:
            in_rollback = True
            if forward_index != len(FORWARD_STAGES) - 1:
                raise OperationsValidationError("rollback began before forward verification")
            if receipt.stage is not ROLLBACK_STAGES[rollback_index]:
                raise OperationsValidationError("invalid prior rollback receipt order")
            rollback_index += 1
        else:
            raise OperationsValidationError("invalid receipt after rollback began")
    if in_rollback and rollback_index != len(ROLLBACK_STAGES):
        raise OperationsValidationError("cannot resume incomplete rollback")
    return surface_index, forward_index, in_rollback


def _validate_prior_ledger(
    ledger: PriorReceiptLedger,
    *,
    scenario: SyntheticCutoverScenario,
    context: _Context,
) -> tuple[int, int, bool]:
    if type(ledger) is not PriorReceiptLedger:
        raise OperationsValidationError("invalid prior receipt ledger")
    predecessor = GENESIS_RECEIPT_DIGEST_SHA256
    keys: set[str] = set()
    for receipt in ledger.receipts:
        _validate_receipt(
            receipt,
            scenario=scenario,
            context=context,
            predecessor=predecessor,
        )
        if receipt.idempotency_key_sha256 in keys:
            raise OperationsValidationError("duplicate prior receipt identity")
        keys.add(receipt.idempotency_key_sha256)
        predecessor = receipt.receipt_digest_sha256
    return _prior_position(ledger.receipts)


def _result(
    *,
    outcome: RehearsalOutcome,
    receipts: list[CutoverReceipt],
    negative_case: NegativeCase,
    scenario: SyntheticCutoverScenario,
    ledger: PriorReceiptLedger,
    context: _Context,
    evaluated_at: datetime,
) -> CutoverRehearsalResult:
    return CutoverRehearsalResult._create(
        outcome=outcome,
        receipts=tuple(receipts),
        negative_case=negative_case,
        manifest_digest_sha256=CUTOVER_MANIFEST_DIGEST_SHA256,
        preflight_digest_sha256=context.preflight_digest_sha256,
        scenario_digest_sha256=context.scenario_digest_sha256,
        prior_ledger_digest_sha256=ledger.ledger_digest_sha256,
        artifact_digest_sha256=context.artifact_digest_sha256,
        artifact_attestation_digest_sha256=context.artifact_attestation_digest_sha256,
        evaluated_at=evaluated_at,
    )


def _append_forward_receipts(
    receipts: list[CutoverReceipt],
    *,
    scenario: SyntheticCutoverScenario,
    surface: SyntheticSurfaceScenario,
    context: _Context,
    evaluated_at: datetime,
    start_stage: int,
    stop_stage: int,
) -> None:
    writer_valid = _writer_evidence_valid(surface)
    for index in range(start_stage, stop_stage):
        stage = FORWARD_STAGES[index]
        passed = True
        if stage is ForwardStage.ONE_WRITER_PROOF:
            passed = writer_valid
        elif stage is ForwardStage.SYNTHETIC_SMOKE:
            passed = surface.smoke_outcome is SurfaceCheckOutcome.PASSED
        elif stage is ForwardStage.VERIFICATION:
            passed = surface.verification_outcome is SurfaceCheckOutcome.PASSED
        elif stage is ForwardStage.GREEN:
            passed = (
                writer_valid
                and surface.smoke_outcome is SurfaceCheckOutcome.PASSED
                and surface.verification_outcome is SurfaceCheckOutcome.PASSED
                and surface.rollback_trigger is None
            )
        predecessor = (
            receipts[-1].receipt_digest_sha256
            if receipts
            else GENESIS_RECEIPT_DIGEST_SHA256
        )
        receipts.append(
            _new_receipt(
                scenario=scenario,
                surface=surface,
                context=context,
                chain_kind=ReceiptChainKind.FORWARD,
                stage=stage,
                predecessor=predecessor,
                outcome=ReceiptOutcome.PASSED if passed else ReceiptOutcome.BLOCKED,
                evaluated_at=evaluated_at,
            )
        )


def _rollback_problem(
    scenario: SyntheticCutoverScenario,
    surface_index: int,
) -> NegativeCase:
    surface = scenario.surfaces[surface_index]
    if surface.rollback_trigger is None:
        return NegativeCase.ROLLBACK_TRIGGER_MISSING
    if any(item.attempt_requested for item in scenario.surfaces[surface_index + 1 :]):
        return NegativeCase.LATER_SURFACE_AFTER_TRIGGER
    if (
        surface.writer_disposition is not WriterDisposition.ONE_SYNTHETIC_WRITER
        and surface.incompatible_state_written
    ):
        return NegativeCase.INCOMPATIBLE_STATE_ON_NON_WRITER
    attempt = surface.rollback_attempt
    if attempt is None:
        return NegativeCase.ROLLBACK_ATTEMPT_MISSING
    stages = attempt.stages
    values = tuple(item.stage for item in stages)
    if len(set(values)) != len(values):
        return NegativeCase.ROLLBACK_STAGE_DUPLICATED
    if len(stages) < len(ROLLBACK_STAGES):
        return NegativeCase.ROLLBACK_STAGE_MISSING
    if values != ROLLBACK_STAGES:
        return NegativeCase.ROLLBACK_STAGE_REORDERED
    failed = next(
        (index for index, item in enumerate(stages) if item.outcome is SurfaceCheckOutcome.FAILED),
        None,
    )
    if failed is not None:
        if failed < len(stages) - 1:
            return NegativeCase.ROLLBACK_STAGE_AFTER_FAILURE
        return NegativeCase.ROLLBACK_STAGE_FAILED
    restore = stages[ROLLBACK_STAGES.index(RollbackStage.SNAPSHOT_RESTORE_DISPOSITION)]
    if restore.disposition is not _expected_restore_disposition(surface):
        return NegativeCase.RESTORE_DISPOSITION_INVALID
    reenable = stages[
        ROLLBACK_STAGES.index(RollbackStage.OLD_SERVICE_REENABLED_DISPOSITION)
    ]
    if reenable.disposition is not _expected_reenable_disposition(surface):
        return NegativeCase.REENABLE_DISPOSITION_INVALID
    if any(
        item.disposition is not _expected_rollback_disposition(surface, item.stage)
        for item in stages
    ):
        return NegativeCase.RESTORE_DISPOSITION_INVALID
    return NegativeCase.NONE


def _append_rollback_receipts(
    receipts: list[CutoverReceipt],
    *,
    scenario: SyntheticCutoverScenario,
    surface: SyntheticSurfaceScenario,
    context: _Context,
    evaluated_at: datetime,
) -> None:
    if surface.rollback_attempt is None:
        return
    seen: set[RollbackStage] = set()
    for expected_index, attempt in enumerate(surface.rollback_attempt.stages):
        if attempt.stage in seen or expected_index >= len(ROLLBACK_STAGES):
            break
        if attempt.stage is not ROLLBACK_STAGES[expected_index]:
            break
        seen.add(attempt.stage)
        predecessor = receipts[-1].receipt_digest_sha256
        receipts.append(
            _new_receipt(
                scenario=scenario,
                surface=surface,
                context=context,
                chain_kind=ReceiptChainKind.ROLLBACK,
                stage=attempt.stage,
                predecessor=predecessor,
                outcome=(
                    ReceiptOutcome.PASSED
                    if attempt.outcome is SurfaceCheckOutcome.PASSED
                    else ReceiptOutcome.BLOCKED
                ),
                evaluated_at=evaluated_at,
                rollback_disposition=attempt.disposition,
                rollback_attempt=attempt,
            )
        )
        if attempt.outcome is SurfaceCheckOutcome.FAILED:
            break


def rehearse_cutover(
    preflight: CutoverDoctorResult,
    scenario: SyntheticCutoverScenario,
    *,
    prior_ledger: PriorReceiptLedger,
    artifact_attestation: ArtifactAttestationEvidence,
    evaluated_at: datetime,
) -> CutoverRehearsalResult:
    """Reduce supplied synthetic metadata into immutable ordered receipts."""
    observed_at = _utc(evaluated_at, "cutover evaluation time")
    if type(scenario) is not SyntheticCutoverScenario:
        raise OperationsValidationError("invalid synthetic cutover scenario")
    scenario.validate()
    preflight_digest = _validate_preflight(preflight)
    _validate_artifact_attestation(artifact_attestation, observed_at)
    context = _Context(
        preflight_digest_sha256=preflight_digest,
        scenario_digest_sha256=scenario.scenario_digest_sha256,
        artifact_digest_sha256=artifact_attestation.artifact.digest_sha256,
        artifact_attestation_digest_sha256=(
            artifact_attestation.attestation_digest_sha256
        ),
    )
    surface_index, forward_index, terminal_rollback = _validate_prior_ledger(
        prior_ledger,
        scenario=scenario,
        context=context,
    )
    expected_resume = (
        None
        if terminal_rollback or surface_index == len(CUTOVER_SURFACE_MANIFEST)
        else CUTOVER_SURFACE_MANIFEST[surface_index].surface
    )
    if scenario.resume_from is not expected_resume:
        raise OperationsValidationError("invalid cutover resume surface")
    receipts = list(prior_ledger.receipts)
    if terminal_rollback:
        rollback_problem = _rollback_problem(scenario, surface_index)
        return _result(
            outcome=(
                RehearsalOutcome.ROLLED_BACK_SYNTHETIC
                if rollback_problem is NegativeCase.NONE
                else RehearsalOutcome.ROLLBACK_BLOCKED
            ),
            receipts=receipts,
            negative_case=rollback_problem,
            scenario=scenario,
            ledger=prior_ledger,
            context=context,
            evaluated_at=observed_at,
        )
    if surface_index == len(CUTOVER_SURFACE_MANIFEST):
        return _result(
            outcome=RehearsalOutcome.SYNTHETIC_GREEN,
            receipts=receipts,
            negative_case=NegativeCase.NONE,
            scenario=scenario,
            ledger=prior_ledger,
            context=context,
            evaluated_at=observed_at,
        )

    for index in range(surface_index, len(CUTOVER_SURFACE_MANIFEST)):
        surface = scenario.surfaces[index]
        start_stage = forward_index if index == surface_index else 0
        if not surface.attempt_requested:
            return _result(
                outcome=RehearsalOutcome.BLOCKED,
                receipts=receipts,
                negative_case=NegativeCase.SURFACE_NOT_REQUESTED,
                scenario=scenario,
                ledger=prior_ledger,
                context=context,
                evaluated_at=observed_at,
            )
        if surface.rollback_trigger is not None:
            _append_forward_receipts(
                receipts,
                scenario=scenario,
                surface=surface,
                context=context,
                evaluated_at=observed_at,
                start_stage=start_stage,
                stop_stage=len(FORWARD_STAGES) - 1,
            )
            problem = _rollback_problem(scenario, index)
            _append_rollback_receipts(
                receipts,
                scenario=scenario,
                surface=surface,
                context=context,
                evaluated_at=observed_at,
            )
            return _result(
                outcome=(
                    RehearsalOutcome.ROLLED_BACK_SYNTHETIC
                    if problem is NegativeCase.NONE
                    else RehearsalOutcome.ROLLBACK_BLOCKED
                ),
                receipts=receipts,
                negative_case=problem,
                scenario=scenario,
                ledger=prior_ledger,
                context=context,
                evaluated_at=observed_at,
            )
        if not _writer_evidence_valid(surface):
            stop = max(start_stage, FORWARD_STAGES.index(ForwardStage.ONE_WRITER_PROOF) + 1)
            _append_forward_receipts(
                receipts,
                scenario=scenario,
                surface=surface,
                context=context,
                evaluated_at=observed_at,
                start_stage=start_stage,
                stop_stage=stop,
            )
            return _result(
                outcome=RehearsalOutcome.BLOCKED,
                receipts=receipts,
                negative_case=NegativeCase.WRITER_EVIDENCE_INVALID,
                scenario=scenario,
                ledger=prior_ledger,
                context=context,
                evaluated_at=observed_at,
            )
        if surface.smoke_outcome is SurfaceCheckOutcome.FAILED:
            _append_forward_receipts(
                receipts,
                scenario=scenario,
                surface=surface,
                context=context,
                evaluated_at=observed_at,
                start_stage=start_stage,
                stop_stage=FORWARD_STAGES.index(ForwardStage.SYNTHETIC_SMOKE) + 1,
            )
            return _result(
                outcome=RehearsalOutcome.BLOCKED,
                receipts=receipts,
                negative_case=NegativeCase.SMOKE_FAILED_WITHOUT_TRIGGER,
                scenario=scenario,
                ledger=prior_ledger,
                context=context,
                evaluated_at=observed_at,
            )
        if surface.verification_outcome is SurfaceCheckOutcome.FAILED:
            _append_forward_receipts(
                receipts,
                scenario=scenario,
                surface=surface,
                context=context,
                evaluated_at=observed_at,
                start_stage=start_stage,
                stop_stage=FORWARD_STAGES.index(ForwardStage.VERIFICATION) + 1,
            )
            return _result(
                outcome=RehearsalOutcome.BLOCKED,
                receipts=receipts,
                negative_case=NegativeCase.VERIFICATION_FAILED_WITHOUT_TRIGGER,
                scenario=scenario,
                ledger=prior_ledger,
                context=context,
                evaluated_at=observed_at,
            )
        _append_forward_receipts(
            receipts,
            scenario=scenario,
            surface=surface,
            context=context,
            evaluated_at=observed_at,
            start_stage=start_stage,
            stop_stage=len(FORWARD_STAGES),
        )
        forward_index = 0

    return _result(
        outcome=RehearsalOutcome.SYNTHETIC_GREEN,
        receipts=receipts,
        negative_case=NegativeCase.NONE,
        scenario=scenario,
        ledger=prior_ledger,
        context=context,
        evaluated_at=observed_at,
    )
