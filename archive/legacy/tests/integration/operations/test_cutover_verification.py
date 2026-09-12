from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import json
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from open_brain_legacy.operations.cutover_verification import (
    OPERATIONAL_FLOW_MANIFEST,
    OPERATIONAL_GENESIS_DIGEST_SHA256,
    OPERATIONAL_MANIFEST_DIGEST_SHA256,
    P7_W2_GREEN_RESULT_DIGEST_SHA256,
    P7_W3_OPERATIONAL_VERSION,
    OperationalEvidenceBundle,
    OperationalEvidenceScope,
    OperationalFlow,
    OperationalReceipt,
    OperationalReceiptOutcome,
    ProductionEvidenceState,
    make_operational_evidence_bundle,
    make_operational_receipt,
)
from open_brain_legacy.operations.models import OperationsValidationError

EVALUATED_AT = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
ARTIFACT_DIGEST = hashlib.sha256(b"p7-w3-artifact").hexdigest()


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


def _receipts(
    *,
    run_id: str = "run_0123456789abcdef",
    attempt: int = 1,
    artifact_digest: str = ARTIFACT_DIGEST,
    p7_w2_result_digest: str = P7_W2_GREEN_RESULT_DIGEST_SHA256,
    evaluated_at: datetime = EVALUATED_AT,
) -> tuple[OperationalReceipt, ...]:
    receipts: list[OperationalReceipt] = []
    predecessor = OPERATIONAL_GENESIS_DIGEST_SHA256
    for spec in OPERATIONAL_FLOW_MANIFEST:
        receipt = make_operational_receipt(
            run_id=run_id,
            attempt=attempt,
            flow=spec.flow,
            predecessor_receipt_digest_sha256=predecessor,
            input_digest_sha256=_digest(f"input-{spec.flow.value}"),
            redacted_output_digest_sha256=_digest(f"output-{spec.flow.value}"),
            current_artifact_digest_sha256=artifact_digest,
            p7_w2_result_digest_sha256=p7_w2_result_digest,
            outcome=OperationalReceiptOutcome.PASSED_SYNTHETIC,
            evaluated_at=evaluated_at,
        )
        receipts.append(receipt)
        predecessor = receipt.receipt_digest_sha256
    return tuple(receipts)


def _receipts_with_outcome(
    outcome: OperationalReceiptOutcome,
    *,
    outcome_index: int = 3,
    run_id: str = "run_0123456789abcdef",
    attempt: int = 1,
) -> tuple[OperationalReceipt, ...]:
    templates = _receipts(run_id=run_id, attempt=attempt)
    receipts: list[OperationalReceipt] = []
    predecessor = OPERATIONAL_GENESIS_DIGEST_SHA256
    for index, template in enumerate(templates):
        receipt = make_operational_receipt(
            run_id=run_id,
            attempt=attempt,
            flow=template.flow,
            predecessor_receipt_digest_sha256=predecessor,
            input_digest_sha256=template.input_digest_sha256,
            redacted_output_digest_sha256=template.redacted_output_digest_sha256,
            current_artifact_digest_sha256=template.current_artifact_digest_sha256,
            p7_w2_result_digest_sha256=template.p7_w2_result_digest_sha256,
            outcome=(outcome if index == outcome_index else template.outcome),
            evaluated_at=template.evaluated_at,
        )
        receipts.append(receipt)
        predecessor = receipt.receipt_digest_sha256
    return tuple(receipts)


def _bundle(**kwargs: Any) -> OperationalEvidenceBundle:
    return make_operational_evidence_bundle(_receipts(**kwargs))


def test_exact_eight_flow_manifest_and_runbook_mapping() -> None:
    assert [
        (spec.ordinal, spec.flow.value, spec.label, spec.runbook_step_id, spec.p7_w2_prerequisite)
        for spec in OPERATIONAL_FLOW_MANIFEST
    ] == [
        (1, "FLOW-001", "capture to ledger", "P7W3-OPS-01", "iOS/raw capture and ledger/review"),
        (2, "FLOW-002", "review approve", "P7W3-OPS-02", "ledger/review"),
        (3, "FLOW-003", "review reject", "P7W3-OPS-03", "ledger/review"),
        (4, "FLOW-004", "complete nightly", "P7W3-OPS-04", "remaining scheduled writers"),
        (5, "FLOW-005", "playlist poll", "P7W3-OPS-05", "YouTube playlist"),
        (6, "FLOW-006", "social/web capture", "P7W3-OPS-06", "social/web drain"),
        (7, "FLOW-007", "backup", "P7W3-OPS-07", "recovery tooling"),
        (8, "FLOW-008", "temporary restore", "P7W3-OPS-08", "recovery tooling"),
    ]
    assert tuple(spec.flow for spec in OPERATIONAL_FLOW_MANIFEST) == tuple(OperationalFlow)
    assert (
        _canonical_digest(
            {
                "contract_version": P7_W3_OPERATIONAL_VERSION,
                "flows": [spec.to_dict() for spec in OPERATIONAL_FLOW_MANIFEST],
            }
        )
        == OPERATIONAL_MANIFEST_DIGEST_SHA256
    )


