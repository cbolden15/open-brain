"""Pure metadata-only P7-W3 operational evidence and reconciliation contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, NoReturn

from .cutover import CUTOVER_SURFACE_MANIFEST, CutoverSurface
from .models import OperationsValidationError

P7_W3_OPERATIONAL_VERSION = "phase7-wave3-operational-v1"
P7_W3_RECONCILIATION_VERSION = "phase7-wave3-reconciliation-v1"
P7_W3_PRIOR_TRUST_VERSION = "phase7-wave3-prior-trust-v1"
P7_W3_ARTIFACT_ATTESTATION_SCHEMA_VERSION = "phase7-wave3-artifact-attestation-v1"
P7_W3_STALE_REFERENCE_SCAN_VERSION = "phase7-wave3-stale-reference-scan-v1"
OPERATIONAL_GENESIS_DIGEST_SHA256 = "0" * 64

P7_W0_RESULT_SET_DIGEST_SHA256 = "db0efd7c4f06c9ea6e80fb5c04490a8114d6456ba587975d7ef6071f8b7b60dd"
P7_W1_RESULT_SET_DIGEST_SHA256 = "1a193fd96636a416ea9e94e7e25eb888d2074bff3475d738d51f698e45bf10c3"
P7_W2_RESULT_SET_DIGEST_SHA256 = "b945513af75cc4d9bfcefb78e72fe86f720c174340d833d224d0b55ad5ab10c4"
P7_W2_GREEN_RESULT_DIGEST_SHA256 = (
    "bf68231472606eea3a32c7aa6dd33295750168d7017af8cca9c5f8822f27dd2c"
)
P7_W3_PRIOR_TRUST_MANIFEST_DIGEST_SHA256 = (
    "2fa78e588e61cf1f546ad3c5027e5b4c9faaf5837c744d497940c4ee43812800"
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID = re.compile(r"^run_[0-9a-f]{16,64}$")
_RECEIPT_ID = re.compile(r"^rcpt_[0-9a-f]{32}$")
_RUNBOOK_STEP = re.compile(r"^P7W3-OPS-[0-9]{2}$")
_ARTIFACT_DISTRIBUTION = re.compile(r"^[a-z0-9]+(?:[-_.][a-z0-9]+)*$")
_ARTIFACT_VERSION = re.compile(r"^[0-9A-Za-z]+(?:[0-9A-Za-z.+-]*[0-9A-Za-z])?$")


class _ContractError(OperationsValidationError):
    def __init__(self, code: str, *, missing: bool = False) -> None:
        super().__init__(code)
        self.missing = missing


def _fail(code: str, *, missing: bool = False) -> NoReturn:
    raise _ContractError(code, missing=missing)


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
        _fail("invalid-canonical-value")


def _digest_value(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _require_digest(value: object, code: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(code)
    return value


def _require_commit(value: object, code: str) -> str:
    if type(value) is not str or _COMMIT_SHA.fullmatch(value) is None:
        _fail(code)
    return value


def _require_positive_int(value: object, code: str) -> int:
    if type(value) is not int or value < 1:
        _fail(code)
    return value


def _require_nonnegative_int(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        _fail(code)
    return value


def _utc(value: object, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _fail(code)
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc(value, "invalid-timestamp").strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class OperationalFlow(StrEnum):
    CAPTURE_TO_LEDGER = "FLOW-001"
    REVIEW_APPROVE = "FLOW-002"
    REVIEW_REJECT = "FLOW-003"
    COMPLETE_NIGHTLY = "FLOW-004"
    PLAYLIST_POLL = "FLOW-005"
    SOCIAL_WEB_CAPTURE = "FLOW-006"
    BACKUP = "FLOW-007"
    TEMPORARY_RESTORE = "FLOW-008"


class OperationalReceiptOutcome(StrEnum):
    PASSED_SYNTHETIC = "passed-synthetic"
    FAILED = "failed"
    BLOCKED_MISSING_EVIDENCE = "blocked-missing-evidence"
    BLOCKED_CONTRADICTION = "blocked-contradiction"


_EXPECTED_OPERATIONAL_RECEIPT_OUTCOME_ROWS = (
    (
        "PASSED_SYNTHETIC",
        "passed-synthetic",
        OperationalReceiptOutcome.PASSED_SYNTHETIC,
    ),
    ("FAILED", "failed", OperationalReceiptOutcome.FAILED),
    (
        "BLOCKED_MISSING_EVIDENCE",
        "blocked-missing-evidence",
        OperationalReceiptOutcome.BLOCKED_MISSING_EVIDENCE,
    ),
    (
        "BLOCKED_CONTRADICTION",
        "blocked-contradiction",
        OperationalReceiptOutcome.BLOCKED_CONTRADICTION,
    ),
)
_CANONICAL_OPERATIONAL_RECEIPT_OUTCOMES = tuple(
    member for _, _, member in _EXPECTED_OPERATIONAL_RECEIPT_OUTCOME_ROWS
)
(
    _CANONICAL_PASSED_SYNTHETIC,
    _CANONICAL_FAILED,
    _CANONICAL_BLOCKED_MISSING_EVIDENCE,
    _CANONICAL_BLOCKED_CONTRADICTION,
) = _CANONICAL_OPERATIONAL_RECEIPT_OUTCOMES


def _validate_operational_receipt_outcome_registry() -> None:
    _validate_closed_enum_registry(OperationalReceiptOutcome)


def _validate_operational_receipt_outcome(value: object) -> None:
    _require_closed_enum_member(
        value,
        OperationalReceiptOutcome,
        "invalid-operational-receipt-outcome",
    )


class OperationalEvidenceScope(StrEnum):
    SYNTHETIC = "synthetic"


class ProductionEvidenceState(StrEnum):
    NOT_PERFORMED_OWNER_GATED = "not-performed-owner-gated"


class TrustedParityRowState(StrEnum):
    MATCH = "match"
    BLOCKED_DIFFERENCE = "blocked-difference"


class StaleReferenceState(StrEnum):
    PASSED_SYNTHETIC = "passed-synthetic"
    FAILED = "failed"
    NOT_PERFORMED_OWNER_GATED = "not-performed-owner-gated"


class ArtifactVerificationClass(StrEnum):
    COORDINATOR_SHA256_WHEEL_BINDING = "coordinator-sha256-wheel-binding"


class Phase7ReconciliationOutcome(StrEnum):
    SYNTHETIC_OPERATIONAL_CHECKPOINT_COMPLETE_PARITY_AND_PRODUCTION_OWNER_GATED = (
        "synthetic-operational-checkpoint-complete-parity-and-production-owner-gated"
    )
    BLOCKED_MISSING_EVIDENCE = "blocked-missing-evidence"
    BLOCKED_CONTRADICTION = "blocked-contradiction"


_SYNTHETIC_CHECKPOINT_COMPLETE = Phase7ReconciliationOutcome(
    "synthetic-operational-checkpoint-complete-parity-and-production-owner-gated"
)


class Phase7FindingClass(StrEnum):
    ARTIFACT_ATTESTATION_MISSING = "artifact-attestation-missing"
    ARTIFACT_ATTESTATION_CONTRADICTION = "artifact-attestation-contradiction"
    OPERATIONAL_MISSING = "operational-evidence-missing"
    OPERATIONAL_CONTRADICTION = "operational-evidence-contradiction"
    PRIOR_TRUST_CONTRADICTION = "prior-trust-contradiction"
    SOURCE_ROW_LINEAGE_CONTRADICTION = "source-row-lineage-contradiction"
    STALE_REFERENCE_MISSING = "stale-reference-evidence-missing"
    STALE_REFERENCE_CONTRADICTION = "stale-reference-evidence-contradiction"


_MISSING_FINDINGS = frozenset(
    {
        Phase7FindingClass.ARTIFACT_ATTESTATION_MISSING,
        Phase7FindingClass.OPERATIONAL_MISSING,
        Phase7FindingClass.STALE_REFERENCE_MISSING,
    }
)
_CONTRADICTION_FINDINGS = frozenset(
    {
        Phase7FindingClass.ARTIFACT_ATTESTATION_CONTRADICTION,
        Phase7FindingClass.OPERATIONAL_CONTRADICTION,
        Phase7FindingClass.PRIOR_TRUST_CONTRADICTION,
        Phase7FindingClass.SOURCE_ROW_LINEAGE_CONTRADICTION,
        Phase7FindingClass.STALE_REFERENCE_CONTRADICTION,
    }
)
_TRUSTED_ROWS_AVAILABLE_FINDINGS = frozenset(
    {
        Phase7FindingClass.STALE_REFERENCE_MISSING,
        Phase7FindingClass.STALE_REFERENCE_CONTRADICTION,
    }
)


class ProductionCheck(StrEnum):
    PRODUCTION_PARITY = "production-parity"
    LIVE_SURFACES = "all-eight-live-surfaces"
    REAL_OPERATIONAL_FLOWS = "all-eight-real-operational-flows"
    OLD_WRITERS_DISABLED = "old-writers-disabled"
    ROLLBACK_AVAILABLE = "rollback-available"
    NO_STALE_LOADED_SERVICE_REFERENCES = "no-stale-loaded-service-references"
    PHASE7_PRODUCTION_COMPLETION = "phase7-production-completion"


_CLOSED_ENUM_SEALS: dict[
    type[StrEnum], tuple[tuple[str, str, StrEnum], ...]
] = {
    CutoverSurface: (
        ("CLI_READS", "cli-reads", CutoverSurface.CLI_READS),
        ("MCP_UI_READS", "mcp-ui-reads", CutoverSurface.MCP_UI_READS),
        ("IOS_RAW_CAPTURE", "ios-raw-capture", CutoverSurface.IOS_RAW_CAPTURE),
        ("YOUTUBE_PLAYLIST", "youtube-playlist", CutoverSurface.YOUTUBE_PLAYLIST),
        ("SOCIAL_WEB_DRAIN", "social-web-drain", CutoverSurface.SOCIAL_WEB_DRAIN),
        (
            "LEDGER_REVIEW_JOBS",
            "ledger-review-jobs",
            CutoverSurface.LEDGER_REVIEW_JOBS,
        ),
        (
            "SCHEDULED_WRITERS",
            "scheduled-writers",
            CutoverSurface.SCHEDULED_WRITERS,
        ),
        ("RECOVERY_TOOLING", "recovery-tooling", CutoverSurface.RECOVERY_TOOLING),
    ),
    OperationalFlow: (
        ("CAPTURE_TO_LEDGER", "FLOW-001", OperationalFlow.CAPTURE_TO_LEDGER),
        ("REVIEW_APPROVE", "FLOW-002", OperationalFlow.REVIEW_APPROVE),
        ("REVIEW_REJECT", "FLOW-003", OperationalFlow.REVIEW_REJECT),
        ("COMPLETE_NIGHTLY", "FLOW-004", OperationalFlow.COMPLETE_NIGHTLY),
        ("PLAYLIST_POLL", "FLOW-005", OperationalFlow.PLAYLIST_POLL),
        ("SOCIAL_WEB_CAPTURE", "FLOW-006", OperationalFlow.SOCIAL_WEB_CAPTURE),
        ("BACKUP", "FLOW-007", OperationalFlow.BACKUP),
        ("TEMPORARY_RESTORE", "FLOW-008", OperationalFlow.TEMPORARY_RESTORE),
    ),
    OperationalReceiptOutcome: _EXPECTED_OPERATIONAL_RECEIPT_OUTCOME_ROWS,
    OperationalEvidenceScope: (
        ("SYNTHETIC", "synthetic", OperationalEvidenceScope.SYNTHETIC),
    ),
    ProductionEvidenceState: (
        (
            "NOT_PERFORMED_OWNER_GATED",
            "not-performed-owner-gated",
            ProductionEvidenceState.NOT_PERFORMED_OWNER_GATED,
        ),
    ),
    TrustedParityRowState: (
        ("MATCH", "match", TrustedParityRowState.MATCH),
        (
            "BLOCKED_DIFFERENCE",
            "blocked-difference",
            TrustedParityRowState.BLOCKED_DIFFERENCE,
        ),
    ),
    StaleReferenceState: (
        ("PASSED_SYNTHETIC", "passed-synthetic", StaleReferenceState.PASSED_SYNTHETIC),
        ("FAILED", "failed", StaleReferenceState.FAILED),
        (
            "NOT_PERFORMED_OWNER_GATED",
            "not-performed-owner-gated",
            StaleReferenceState.NOT_PERFORMED_OWNER_GATED,
        ),
    ),
    ArtifactVerificationClass: (
        (
            "COORDINATOR_SHA256_WHEEL_BINDING",
            "coordinator-sha256-wheel-binding",
            ArtifactVerificationClass.COORDINATOR_SHA256_WHEEL_BINDING,
        ),
    ),
    Phase7ReconciliationOutcome: (
        (
            "SYNTHETIC_OPERATIONAL_CHECKPOINT_COMPLETE_PARITY_AND_PRODUCTION_OWNER_GATED",
            "synthetic-operational-checkpoint-complete-parity-and-production-owner-gated",
            _SYNTHETIC_CHECKPOINT_COMPLETE,
        ),
        (
            "BLOCKED_MISSING_EVIDENCE",
            "blocked-missing-evidence",
            Phase7ReconciliationOutcome.BLOCKED_MISSING_EVIDENCE,
        ),
        (
            "BLOCKED_CONTRADICTION",
            "blocked-contradiction",
            Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION,
        ),
    ),
    Phase7FindingClass: (
        (
            "ARTIFACT_ATTESTATION_MISSING",
            "artifact-attestation-missing",
            Phase7FindingClass.ARTIFACT_ATTESTATION_MISSING,
        ),
        (
            "ARTIFACT_ATTESTATION_CONTRADICTION",
            "artifact-attestation-contradiction",
            Phase7FindingClass.ARTIFACT_ATTESTATION_CONTRADICTION,
        ),
        (
            "OPERATIONAL_MISSING",
            "operational-evidence-missing",
            Phase7FindingClass.OPERATIONAL_MISSING,
        ),
        (
            "OPERATIONAL_CONTRADICTION",
            "operational-evidence-contradiction",
            Phase7FindingClass.OPERATIONAL_CONTRADICTION,
        ),
        (
            "PRIOR_TRUST_CONTRADICTION",
            "prior-trust-contradiction",
            Phase7FindingClass.PRIOR_TRUST_CONTRADICTION,
        ),
        (
            "SOURCE_ROW_LINEAGE_CONTRADICTION",
            "source-row-lineage-contradiction",
            Phase7FindingClass.SOURCE_ROW_LINEAGE_CONTRADICTION,
        ),
        (
            "STALE_REFERENCE_MISSING",
            "stale-reference-evidence-missing",
            Phase7FindingClass.STALE_REFERENCE_MISSING,
        ),
        (
            "STALE_REFERENCE_CONTRADICTION",
            "stale-reference-evidence-contradiction",
            Phase7FindingClass.STALE_REFERENCE_CONTRADICTION,
        ),
    ),
    ProductionCheck: (
        ("PRODUCTION_PARITY", "production-parity", ProductionCheck.PRODUCTION_PARITY),
        ("LIVE_SURFACES", "all-eight-live-surfaces", ProductionCheck.LIVE_SURFACES),
        (
            "REAL_OPERATIONAL_FLOWS",
            "all-eight-real-operational-flows",
            ProductionCheck.REAL_OPERATIONAL_FLOWS,
        ),
        (
            "OLD_WRITERS_DISABLED",
            "old-writers-disabled",
            ProductionCheck.OLD_WRITERS_DISABLED,
        ),
        ("ROLLBACK_AVAILABLE", "rollback-available", ProductionCheck.ROLLBACK_AVAILABLE),
        (
            "NO_STALE_LOADED_SERVICE_REFERENCES",
            "no-stale-loaded-service-references",
            ProductionCheck.NO_STALE_LOADED_SERVICE_REFERENCES,
        ),
        (
            "PHASE7_PRODUCTION_COMPLETION",
            "phase7-production-completion",
            ProductionCheck.PHASE7_PRODUCTION_COMPLETION,
        ),
    ),
}


def _validate_closed_enum_registry(enum_type: type[StrEnum]) -> None:
    rows = _CLOSED_ENUM_SEALS.get(enum_type)
    if rows is None:
        _fail("unknown-closed-enum-registry")
    registry: Any = enum_type
    expected_names = tuple(name for name, _, _ in rows)
    expected_values = tuple(value for _, value, _ in rows)
    expected_members = tuple(member for _, _, member in rows)
    try:
        live_names = tuple(registry._member_names_)
        live_members = tuple(enum_type)
        member_map = registry._member_map_
        value_map = registry._value2member_map_
    except (AttributeError, KeyError, TypeError):
        _fail("invalid-closed-enum-registry")
    if (
        live_names != expected_names
        or len(live_members) != len(expected_members)
        or any(
            live is not expected
            for live, expected in zip(live_members, expected_members, strict=True)
        )
        or set(member_map) != set(expected_names)
        or set(value_map) != set(expected_values)
    ):
        _fail("invalid-closed-enum-registry")
    for name, value, member in rows:
        if (
            type(member) is not enum_type
            or member.name != name
            or member.value != value
            or member_map.get(name) is not member
            or value_map.get(value) is not member
        ):
            _fail("invalid-closed-enum-registry")


def _require_closed_enum_member(
    value: object,
    enum_type: type[StrEnum],
    code: str,
) -> None:
    _validate_closed_enum_registry(enum_type)
    rows = _CLOSED_ENUM_SEALS[enum_type]
    if type(value) is not enum_type or not any(value is member for _, _, member in rows):
        _fail(code)


@dataclass(frozen=True, slots=True)
class OperationalFlowSpec:
    ordinal: int
    flow: OperationalFlow
    label: str
    runbook_step_id: str
    p7_w2_prerequisite: str

    def to_dict(self) -> dict[str, object]:
        _validate_flow_spec(self)
        return _flow_spec_payload(self)


OPERATIONAL_FLOW_MANIFEST = (
    OperationalFlowSpec(
        1,
        OperationalFlow.CAPTURE_TO_LEDGER,
        "capture to ledger",
        "P7W3-OPS-01",
        "iOS/raw capture and ledger/review",
    ),
    OperationalFlowSpec(
        2,
        OperationalFlow.REVIEW_APPROVE,
        "review approve",
        "P7W3-OPS-02",
        "ledger/review",
    ),
    OperationalFlowSpec(
        3,
        OperationalFlow.REVIEW_REJECT,
        "review reject",
        "P7W3-OPS-03",
        "ledger/review",
    ),
    OperationalFlowSpec(
        4,
        OperationalFlow.COMPLETE_NIGHTLY,
        "complete nightly",
        "P7W3-OPS-04",
        "remaining scheduled writers",
    ),
    OperationalFlowSpec(
        5,
        OperationalFlow.PLAYLIST_POLL,
        "playlist poll",
        "P7W3-OPS-05",
        "YouTube playlist",
    ),
    OperationalFlowSpec(
        6,
        OperationalFlow.SOCIAL_WEB_CAPTURE,
        "social/web capture",
        "P7W3-OPS-06",
        "social/web drain",
    ),
    OperationalFlowSpec(
        7,
        OperationalFlow.BACKUP,
        "backup",
        "P7W3-OPS-07",
        "recovery tooling",
    ),
    OperationalFlowSpec(
        8,
        OperationalFlow.TEMPORARY_RESTORE,
        "temporary restore",
        "P7W3-OPS-08",
        "recovery tooling",
    ),
)

_EXPECTED_FLOW_ROWS = (
    (
        1,
        OperationalFlow.CAPTURE_TO_LEDGER,
        "capture to ledger",
        "P7W3-OPS-01",
        "iOS/raw capture and ledger/review",
    ),
    (2, OperationalFlow.REVIEW_APPROVE, "review approve", "P7W3-OPS-02", "ledger/review"),
    (3, OperationalFlow.REVIEW_REJECT, "review reject", "P7W3-OPS-03", "ledger/review"),
    (
        4,
        OperationalFlow.COMPLETE_NIGHTLY,
        "complete nightly",
        "P7W3-OPS-04",
        "remaining scheduled writers",
    ),
    (
        5,
        OperationalFlow.PLAYLIST_POLL,
        "playlist poll",
        "P7W3-OPS-05",
        "YouTube playlist",
    ),
    (
        6,
        OperationalFlow.SOCIAL_WEB_CAPTURE,
        "social/web capture",
        "P7W3-OPS-06",
        "social/web drain",
    ),
    (7, OperationalFlow.BACKUP, "backup", "P7W3-OPS-07", "recovery tooling"),
    (
        8,
        OperationalFlow.TEMPORARY_RESTORE,
        "temporary restore",
        "P7W3-OPS-08",
        "recovery tooling",
    ),
)


def _flow_spec_payload(spec: OperationalFlowSpec) -> dict[str, object]:
    return {
        "ordinal": spec.ordinal,
        "flow": spec.flow.value,
        "label": spec.label,
        "runbook_step_id": spec.runbook_step_id,
        "p7_w2_prerequisite": spec.p7_w2_prerequisite,
    }


def _validate_flow_spec(spec: OperationalFlowSpec) -> None:
    _require_closed_enum_member(spec.flow, OperationalFlow, "invalid-flow-spec")
    if type(spec.ordinal) is not int or not 1 <= spec.ordinal <= 8:
        _fail("invalid-flow-spec")
    actual = (
        spec.ordinal,
        spec.flow,
        spec.label,
        spec.runbook_step_id,
        spec.p7_w2_prerequisite,
    )
    if actual != _EXPECTED_FLOW_ROWS[spec.ordinal - 1]:
        _fail("invalid-flow-spec")


def _validate_operational_manifest() -> None:
    _validate_closed_enum_registry(OperationalFlow)
    if len(OPERATIONAL_FLOW_MANIFEST) != 8 or tuple(OperationalFlow) != tuple(
        spec.flow for spec in OPERATIONAL_FLOW_MANIFEST
    ):
        _fail("invalid-operational-manifest")
    rows = tuple(
        (
            spec.ordinal,
            spec.flow,
            spec.label,
            spec.runbook_step_id,
            spec.p7_w2_prerequisite,
        )
        for spec in OPERATIONAL_FLOW_MANIFEST
    )
    if rows != _EXPECTED_FLOW_ROWS:
        _fail("invalid-operational-manifest")
    for spec in OPERATIONAL_FLOW_MANIFEST:
        _validate_flow_spec(spec)
    live_digest = _digest_value(
        {
            "contract_version": P7_W3_OPERATIONAL_VERSION,
            "flows": [_flow_spec_payload(spec) for spec in OPERATIONAL_FLOW_MANIFEST],
        }
    )
    if live_digest != OPERATIONAL_MANIFEST_DIGEST_SHA256:
        _fail("conflicting-operational-manifest-digest")


OPERATIONAL_MANIFEST_DIGEST_SHA256 = _digest_value(
    {
        "contract_version": P7_W3_OPERATIONAL_VERSION,
        "flows": [_flow_spec_payload(spec) for spec in OPERATIONAL_FLOW_MANIFEST],
    }
)


def _flow_spec(flow: OperationalFlow) -> OperationalFlowSpec:
    _require_closed_enum_member(flow, OperationalFlow, "invalid-operational-flow")
    _validate_operational_manifest()
    return OPERATIONAL_FLOW_MANIFEST[tuple(OperationalFlow).index(flow)]


def _idempotency_payload(receipt: OperationalReceipt) -> dict[str, object]:
    return {
        "contract_version": receipt.contract_version,
        "operational_manifest_digest_sha256": receipt.operational_manifest_digest_sha256,
        "run_id": receipt.run_id,
        "attempt": receipt.attempt,
        "flow": receipt.flow.value,
        "ordinal": receipt.ordinal,
        "runbook_step_id": receipt.runbook_step_id,
        "input_digest_sha256": receipt.input_digest_sha256,
        "current_artifact_digest_sha256": receipt.current_artifact_digest_sha256,
        "p7_w2_result_digest_sha256": receipt.p7_w2_result_digest_sha256,
    }


def _stage_payload(receipt: OperationalReceipt) -> dict[str, object]:
    return {
        **_idempotency_payload(receipt),
        "outcome": receipt.outcome.value,
        "scope": receipt.scope.value,
        "redacted_output_digest_sha256": receipt.redacted_output_digest_sha256,
        "evaluated_at": _timestamp(receipt.evaluated_at),
        "redacted": receipt.redacted,
        "real_flow_state": receipt.real_flow_state.value,
        "production_state": receipt.production_state.value,
    }


def _receipt_dict(receipt: OperationalReceipt) -> dict[str, object]:
    return {
        "schema_version": receipt.schema_version,
        "contract_version": receipt.contract_version,
        "operational_manifest_digest_sha256": receipt.operational_manifest_digest_sha256,
        "run_id": receipt.run_id,
        "receipt_id": receipt.receipt_id,
        "idempotency_key_sha256": receipt.idempotency_key_sha256,
        "flow": receipt.flow.value,
        "ordinal": receipt.ordinal,
        "attempt": receipt.attempt,
        "runbook_step_id": receipt.runbook_step_id,
        "outcome": receipt.outcome.value,
        "scope": receipt.scope.value,
        "predecessor_receipt_digest_sha256": receipt.predecessor_receipt_digest_sha256,
        "input_digest_sha256": receipt.input_digest_sha256,
        "redacted_output_digest_sha256": receipt.redacted_output_digest_sha256,
        "current_artifact_digest_sha256": receipt.current_artifact_digest_sha256,
        "p7_w2_result_digest_sha256": receipt.p7_w2_result_digest_sha256,
        "stage_payload_digest_sha256": receipt.stage_payload_digest_sha256,
        "evaluated_at": _timestamp(receipt.evaluated_at),
        "redacted": receipt.redacted,
        "real_flow_state": receipt.real_flow_state.value,
        "production_state": receipt.production_state.value,
        "receipt_digest_sha256": receipt.receipt_digest_sha256,
    }


def _receipt_digest(receipt: OperationalReceipt) -> str:
    payload = _receipt_dict(receipt)
    payload.pop("receipt_digest_sha256")
    return _digest_value(payload)


@dataclass(frozen=True, slots=True)
class OperationalReceipt:
    schema_version: int
    contract_version: str
    operational_manifest_digest_sha256: str
    run_id: str
    receipt_id: str
    idempotency_key_sha256: str
    flow: OperationalFlow
    ordinal: int
    attempt: int
    runbook_step_id: str
    outcome: OperationalReceiptOutcome
    scope: OperationalEvidenceScope
    predecessor_receipt_digest_sha256: str
    input_digest_sha256: str
    redacted_output_digest_sha256: str
    current_artifact_digest_sha256: str
    p7_w2_result_digest_sha256: str
    stage_payload_digest_sha256: str
    evaluated_at: datetime
    redacted: bool
    real_flow_state: ProductionEvidenceState
    production_state: ProductionEvidenceState
    receipt_digest_sha256: str

    def __post_init__(self) -> None:
        _validate_operational_receipt(self)

    def to_dict(self) -> dict[str, object]:
        _validate_operational_receipt(self)
        return _receipt_dict(self)


def _validate_operational_receipt(receipt: OperationalReceipt) -> None:
    _validate_operational_manifest()
    if type(receipt) is not OperationalReceipt:
        _fail("invalid-operational-receipt")
    _validate_operational_receipt_outcome(receipt.outcome)
    _require_closed_enum_member(
        receipt.scope,
        OperationalEvidenceScope,
        "invalid-operational-receipt-scope",
    )
    _require_closed_enum_member(
        receipt.real_flow_state,
        ProductionEvidenceState,
        "invalid-real-flow-state",
    )
    _require_closed_enum_member(
        receipt.production_state,
        ProductionEvidenceState,
        "invalid-production-state",
    )
    if type(receipt.flow) is not OperationalFlow:
        _fail("invalid-operational-receipt-flow")
    spec = _flow_spec(receipt.flow)
    if (
        type(receipt.schema_version) is not int
        or receipt.schema_version != 1
        or type(receipt.contract_version) is not str
        or receipt.contract_version != P7_W3_OPERATIONAL_VERSION
        or receipt.operational_manifest_digest_sha256 != OPERATIONAL_MANIFEST_DIGEST_SHA256
        or type(receipt.run_id) is not str
        or _RUN_ID.fullmatch(receipt.run_id) is None
        or type(receipt.receipt_id) is not str
        or _RECEIPT_ID.fullmatch(receipt.receipt_id) is None
        or type(receipt.ordinal) is not int
        or receipt.ordinal != spec.ordinal
        or type(receipt.attempt) is not int
        or receipt.attempt < 1
        or type(receipt.runbook_step_id) is not str
        or receipt.runbook_step_id != spec.runbook_step_id
        or type(receipt.scope) is not OperationalEvidenceScope
        or receipt.scope is not OperationalEvidenceScope.SYNTHETIC
        or receipt.redacted is not True
        or type(receipt.real_flow_state) is not ProductionEvidenceState
        or receipt.real_flow_state is not ProductionEvidenceState.NOT_PERFORMED_OWNER_GATED
        or type(receipt.production_state) is not ProductionEvidenceState
        or receipt.production_state is not ProductionEvidenceState.NOT_PERFORMED_OWNER_GATED
    ):
        _fail("invalid-operational-receipt")
    for value, code in (
        (receipt.operational_manifest_digest_sha256, "invalid-operational-manifest-digest"),
        (receipt.idempotency_key_sha256, "invalid-idempotency-key"),
        (receipt.predecessor_receipt_digest_sha256, "invalid-predecessor-digest"),
        (receipt.input_digest_sha256, "invalid-input-digest"),
        (receipt.redacted_output_digest_sha256, "invalid-redacted-output-digest"),
        (receipt.current_artifact_digest_sha256, "invalid-current-artifact-digest"),
        (receipt.p7_w2_result_digest_sha256, "invalid-p7-w2-result-digest"),
        (receipt.stage_payload_digest_sha256, "invalid-stage-payload-digest"),
        (receipt.receipt_digest_sha256, "invalid-receipt-digest"),
    ):
        _require_digest(value, code)
    normalized = _utc(receipt.evaluated_at, "invalid-receipt-evaluation-time")
    if normalized != receipt.evaluated_at or receipt.evaluated_at.utcoffset() != UTC.utcoffset(
        None
    ):
        _fail("invalid-receipt-evaluation-time")
    expected_key = _digest_value(_idempotency_payload(receipt))
    if (
        receipt.idempotency_key_sha256 != expected_key
        or receipt.receipt_id != f"rcpt_{expected_key[:32]}"
        or receipt.stage_payload_digest_sha256 != _digest_value(_stage_payload(receipt))
        or receipt.receipt_digest_sha256 != _receipt_digest(receipt)
    ):
        _fail("conflicting-operational-receipt")


def make_operational_receipt(
    *,
    run_id: str,
    attempt: int,
    flow: OperationalFlow,
    predecessor_receipt_digest_sha256: str,
    input_digest_sha256: str,
    redacted_output_digest_sha256: str,
    current_artifact_digest_sha256: str,
    p7_w2_result_digest_sha256: str,
    outcome: OperationalReceiptOutcome,
    evaluated_at: datetime,
) -> OperationalReceipt:
    _validate_operational_receipt_outcome(outcome)
    _validate_closed_enum_registry(OperationalEvidenceScope)
    _validate_closed_enum_registry(ProductionEvidenceState)
    spec = _flow_spec(flow)
    _require_positive_int(attempt, "invalid-operational-attempt")
    normalized = _utc(evaluated_at, "invalid-receipt-evaluation-time")
    base = {
        "schema_version": 1,
        "contract_version": P7_W3_OPERATIONAL_VERSION,
        "operational_manifest_digest_sha256": OPERATIONAL_MANIFEST_DIGEST_SHA256,
        "run_id": run_id,
        "receipt_id": "rcpt_" + "0" * 32,
        "idempotency_key_sha256": "0" * 64,
        "flow": flow,
        "ordinal": spec.ordinal,
        "attempt": attempt,
        "runbook_step_id": spec.runbook_step_id,
        "outcome": outcome,
        "scope": OperationalEvidenceScope.SYNTHETIC,
        "predecessor_receipt_digest_sha256": predecessor_receipt_digest_sha256,
        "input_digest_sha256": input_digest_sha256,
        "redacted_output_digest_sha256": redacted_output_digest_sha256,
        "current_artifact_digest_sha256": current_artifact_digest_sha256,
        "p7_w2_result_digest_sha256": p7_w2_result_digest_sha256,
        "stage_payload_digest_sha256": "0" * 64,
        "evaluated_at": normalized,
        "redacted": True,
        "real_flow_state": ProductionEvidenceState.NOT_PERFORMED_OWNER_GATED,
        "production_state": ProductionEvidenceState.NOT_PERFORMED_OWNER_GATED,
        "receipt_digest_sha256": "0" * 64,
    }
    receipt = object.__new__(OperationalReceipt)
    for name, value in base.items():
        object.__setattr__(receipt, name, value)
    key = _digest_value(_idempotency_payload(receipt))
    object.__setattr__(receipt, "idempotency_key_sha256", key)
    object.__setattr__(receipt, "receipt_id", f"rcpt_{key[:32]}")
    object.__setattr__(
        receipt, "stage_payload_digest_sha256", _digest_value(_stage_payload(receipt))
    )
    object.__setattr__(receipt, "receipt_digest_sha256", _receipt_digest(receipt))
    _validate_operational_receipt(receipt)
    return receipt


def _bundle_payload(bundle: OperationalEvidenceBundle) -> dict[str, object]:
    return {
        "schema_version": bundle.schema_version,
        "contract_version": bundle.contract_version,
        "operational_manifest_digest_sha256": bundle.operational_manifest_digest_sha256,
        "run_id": bundle.run_id,
        "attempt": bundle.attempt,
        "current_artifact_digest_sha256": bundle.current_artifact_digest_sha256,
        "p7_w2_result_digest_sha256": bundle.p7_w2_result_digest_sha256,
        "scope": bundle.scope.value,
        "evaluated_at": _timestamp(bundle.evaluated_at),
        "receipts": [receipt.to_dict() for receipt in bundle.receipts],
    }


@dataclass(frozen=True, slots=True)
class OperationalEvidenceBundle:
    schema_version: int
    contract_version: str
    operational_manifest_digest_sha256: str
    run_id: str
    attempt: int
    current_artifact_digest_sha256: str
    p7_w2_result_digest_sha256: str
    scope: OperationalEvidenceScope
    evaluated_at: datetime
    receipts: tuple[OperationalReceipt, ...]
    bundle_digest_sha256: str

    def __post_init__(self) -> None:
        _validate_operational_bundle(self)

    def to_dict(self) -> dict[str, object]:
        _validate_operational_bundle(self)
        return {**_bundle_payload(self), "bundle_digest_sha256": self.bundle_digest_sha256}


def _validate_operational_bundle(bundle: OperationalEvidenceBundle) -> None:
    _validate_operational_receipt_outcome_registry()
    if type(bundle) is not OperationalEvidenceBundle or type(bundle.receipts) is not tuple:
        _fail("invalid-operational-bundle")
    _require_closed_enum_member(
        bundle.scope,
        OperationalEvidenceScope,
        "invalid-operational-bundle-scope",
    )
    if len(bundle.receipts) < len(OPERATIONAL_FLOW_MANIFEST):
        _fail("missing-operational-receipts", missing=True)
    if len(bundle.receipts) != len(OPERATIONAL_FLOW_MANIFEST):
        _fail("invalid-operational-receipt-count")
    if any(type(receipt) is not OperationalReceipt for receipt in bundle.receipts):
        _fail("invalid-operational-receipt-type")
    if (
        type(bundle.schema_version) is not int
        or bundle.schema_version != 1
        or type(bundle.contract_version) is not str
        or bundle.contract_version != P7_W3_OPERATIONAL_VERSION
        or bundle.operational_manifest_digest_sha256 != OPERATIONAL_MANIFEST_DIGEST_SHA256
        or type(bundle.run_id) is not str
        or _RUN_ID.fullmatch(bundle.run_id) is None
        or type(bundle.attempt) is not int
        or bundle.attempt < 1
        or type(bundle.scope) is not OperationalEvidenceScope
        or bundle.scope is not OperationalEvidenceScope.SYNTHETIC
    ):
        _fail("invalid-operational-bundle")
    for value, code in (
        (bundle.operational_manifest_digest_sha256, "invalid-operational-manifest-digest"),
        (bundle.current_artifact_digest_sha256, "invalid-current-artifact-digest"),
        (bundle.p7_w2_result_digest_sha256, "invalid-p7-w2-result-digest"),
        (bundle.bundle_digest_sha256, "invalid-operational-bundle-digest"),
    ):
        _require_digest(value, code)
    evaluated_at = _utc(bundle.evaluated_at, "invalid-bundle-evaluation-time")
    if evaluated_at != bundle.evaluated_at or bundle.evaluated_at.utcoffset() != UTC.utcoffset(
        None
    ):
        _fail("invalid-bundle-evaluation-time")
    predecessor = OPERATIONAL_GENESIS_DIGEST_SHA256
    receipt_ids: set[str] = set()
    idempotency_keys: set[str] = set()
    for spec, receipt in zip(OPERATIONAL_FLOW_MANIFEST, bundle.receipts, strict=True):
        _validate_operational_receipt(receipt)
        if receipt.flow is not spec.flow or receipt.ordinal != spec.ordinal:
            _fail("invalid-operational-receipt-order")
        if receipt.predecessor_receipt_digest_sha256 != predecessor:
            _fail("invalid-operational-receipt-chain")
        if receipt.receipt_id in receipt_ids or receipt.idempotency_key_sha256 in idempotency_keys:
            _fail("duplicate-operational-receipt-identity")
        if (
            receipt.run_id != bundle.run_id
            or receipt.attempt != bundle.attempt
            or receipt.current_artifact_digest_sha256 != bundle.current_artifact_digest_sha256
            or receipt.p7_w2_result_digest_sha256 != bundle.p7_w2_result_digest_sha256
            or receipt.scope is not bundle.scope
            or receipt.evaluated_at != bundle.evaluated_at
        ):
            _fail("mixed-operational-bundle-bindings")
        receipt_ids.add(receipt.receipt_id)
        idempotency_keys.add(receipt.idempotency_key_sha256)
        predecessor = receipt.receipt_digest_sha256
    if bundle.bundle_digest_sha256 != _digest_value(_bundle_payload(bundle)):
        _fail("conflicting-operational-bundle-digest")


def make_operational_evidence_bundle(
    receipts: tuple[OperationalReceipt, ...],
) -> OperationalEvidenceBundle:
    if type(receipts) is not tuple or not receipts or type(receipts[0]) is not OperationalReceipt:
        _fail("invalid-operational-receipts")
    first = receipts[0]
    bundle = object.__new__(OperationalEvidenceBundle)
    values = {
        "schema_version": 1,
        "contract_version": P7_W3_OPERATIONAL_VERSION,
        "operational_manifest_digest_sha256": OPERATIONAL_MANIFEST_DIGEST_SHA256,
        "run_id": first.run_id,
        "attempt": first.attempt,
        "current_artifact_digest_sha256": first.current_artifact_digest_sha256,
        "p7_w2_result_digest_sha256": first.p7_w2_result_digest_sha256,
        "scope": OperationalEvidenceScope.SYNTHETIC,
        "evaluated_at": first.evaluated_at,
        "receipts": receipts,
        "bundle_digest_sha256": "0" * 64,
    }
    for name, value in values.items():
        object.__setattr__(bundle, name, value)
    object.__setattr__(bundle, "bundle_digest_sha256", _digest_value(_bundle_payload(bundle)))
    _validate_operational_bundle(bundle)
    return bundle


_W0_SCENARIOS = (
    (
        "youtube_playlist_hold",
        "a514723e4a96a394b711f705b4a6a91497adf20c895a8c01a8d18d89d03198c9",
        "1e3eee4d6001b9fef9c2f9980d3ebb135a810412e638fa382303d25969fa926a",
        (
            "blocked-difference",
            "blocked-difference",
            "match",
            "blocked-difference",
            "blocked-difference",
            "match",
            "match",
            "blocked-difference",
            "blocked-difference",
        ),
        (True, False, False, False, False, False, False, False, True),
    ),
    (
        "social_reference",
        "ace0c350ef1f22b022ac432a812a61cd3f13d11bcd86c3ff4b561d87f4eeaf39",
        "ef1f6a09a9a8092a2fc18533ddecb936e20a63c649ce7c7662aa8f15e0f57b83",
        (
            "blocked-difference",
            "blocked-difference",
            "blocked-difference",
            "blocked-difference",
            "match",
            "match",
            "match",
            "blocked-difference",
            "blocked-difference",
        ),
        (True, False, False, False, False, False, False, False, False),
    ),
    (
        "saved_web_reference",
        "54ae3f7f9d22ddde64bfce8869ae26c44d82327354cdab3e12eed64daf90c37b",
        "0b373d5fdfcd7e1accb6683a1929fbc6d10c2a2605f1554b7f6c65e0171af288",
        (
            "blocked-difference",
            "blocked-difference",
            "blocked-difference",
            "blocked-difference",
            "match",
            "match",
            "match",
            "blocked-difference",
            "blocked-difference",
        ),
        (True, False, False, False, False, False, False, False, False),
    ),
    (
        "idea_candidate",
        "95be7592bf5278d4d6118dd2621a0db6d80c0e6ea7a14c42082ccf0d0c388a96",
        "1898424f5cf3475abd612eafd381d065e522cffb594b30aeb450aff98700798b",
        (
            "blocked-difference",
            "blocked-difference",
            "blocked-difference",
            "blocked-difference",
            "blocked-difference",
            "match",
            "blocked-difference",
            "blocked-difference",
            "blocked-difference",
        ),
        (True, False, False, False, True, False, False, False, False),
    ),
    (
        "third_party_action_candidate",
        "2d29e195c004098115cdeba19ac3eea86ae93271e42ef9af032ec565bcbb6462",
        "7335becb16422f398131bb6b26e5038bd960e152e321271dada323065158e63c",
        (
            "blocked-difference",
            "blocked-difference",
            "blocked-difference",
            "blocked-difference",
            "blocked-difference",
            "match",
            "blocked-difference",
            "blocked-difference",
            "blocked-difference",
        ),
        (True, False, False, False, True, False, False, False, False),
    ),
)

_W0_RESULT_PROJECTION: list[dict[str, object]] = [
    {
        "scenario": scenario,
        "comparison_digest_sha256": comparison,
        "facets": [
            {
                "facet": f"PAR7-{index:03d}",
                "outcome": outcomes[index - 1],
                "unavailable": unavailable[index - 1],
                "artifact_attestation_digest_sha256": attestation,
            }
            for index in range(1, 10)
        ],
    }
    for scenario, comparison, attestation, outcomes, unavailable in _W0_SCENARIOS
]

_W1_RESULT_PROJECTION: dict[str, object] = {
    "manifest_version": "phase7-wave1-shadow-v1",
    "schema_digest_sha256": "290e7aeded08fb27b049985b72b451d16e4f05d45a43f6d40011fb32ccf957dc",
    "comparison_digest_sha256": "cdc903d0da8d2dab9c72e52de932e0b5ff813fc7d17949e0279ce3705609d3f3",
    "matching": {
        "case_id": "matching",
        "expected_disposition": "resolved",
        "observed_disposition": "resolved",
        "reason_code": "MATCH",
        "resolved": True,
        "verifier_called": True,
    },
}

_W2_RESULT_PROJECTION = {
    "green_run": {
        "result_digest_sha256": P7_W2_GREEN_RESULT_DIGEST_SHA256,
        "outcome": "synthetic-green",
        "receipt_set_digest_sha256": (
            "69904525bbd0da555eab522f5be92d2840231819b2e13bbbe6d9fbeace2f3067"
        ),
        "terminal_receipt_digest_sha256": (
            "e2df49b3ed3de1e214436782e8d56514cf5e23f1827f72a5e68f489647555d62"
        ),
    },
    "artifact_attestation_digest_sha256": (
        "b4014090e1a3d967380b29eac00b2efb6ef100d7114f29161d8888ef03623dca"
    ),
}

_PRIOR_TRUST_PAYLOAD: dict[str, object] = {
    "version": P7_W3_PRIOR_TRUST_VERSION,
    "waves": [
        {
            "wave": "P7-W0",
            "source_commit": "36f7c0d2a37b48abfce6bc26249345c5712b6c14",
            "evidence_sha256": "b4179b5aa6838499111fae67de10255599f6d5da99cc2d90f357f2e79660d02a",
            "artifact_sha256": "d871ab7e0ec46362c5dc605a0f552ccfcc784e85242cbdee4e2215da5af91b0a",
            "result_set_sha256": P7_W0_RESULT_SET_DIGEST_SHA256,
            "final_report_sha256": (
                "d3200a70dd2d5433f8842d3f3ed3332906130261d4d9bb7591b76edfc651f5d6"
            ),
        },
        {
            "wave": "P7-W1",
            "source_commit": "8de2f56a8819df1e8ddd373e3a39289e498a72fc",
            "evidence_sha256": "741c682a5610f7aaa250b2cfce0368b39820ea6775ceecbe18208c5ce8a01124",
            "artifact_sha256": "d262fb5574db8b1b1de64afa2a1d98c0a9dccddc0f8d49fa24d81779e0a157ca",
            "result_set_sha256": P7_W1_RESULT_SET_DIGEST_SHA256,
            "comparison_digest_sha256": (
                "cdc903d0da8d2dab9c72e52de932e0b5ff813fc7d17949e0279ce3705609d3f3"
            ),
            "security_report_sha256": (
                "7c6fc59f540319cb4fd986495908576b9fbd2e3bdc738cc9008a314c5ed54edb"
            ),
            "final_report_sha256": (
                "163936c58fb01498bf68d2d3960be5b292048f7b7decdf595c2735136b5b71ae"
            ),
        },
        {
            "wave": "P7-W2",
            "source_commit": "fd543caa1ef4dfd921d32df4ca7e5af980bd704a",
            "evidence_commit": "016a6fb85c50314706b0551bf765aa1e6e595227",
            "evidence_sha256": "a81f1509d200c18a98e57e97cd36dfffb6f5699aca4cbbcc6b0d0580d361845e",
            "artifact_sha256": "2e65cacc46189cca4e1d8e06e1af850eca2356d926f51b73eb9cdfcb9237506c",
            "artifact_attestation_sha256": (
                "b4014090e1a3d967380b29eac00b2efb6ef100d7114f29161d8888ef03623dca"
            ),
            "result_set_sha256": P7_W2_RESULT_SET_DIGEST_SHA256,
            "result_digest_sha256": P7_W2_GREEN_RESULT_DIGEST_SHA256,
            "security_report_sha256": (
                "c4a387855106991866895a8874943332b105bd0123df3a42ca1671bb53401a64"
            ),
            "final_report_sha256": (
                "c37a5915b3f79ba0bb77be6770d69cdfcb1f3c595b5b062d46a38a81f3e83eef"
            ),
        },
    ],
}


@dataclass(frozen=True, slots=True)
class TrustedSourceRow:
    wave: str
    scenario_id: str
    source_result_digest_sha256: str
    facet_id: str
    state: TrustedParityRowState
    unavailable: bool
    source_artifact_digest_sha256: str
    source_attestation_digest_sha256: str | None

    def to_dict(self) -> dict[str, object]:
        _validate_trusted_source_row(self)
        payload: dict[str, object] = {
            "wave": self.wave,
            "scenario_id": self.scenario_id,
            "source_result_digest_sha256": self.source_result_digest_sha256,
            "facet_id": self.facet_id,
            "state": self.state.value,
            "unavailable": self.unavailable,
            "source_artifact_digest_sha256": self.source_artifact_digest_sha256,
        }
        if self.source_attestation_digest_sha256 is not None:
            payload["source_attestation_digest_sha256"] = self.source_attestation_digest_sha256
        return payload


def _validate_trusted_source_row(row: TrustedSourceRow) -> None:
    if (
        type(row) is not TrustedSourceRow
        or row.wave not in {"P7-W0", "P7-W1"}
        or type(row.scenario_id) is not str
        or not row.scenario_id
        or type(row.facet_id) is not str
        or re.fullmatch(r"PAR7-[0-9]{3}", row.facet_id) is None
        or type(row.state) is not TrustedParityRowState
        or type(row.unavailable) is not bool
    ):
        _fail("invalid-trusted-source-row")
    _require_closed_enum_member(
        row.state,
        TrustedParityRowState,
        "invalid-trusted-source-row-state",
    )
    _require_digest(row.source_result_digest_sha256, "invalid-source-result-digest")
    _require_digest(row.source_artifact_digest_sha256, "invalid-source-artifact-digest")
    if row.source_attestation_digest_sha256 is not None:
        _require_digest(row.source_attestation_digest_sha256, "invalid-source-attestation-digest")


def _trusted_rows_from_projections() -> tuple[TrustedSourceRow, ...]:
    _validate_closed_enum_registry(TrustedParityRowState)
    rows: list[TrustedSourceRow] = []
    for scenario in _W0_RESULT_PROJECTION:
        scenario_id = scenario["scenario"]
        comparison = scenario["comparison_digest_sha256"]
        facets = scenario["facets"]
        if type(scenario_id) is not str or type(comparison) is not str or type(facets) is not list:
            _fail("invalid-w0-result-projection")
        for facet in facets:
            if type(facet) is not dict:
                _fail("invalid-w0-result-projection")
            try:
                state = TrustedParityRowState(facet["outcome"])
                row = TrustedSourceRow(
                    wave="P7-W0",
                    scenario_id=scenario_id,
                    source_result_digest_sha256=comparison,
                    facet_id=facet["facet"],
                    state=state,
                    unavailable=facet["unavailable"],
                    source_artifact_digest_sha256=(
                        "d871ab7e0ec46362c5dc605a0f552ccfcc784e85242cbdee4e2215da5af91b0a"
                    ),
                    source_attestation_digest_sha256=facet["artifact_attestation_digest_sha256"],
                )
            except (KeyError, TypeError, ValueError):
                _fail("invalid-w0-result-projection")
            _validate_trusted_source_row(row)
            rows.append(row)
    matching = _W1_RESULT_PROJECTION.get("matching")
    comparison = _W1_RESULT_PROJECTION.get("comparison_digest_sha256")
    if type(matching) is not dict or type(comparison) is not str:
        _fail("invalid-w1-result-projection")
    if matching != {
        "case_id": "matching",
        "expected_disposition": "resolved",
        "observed_disposition": "resolved",
        "reason_code": "MATCH",
        "resolved": True,
        "verifier_called": True,
    }:
        _fail("invalid-w1-result-projection")
    rows.append(
        TrustedSourceRow(
            wave="P7-W1",
            scenario_id="matching",
            source_result_digest_sha256=comparison,
            facet_id="PAR7-010",
            state=TrustedParityRowState.MATCH,
            unavailable=False,
            source_artifact_digest_sha256=(
                "d262fb5574db8b1b1de64afa2a1d98c0a9dccddc0f8d49fa24d81779e0a157ca"
            ),
            source_attestation_digest_sha256=None,
        )
    )
    if len(rows) != 46:
        _fail("invalid-trusted-source-row-count")
    return tuple(rows)


def _derive_trusted_source_rows() -> tuple[TrustedSourceRow, ...]:
    return _trusted_rows_from_projections()


P7_W3_TRUSTED_SOURCE_ROWS_DIGEST_SHA256 = (
    "ebe07828809df6d461e3086b5897ef99ccec86dd6031d6a717e85c079e18997b"
)
_UNAVAILABLE_TRUSTED_SOURCE_ROWS_DIGEST_SHA256 = _digest_value(
    {"unavailable": "trusted-source-rows"}
)


@dataclass(frozen=True, slots=True, init=False)
class P7W3PriorTrustManifest:
    _payload_json: str
    _trusted_rows: tuple[TrustedSourceRow, ...]
    _manifest_digest_sha256: str

    def __init__(self) -> None:
        raise TypeError("P7W3PriorTrustManifest is factory-created")

    @classmethod
    def _create(
        cls,
        payload_json: str,
        trusted_rows: tuple[TrustedSourceRow, ...],
    ) -> P7W3PriorTrustManifest:
        value = cls.__new__(cls)
        object.__setattr__(value, "_payload_json", payload_json)
        object.__setattr__(value, "_trusted_rows", trusted_rows)
        object.__setattr__(
            value, "_manifest_digest_sha256", _digest_value(json.loads(payload_json))
        )
        value._validate()
        return value

    def _validate(self) -> None:
        try:
            payload = json.loads(self._payload_json)
        except (TypeError, json.JSONDecodeError):
            _fail("invalid-prior-trust-payload")
        if (
            payload != _PRIOR_TRUST_PAYLOAD
            or _digest_value(payload) != P7_W3_PRIOR_TRUST_MANIFEST_DIGEST_SHA256
            or self._manifest_digest_sha256 != P7_W3_PRIOR_TRUST_MANIFEST_DIGEST_SHA256
            or _digest_value(_W0_RESULT_PROJECTION) != P7_W0_RESULT_SET_DIGEST_SHA256
            or _digest_value(_W1_RESULT_PROJECTION) != P7_W1_RESULT_SET_DIGEST_SHA256
            or _digest_value(_W2_RESULT_PROJECTION) != P7_W2_RESULT_SET_DIGEST_SHA256
            or self._trusted_rows != _trusted_rows_from_projections()
        ):
            _fail("conflicting-prior-trust-manifest")
        _validate_prior_wave_payload(payload)

    @property
    def manifest_digest_sha256(self) -> str:
        self._validate()
        return self._manifest_digest_sha256

    @property
    def trusted_rows(self) -> tuple[TrustedSourceRow, ...]:
        self._validate()
        return self._trusted_rows

    @property
    def trusted_rows_digest_sha256(self) -> str:
        self._validate()
        return _digest_value([row.to_dict() for row in self._trusted_rows])

    def to_dict(self) -> dict[str, object]:
        self._validate()
        payload = json.loads(self._payload_json)
        if type(payload) is not dict:
            _fail("invalid-prior-trust-payload")
        return payload


def _validate_prior_wave_payload(payload: object) -> None:
    if type(payload) is not dict or set(payload) != {"version", "waves"}:
        _fail("invalid-prior-trust-payload")
    if payload["version"] != P7_W3_PRIOR_TRUST_VERSION or type(payload["waves"]) is not list:
        _fail("invalid-prior-trust-payload")
    waves = payload["waves"]
    if len(waves) != 3 or [wave.get("wave") for wave in waves] != ["P7-W0", "P7-W1", "P7-W2"]:
        _fail("invalid-prior-trust-wave-order")
    for wave in waves:
        if type(wave) is not dict:
            _fail("invalid-prior-trust-wave")
        for key, value in wave.items():
            if key == "wave":
                if type(value) is not str:
                    _fail("invalid-prior-trust-wave")
            elif key in {"source_commit", "evidence_commit"}:
                _require_commit(value, "invalid-prior-trust-commit")
            else:
                _require_digest(value, "invalid-prior-trust-digest")


def p7_w3_prior_trust_manifest() -> P7W3PriorTrustManifest:
    payload_json = _canonical_bytes(_PRIOR_TRUST_PAYLOAD).decode("utf-8")
    return P7W3PriorTrustManifest._create(payload_json, _trusted_rows_from_projections())


_STALE_REFERENCE_SURFACES = tuple(
    member for _, _, member in _CLOSED_ENUM_SEALS[CutoverSurface]
)


def _stale_reference_scan_policy_payload() -> dict[str, object]:
    return {
        "version": P7_W3_STALE_REFERENCE_SCAN_VERSION,
        "surfaces": [
            {"ordinal": spec.ordinal, "surface": spec.surface.value}
            for spec in CUTOVER_SURFACE_MANIFEST
        ],
    }


STALE_REFERENCE_SCAN_POLICY_DIGEST_SHA256 = _digest_value(
    _stale_reference_scan_policy_payload()
)


def _validate_stale_surface_registry() -> None:
    _validate_closed_enum_registry(CutoverSurface)
    try:
        manifest_rows = tuple(
            (spec.ordinal, spec.surface) for spec in CUTOVER_SURFACE_MANIFEST
        )
        live_policy_digest = _digest_value(_stale_reference_scan_policy_payload())
    except (AttributeError, TypeError):
        _fail("invalid-stale-reference-registry")
    expected_manifest_rows = tuple(enumerate(_STALE_REFERENCE_SURFACES, start=1))
    if len(manifest_rows) != 8 or any(
        type(ordinal) is not int
        or ordinal != expected_ordinal
        or surface is not expected_surface
        for (ordinal, surface), (expected_ordinal, expected_surface) in zip(
            manifest_rows,
            expected_manifest_rows,
            strict=True,
        )
    ):
        _fail("invalid-stale-reference-registry")
    if live_policy_digest != STALE_REFERENCE_SCAN_POLICY_DIGEST_SHA256:
        _fail("invalid-stale-reference-scan-policy")


@dataclass(frozen=True, slots=True)
class StaleReferenceEvidence:
    schema_version: int
    reconciliation_version: str
    surface: CutoverSurface
    ordinal: int
    scan_policy_version: str
    scan_policy_digest_sha256: str
    current_artifact_digest_sha256: str
    redacted_result_count: int
    state: StaleReferenceState
    production_state: ProductionEvidenceState
    evaluated_at: datetime
    authority_reference_digest_sha256: str
    redacted: bool

    def __post_init__(self) -> None:
        _validate_stale_reference(self)

    def to_dict(self) -> dict[str, object]:
        _validate_stale_reference(self)
        return {
            "schema_version": self.schema_version,
            "reconciliation_version": self.reconciliation_version,
            "surface": self.surface.value,
            "ordinal": self.ordinal,
            "scan_policy_version": self.scan_policy_version,
            "scan_policy_digest_sha256": self.scan_policy_digest_sha256,
            "current_artifact_digest_sha256": self.current_artifact_digest_sha256,
            "redacted_result_count": self.redacted_result_count,
            "state": self.state.value,
            "production_state": self.production_state.value,
            "evaluated_at": _timestamp(self.evaluated_at),
            "authority_reference_digest_sha256": self.authority_reference_digest_sha256,
            "redacted": self.redacted,
        }


def _validate_stale_reference(value: StaleReferenceEvidence) -> None:
    _validate_stale_surface_registry()
    if type(value) is not StaleReferenceEvidence:
        _fail("invalid-stale-reference")
    _require_closed_enum_member(
        value.surface,
        CutoverSurface,
        "invalid-stale-reference-surface",
    )
    _require_closed_enum_member(
        value.state,
        StaleReferenceState,
        "invalid-stale-reference-state",
    )
    _require_closed_enum_member(
        value.production_state,
        ProductionEvidenceState,
        "invalid-stale-production-state",
    )
    ordinal = _STALE_REFERENCE_SURFACES.index(value.surface) + 1
    if (
        type(value.schema_version) is not int
        or value.schema_version != 1
        or value.reconciliation_version != P7_W3_RECONCILIATION_VERSION
        or type(value.ordinal) is not int
        or value.ordinal != ordinal
        or value.scan_policy_version != P7_W3_STALE_REFERENCE_SCAN_VERSION
        or value.scan_policy_digest_sha256 != STALE_REFERENCE_SCAN_POLICY_DIGEST_SHA256
        or type(value.redacted_result_count) is not int
        or value.redacted_result_count < 0
        or type(value.state) is not StaleReferenceState
        or type(value.production_state) is not ProductionEvidenceState
        or value.production_state is not ProductionEvidenceState.NOT_PERFORMED_OWNER_GATED
        or value.redacted is not True
    ):
        _fail("invalid-stale-reference")
    _require_digest(value.scan_policy_digest_sha256, "invalid-stale-scan-policy-digest")
    _require_digest(value.current_artifact_digest_sha256, "invalid-stale-artifact-digest")
    _require_digest(value.authority_reference_digest_sha256, "invalid-stale-authority-digest")
    evaluated_at = _utc(value.evaluated_at, "invalid-stale-evaluation-time")
    if evaluated_at != value.evaluated_at or value.evaluated_at.utcoffset() != UTC.utcoffset(None):
        _fail("invalid-stale-evaluation-time")
    if value.state is StaleReferenceState.PASSED_SYNTHETIC and value.redacted_result_count != 0:
        _fail("contradictory-stale-reference-count")
    if value.state is StaleReferenceState.FAILED and value.redacted_result_count < 1:
        _fail("contradictory-stale-reference-count")


def make_stale_reference_evidence(
    *,
    surface: CutoverSurface,
    current_artifact_digest_sha256: str,
    redacted_result_count: int,
    state: StaleReferenceState,
    authority_reference_digest_sha256: str,
    evaluated_at: datetime,
) -> StaleReferenceEvidence:
    _validate_stale_surface_registry()
    _validate_closed_enum_registry(StaleReferenceState)
    _validate_closed_enum_registry(ProductionEvidenceState)
    _require_closed_enum_member(
        surface,
        CutoverSurface,
        "invalid-stale-reference-surface",
    )
    _require_nonnegative_int(redacted_result_count, "invalid-stale-result-count")
    return StaleReferenceEvidence(
        schema_version=1,
        reconciliation_version=P7_W3_RECONCILIATION_VERSION,
        surface=surface,
        ordinal=_STALE_REFERENCE_SURFACES.index(surface) + 1,
        scan_policy_version=P7_W3_STALE_REFERENCE_SCAN_VERSION,
        scan_policy_digest_sha256=STALE_REFERENCE_SCAN_POLICY_DIGEST_SHA256,
        current_artifact_digest_sha256=current_artifact_digest_sha256,
        redacted_result_count=redacted_result_count,
        state=state,
        production_state=ProductionEvidenceState.NOT_PERFORMED_OWNER_GATED,
        evaluated_at=_utc(evaluated_at, "invalid-stale-evaluation-time"),
        authority_reference_digest_sha256=authority_reference_digest_sha256,
        redacted=True,
    )


def _attestation_statement(value: P7W3ArtifactAttestationEvidence) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "reconciliation_version": value.reconciliation_version,
        "operational_manifest_digest_sha256": value.operational_manifest_digest_sha256,
        "prior_trust_manifest_digest_sha256": value.prior_trust_manifest_digest_sha256,
        "source_commit_sha": value.source_commit_sha,
        "artifact_distribution": value.artifact_distribution,
        "artifact_version": value.artifact_version,
        "artifact_digest_sha256": value.artifact_digest_sha256,
        "scope": value.scope.value,
        "evaluated_at": _timestamp(value.evaluated_at),
        "expires_at": _timestamp(value.expires_at),
        "verification_class": value.verification_class.value,
    }


@dataclass(frozen=True, slots=True)
class P7W3ArtifactAttestationEvidence:
    schema_version: str
    reconciliation_version: str
    operational_manifest_digest_sha256: str
    prior_trust_manifest_digest_sha256: str
    source_commit_sha: str
    artifact_distribution: str
    artifact_version: str
    artifact_digest_sha256: str
    scope: OperationalEvidenceScope
    evaluated_at: datetime
    expires_at: datetime
    verification_class: ArtifactVerificationClass
    verifier_id: str
    statement_digest_sha256: str
    attestation_digest_sha256: str

    def __post_init__(self) -> None:
        _validate_artifact_attestation(self)

    def to_dict(self) -> dict[str, object]:
        _validate_artifact_attestation(self)
        return {
            **_attestation_statement(self),
            "verifier_id": self.verifier_id,
            "statement_digest_sha256": self.statement_digest_sha256,
            "attestation_digest_sha256": self.attestation_digest_sha256,
        }


def _validate_artifact_attestation(
    value: P7W3ArtifactAttestationEvidence,
    *,
    reconciliation_time: datetime | None = None,
) -> None:
    _validate_operational_manifest()
    if type(value) is not P7W3ArtifactAttestationEvidence:
        _fail("invalid-p7-w3-artifact-attestation")
    _require_closed_enum_member(
        value.scope,
        OperationalEvidenceScope,
        "invalid-artifact-attestation-scope",
    )
    _require_closed_enum_member(
        value.verification_class,
        ArtifactVerificationClass,
        "invalid-artifact-verification-class",
    )
    if (
        value.schema_version != P7_W3_ARTIFACT_ATTESTATION_SCHEMA_VERSION
        or value.reconciliation_version != P7_W3_RECONCILIATION_VERSION
        or value.operational_manifest_digest_sha256 != OPERATIONAL_MANIFEST_DIGEST_SHA256
        or value.prior_trust_manifest_digest_sha256 != P7_W3_PRIOR_TRUST_MANIFEST_DIGEST_SHA256
        or type(value.artifact_distribution) is not str
        or _ARTIFACT_DISTRIBUTION.fullmatch(value.artifact_distribution) is None
        or type(value.artifact_version) is not str
        or _ARTIFACT_VERSION.fullmatch(value.artifact_version) is None
        or type(value.scope) is not OperationalEvidenceScope
        or value.scope is not OperationalEvidenceScope.SYNTHETIC
        or type(value.verification_class) is not ArtifactVerificationClass
        or value.verification_class
        is not ArtifactVerificationClass.COORDINATOR_SHA256_WHEEL_BINDING
    ):
        _fail("invalid-p7-w3-artifact-attestation")
    _require_digest(value.operational_manifest_digest_sha256, "invalid-operational-manifest-digest")
    _require_digest(value.prior_trust_manifest_digest_sha256, "invalid-prior-trust-digest")
    _require_commit(value.source_commit_sha, "invalid-attestation-source-commit")
    _require_digest(value.artifact_digest_sha256, "invalid-attested-artifact-digest")
    _require_digest(value.statement_digest_sha256, "invalid-attestation-statement-digest")
    _require_digest(value.attestation_digest_sha256, "invalid-attestation-digest")
    evaluated_at = _utc(value.evaluated_at, "invalid-attestation-evaluation-time")
    expires_at = _utc(value.expires_at, "invalid-attestation-expiry-time")
    if evaluated_at >= expires_at:
        _fail("invalid-attestation-time-window")
    statement_digest = _digest_value(_attestation_statement(value))
    if (
        value.statement_digest_sha256 != statement_digest
        or value.attestation_digest_sha256 != statement_digest
        or value.verifier_id != f"artifactverifier_{statement_digest}"
    ):
        _fail("conflicting-p7-w3-artifact-attestation")
    if reconciliation_time is not None:
        current = _utc(reconciliation_time, "invalid-reconciliation-time")
        if not evaluated_at <= current < expires_at:
            _fail("attestation-outside-reconciliation-window")


def make_p7_w3_artifact_attestation(
    *,
    source_commit_sha: str,
    artifact_distribution: str,
    artifact_version: str,
    artifact_digest_sha256: str,
    evaluated_at: datetime,
    expires_at: datetime,
) -> P7W3ArtifactAttestationEvidence:
    _validate_closed_enum_registry(OperationalEvidenceScope)
    _validate_closed_enum_registry(ArtifactVerificationClass)
    value = object.__new__(P7W3ArtifactAttestationEvidence)
    values = {
        "schema_version": P7_W3_ARTIFACT_ATTESTATION_SCHEMA_VERSION,
        "reconciliation_version": P7_W3_RECONCILIATION_VERSION,
        "operational_manifest_digest_sha256": OPERATIONAL_MANIFEST_DIGEST_SHA256,
        "prior_trust_manifest_digest_sha256": P7_W3_PRIOR_TRUST_MANIFEST_DIGEST_SHA256,
        "source_commit_sha": source_commit_sha,
        "artifact_distribution": artifact_distribution,
        "artifact_version": artifact_version,
        "artifact_digest_sha256": artifact_digest_sha256,
        "scope": OperationalEvidenceScope.SYNTHETIC,
        "evaluated_at": _utc(evaluated_at, "invalid-attestation-evaluation-time"),
        "expires_at": _utc(expires_at, "invalid-attestation-expiry-time"),
        "verification_class": ArtifactVerificationClass.COORDINATOR_SHA256_WHEEL_BINDING,
        "verifier_id": "artifactverifier_" + "0" * 64,
        "statement_digest_sha256": "0" * 64,
        "attestation_digest_sha256": "0" * 64,
    }
    for name, item in values.items():
        object.__setattr__(value, name, item)
    statement_digest = _digest_value(_attestation_statement(value))
    object.__setattr__(value, "verifier_id", f"artifactverifier_{statement_digest}")
    object.__setattr__(value, "statement_digest_sha256", statement_digest)
    object.__setattr__(value, "attestation_digest_sha256", statement_digest)
    _validate_artifact_attestation(value)
    return value


def _validate_stale_references(
    values: tuple[StaleReferenceEvidence, ...],
    *,
    artifact_digest_sha256: str,
    evaluated_at: datetime,
) -> str:
    if type(values) is not tuple:
        _fail("invalid-stale-reference-set")
    if len(values) < 8:
        _fail("missing-stale-reference-evidence", missing=True)
    if len(values) != 8:
        _fail("invalid-stale-reference-count")
    if any(type(value) is not StaleReferenceEvidence for value in values):
        _fail("invalid-stale-reference-type")
    for spec, value in zip(CUTOVER_SURFACE_MANIFEST, values, strict=True):
        _validate_stale_reference(value)
        if value.surface is not spec.surface or value.ordinal != spec.ordinal:
            _fail("invalid-stale-reference-order")
        if (
            value.current_artifact_digest_sha256 != artifact_digest_sha256
            or value.evaluated_at != evaluated_at
        ):
            _fail("conflicting-stale-reference-binding")
        if value.state is StaleReferenceState.NOT_PERFORMED_OWNER_GATED:
            _fail("missing-stale-reference-scan", missing=True)
        if value.state is not StaleReferenceState.PASSED_SYNTHETIC:
            _fail("failed-stale-reference-scan")
    return _digest_value([value.to_dict() for value in values])


_PRODUCTION_CHECKS = tuple(ProductionCheck)


@dataclass(frozen=True, slots=True, init=False)
class Phase7ReconciliationResult:
    _outcome: Phase7ReconciliationOutcome
    _synthetic_operational_checkpoint_complete: bool
    _phase7_production_complete: bool
    _finding_classes: tuple[Phase7FindingClass, ...]
    _operational_bundle_digest_sha256: str
    _prior_trust_manifest_digest_sha256: str
    _trusted_source_rows_digest_sha256: str
    _stale_reference_rows_digest_sha256: str
    _current_artifact_digest_sha256: str
    _current_attestation_digest_sha256: str
    _trusted_match_count: int
    _trusted_blocked_difference_count: int
    _evaluated_at: datetime
    _sealed_payload_json: str
    _result_digest_sha256: str

    def __init__(self) -> None:
        raise TypeError("Phase7ReconciliationResult is reducer-created")

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "reconciliation_version": P7_W3_RECONCILIATION_VERSION,
            "outcome": self._outcome.value,
            "synthetic_operational_checkpoint_complete": (
                self._synthetic_operational_checkpoint_complete
            ),
            "phase7_production_complete": self._phase7_production_complete,
            "finding_classes": [finding.value for finding in self._finding_classes],
            "operational_bundle_digest_sha256": self._operational_bundle_digest_sha256,
            "prior_trust_manifest_digest_sha256": (self._prior_trust_manifest_digest_sha256),
            "trusted_source_rows_digest_sha256": self._trusted_source_rows_digest_sha256,
            "stale_reference_rows_digest_sha256": self._stale_reference_rows_digest_sha256,
            "current_artifact_digest_sha256": self._current_artifact_digest_sha256,
            "current_attestation_digest_sha256": self._current_attestation_digest_sha256,
            "trusted_match_count": self._trusted_match_count,
            "trusted_blocked_difference_count": self._trusted_blocked_difference_count,
            "evaluated_at": _timestamp(self._evaluated_at),
            "production_checks": [
                {
                    "check": check.value,
                    "state": ProductionEvidenceState.NOT_PERFORMED_OWNER_GATED.value,
                }
                for check in _PRODUCTION_CHECKS
            ],
            "redacted": True,
        }

    def _validate(self) -> None:
        _require_closed_enum_member(
            self._outcome,
            Phase7ReconciliationOutcome,
            "invalid-phase7-reconciliation-outcome",
        )
        _validate_closed_enum_registry(Phase7FindingClass)
        _validate_closed_enum_registry(ProductionEvidenceState)
        _validate_closed_enum_registry(ProductionCheck)
        for finding in self._finding_classes:
            _require_closed_enum_member(
                finding,
                Phase7FindingClass,
                "invalid-phase7-finding-class",
            )
        expected_checks = tuple(
            member for _, _, member in _CLOSED_ENUM_SEALS[ProductionCheck]
        )
        if len(_PRODUCTION_CHECKS) != len(expected_checks) or any(
            live is not expected
            for live, expected in zip(_PRODUCTION_CHECKS, expected_checks, strict=True)
        ):
            _fail("invalid-production-check-registry-binding")
        complete = self._outcome is _SYNTHETIC_CHECKPOINT_COMPLETE
        if (
            type(self._outcome) is not Phase7ReconciliationOutcome
            or type(self._synthetic_operational_checkpoint_complete) is not bool
            or self._synthetic_operational_checkpoint_complete is not complete
            or self._phase7_production_complete is not False
            or type(self._finding_classes) is not tuple
            or any(type(item) is not Phase7FindingClass for item in self._finding_classes)
            or (complete and self._finding_classes)
            or (not complete and len(self._finding_classes) != 1)
            or type(self._trusted_match_count) is not int
            or type(self._trusted_blocked_difference_count) is not int
            or self._trusted_match_count < 0
            or self._trusted_blocked_difference_count < 0
        ):
            _fail("invalid-phase7-reconciliation-result")
        if self._prior_trust_manifest_digest_sha256 != (
            P7_W3_PRIOR_TRUST_MANIFEST_DIGEST_SHA256
        ):
            _fail("conflicting-result-prior-trust-digest")
        expected_outcome: Phase7ReconciliationOutcome
        expected_rows_digest: str
        expected_match_count: int
        expected_blocked_count: int
        if complete:
            expected_outcome = _SYNTHETIC_CHECKPOINT_COMPLETE
            expected_rows_digest = P7_W3_TRUSTED_SOURCE_ROWS_DIGEST_SHA256
            expected_match_count = 12
            expected_blocked_count = 34
        else:
            finding = self._finding_classes[0]
            if finding in _MISSING_FINDINGS:
                expected_outcome = Phase7ReconciliationOutcome.BLOCKED_MISSING_EVIDENCE
            elif finding in _CONTRADICTION_FINDINGS:
                expected_outcome = Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION
            else:
                _fail("invalid-phase7-finding-class")
            if finding in _TRUSTED_ROWS_AVAILABLE_FINDINGS:
                expected_rows_digest = P7_W3_TRUSTED_SOURCE_ROWS_DIGEST_SHA256
                expected_match_count = 12
                expected_blocked_count = 34
            else:
                expected_rows_digest = _UNAVAILABLE_TRUSTED_SOURCE_ROWS_DIGEST_SHA256
                expected_match_count = 0
                expected_blocked_count = 0
        if (
            self._outcome is not expected_outcome
            or self._trusted_source_rows_digest_sha256 != expected_rows_digest
            or self._trusted_match_count != expected_match_count
            or self._trusted_blocked_difference_count != expected_blocked_count
        ):
            _fail("conflicting-phase7-reconciliation-semantics")
        if self._evaluated_at.tzinfo is not UTC:
            _fail("noncanonical-phase7-reconciliation-time")
        for value, code in (
            (self._operational_bundle_digest_sha256, "invalid-result-operational-digest"),
            (self._prior_trust_manifest_digest_sha256, "invalid-result-prior-trust-digest"),
            (self._trusted_source_rows_digest_sha256, "invalid-result-source-rows-digest"),
            (self._stale_reference_rows_digest_sha256, "invalid-result-stale-rows-digest"),
            (self._current_artifact_digest_sha256, "invalid-result-artifact-digest"),
            (self._current_attestation_digest_sha256, "invalid-result-attestation-digest"),
            (self._result_digest_sha256, "invalid-result-digest"),
        ):
            _require_digest(value, code)
        payload = self._payload()
        if self._sealed_payload_json != _canonical_bytes(payload).decode("utf-8"):
            _fail("conflicting-phase7-reconciliation-seal")
        if self._result_digest_sha256 != _digest_value(payload):
            _fail("conflicting-phase7-reconciliation-result")

    @property
    def outcome(self) -> Phase7ReconciliationOutcome:
        self._validate()
        return self._outcome

    @property
    def synthetic_operational_checkpoint_complete(self) -> bool:
        self._validate()
        return self._synthetic_operational_checkpoint_complete

    @property
    def phase7_production_complete(self) -> bool:
        self._validate()
        return self._phase7_production_complete

    @property
    def finding_classes(self) -> tuple[Phase7FindingClass, ...]:
        self._validate()
        return self._finding_classes

    @property
    def trusted_match_count(self) -> int:
        self._validate()
        return self._trusted_match_count

    @property
    def trusted_blocked_difference_count(self) -> int:
        self._validate()
        return self._trusted_blocked_difference_count

    @property
    def result_digest_sha256(self) -> str:
        self._validate()
        return self._result_digest_sha256

    def to_dict(self) -> dict[str, object]:
        self._validate()
        return {**self._payload(), "result_digest_sha256": self._result_digest_sha256}


def _make_reconciliation_result(
    *,
    outcome: Phase7ReconciliationOutcome,
    finding_classes: tuple[Phase7FindingClass, ...],
    operational_bundle_digest_sha256: str,
    prior_trust_manifest_digest_sha256: str,
    trusted_source_rows_digest_sha256: str,
    stale_reference_rows_digest_sha256: str,
    current_artifact_digest_sha256: str,
    current_attestation_digest_sha256: str,
    trusted_match_count: int,
    trusted_blocked_difference_count: int,
    evaluated_at: datetime,
) -> Phase7ReconciliationResult:
    result = Phase7ReconciliationResult.__new__(Phase7ReconciliationResult)
    complete = outcome is _SYNTHETIC_CHECKPOINT_COMPLETE
    values = {
        "_outcome": outcome,
        "_synthetic_operational_checkpoint_complete": complete,
        "_phase7_production_complete": False,
        "_finding_classes": finding_classes,
        "_operational_bundle_digest_sha256": operational_bundle_digest_sha256,
        "_prior_trust_manifest_digest_sha256": prior_trust_manifest_digest_sha256,
        "_trusted_source_rows_digest_sha256": trusted_source_rows_digest_sha256,
        "_stale_reference_rows_digest_sha256": stale_reference_rows_digest_sha256,
        "_current_artifact_digest_sha256": current_artifact_digest_sha256,
        "_current_attestation_digest_sha256": current_attestation_digest_sha256,
        "_trusted_match_count": trusted_match_count,
        "_trusted_blocked_difference_count": trusted_blocked_difference_count,
        "_evaluated_at": evaluated_at,
    }
    for name, item in values.items():
        object.__setattr__(result, name, item)
    payload = result._payload()
    object.__setattr__(result, "_sealed_payload_json", _canonical_bytes(payload).decode("utf-8"))
    object.__setattr__(result, "_result_digest_sha256", _digest_value(payload))
    result._validate()
    return result


def _safe_digest(value: object, attribute: str, fallback_label: str) -> str:
    candidate = getattr(value, attribute, None)
    if type(candidate) is str and _SHA256.fullmatch(candidate) is not None:
        return candidate
    return _digest_value({"unavailable": fallback_label})


def _safe_stale_digest(values: object) -> str:
    if type(values) is not tuple:
        return _digest_value({"invalid_stale_reference_set": True})
    try:
        return _digest_value([value.to_dict() for value in values])
    except (AttributeError, OperationsValidationError):
        return _digest_value({"invalid_stale_reference_count": len(values)})


def _blocked_result(
    *,
    missing: bool,
    finding: Phase7FindingClass,
    operational_bundle: object,
    stale_references: object,
    artifact_attestation: object,
    evaluated_at: datetime,
    prior_trust_digest: str = P7_W3_PRIOR_TRUST_MANIFEST_DIGEST_SHA256,
    trusted_rows_digest: str | None = None,
    match_count: int = 0,
    blocked_count: int = 0,
) -> Phase7ReconciliationResult:
    return _make_reconciliation_result(
        outcome=(
            Phase7ReconciliationOutcome.BLOCKED_MISSING_EVIDENCE
            if missing
            else Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION
        ),
        finding_classes=(finding,),
        operational_bundle_digest_sha256=_safe_digest(
            operational_bundle, "bundle_digest_sha256", "operational-bundle"
        ),
        prior_trust_manifest_digest_sha256=prior_trust_digest,
        trusted_source_rows_digest_sha256=(
            trusted_rows_digest
            if trusted_rows_digest is not None
            else _UNAVAILABLE_TRUSTED_SOURCE_ROWS_DIGEST_SHA256
        ),
        stale_reference_rows_digest_sha256=_safe_stale_digest(stale_references),
        current_artifact_digest_sha256=_safe_digest(
            artifact_attestation, "artifact_digest_sha256", "current-artifact"
        ),
        current_attestation_digest_sha256=_safe_digest(
            artifact_attestation, "attestation_digest_sha256", "current-attestation"
        ),
        trusted_match_count=match_count,
        trusted_blocked_difference_count=blocked_count,
        evaluated_at=evaluated_at,
    )


def reconcile_phase7(
    *,
    operational_bundle: OperationalEvidenceBundle,
    stale_references: tuple[StaleReferenceEvidence, ...],
    artifact_attestation: P7W3ArtifactAttestationEvidence,
    evaluated_at: datetime,
) -> Phase7ReconciliationResult:
    current = _utc(evaluated_at, "invalid-phase7-reconciliation-time")
    try:
        _validate_closed_enum_registry(OperationalReceiptOutcome)
    except OperationsValidationError:
        return _blocked_result(
            missing=False,
            finding=Phase7FindingClass.OPERATIONAL_CONTRADICTION,
            operational_bundle=operational_bundle,
            stale_references=stale_references,
            artifact_attestation=artifact_attestation,
            evaluated_at=current,
        )
    for enum_type in _CLOSED_ENUM_SEALS:
        if enum_type is not OperationalReceiptOutcome:
            _validate_closed_enum_registry(enum_type)

    if artifact_attestation is None:
        return _blocked_result(
            missing=True,
            finding=Phase7FindingClass.ARTIFACT_ATTESTATION_MISSING,
            operational_bundle=operational_bundle,
            stale_references=stale_references,
            artifact_attestation=artifact_attestation,
            evaluated_at=current,
        )
    try:
        _validate_artifact_attestation(artifact_attestation, reconciliation_time=current)
    except OperationsValidationError:
        return _blocked_result(
            missing=False,
            finding=Phase7FindingClass.ARTIFACT_ATTESTATION_CONTRADICTION,
            operational_bundle=operational_bundle,
            stale_references=stale_references,
            artifact_attestation=artifact_attestation,
            evaluated_at=current,
        )

    try:
        _validate_operational_bundle(operational_bundle)
        if (
            operational_bundle.current_artifact_digest_sha256
            != artifact_attestation.artifact_digest_sha256
            or operational_bundle.p7_w2_result_digest_sha256 != P7_W2_GREEN_RESULT_DIGEST_SHA256
        ):
            _fail("conflicting-operational-prerequisite")
        for receipt in operational_bundle.receipts:
            if receipt.outcome is _CANONICAL_PASSED_SYNTHETIC:
                continue
            if receipt.outcome is _CANONICAL_BLOCKED_MISSING_EVIDENCE:
                _fail("missing-terminal-operational-evidence", missing=True)
            if receipt.outcome is _CANONICAL_FAILED or (
                receipt.outcome is _CANONICAL_BLOCKED_CONTRADICTION
            ):
                _fail("failed-terminal-operational-evidence")
            _fail("unrecognized-terminal-operational-evidence")
    except _ContractError as exc:
        return _blocked_result(
            missing=exc.missing,
            finding=(
                Phase7FindingClass.OPERATIONAL_MISSING
                if exc.missing
                else Phase7FindingClass.OPERATIONAL_CONTRADICTION
            ),
            operational_bundle=operational_bundle,
            stale_references=stale_references,
            artifact_attestation=artifact_attestation,
            evaluated_at=current,
        )

    try:
        prior_trust = p7_w3_prior_trust_manifest()
        prior_trust_digest = prior_trust.manifest_digest_sha256
    except OperationsValidationError:
        return _blocked_result(
            missing=False,
            finding=Phase7FindingClass.PRIOR_TRUST_CONTRADICTION,
            operational_bundle=operational_bundle,
            stale_references=stale_references,
            artifact_attestation=artifact_attestation,
            evaluated_at=current,
        )

    try:
        trusted_rows = _derive_trusted_source_rows()
        for row in trusted_rows:
            _validate_trusted_source_row(row)
        if trusted_rows != prior_trust.trusted_rows:
            _fail("conflicting-trusted-source-row-lineage")
        trusted_rows_digest = _digest_value([row.to_dict() for row in trusted_rows])
        match_count = sum(row.state is TrustedParityRowState.MATCH for row in trusted_rows)
        blocked_count = sum(
            row.state is TrustedParityRowState.BLOCKED_DIFFERENCE for row in trusted_rows
        )
        if len(trusted_rows) != 46 or match_count != 12 or blocked_count != 34:
            _fail("invalid-trusted-source-row-summary")
    except OperationsValidationError:
        return _blocked_result(
            missing=False,
            finding=Phase7FindingClass.SOURCE_ROW_LINEAGE_CONTRADICTION,
            operational_bundle=operational_bundle,
            stale_references=stale_references,
            artifact_attestation=artifact_attestation,
            evaluated_at=current,
            prior_trust_digest=prior_trust_digest,
        )

    try:
        stale_digest = _validate_stale_references(
            stale_references,
            artifact_digest_sha256=artifact_attestation.artifact_digest_sha256,
            evaluated_at=current,
        )
    except _ContractError as exc:
        return _blocked_result(
            missing=exc.missing,
            finding=(
                Phase7FindingClass.STALE_REFERENCE_MISSING
                if exc.missing
                else Phase7FindingClass.STALE_REFERENCE_CONTRADICTION
            ),
            operational_bundle=operational_bundle,
            stale_references=stale_references,
            artifact_attestation=artifact_attestation,
            evaluated_at=current,
            prior_trust_digest=prior_trust_digest,
            trusted_rows_digest=trusted_rows_digest,
            match_count=match_count,
            blocked_count=blocked_count,
        )

    return _make_reconciliation_result(
        outcome=_SYNTHETIC_CHECKPOINT_COMPLETE,
        finding_classes=(),
        operational_bundle_digest_sha256=operational_bundle.bundle_digest_sha256,
        prior_trust_manifest_digest_sha256=prior_trust_digest,
        trusted_source_rows_digest_sha256=trusted_rows_digest,
        stale_reference_rows_digest_sha256=stale_digest,
        current_artifact_digest_sha256=artifact_attestation.artifact_digest_sha256,
        current_attestation_digest_sha256=artifact_attestation.attestation_digest_sha256,
        trusted_match_count=match_count,
        trusted_blocked_difference_count=blocked_count,
        evaluated_at=current,
    )
