from __future__ import annotations

import copy
import hashlib
import inspect
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

import pytest

import open_brain_legacy.operations.cutover_verification as verification
from open_brain_legacy.operations.cutover import CUTOVER_SURFACE_MANIFEST, CutoverSurface
from open_brain_legacy.operations.cutover_verification import (
    OPERATIONAL_FLOW_MANIFEST,
    OPERATIONAL_GENESIS_DIGEST_SHA256,
    P7_W0_RESULT_SET_DIGEST_SHA256,
    P7_W1_RESULT_SET_DIGEST_SHA256,
    P7_W2_GREEN_RESULT_DIGEST_SHA256,
    P7_W2_RESULT_SET_DIGEST_SHA256,
    P7_W3_OPERATIONAL_VERSION,
    P7_W3_PRIOR_TRUST_MANIFEST_DIGEST_SHA256,
    P7_W3_PRIOR_TRUST_VERSION,
    P7_W3_RECONCILIATION_VERSION,
    ArtifactVerificationClass,
    OperationalEvidenceBundle,
    OperationalEvidenceScope,
    OperationalFlow,
    OperationalReceipt,
    OperationalReceiptOutcome,
    P7W3ArtifactAttestationEvidence,
    Phase7FindingClass,
    Phase7ReconciliationOutcome,
    Phase7ReconciliationResult,
    ProductionCheck,
    ProductionEvidenceState,
    StaleReferenceEvidence,
    StaleReferenceState,
    TrustedParityRowState,
    TrustedSourceRow,
    make_operational_evidence_bundle,
    make_operational_receipt,
    make_p7_w3_artifact_attestation,
    make_stale_reference_evidence,
    p7_w3_prior_trust_manifest,
    reconcile_phase7,
)
from open_brain_legacy.operations.models import OperationsValidationError

EVALUATED_AT = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
ARTIFACT_DIGEST = hashlib.sha256(b"p7-w3-artifact").hexdigest()
SOURCE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _forge[T](value: T, **changes: object) -> T:
    forged = copy.copy(value)
    for name, replacement in changes.items():
        object.__setattr__(forged, name, replacement)
    return forged


EXPECTED_TRUST_PAYLOAD = {
    "version": "phase7-wave3-prior-trust-v1",
    "waves": [
        {
            "wave": "P7-W0",
            "source_commit": "36f7c0d2a37b48abfce6bc26249345c5712b6c14",
            "evidence_sha256": "b4179b5aa6838499111fae67de10255599f6d5da99cc2d90f357f2e79660d02a",
            "artifact_sha256": "d871ab7e0ec46362c5dc605a0f552ccfcc784e85242cbdee4e2215da5af91b0a",
            "result_set_sha256": "db0efd7c4f06c9ea6e80fb5c04490a8114d6456ba587975d7ef6071f8b7b60dd",
            "final_report_sha256": (
                "d3200a70dd2d5433f8842d3f3ed3332906130261d4d9bb7591b76edfc651f5d6"
            ),
        },
        {
            "wave": "P7-W1",
            "source_commit": "8de2f56a8819df1e8ddd373e3a39289e498a72fc",
            "evidence_sha256": "741c682a5610f7aaa250b2cfce0368b39820ea6775ceecbe18208c5ce8a01124",
            "artifact_sha256": "d262fb5574db8b1b1de64afa2a1d98c0a9dccddc0f8d49fa24d81779e0a157ca",
            "result_set_sha256": "1a193fd96636a416ea9e94e7e25eb888d2074bff3475d738d51f698e45bf10c3",
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
            "result_set_sha256": "b945513af75cc4d9bfcefb78e72fe86f720c174340d833d224d0b55ad5ab10c4",
            "result_digest_sha256": (
                "bf68231472606eea3a32c7aa6dd33295750168d7017af8cca9c5f8822f27dd2c"
            ),
            "security_report_sha256": (
                "c4a387855106991866895a8874943332b105bd0123df3a42ca1671bb53401a64"
            ),
            "final_report_sha256": (
                "c37a5915b3f79ba0bb77be6770d69cdfcb1f3c595b5b062d46a38a81f3e83eef"
            ),
        },
    ],
}