def test_complete_bundle_is_deterministic_and_fully_chained() -> None:
    first = _bundle()
    second = _bundle()

    assert first.to_dict() == second.to_dict()
    assert first.bundle_digest_sha256 == second.bundle_digest_sha256
    assert len(first.receipts) == 8
    assert first.receipts[0].predecessor_receipt_digest_sha256 == (
        OPERATIONAL_GENESIS_DIGEST_SHA256
    )
    for predecessor, current in zip(first.receipts[:-1], first.receipts[1:], strict=True):
        assert current.predecessor_receipt_digest_sha256 == predecessor.receipt_digest_sha256


def test_receipt_idempotency_payload_and_identity_are_exact() -> None:
    receipt = _receipts()[0]
    expected = _canonical_digest(
        {
            "contract_version": P7_W3_OPERATIONAL_VERSION,
            "operational_manifest_digest_sha256": OPERATIONAL_MANIFEST_DIGEST_SHA256,
            "run_id": receipt.run_id,
            "attempt": receipt.attempt,
            "flow": receipt.flow.value,
            "ordinal": receipt.ordinal,
            "runbook_step_id": receipt.runbook_step_id,
            "input_digest_sha256": receipt.input_digest_sha256,
            "current_artifact_digest_sha256": receipt.current_artifact_digest_sha256,
            "p7_w2_result_digest_sha256": receipt.p7_w2_result_digest_sha256,
        }
    )

    assert receipt.idempotency_key_sha256 == expected
    assert receipt.receipt_id == f"rcpt_{expected[:32]}"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("contract_version", "phase7-wave3-operational-v2"),
        ("operational_manifest_digest_sha256", "1" * 64),
        ("run_id", "run_fedcba9876543210"),
        ("attempt", 2),
        ("flow", OperationalFlow.REVIEW_APPROVE),
        ("ordinal", 2),
        ("runbook_step_id", "P7W3-OPS-02"),
        ("input_digest_sha256", "2" * 64),
        ("current_artifact_digest_sha256", "3" * 64),
        ("p7_w2_result_digest_sha256", "4" * 64),
    ],
)
def test_every_idempotency_payload_field_is_bound(field: str, replacement: object) -> None:
    receipt = _forge(_receipts()[0], **{field: replacement})
    with pytest.raises(OperationsValidationError):
        receipt.to_dict()


@pytest.mark.parametrize("position", range(8))
def test_every_predecessor_link_is_bound(position: int) -> None:
    bundle = _bundle()
    receipts = list(bundle.receipts)
    receipts[position] = _forge(receipts[position], predecessor_receipt_digest_sha256="9" * 64)
    forged = _forge(bundle, receipts=tuple(receipts))

    with pytest.raises(OperationsValidationError):
        forged.to_dict()


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "reordered"])
def test_bundle_rejects_inventory_mutations(mutation: str) -> None:
    bundle = _bundle()
    receipts = list(bundle.receipts)
    if mutation == "missing":
        receipts.pop()
    elif mutation == "duplicate":
        receipts[-1] = receipts[-2]
    else:
        receipts[0], receipts[1] = receipts[1], receipts[0]

    with pytest.raises(OperationsValidationError):
        _forge(bundle, receipts=tuple(receipts)).to_dict()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("receipt_digest_sha256", "a" * 64),
        ("stage_payload_digest_sha256", "b" * 64),
        ("redacted_output_digest_sha256", "c" * 64),
        ("current_artifact_digest_sha256", "d" * 64),
        ("idempotency_key_sha256", "e" * 64),
        ("receipt_id", "rcpt_" + "f" * 32),
    ],
)
def test_receipt_digest_and_binding_mutations_fail_closed(field: str, replacement: str) -> None:
    receipt = _forge(_receipts()[0], **{field: replacement})
    with pytest.raises(OperationsValidationError):
        receipt.to_dict()