W0_SCENARIOS = (
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


def _w0_projection() -> list[dict[str, object]]:
    return [
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
        for scenario, comparison, attestation, outcomes, unavailable in W0_SCENARIOS
    ]


W1_PROJECTION = {
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

W2_PROJECTION = {
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


def _receipts(
    *, p7_w2_result_digest: str = P7_W2_GREEN_RESULT_DIGEST_SHA256
) -> tuple[OperationalReceipt, ...]:
    predecessor = OPERATIONAL_GENESIS_DIGEST_SHA256
    receipts: list[OperationalReceipt] = []
    for spec in OPERATIONAL_FLOW_MANIFEST:
        receipt = make_operational_receipt(
            run_id="run_0123456789abcdef",
            attempt=1,
            flow=spec.flow,
            predecessor_receipt_digest_sha256=predecessor,
            input_digest_sha256=_digest(f"input-{spec.flow.value}"),
            redacted_output_digest_sha256=_digest(f"output-{spec.flow.value}"),
            current_artifact_digest_sha256=ARTIFACT_DIGEST,
            p7_w2_result_digest_sha256=p7_w2_result_digest,
            outcome=OperationalReceiptOutcome.PASSED_SYNTHETIC,
            evaluated_at=EVALUATED_AT,
        )
        receipts.append(receipt)
        predecessor = receipt.receipt_digest_sha256
    return tuple(receipts)


def _bundle(
    *, p7_w2_result_digest: str = P7_W2_GREEN_RESULT_DIGEST_SHA256
) -> OperationalEvidenceBundle:
    return make_operational_evidence_bundle(_receipts(p7_w2_result_digest=p7_w2_result_digest))


def _bundle_with_outcomes(
    outcomes: dict[int, OperationalReceiptOutcome],
    *,
    run_id: str = "run_0123456789abcdef",
    attempt: int = 1,
) -> OperationalEvidenceBundle:
    predecessor = OPERATIONAL_GENESIS_DIGEST_SHA256
    receipts: list[OperationalReceipt] = []
    for index, spec in enumerate(OPERATIONAL_FLOW_MANIFEST):
        receipt = make_operational_receipt(
            run_id=run_id,
            attempt=attempt,
            flow=spec.flow,
            predecessor_receipt_digest_sha256=predecessor,
            input_digest_sha256=_digest(f"input-{spec.flow.value}"),
            redacted_output_digest_sha256=_digest(f"output-{spec.flow.value}"),
            current_artifact_digest_sha256=ARTIFACT_DIGEST,
            p7_w2_result_digest_sha256=P7_W2_GREEN_RESULT_DIGEST_SHA256,
            outcome=outcomes.get(index, OperationalReceiptOutcome.PASSED_SYNTHETIC),
            evaluated_at=EVALUATED_AT,
        )
        receipts.append(receipt)
        predecessor = receipt.receipt_digest_sha256
    return make_operational_evidence_bundle(tuple(receipts))


_CANONICAL_PASS_OUTCOME = OperationalReceiptOutcome.PASSED_SYNTHETIC
_OUTCOME_BOUNDARIES = ("construction", "receipt", "bundle", "reconciliation")
_OUTCOME_REGISTRY_DRIFTS = (
    "extra-member",
    "canonical-identity",
    "canonical-name",
    "canonical-value",
)
_CUTOVER_SURFACE_BOUNDARIES = ("construction", "serialization", "reconciliation")
_CLOSED_ENUM_TYPES: tuple[type[StrEnum], ...] = (
    OperationalFlow,
    OperationalReceiptOutcome,
    OperationalEvidenceScope,
    ProductionEvidenceState,
    TrustedParityRowState,
    StaleReferenceState,
    ArtifactVerificationClass,
    Phase7ReconciliationOutcome,
    Phase7FindingClass,
    ProductionCheck,
)


def _forged_operational_outcome(
    *, name: str = "PERFORMED_LIVE", value: str = "performed-live"
) -> OperationalReceiptOutcome:
    forged = _forged_closed_enum_member(OperationalReceiptOutcome, name=name, value=value)
    return cast(OperationalReceiptOutcome, forged)


def _forged_closed_enum_member(
    enum_type: type[StrEnum], *, name: str, value: str
) -> StrEnum:
    forged = str.__new__(enum_type, value)
    object.__setattr__(forged, "_name_", name)
    object.__setattr__(forged, "_value_", value)
    return forged


def _construct_receipt_with_outcome(
    outcome: OperationalReceiptOutcome,
) -> OperationalReceipt:
    template = _receipts()[0]
    return make_operational_receipt(
        run_id=template.run_id,
        attempt=template.attempt,
        flow=template.flow,
        predecessor_receipt_digest_sha256=template.predecessor_receipt_digest_sha256,
        input_digest_sha256=template.input_digest_sha256,
        redacted_output_digest_sha256=template.redacted_output_digest_sha256,
        current_artifact_digest_sha256=template.current_artifact_digest_sha256,
        p7_w2_result_digest_sha256=template.p7_w2_result_digest_sha256,
        outcome=outcome,
        evaluated_at=template.evaluated_at,
    )


def _self_consistent_receipt_with_outcome(
    receipt: OperationalReceipt, outcome: OperationalReceiptOutcome
) -> OperationalReceipt:
    forged = _forge(receipt, outcome=outcome)
    object.__setattr__(
        forged,
        "stage_payload_digest_sha256",
        verification._digest_value(verification._stage_payload(forged)),
    )
    object.__setattr__(
        forged,
        "receipt_digest_sha256",
        verification._receipt_digest(forged),
    )
    return forged


def _bundle_with_exact_outcome(outcome: OperationalReceiptOutcome) -> OperationalEvidenceBundle:
    bundle = _bundle()
    forged_receipt = _self_consistent_receipt_with_outcome(bundle.receipts[-1], outcome)
    receipts = (*bundle.receipts[:-1], forged_receipt)
    forged_bundle = _forge(bundle, receipts=receipts)
    payload = {
        "schema_version": forged_bundle.schema_version,
        "contract_version": forged_bundle.contract_version,
        "operational_manifest_digest_sha256": (
            forged_bundle.operational_manifest_digest_sha256
        ),
        "run_id": forged_bundle.run_id,
        "attempt": forged_bundle.attempt,
        "current_artifact_digest_sha256": forged_bundle.current_artifact_digest_sha256,
        "p7_w2_result_digest_sha256": forged_bundle.p7_w2_result_digest_sha256,
        "scope": forged_bundle.scope.value,
        "evaluated_at": verification._timestamp(forged_bundle.evaluated_at),
        "receipts": [verification._receipt_dict(receipt) for receipt in receipts],
    }
    object.__setattr__(forged_bundle, "bundle_digest_sha256", _canonical_digest(payload))
    return forged_bundle


@contextmanager
def _closed_enum_registry_drift(
    enum_type: type[StrEnum], kind: str
) -> Iterator[None]:
    registry: Any = enum_type
    member_names = list(registry._member_names_)
    member_map = dict(registry._member_map_)
    value_map = dict(registry._value2member_map_)
    canonical_members = tuple(member_map.values())
    member_state = tuple(
        (member, member._name_, member._value_) for member in canonical_members
    )
    canonical = canonical_members[0]
    try:
        if kind == "extra-member":
            forged = _forged_closed_enum_member(
                enum_type, name="FORGED_EXTRA", value="forged-extra"
            )
            registry._member_names_.append("FORGED_EXTRA")
            registry._member_map_["FORGED_EXTRA"] = forged
            registry._value2member_map_["forged-extra"] = forged
        elif kind == "canonical-identity":
            forged = _forged_closed_enum_member(
                enum_type,
                name=canonical._name_,
                value=canonical._value_,
            )
            registry._member_map_[canonical._name_] = forged
            registry._value2member_map_[canonical._value_] = forged
        elif kind == "canonical-name":
            object.__setattr__(canonical, "_name_", "FORGED_NAME")
        elif kind == "canonical-value":
            object.__setattr__(canonical, "_value_", "forged-value")
        else:
            raise AssertionError(f"unhandled closed-enum registry drift: {kind}")
        yield
    finally:
        for member, name, value in member_state:
            object.__setattr__(member, "_name_", name)
            object.__setattr__(member, "_value_", value)
        registry._member_names_[:] = member_names
        registry._member_map_.clear()
        registry._member_map_.update(member_map)
        registry._value2member_map_.clear()
        registry._value2member_map_.update(value_map)


@contextmanager
def _outcome_registry_drift(kind: str) -> Iterator[None]:
    with _closed_enum_registry_drift(OperationalReceiptOutcome, kind):
        yield


def _assert_outcome_boundary_rejected(
    boundary: str,
    *,
    outcome: OperationalReceiptOutcome,
    receipt: OperationalReceipt,
    bundle: OperationalEvidenceBundle,
) -> None:
    if boundary == "construction":
        with pytest.raises(OperationsValidationError):
            _construct_receipt_with_outcome(outcome)
        return
    if boundary == "receipt":
        with pytest.raises(OperationsValidationError):
            receipt.to_dict()
        return
    if boundary == "bundle":
        with pytest.raises(OperationsValidationError):
            bundle.to_dict()
        return
    if boundary == "reconciliation":
        result = _reconcile(bundle=bundle)
        assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION
        assert result.finding_classes == (Phase7FindingClass.OPERATIONAL_CONTRADICTION,)
        assert result.synthetic_operational_checkpoint_complete is False
        return
    raise AssertionError(f"unhandled outcome boundary: {boundary}")


def _closed_enum_public_accessor(enum_type: type[StrEnum]) -> Callable[[], object]:
    if enum_type is OperationalFlow:
        return OPERATIONAL_FLOW_MANIFEST[0].to_dict
    if enum_type in {
        OperationalReceiptOutcome,
        OperationalEvidenceScope,
    }:
        return _receipts()[0].to_dict
    if enum_type is ProductionEvidenceState:
        return _reconcile().to_dict
    if enum_type is TrustedParityRowState:
        return verification._derive_trusted_source_rows()[0].to_dict
    if enum_type is StaleReferenceState:
        return _stale_references()[0].to_dict
    if enum_type is ArtifactVerificationClass:
        return _attestation().to_dict
    if enum_type in {
        Phase7ReconciliationOutcome,
        Phase7FindingClass,
        ProductionCheck,
    }:
        return _reconcile().to_dict
    raise AssertionError(f"unhandled closed enum: {enum_type.__name__}")


def _make_result_with_finding(finding: Phase7FindingClass) -> Phase7ReconciliationResult:
    bundle = _bundle()
    blocked = _reconcile(bundle=_forge(bundle, receipts=bundle.receipts[:-1]))
    return verification._make_reconciliation_result(
        outcome=blocked._outcome,
        finding_classes=(finding,),
        operational_bundle_digest_sha256=blocked._operational_bundle_digest_sha256,
        prior_trust_manifest_digest_sha256=blocked._prior_trust_manifest_digest_sha256,
        trusted_source_rows_digest_sha256=blocked._trusted_source_rows_digest_sha256,
        stale_reference_rows_digest_sha256=blocked._stale_reference_rows_digest_sha256,
        current_artifact_digest_sha256=blocked._current_artifact_digest_sha256,
        current_attestation_digest_sha256=blocked._current_attestation_digest_sha256,
        trusted_match_count=blocked._trusted_match_count,
        trusted_blocked_difference_count=blocked._trusted_blocked_difference_count,
        evaluated_at=blocked._evaluated_at,
    )


def _assert_exact_closed_enum_member_rejected(enum_type: type[StrEnum]) -> None:
    canonical = tuple(enum_type)[0]
    forged = _forged_closed_enum_member(
        enum_type,
        name=canonical.name,
        value=canonical.value,
    )
    assert type(forged) is enum_type
    assert forged is not canonical

    if enum_type is OperationalFlow:
        spec = OPERATIONAL_FLOW_MANIFEST[0]
        original = spec.flow
        object.__setattr__(spec, "flow", forged)
        try:
            with pytest.raises(OperationsValidationError):
                spec.to_dict()
        finally:
            object.__setattr__(spec, "flow", original)
        return
    if enum_type is OperationalReceiptOutcome:
        with pytest.raises(OperationsValidationError):
            _construct_receipt_with_outcome(cast(OperationalReceiptOutcome, forged))
        return
    if enum_type is OperationalEvidenceScope:
        receipt = _forge(_receipts()[0], scope=forged)
        object.__setattr__(
            receipt,
            "stage_payload_digest_sha256",
            verification._digest_value(verification._stage_payload(receipt)),
        )
        object.__setattr__(receipt, "receipt_digest_sha256", verification._receipt_digest(receipt))
        with pytest.raises(OperationsValidationError):
            receipt.to_dict()
        return
    if enum_type is ProductionEvidenceState:
        receipt = _forge(_receipts()[0], production_state=forged)
        object.__setattr__(
            receipt,
            "stage_payload_digest_sha256",
            verification._digest_value(verification._stage_payload(receipt)),
        )
        object.__setattr__(receipt, "receipt_digest_sha256", verification._receipt_digest(receipt))
        with pytest.raises(OperationsValidationError):
            receipt.to_dict()
        return
    if enum_type is TrustedParityRowState:
        trusted_row = _forge(
            verification._derive_trusted_source_rows()[0], state=forged
        )
        with pytest.raises(OperationsValidationError):
            trusted_row.to_dict()
        return
    if enum_type is StaleReferenceState:
        stale_row = _forge(_stale_references()[0], state=forged)
        with pytest.raises(OperationsValidationError):
            stale_row.to_dict()
        return
    if enum_type is ArtifactVerificationClass:
        attestation = _forge(_attestation(), verification_class=forged)
        statement_digest = _canonical_digest(verification._attestation_statement(attestation))
        object.__setattr__(
            attestation,
            "verifier_id",
            f"artifactverifier_{statement_digest}",
        )
        object.__setattr__(attestation, "statement_digest_sha256", statement_digest)
        object.__setattr__(attestation, "attestation_digest_sha256", statement_digest)
        with pytest.raises(OperationsValidationError):
            attestation.to_dict()
        return
    if enum_type is Phase7ReconciliationOutcome:
        complete = _reconcile()
        with pytest.raises(OperationsValidationError):
            verification._make_reconciliation_result(
                outcome=cast(Phase7ReconciliationOutcome, forged),
                finding_classes=(),
                operational_bundle_digest_sha256=complete._operational_bundle_digest_sha256,
                prior_trust_manifest_digest_sha256=complete._prior_trust_manifest_digest_sha256,
                trusted_source_rows_digest_sha256=complete._trusted_source_rows_digest_sha256,
                stale_reference_rows_digest_sha256=complete._stale_reference_rows_digest_sha256,
                current_artifact_digest_sha256=complete._current_artifact_digest_sha256,
                current_attestation_digest_sha256=complete._current_attestation_digest_sha256,
                trusted_match_count=complete._trusted_match_count,
                trusted_blocked_difference_count=complete._trusted_blocked_difference_count,
                evaluated_at=complete._evaluated_at,
            )
        return
    if enum_type is Phase7FindingClass:
        with pytest.raises(OperationsValidationError):
            _make_result_with_finding(cast(Phase7FindingClass, forged))
        return
    if enum_type is ProductionCheck:
        checks = verification._PRODUCTION_CHECKS
        verification._PRODUCTION_CHECKS = (cast(ProductionCheck, forged), *checks[1:])
        try:
            with pytest.raises(OperationsValidationError):
                _reconcile().to_dict()
        finally:
            verification._PRODUCTION_CHECKS = checks
        return
    raise AssertionError(f"unhandled exact-type closed enum: {enum_type.__name__}")


def _stale_references() -> tuple[StaleReferenceEvidence, ...]:
    return tuple(
        make_stale_reference_evidence(
            surface=spec.surface,
            current_artifact_digest_sha256=ARTIFACT_DIGEST,
            redacted_result_count=0,
            state=StaleReferenceState.PASSED_SYNTHETIC,
            authority_reference_digest_sha256=_digest(f"authority-{spec.surface.value}"),
            evaluated_at=EVALUATED_AT,
        )
        for spec in CUTOVER_SURFACE_MANIFEST
    )


def _make_stale_reference_for_surface(
    surface: CutoverSurface,
) -> StaleReferenceEvidence:
    return make_stale_reference_evidence(
        surface=surface,
        current_artifact_digest_sha256=ARTIFACT_DIGEST,
        redacted_result_count=0,
        state=StaleReferenceState.PASSED_SYNTHETIC,
        authority_reference_digest_sha256=_digest("authority-cutover-surface"),
        evaluated_at=EVALUATED_AT,
    )


def _assert_cutover_surface_boundary_rejected(
    boundary: str,
    *,
    surface: CutoverSurface,
    stale_row: StaleReferenceEvidence,
    stale_references: tuple[StaleReferenceEvidence, ...],
) -> None:
    if boundary == "construction":
        with pytest.raises(OperationsValidationError):
            _make_stale_reference_for_surface(surface)
        return
    if boundary == "serialization":
        with pytest.raises(OperationsValidationError):
            stale_row.to_dict()
        return
    if boundary == "reconciliation":
        try:
            result = _reconcile(stale=stale_references)
        except OperationsValidationError:
            return
        assert result.synthetic_operational_checkpoint_complete is False
        assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION
        return
    raise AssertionError(f"unhandled cutover surface boundary: {boundary}")


def _attestation(**overrides: Any) -> P7W3ArtifactAttestationEvidence:
    values: dict[str, object] = {
        "source_commit_sha": SOURCE_COMMIT,
        "artifact_distribution": "open-brain",
        "artifact_version": "0.1.0",
        "artifact_digest_sha256": ARTIFACT_DIGEST,
        "evaluated_at": EVALUATED_AT - timedelta(minutes=1),
        "expires_at": EVALUATED_AT + timedelta(hours=1),
    }
    values.update(overrides)
    return make_p7_w3_artifact_attestation(
        source_commit_sha=cast(str, values["source_commit_sha"]),
        artifact_distribution=cast(str, values["artifact_distribution"]),
        artifact_version=cast(str, values["artifact_version"]),
        artifact_digest_sha256=cast(str, values["artifact_digest_sha256"]),
        evaluated_at=cast(datetime, values["evaluated_at"]),
        expires_at=cast(datetime, values["expires_at"]),
    )


def _reconcile(
    *,
    bundle: OperationalEvidenceBundle | None = None,
    stale: tuple[StaleReferenceEvidence, ...] | None = None,
    attestation: P7W3ArtifactAttestationEvidence | None = None,
) -> Phase7ReconciliationResult:
    return reconcile_phase7(
        operational_bundle=_bundle() if bundle is None else bundle,
        stale_references=_stale_references() if stale is None else stale,
        artifact_attestation=_attestation() if attestation is None else attestation,
        evaluated_at=EVALUATED_AT,
    )


def test_complete_normative_prior_trust_payload_and_all_four_digests() -> None:
    manifest = p7_w3_prior_trust_manifest()
    assert _canonical_digest(_w0_projection()) == P7_W0_RESULT_SET_DIGEST_SHA256
    assert _canonical_digest(W1_PROJECTION) == P7_W1_RESULT_SET_DIGEST_SHA256
    assert _canonical_digest(W2_PROJECTION) == P7_W2_RESULT_SET_DIGEST_SHA256
    assert _canonical_digest(EXPECTED_TRUST_PAYLOAD) == (P7_W3_PRIOR_TRUST_MANIFEST_DIGEST_SHA256)
    assert manifest.to_dict() == EXPECTED_TRUST_PAYLOAD
    assert manifest.manifest_digest_sha256 == P7_W3_PRIOR_TRUST_MANIFEST_DIGEST_SHA256


def test_prior_trust_derives_exact_46_authoritative_rows() -> None:
    rows = p7_w3_prior_trust_manifest().trusted_rows
    assert len(rows) == 46
    assert sum(row.state is TrustedParityRowState.MATCH for row in rows) == 12
    assert sum(row.state is TrustedParityRowState.BLOCKED_DIFFERENCE for row in rows) == 34
    assert [(row.scenario_id, row.facet_id) for row in rows[:9]] == [
        ("youtube_playlist_hold", f"PAR7-{index:03d}") for index in range(1, 10)
    ]
    assert rows[-1].wave == "P7-W1"
    assert rows[-1].scenario_id == "matching"
    assert rows[-1].facet_id == "PAR7-010"


def test_complete_synthetic_checkpoint_keeps_production_owner_gated() -> None:
    result = _reconcile()
    assert result.outcome is (
        Phase7ReconciliationOutcome.SYNTHETIC_OPERATIONAL_CHECKPOINT_COMPLETE_PARITY_AND_PRODUCTION_OWNER_GATED
    )
    assert result.synthetic_operational_checkpoint_complete is True
    assert result.phase7_production_complete is False
    assert result.finding_classes == ()
    assert result.trusted_match_count == 12
    assert result.trusted_blocked_difference_count == 34
    production_checks = result.to_dict()["production_checks"]
    assert isinstance(production_checks, list)
    assert all(
        isinstance(row, dict)
        and row.get("state") == ProductionEvidenceState.NOT_PERFORMED_OWNER_GATED.value
        for row in production_checks
    )


def test_operational_receipt_outcome_registry_is_exact_and_canonical() -> None:
    expected = (
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
    registry: Any = OperationalReceiptOutcome
    assert tuple(registry._member_names_) == tuple(name for name, _, _ in expected)
    assert tuple(OperationalReceiptOutcome) == tuple(member for _, _, member in expected)
    assert tuple((member.name, member.value) for member in OperationalReceiptOutcome) == tuple(
        (name, value) for name, value, _ in expected
    )
    assert all(registry._member_map_[name] is member for name, _, member in expected)
    assert all(registry._value2member_map_[value] is member for _, value, member in expected)


@pytest.mark.parametrize("boundary", _OUTCOME_BOUNDARIES)
def test_exact_type_forged_outcome_fails_every_boundary(boundary: str) -> None:
    forged_outcome = _forged_operational_outcome()
    assert type(forged_outcome) is OperationalReceiptOutcome
    forged_receipt = _self_consistent_receipt_with_outcome(_receipts()[-1], forged_outcome)
    forged_bundle = _bundle_with_exact_outcome(forged_outcome)
    _assert_outcome_boundary_rejected(
        boundary,
        outcome=forged_outcome,
        receipt=forged_receipt,
        bundle=forged_bundle,
    )


@pytest.mark.parametrize("drift", _OUTCOME_REGISTRY_DRIFTS)
@pytest.mark.parametrize("boundary", _OUTCOME_BOUNDARIES)
def test_outcome_registry_and_member_drift_fails_every_boundary(
    drift: str, boundary: str
) -> None:
    receipt = _receipts()[-1]
    bundle = _bundle()
    with _outcome_registry_drift(drift):
        _assert_outcome_boundary_rejected(
            boundary,
            outcome=_CANONICAL_PASS_OUTCOME,
            receipt=receipt,
            bundle=bundle,
        )


def test_closed_enum_matrix_cardinality_is_fixed() -> None:
    assert len(_CLOSED_ENUM_TYPES) == 10
    assert len(_OUTCOME_REGISTRY_DRIFTS) == 4
    assert len(_CLOSED_ENUM_TYPES) * len(_OUTCOME_REGISTRY_DRIFTS) == 40


@pytest.mark.parametrize("enum_type", _CLOSED_ENUM_TYPES, ids=lambda value: value.__name__)
def test_exact_type_forged_member_fails_for_every_closed_enum(
    enum_type: type[StrEnum],
) -> None:
    _assert_exact_closed_enum_member_rejected(enum_type)


@pytest.mark.parametrize("drift", _OUTCOME_REGISTRY_DRIFTS)
@pytest.mark.parametrize("enum_type", _CLOSED_ENUM_TYPES, ids=lambda value: value.__name__)
def test_every_closed_enum_registry_drift_fails_public_access(
    enum_type: type[StrEnum], drift: str
) -> None:
    accessor = _closed_enum_public_accessor(enum_type)
    with (
        _closed_enum_registry_drift(enum_type, drift),
        pytest.raises(OperationsValidationError),
    ):
        accessor()


@pytest.mark.parametrize("boundary", _CUTOVER_SURFACE_BOUNDARIES)
def test_exact_type_forged_cutover_surface_fails_every_boundary(boundary: str) -> None:
    canonical = CutoverSurface.CLI_READS
    forged = cast(
        CutoverSurface,
        _forged_closed_enum_member(
            CutoverSurface,
            name=canonical.name,
            value=canonical.value,
        ),
    )
    stale = list(_stale_references())
    stale[0] = _forge(stale[0], surface=forged)

    assert type(forged) is CutoverSurface
    assert forged is not canonical
    _assert_cutover_surface_boundary_rejected(
        boundary,
        surface=forged,
        stale_row=stale[0],
        stale_references=tuple(stale),
    )


@pytest.mark.parametrize("drift", _OUTCOME_REGISTRY_DRIFTS)
@pytest.mark.parametrize("boundary", _CUTOVER_SURFACE_BOUNDARIES)
def test_cutover_surface_registry_drift_fails_every_boundary(
    drift: str, boundary: str
) -> None:
    surface = CutoverSurface.CLI_READS
    stale = _stale_references()

    with _closed_enum_registry_drift(CutoverSurface, drift):
        _assert_cutover_surface_boundary_rejected(
            boundary,
            surface=surface,
            stale_row=stale[0],
            stale_references=stale,
        )


@pytest.mark.parametrize("boundary", _CUTOVER_SURFACE_BOUNDARIES)
def test_stale_scan_policy_digest_is_live_bound_at_every_boundary(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    surface = CutoverSurface.CLI_READS
    stale = _stale_references()
    monkeypatch.setattr(
        verification,
        "STALE_REFERENCE_SCAN_POLICY_DIGEST_SHA256",
        "f" * 64,
    )

    _assert_cutover_surface_boundary_rejected(
        boundary,
        surface=surface,
        stale_row=stale[0],
        stale_references=stale,
    )


@pytest.mark.parametrize("boundary", ["receipt-construction", "reconciliation"])
def test_production_state_value_drift_cannot_claim_completion(boundary: str) -> None:
    with _closed_enum_registry_drift(ProductionEvidenceState, "canonical-value"):
        if boundary == "receipt-construction":
            with pytest.raises(OperationsValidationError):
                _receipts()
            return
        try:
            result = _reconcile()
        except OperationsValidationError:
            return
        assert result.synthetic_operational_checkpoint_complete is False
        assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION


def test_reconciliation_api_has_only_four_keyword_inputs_and_no_retirement_path() -> None:
    signature = inspect.signature(reconcile_phase7)
    assert list(signature.parameters) == [
        "operational_bundle",
        "stale_references",
        "artifact_attestation",
        "evaluated_at",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )
    source = inspect.getsource(verification)
    assert "compare_synthetic_parity" not in source
    assert "retirement" not in source.lower()
    assert "approval_digest" not in source


_FLOW_FIELDS = ("ordinal", "flow", "label", "runbook_step_id", "p7_w2_prerequisite")


@pytest.mark.parametrize(
    ("row_index", "field"),
    [(row_index, field) for row_index in range(8) for field in _FLOW_FIELDS],
)
def test_every_live_flow_row_field_drift_fails_all_dependent_public_access(
    row_index: int, field: str
) -> None:
    spec = OPERATIONAL_FLOW_MANIFEST[row_index]
    receipt = _receipts()[0]
    bundle = _bundle()
    stale = _stale_references()
    attestation = _attestation()
    original = getattr(spec, field)
    replacements: dict[str, object] = {
        "ordinal": 99,
        "flow": OPERATIONAL_FLOW_MANIFEST[(row_index + 1) % 8].flow,
        "label": "forged flow label",
        "runbook_step_id": "P7W3-OPS-99",
        "p7_w2_prerequisite": "forged prerequisite",
    }
    object.__setattr__(spec, field, replacements[field])
    try:
        with pytest.raises(OperationsValidationError):
            spec.to_dict()
        with pytest.raises(OperationsValidationError):
            receipt.to_dict()
        with pytest.raises(OperationsValidationError):
            bundle.to_dict()
        result = reconcile_phase7(
            operational_bundle=bundle,
            stale_references=stale,
            artifact_attestation=attestation,
            evaluated_at=EVALUATED_AT,
        )
        assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION
        assert result.synthetic_operational_checkpoint_complete is False
    finally:
        object.__setattr__(spec, field, original)


@pytest.mark.parametrize("outcome_index", range(8))
@pytest.mark.parametrize(
    ("receipt_outcome", "expected_outcome", "expected_finding"),
    [
        (
            OperationalReceiptOutcome.BLOCKED_MISSING_EVIDENCE,
            Phase7ReconciliationOutcome.BLOCKED_MISSING_EVIDENCE,
            Phase7FindingClass.OPERATIONAL_MISSING,
        ),
        (
            OperationalReceiptOutcome.FAILED,
            Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION,
            Phase7FindingClass.OPERATIONAL_CONTRADICTION,
        ),
        (
            OperationalReceiptOutcome.BLOCKED_CONTRADICTION,
            Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION,
            Phase7FindingClass.OPERATIONAL_CONTRADICTION,
        ),
    ],
)
def test_each_negative_receipt_reduces_by_closed_outcome_in_manifest_order(
    outcome_index: int,
    receipt_outcome: OperationalReceiptOutcome,
    expected_outcome: Phase7ReconciliationOutcome,
    expected_finding: Phase7FindingClass,
) -> None:
    result = _reconcile(bundle=_bundle_with_outcomes({outcome_index: receipt_outcome}))
    assert result.outcome is expected_outcome
    assert result.finding_classes == (expected_finding,)


@pytest.mark.parametrize(
    ("outcomes", "expected_outcome", "expected_finding"),
    [
        (
            {
                1: OperationalReceiptOutcome.FAILED,
                6: OperationalReceiptOutcome.BLOCKED_MISSING_EVIDENCE,
            },
            Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION,
            Phase7FindingClass.OPERATIONAL_CONTRADICTION,
        ),
        (
            {
                1: OperationalReceiptOutcome.BLOCKED_MISSING_EVIDENCE,
                6: OperationalReceiptOutcome.FAILED,
            },
            Phase7ReconciliationOutcome.BLOCKED_MISSING_EVIDENCE,
            Phase7FindingClass.OPERATIONAL_MISSING,
        ),
    ],
)
def test_first_negative_receipt_controls_reducer_finding(
    outcomes: dict[int, OperationalReceiptOutcome],
    expected_outcome: Phase7ReconciliationOutcome,
    expected_finding: Phase7FindingClass,
) -> None:
    result = _reconcile(bundle=_bundle_with_outcomes(outcomes))
    assert result.outcome is expected_outcome
    assert result.finding_classes == (expected_finding,)


_SOURCE_ROW_MUTATION_FIELDS = (
    "scenario_id",
    "source_result_digest_sha256",
    "facet_id",
    "state",
    "unavailable",
    "source_attestation_digest_sha256",
)
_SOURCE_ROW_MUTATION_CASES = tuple(
    (row_index, field) for row_index in range(46) for field in _SOURCE_ROW_MUTATION_FIELDS
)


def _different_hex(value: str) -> str:
    replacement = "0" * len(value)
    return replacement if replacement != value else "1" * len(value)


def _source_row_replacement(row: TrustedSourceRow, field: str) -> object:
    if field == "scenario_id":
        return f"{row.scenario_id}-forged"
    if field == "source_result_digest_sha256":
        return _different_hex(row.source_result_digest_sha256)
    if field == "facet_id":
        return "PAR7-999" if row.facet_id != "PAR7-999" else "PAR7-998"
    if field == "state":
        return (
            TrustedParityRowState.BLOCKED_DIFFERENCE
            if row.state is TrustedParityRowState.MATCH
            else TrustedParityRowState.MATCH
        )
    if field == "unavailable":
        return not row.unavailable
    if field == "source_attestation_digest_sha256":
        return (
            "0" * 64
            if row.source_attestation_digest_sha256 is None
            else _different_hex(row.source_attestation_digest_sha256)
        )
    raise AssertionError(f"unhandled source-row mutation field: {field}")


@pytest.mark.parametrize(("row_index", "field"), _SOURCE_ROW_MUTATION_CASES)
def test_every_source_row_mutation_class_fails_across_all_46_rows(
    monkeypatch: pytest.MonkeyPatch, row_index: int, field: str
) -> None:
    rows = list(verification._derive_trusted_source_rows())
    original = rows[row_index]
    replacement = _source_row_replacement(original, field)
    assert replacement != getattr(original, field)
    rows[row_index] = _forge(original, **{field: replacement})

    if original.wave == "P7-W1":
        assert original.source_attestation_digest_sha256 is None
        if field == "source_attestation_digest_sha256":
            assert rows[row_index].source_attestation_digest_sha256 is not None
        else:
            assert rows[row_index].source_attestation_digest_sha256 is None

    monkeypatch.setattr(verification, "_derive_trusted_source_rows", lambda: tuple(rows))
    result = _reconcile()
    assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION
    assert result.finding_classes == (Phase7FindingClass.SOURCE_ROW_LINEAGE_CONTRADICTION,)


def test_w1_trusted_row_preserves_intentional_absent_attestation() -> None:
    row = verification._derive_trusted_source_rows()[-1]
    assert row.wave == "P7-W1"
    assert row.source_attestation_digest_sha256 is None
    assert "source_attestation_digest_sha256" not in row.to_dict()


_PRIOR_WAVE_DIGEST_FIELDS = {
    0: (
        "evidence_sha256",
        "artifact_sha256",
        "result_set_sha256",
        "final_report_sha256",
    ),
    1: (
        "evidence_sha256",
        "artifact_sha256",
        "result_set_sha256",
        "comparison_digest_sha256",
        "security_report_sha256",
        "final_report_sha256",
    ),
    2: (
        "evidence_sha256",
        "artifact_sha256",
        "artifact_attestation_sha256",
        "result_set_sha256",
        "result_digest_sha256",
        "security_report_sha256",
        "final_report_sha256",
    ),
}
_PRIOR_WAVE_COMMIT_FIELDS = {
    0: ("source_commit",),
    1: ("source_commit",),
    2: ("source_commit", "evidence_commit"),
}
_PRIOR_WAVE_BINDING_CASES = tuple(
    (wave_index, field, "digest")
    for wave_index, fields in _PRIOR_WAVE_DIGEST_FIELDS.items()
    for field in fields
) + tuple(
    (wave_index, field, "commit")
    for wave_index, fields in _PRIOR_WAVE_COMMIT_FIELDS.items()
    for field in fields
)


def test_exhaustive_mutation_matrix_cardinality_is_fixed() -> None:
    assert len(_SOURCE_ROW_MUTATION_CASES) == 46 * 6 == 276
    assert sum(len(fields) for fields in _PRIOR_WAVE_DIGEST_FIELDS.values()) == 17
    assert sum(len(fields) for fields in _PRIOR_WAVE_COMMIT_FIELDS.values()) == 4
    assert len(_PRIOR_WAVE_BINDING_CASES) == 21


@pytest.mark.parametrize(("wave_index", "field", "binding_class"), _PRIOR_WAVE_BINDING_CASES)
def test_each_prior_wave_digest_and_commit_binding_mutation_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    wave_index: int,
    field: str,
    binding_class: str,
) -> None:
    payload = copy.deepcopy(EXPECTED_TRUST_PAYLOAD)
    waves = cast(list[dict[str, object]], payload["waves"])
    value = waves[wave_index][field]
    assert isinstance(value, str)
    assert len(value) == (64 if binding_class == "digest" else 40)
    replacement = _different_hex(value)
    assert replacement != value
    waves[wave_index][field] = replacement
    monkeypatch.setattr(verification, "_PRIOR_TRUST_PAYLOAD", payload)

    result = _reconcile()
    assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION
    assert result.finding_classes == (Phase7FindingClass.PRIOR_TRUST_CONTRADICTION,)


@pytest.mark.parametrize("index", range(8))
def test_each_missing_stale_reference_row_is_blocked_missing(index: int) -> None:
    stale = list(_stale_references())
    stale.pop(index)
    result = _reconcile(stale=tuple(stale))
    assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_MISSING_EVIDENCE
    assert result.finding_classes == (Phase7FindingClass.STALE_REFERENCE_MISSING,)


@pytest.mark.parametrize("mutation", ["duplicate", "failed", "production", "artifact"])
def test_stale_reference_mutations_fail_closed(mutation: str) -> None:
    stale = list(_stale_references())
    if mutation == "duplicate":
        stale[-1] = stale[-2]
    elif mutation == "failed":
        stale[3] = _forge(stale[3], state=StaleReferenceState.FAILED)
    elif mutation == "production":
        stale[3] = _forge(stale[3], production_state="performed")
    else:
        stale[3] = _forge(stale[3], current_artifact_digest_sha256="e" * 64)

    result = _reconcile(stale=tuple(stale))
    assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION
    assert result.finding_classes == (Phase7FindingClass.STALE_REFERENCE_CONTRADICTION,)


def test_exact_eight_stale_surfaces_are_separate_from_eight_operational_flows() -> None:
    stale = _stale_references()
    assert [row.surface for row in stale] == [spec.surface for spec in CUTOVER_SURFACE_MANIFEST]
    assert len(stale) == len(tuple(CutoverSurface)) == 8
    assert len(OPERATIONAL_FLOW_MANIFEST) == 8
    assert {row.surface.value for row in stale}.isdisjoint(
        {spec.flow.value for spec in OPERATIONAL_FLOW_MANIFEST}
    )


def test_p7_w2_non_green_or_mismatched_result_is_rejected() -> None:
    result = _reconcile(bundle=_bundle(p7_w2_result_digest="d" * 64))
    assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION
    assert result.finding_classes == (Phase7FindingClass.OPERATIONAL_CONTRADICTION,)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", "phase7-wave3-artifact-attestation-v2"),
        ("reconciliation_version", "phase7-wave3-reconciliation-v2"),
        ("operational_manifest_digest_sha256", "1" * 64),
        ("prior_trust_manifest_digest_sha256", "2" * 64),
        ("source_commit_sha", "0" * 40),
        ("artifact_distribution", "forged-package"),
        ("artifact_version", "9.9.9"),
        ("artifact_digest_sha256", "3" * 64),
        ("scope", "live"),
        ("verification_class", "callback"),
        ("verifier_id", "artifactverifier_forged"),
        ("statement_digest_sha256", "4" * 64),
        ("attestation_digest_sha256", "5" * 64),
    ],
)
def test_every_artifact_attestation_field_and_digest_is_bound(
    field: str, replacement: object
) -> None:
    result = _reconcile(attestation=_forge(_attestation(), **{field: replacement}))
    assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION
    assert result.finding_classes == (Phase7FindingClass.ARTIFACT_ATTESTATION_CONTRADICTION,)


@pytest.mark.parametrize(
    ("evaluated_at", "expires_at", "reconciliation_at"),
    [
        (EVALUATED_AT - timedelta(hours=2), EVALUATED_AT, EVALUATED_AT),
        (EVALUATED_AT + timedelta(minutes=1), EVALUATED_AT + timedelta(hours=1), EVALUATED_AT),
        (EVALUATED_AT, EVALUATED_AT - timedelta(seconds=1), EVALUATED_AT),
    ],
)
def test_expired_future_or_inverted_attestation_is_rejected(
    evaluated_at: datetime, expires_at: datetime, reconciliation_at: datetime
) -> None:
    if evaluated_at >= expires_at:
        with pytest.raises(OperationsValidationError):
            _attestation(evaluated_at=evaluated_at, expires_at=expires_at)
        return
    attestation = _attestation(evaluated_at=evaluated_at, expires_at=expires_at)
    result = reconcile_phase7(
        operational_bundle=_bundle(),
        stale_references=_stale_references(),
        artifact_attestation=attestation,
        evaluated_at=reconciliation_at,
    )
    assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION


def test_attestation_is_new_callback_free_metadata_type() -> None:
    assert P7W3ArtifactAttestationEvidence.__name__ == "P7W3ArtifactAttestationEvidence"
    signature = inspect.signature(make_p7_w3_artifact_attestation)
    assert "verifier" not in signature.parameters
    assert "path" not in signature.parameters
    assert "artifact_bytes" not in signature.parameters
    attestation = _attestation()
    assert attestation.scope is OperationalEvidenceScope.SYNTHETIC
    assert attestation.verification_class is (
        ArtifactVerificationClass.COORDINATOR_SHA256_WHEEL_BINDING
    )
    assert attestation.attestation_digest_sha256 == attestation.statement_digest_sha256


def test_missing_operational_inventory_precedes_stale_contradiction() -> None:
    bundle = _bundle()
    missing_bundle = _forge(bundle, receipts=bundle.receipts[:-1])
    stale = list(_stale_references())
    stale[0] = _forge(stale[0], production_state="performed")

    result = _reconcile(bundle=missing_bundle, stale=tuple(stale))
    assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_MISSING_EVIDENCE
    assert result.finding_classes == (Phase7FindingClass.OPERATIONAL_MISSING,)


def test_attestation_contradiction_precedes_operational_missing() -> None:
    bundle = _bundle()
    missing_bundle = _forge(bundle, receipts=bundle.receipts[:-1])
    attestation = _forge(_attestation(), artifact_digest_sha256="a" * 64)

    result = _reconcile(bundle=missing_bundle, attestation=attestation)
    assert result.outcome is Phase7ReconciliationOutcome.BLOCKED_CONTRADICTION
    assert result.finding_classes == (Phase7FindingClass.ARTIFACT_ATTESTATION_CONTRADICTION,)


def test_result_is_metadata_only_redacted_and_revalidates_public_access() -> None:
    result = _reconcile()
    payload = result.to_dict()
    serialized = json.dumps(payload, sort_keys=True)
    assert payload["redacted"] is True
    assert payload["phase7_production_complete"] is False
    assert "content" not in payload
    assert "path" not in payload
    assert "url" not in payload
    assert "stdout" not in payload
    assert "https://" not in serialized
    assert "file://" not in serialized

    forged = _forge(result, _result_digest_sha256="0" * 64)
    with pytest.raises(OperationsValidationError):
        forged.to_dict()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("_outcome", Phase7ReconciliationOutcome.BLOCKED_MISSING_EVIDENCE),
        ("_synthetic_operational_checkpoint_complete", False),
        ("_phase7_production_complete", True),
        ("_finding_classes", (Phase7FindingClass.OPERATIONAL_MISSING,)),
        ("_operational_bundle_digest_sha256", "1" * 64),
        ("_prior_trust_manifest_digest_sha256", "2" * 64),
        ("_trusted_source_rows_digest_sha256", "3" * 64),
        ("_stale_reference_rows_digest_sha256", "4" * 64),
        ("_current_artifact_digest_sha256", "5" * 64),
        ("_current_attestation_digest_sha256", "6" * 64),
        ("_trusted_match_count", 11),
        ("_trusted_blocked_difference_count", 33),
        ("_evaluated_at", EVALUATED_AT + timedelta(seconds=1)),
    ],
)
def test_self_consistent_result_field_forgery_fails_every_public_access(
    field: str, replacement: object
) -> None:
    forged = _forge(_reconcile(), **{field: replacement})
    object.__setattr__(forged, "_result_digest_sha256", _canonical_digest(forged._payload()))
    accessors: tuple[Callable[[Phase7ReconciliationResult], object], ...] = (
        lambda value: value.outcome,
        lambda value: value.synthetic_operational_checkpoint_complete,
        lambda value: value.phase7_production_complete,
        lambda value: value.finding_classes,
        lambda value: value.trusted_match_count,
        lambda value: value.trusted_blocked_difference_count,
        lambda value: value.result_digest_sha256,
        lambda value: value.to_dict(),
    )
    for accessor in accessors:
        with pytest.raises(OperationsValidationError):
            accessor(forged)