def test_bundle_digest_mutation_fails_closed() -> None:
    forged = _forge(_bundle(), bundle_digest_sha256="f" * 64)
    with pytest.raises(OperationsValidationError):
        forged.to_dict()


@pytest.mark.parametrize(
    "outcome",
    [
        OperationalReceiptOutcome.FAILED,
        OperationalReceiptOutcome.BLOCKED_MISSING_EVIDENCE,
        OperationalReceiptOutcome.BLOCKED_CONTRADICTION,
    ],
)
def test_failed_or_blocked_bundle_is_serializable_immutable_and_retry_separated(
    outcome: OperationalReceiptOutcome,
) -> None:
    bundle = make_operational_evidence_bundle(_receipts_with_outcome(outcome))
    payload = bundle.to_dict()

    assert len(bundle.receipts) == 8
    assert payload["receipts"][3]["outcome"] == outcome.value  # type: ignore[index]
    forged_receipt = _forge(bundle.receipts[3], outcome=OperationalReceiptOutcome.PASSED_SYNTHETIC)
    forged_bundle = _forge(
        bundle,
        receipts=(*bundle.receipts[:3], forged_receipt, *bundle.receipts[4:]),
    )
    with pytest.raises(OperationsValidationError):
        forged_bundle.to_dict()

    retry = make_operational_evidence_bundle(
        _receipts_with_outcome(
            OperationalReceiptOutcome.PASSED_SYNTHETIC,
            run_id="run_fedcba9876543210",
            attempt=2,
        )
    )
    assert retry.run_id != bundle.run_id
    assert retry.attempt != bundle.attempt
    assert retry.bundle_digest_sha256 != bundle.bundle_digest_sha256
    assert {receipt.receipt_id for receipt in retry.receipts}.isdisjoint(
        {receipt.receipt_id for receipt in bundle.receipts}
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("run_id", "run_fedcba9876543210"),
        ("attempt", 2),
        ("evaluated_at", datetime(2026, 8, 16, 12, 1, tzinfo=UTC)),
        ("current_artifact_digest_sha256", "1" * 64),
        ("p7_w2_result_digest_sha256", "2" * 64),
        ("scope", "synthetic"),
    ],
)
def test_bundle_rejects_mixed_shared_bindings(field: str, replacement: object) -> None:
    bundle = _bundle()
    receipts = list(bundle.receipts)
    receipts[-1] = _forge(receipts[-1], **{field: replacement})
    with pytest.raises(OperationsValidationError):
        _forge(bundle, receipts=tuple(receipts)).to_dict()


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("flow", "FLOW-001"),
        ("ordinal", True),
        ("attempt", True),
        ("outcome", "passed-synthetic"),
        ("scope", "synthetic"),
        ("redacted", False),
        ("real_flow_state", "performed"),
        ("production_state", "complete"),
    ],
)
def test_forged_closed_values_and_production_claims_are_rejected(
    field: str, replacement: object
) -> None:
    receipt = _forge(_receipts()[0], **{field: replacement})
    with pytest.raises(OperationsValidationError):
        receipt.to_dict()


def test_duplicate_idempotency_key_with_different_receipt_is_rejected() -> None:
    bundle = _bundle()
    receipts = list(bundle.receipts)
    receipts[1] = _forge(receipts[1], idempotency_key_sha256=receipts[0].idempotency_key_sha256)
    with pytest.raises(OperationsValidationError):
        _forge(bundle, receipts=tuple(receipts)).to_dict()


def test_same_receipt_identity_with_changed_idempotency_key_is_rejected() -> None:
    receipt = _receipts()[0]
    forged = _forge(receipt, idempotency_key_sha256="a" * 64)
    with pytest.raises(OperationsValidationError):
        forged.to_dict()


@pytest.mark.parametrize(
    ("attempt", "run_id"), [(1, "run_fedcba9876543210"), (2, "run_0123456789abcdef")]
)
def test_retry_run_or_attempt_has_separate_identity(attempt: int, run_id: str) -> None:
    original = _receipts()[0]
    retry = _receipts(run_id=run_id, attempt=attempt)[0]
    assert retry.idempotency_key_sha256 != original.idempotency_key_sha256
    assert retry.receipt_id != original.receipt_id