def test_reducer_created_result_cannot_be_directly_constructed() -> None:
    with pytest.raises(TypeError):
        Phase7ReconciliationResult()
    assert "_create" not in Phase7ReconciliationResult.__dict__


def test_manifest_public_access_revalidates_object_forgery() -> None:
    manifest = p7_w3_prior_trust_manifest()
    forged = _forge(manifest, _manifest_digest_sha256="0" * 64)
    with pytest.raises(OperationsValidationError):
        forged.to_dict()


def test_stale_reference_and_attestation_revalidate_object_forgery() -> None:
    stale = _forge(_stale_references()[0], redacted=False)
    attestation = _forge(_attestation(), verifier_id="artifactverifier_bad")
    with pytest.raises(OperationsValidationError):
        stale.to_dict()
    with pytest.raises(OperationsValidationError):
        attestation.to_dict()


def test_version_and_production_contract_are_closed() -> None:
    assert P7_W3_OPERATIONAL_VERSION == "phase7-wave3-operational-v1"
    assert P7_W3_RECONCILIATION_VERSION == "phase7-wave3-reconciliation-v1"
    assert P7_W3_PRIOR_TRUST_VERSION == "phase7-wave3-prior-trust-v1"
    assert list(ProductionEvidenceState) == [ProductionEvidenceState.NOT_PERFORMED_OWNER_GATED]
    assert not any(member.name == "PHASE7_COMPLETE" for member in Phase7ReconciliationOutcome)