def test_public_serialization_revalidates_forged_frozen_values() -> None:
    receipt = _forge(_receipts()[0], receipt_digest_sha256="0" * 64)
    bundle = _forge(_bundle(), receipts=(receipt, *_bundle().receipts[1:]))

    with pytest.raises(OperationsValidationError):
        receipt.to_dict()
    with pytest.raises(OperationsValidationError):
        bundle.to_dict()


def test_receipts_and_bundle_have_no_raw_or_free_form_fields() -> None:
    forbidden = {
        "content",
        "title",
        "url",
        "path",
        "repository",
        "host",
        "service",
        "credential",
        "environment",
        "prompt",
        "owner_text",
        "review_reason",
        "stdout",
        "stderr",
        "exception",
        "error_message",
    }
    receipt_fields = {field.name for field in fields(OperationalReceipt)}
    bundle_fields = {field.name for field in fields(OperationalEvidenceBundle)}
    assert forbidden.isdisjoint(receipt_fields | bundle_fields)

    serialized = json.dumps(_bundle().to_dict(), sort_keys=True)
    assert "https://" not in serialized
    assert "file://" not in serialized
    assert "BEGIN PRIVATE" not in serialized


_FORBIDDEN_IMPORT_ROOTS = {
    "asyncio",
    "http",
    "os",
    "pathlib",
    "requests",
    "shutil",
    "socket",
    "subprocess",
    "urllib",
}


def _symbol_name(value: ast.expr) -> str:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return ""


def _capability_violations(source: str) -> set[str]:
    tree = ast.parse(source)
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in _FORBIDDEN_IMPORT_ROOTS:
                    violations.add(f"forbidden-import:{root}")
                if (
                    alias.name in {"open_brain.parity", "open_brain_legacy.parity"}
                    or alias.name.startswith(
                        ("open_brain.parity.", "open_brain_legacy.parity.")
                    )
                ):
                    violations.add("parity-comparison-import")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".", 1)[0]
            if root in _FORBIDDEN_IMPORT_ROOTS:
                violations.add(f"forbidden-import:{root}")
            if module in {"open_brain.parity", "open_brain_legacy.parity"} or module.startswith(
                ("open_brain.parity.", "open_brain_legacy.parity.")
            ):
                violations.add("parity-comparison-import")
            if any("compare" in alias.name.lower() for alias in node.names):
                violations.add("parity-comparison-import")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lowered = node.name.lower()
            if "compare" in lowered or "disposition" in lowered:
                violations.add("second-disposition-path")
        elif isinstance(node, ast.Call):
            lowered = _symbol_name(node.func).lower()
            if "compare" in lowered or "disposition" in lowered:
                violations.add("second-disposition-path")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any("disposition" in _symbol_name(target).lower() for target in targets):
                violations.add("second-disposition-path")
    return violations


def test_api_is_pure_keyword_metadata_and_source_has_no_forbidden_capability() -> None:
    signature = inspect.signature(make_operational_receipt)
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )

    source_path = Path(inspect.getsourcefile(make_operational_receipt) or "")
    source = source_path.read_text(encoding="utf-8")
    assert _capability_violations(source) == set()
    assert "open(" not in source
    assert "getenv" not in source


def test_closed_gate_defaults_are_exact() -> None:
    receipt = _receipts()[0]
    assert receipt.scope is OperationalEvidenceScope.SYNTHETIC
    assert receipt.real_flow_state is ProductionEvidenceState.NOT_PERFORMED_OWNER_GATED
    assert receipt.production_state is ProductionEvidenceState.NOT_PERFORMED_OWNER_GATED
    assert receipt.redacted is True


@pytest.mark.parametrize(
    ("mutated_source", "expected_violation"),
    [
        ("from subprocess import run", "forbidden-import:subprocess"),
        ("from pathlib import Path", "forbidden-import:pathlib"),
        (
            "from open_brain_legacy.parity.harness import compare_synthetic_parity as reconcile",
            "parity-comparison-import",
        ),
        (
            "def derive_disposition() -> str:\n    return 'match'",
            "second-disposition-path",
        ),
    ],
)
def test_capability_guard_kills_import_alias_and_disposition_mutations(
    mutated_source: str, expected_violation: str
) -> None:
    assert expected_violation in _capability_violations(mutated_source)
