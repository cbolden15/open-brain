from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

import open_brain_legacy.operations.cutover as cutover_module
from open_brain_legacy.operations.cutover import (
    CUTOVER_MANIFEST_DIGEST_SHA256,
    CUTOVER_REHEARSAL_VERSION,
    CUTOVER_SURFACE_MANIFEST,
    FORWARD_STAGES,
    GENESIS_RECEIPT_DIGEST_SHA256,
    ROLLBACK_STAGES,
    CutoverDiagnosticClass,
    CutoverReceipt,
    CutoverRehearsalResult,
    CutoverSurface,
    ForwardStage,
    NegativeCase,
    OwnerGateState,
    PriorReceiptLedger,
    ReceiptChainKind,
    ReceiptOutcome,
    RehearsalOutcome,
    RollbackDisposition,
    RollbackStage,
    RollbackStageAttempt,
    RollbackTrigger,
    SurfaceCheckOutcome,
    SyntheticCutoverScenario,
    SyntheticRollbackAttempt,
    SyntheticSurfaceScenario,
    WriterDisposition,
    rehearse_cutover,
)
from open_brain_legacy.operations.cutover_doctor import (
    CutoverCheck,
    CutoverCheckState,
    CutoverDoctorOutcome,
    CutoverDoctorResult,
    CutoverProbeName,
    phase6_cutover_manifest,
)
from open_brain_legacy.operations.models import OperationsValidationError
from open_brain_legacy.parity import (
    P7_W1_SHADOW_SCHEMA_DIGEST_SHA256,
    P7_W1_SHADOW_VERSION,
    ArtifactAttestationEvidence,
    BuiltArtifactIdentity,
    EvidenceScope,
)

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
_ARTIFACT = BuiltArtifactIdentity(version="0.1.0", digest_sha256=_A)


def _preflight(
    outcome: CutoverDoctorOutcome = CutoverDoctorOutcome.SYNTHETIC_READY,
) -> CutoverDoctorResult:
    return CutoverDoctorResult._create(
        manifest=phase6_cutover_manifest(),
        strict=True,
        outcome=outcome,
        exit_code=0 if outcome is CutoverDoctorOutcome.SYNTHETIC_READY else 78,
        checks=tuple(
            CutoverCheck(probe, CutoverCheckState.HEALTHY, ())
            for probe in CutoverProbeName
        ),
    )


def _attestation(*, expires_at: datetime | None = None) -> ArtifactAttestationEvidence:
    return ArtifactAttestationEvidence(
        verifier_id=f"verifier_{_B}",
        attestation_id=f"attestation_{_C}",
        attestation_digest_sha256=_D,
        artifact=_ARTIFACT,
        manifest_version=P7_W1_SHADOW_VERSION,
        schema_digest_sha256=P7_W1_SHADOW_SCHEMA_DIGEST_SHA256,
        scope=EvidenceScope.SYNTHETIC,
        evaluated_at=_NOW - timedelta(minutes=1),
        expires_at=expires_at or _NOW + timedelta(hours=1),
    )


def _surface(index: int) -> SyntheticSurfaceScenario:
    spec = CUTOVER_SURFACE_MANIFEST[index]
    writing = spec.writer_disposition is WriterDisposition.ONE_SYNTHETIC_WRITER
    return SyntheticSurfaceScenario(
        surface=spec.surface,
        ordinal=spec.ordinal,
        attempt=1,
        attempt_requested=True,
        writer_disposition=spec.writer_disposition,
        writer_identity_digest_sha256=_B if writing else None,
        legacy_writer_count=0,
        smoke_outcome=SurfaceCheckOutcome.PASSED,
        verification_outcome=SurfaceCheckOutcome.PASSED,
        rollback_trigger=None,
        incompatible_state_written=False,
        diagnostic_class=CutoverDiagnosticClass.NONE,
        rollback_attempt=None,
    )


def _scenario(
    *,
    surfaces: tuple[SyntheticSurfaceScenario, ...] | None = None,
    resume_from: CutoverSurface | None = CutoverSurface.CLI_READS,
    input_digest: str = _A,
    real_capture_gate: OwnerGateState = OwnerGateState.NOT_PERFORMED_OWNER_GATED,
) -> SyntheticCutoverScenario:
    return SyntheticCutoverScenario(
        run_id=f"run_{_A[:32]}",
        manifest_version=CUTOVER_REHEARSAL_VERSION,
        manifest_digest_sha256=CUTOVER_MANIFEST_DIGEST_SHA256,
        input_digest_sha256=input_digest,
        snapshot_digest_sha256=_B,
        surfaces=surfaces or tuple(_surface(index) for index in range(8)),
        resume_from=resume_from,
        real_capture_gate=real_capture_gate,
    )


def _rollback_stages(
    surface: SyntheticSurfaceScenario,
) -> tuple[RollbackStageAttempt, ...]:
    dispositions = (
        RollbackDisposition.TRIGGER_RECORDED,
        RollbackDisposition.DISABLED_SYNTHETIC,
        (
            RollbackDisposition.RESTORED_SYNTHETIC
            if surface.incompatible_state_written
            and surface.writer_disposition is WriterDisposition.ONE_SYNTHETIC_WRITER
            else RollbackDisposition.NOT_REQUIRED
        ),
        (
            RollbackDisposition.REENABLED_SYNTHETIC
            if surface.writer_disposition is WriterDisposition.ONE_SYNTHETIC_WRITER
            else RollbackDisposition.NOT_APPLICABLE_READ_ONLY
            if surface.writer_disposition is WriterDisposition.NOT_APPLICABLE_READ_ONLY
            else RollbackDisposition.NOT_APPLICABLE_TOOLING
        ),
        RollbackDisposition.PRESERVED_REDACTED,
        RollbackDisposition.VERIFIED_SYNTHETIC,
    )
    return tuple(
        RollbackStageAttempt(stage, SurfaceCheckOutcome.PASSED, disposition)
        for stage, disposition in zip(ROLLBACK_STAGES, dispositions, strict=True)
    )


def _trigger_scenario(
    trigger: RollbackTrigger,
    *,
    target_index: int = 2,
    incompatible_state_written: bool = False,
    stages: tuple[RollbackStageAttempt, ...] | None = None,
    keep_later_attempts: bool = False,
) -> SyntheticCutoverScenario:
    surfaces = list(_surface(index) for index in range(8))
    target = replace(
        surfaces[target_index],
        rollback_trigger=trigger,
        incompatible_state_written=incompatible_state_written,
        diagnostic_class=CutoverDiagnosticClass.SYNTHETIC_CHECK_FAILED,
    )
    selected_stages = stages if stages is not None else _rollback_stages(target)
    surfaces[target_index] = replace(
        target,
        rollback_attempt=SyntheticRollbackAttempt(selected_stages),
    )
    if not keep_later_attempts:
        for index in range(target_index + 1, len(surfaces)):
            surfaces[index] = replace(surfaces[index], attempt_requested=False)
    return _scenario(surfaces=tuple(surfaces))


def _run(
    scenario: SyntheticCutoverScenario | None = None,
    *,
    ledger: PriorReceiptLedger | None = None,
    preflight: CutoverDoctorResult | None = None,
    attestation: ArtifactAttestationEvidence | None = None,
) -> CutoverRehearsalResult:
    return rehearse_cutover(
        preflight or _preflight(),
        scenario or _scenario(),
        prior_ledger=ledger or PriorReceiptLedger.empty(),
        artifact_attestation=attestation or _attestation(),
        evaluated_at=_NOW,
    )


def _nested_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            nested
            for item in value.values()
            for nested in _nested_keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _nested_keys(item)}
    return set()


def test_cutover_manifest_fixes_the_authoritative_surface_order() -> None:
    assert CUTOVER_REHEARSAL_VERSION == "phase7-wave2-rehearsal-v1"
    assert tuple(spec.surface for spec in CUTOVER_SURFACE_MANIFEST) == tuple(CutoverSurface)
    assert tuple(spec.ordinal for spec in CUTOVER_SURFACE_MANIFEST) == tuple(range(1, 9))
    assert len(CUTOVER_MANIFEST_DIGEST_SHA256) == 64


def test_all_eight_surfaces_green_in_order_with_digest_linked_receipts() -> None:
    result = _run()

    assert result.outcome is RehearsalOutcome.SYNTHETIC_GREEN
    assert result.resolved is True
    assert result.negative_case is NegativeCase.NONE
    assert len(result.receipts) == 8 * len(FORWARD_STAGES)
    assert result.receipts[0].predecessor_receipt_digest_sha256 == (
        GENESIS_RECEIPT_DIGEST_SHA256
    )
    for previous, current in zip(result.receipts, result.receipts[1:], strict=False):
        assert current.predecessor_receipt_digest_sha256 == previous.receipt_digest_sha256
    for index, spec in enumerate(CUTOVER_SURFACE_MANIFEST):
        receipts = result.receipts[index * 7 : (index + 1) * 7]
        assert tuple(receipt.stage for receipt in receipts) == FORWARD_STAGES
        assert all(receipt.surface is spec.surface for receipt in receipts)
        assert receipts[-1].stage is ForwardStage.GREEN
        assert receipts[-1].outcome is ReceiptOutcome.PASSED


def test_same_inputs_replay_exactly_without_ambient_state() -> None:
    first = _run()
    second = _run()

    assert first.to_dict() == second.to_dict()
    assert first.result_digest_sha256 == second.result_digest_sha256


@pytest.mark.parametrize(
    "surfaces",
    (
        tuple(_surface(index) for index in range(7)),
        tuple(_surface(index) for index in range(8)) + (_surface(7),),
        (_surface(1), _surface(0), *tuple(_surface(index) for index in range(2, 8))),
    ),
)
def test_missing_duplicate_or_reordered_surface_is_rejected(
    surfaces: tuple[SyntheticSurfaceScenario, ...],
) -> None:
    with pytest.raises(OperationsValidationError, match="surface"):
        _scenario(surfaces=surfaces)


def test_later_surface_never_starts_before_prior_green() -> None:
    surfaces = list(_surface(index) for index in range(8))
    surfaces[0] = replace(surfaces[0], attempt_requested=False)

    result = _run(_scenario(surfaces=tuple(surfaces)))

    assert result.outcome is RehearsalOutcome.BLOCKED
    assert result.negative_case is NegativeCase.SURFACE_NOT_REQUESTED
    assert result.receipts == ()


@pytest.mark.parametrize("target_index", (0, 1, 7))
def test_nonwriting_surfaces_reject_writer_identity(target_index: int) -> None:
    surfaces = list(_surface(index) for index in range(8))
    surfaces[target_index] = replace(
        surfaces[target_index],
        writer_identity_digest_sha256=_C,
    )

    result = _run(_scenario(surfaces=tuple(surfaces)))

    assert result.outcome is RehearsalOutcome.BLOCKED
    assert result.negative_case is NegativeCase.WRITER_EVIDENCE_INVALID


@pytest.mark.parametrize(
    "change",
    (
        lambda value: replace(value, writer_identity_digest_sha256=None),
        lambda value: replace(value, legacy_writer_count=1),
        lambda value: replace(
            value,
            writer_disposition=WriterDisposition.NOT_APPLICABLE_READ_ONLY,
        ),
    ),
)
def test_writing_surface_requires_exactly_one_synthetic_writer(
    change: Callable[[SyntheticSurfaceScenario], SyntheticSurfaceScenario],
) -> None:
    surfaces = list(_surface(index) for index in range(8))
    surfaces[2] = change(surfaces[2])

    result = _run(_scenario(surfaces=tuple(surfaces)))

    assert result.outcome is RehearsalOutcome.BLOCKED
    assert result.negative_case is NegativeCase.WRITER_EVIDENCE_INVALID


@pytest.mark.parametrize("trigger", tuple(RollbackTrigger))
def test_each_trigger_stops_forward_progress_and_completes_ordered_rollback(
    trigger: RollbackTrigger,
) -> None:
    result = _run(_trigger_scenario(trigger))

    assert result.outcome is RehearsalOutcome.ROLLED_BACK_SYNTHETIC
    assert result.resolved is False
    assert result.negative_case is NegativeCase.NONE
    rollback = tuple(
        receipt for receipt in result.receipts if receipt.chain_kind is ReceiptChainKind.ROLLBACK
    )
    assert tuple(receipt.stage for receipt in rollback) == ROLLBACK_STAGES
    assert all(receipt.surface is CutoverSurface.IOS_RAW_CAPTURE for receipt in rollback)
    assert all(
        receipt.surface.value not in {surface.value for surface in tuple(CutoverSurface)[3:]}
        for receipt in result.receipts
    )


@pytest.mark.parametrize("stage_index", range(len(ROLLBACK_STAGES)))
def test_every_missing_rollback_stage_is_blocked(stage_index: int) -> None:
    target = _surface(2)
    stages = list(_rollback_stages(target))
    del stages[stage_index]

    result = _run(
        _trigger_scenario(
            RollbackTrigger.REQUIRED_HEALTH_RED,
            stages=tuple(stages),
        )
    )

    assert result.outcome is RehearsalOutcome.ROLLBACK_BLOCKED
    assert result.negative_case is NegativeCase.ROLLBACK_STAGE_MISSING


@pytest.mark.parametrize("stage_index", range(len(ROLLBACK_STAGES)))
def test_every_failed_rollback_stage_is_blocked(stage_index: int) -> None:
    target = _surface(2)
    stages = list(_rollback_stages(target))
    stages[stage_index] = replace(
        stages[stage_index],
        outcome=SurfaceCheckOutcome.FAILED,
        diagnostic_class=CutoverDiagnosticClass.ROLLBACK_STAGE_FAILED,
    )

    result = _run(
        _trigger_scenario(
            RollbackTrigger.REQUIRED_HEALTH_RED,
            stages=tuple(stages),
        )
    )

    assert result.outcome is RehearsalOutcome.ROLLBACK_BLOCKED
    assert result.negative_case in {
        NegativeCase.ROLLBACK_STAGE_FAILED,
        NegativeCase.ROLLBACK_STAGE_AFTER_FAILURE,
    }


@pytest.mark.parametrize("stage_index", range(len(ROLLBACK_STAGES) - 1))
def test_every_adjacent_rollback_reorder_is_blocked(stage_index: int) -> None:
    target = _surface(2)
    stages = list(_rollback_stages(target))
    stages[stage_index], stages[stage_index + 1] = stages[stage_index + 1], stages[stage_index]

    result = _run(
        _trigger_scenario(
            RollbackTrigger.REQUIRED_HEALTH_RED,
            stages=tuple(stages),
        )
    )

    assert result.outcome is RehearsalOutcome.ROLLBACK_BLOCKED
    assert result.negative_case is NegativeCase.ROLLBACK_STAGE_REORDERED


@pytest.mark.parametrize("stage_index", range(len(ROLLBACK_STAGES) - 1))
def test_every_duplicate_rollback_stage_is_blocked(stage_index: int) -> None:
    target = _surface(2)
    stages = list(_rollback_stages(target))
    stages[stage_index + 1] = stages[stage_index]

    result = _run(
        _trigger_scenario(
            RollbackTrigger.REQUIRED_HEALTH_RED,
            stages=tuple(stages),
        )
    )

    assert result.outcome is RehearsalOutcome.ROLLBACK_BLOCKED
    assert result.negative_case is NegativeCase.ROLLBACK_STAGE_DUPLICATED


def test_later_surface_attempt_after_trigger_is_rollback_blocked() -> None:
    result = _run(
        _trigger_scenario(
            RollbackTrigger.DATA_LOSS_OR_DUPLICATE_WRITE,
            keep_later_attempts=True,
        )
    )

    assert result.outcome is RehearsalOutcome.ROLLBACK_BLOCKED
    assert result.negative_case is NegativeCase.LATER_SURFACE_AFTER_TRIGGER


def test_trigger_without_rollback_attempt_is_rollback_blocked() -> None:
    surfaces = list(_surface(index) for index in range(8))
    surfaces[2] = replace(
        surfaces[2],
        rollback_trigger=RollbackTrigger.REQUIRED_HEALTH_RED,
        diagnostic_class=CutoverDiagnosticClass.SYNTHETIC_CHECK_FAILED,
    )
    for index in range(3, len(surfaces)):
        surfaces[index] = replace(surfaces[index], attempt_requested=False)

    result = _run(_scenario(surfaces=tuple(surfaces)))

    assert result.outcome is RehearsalOutcome.ROLLBACK_BLOCKED
    assert result.negative_case is NegativeCase.ROLLBACK_ATTEMPT_MISSING


@pytest.mark.parametrize("target_index", (0, 1, 7))
def test_nonwriting_surface_cannot_claim_incompatible_state(target_index: int) -> None:
    result = _run(
        _trigger_scenario(
            RollbackTrigger.PRIVACY_TIER_MISMATCH,
            target_index=target_index,
            incompatible_state_written=True,
        )
    )

    assert result.outcome is RehearsalOutcome.ROLLBACK_BLOCKED
    assert result.negative_case is NegativeCase.INCOMPATIBLE_STATE_ON_NON_WRITER


def test_incompatible_writing_state_requires_restore_disposition() -> None:
    target = replace(_surface(2), incompatible_state_written=True)
    stages = list(_rollback_stages(target))
    restore_index = ROLLBACK_STAGES.index(RollbackStage.SNAPSHOT_RESTORE_DISPOSITION)
    stages[restore_index] = replace(
        stages[restore_index],
        disposition=RollbackDisposition.NOT_REQUIRED,
    )

    result = _run(
        _trigger_scenario(
            RollbackTrigger.DATA_LOSS_OR_DUPLICATE_WRITE,
            incompatible_state_written=True,
            stages=tuple(stages),
        )
    )

    assert result.outcome is RehearsalOutcome.ROLLBACK_BLOCKED
    assert result.negative_case is NegativeCase.RESTORE_DISPOSITION_INVALID


@pytest.mark.parametrize("target_index", (0, 1, 7))
def test_nonwriting_surface_requires_not_required_restore(target_index: int) -> None:
    target = _surface(target_index)
    stages = list(_rollback_stages(target))
    restore_index = ROLLBACK_STAGES.index(RollbackStage.SNAPSHOT_RESTORE_DISPOSITION)
    stages[restore_index] = replace(
        stages[restore_index],
        disposition=RollbackDisposition.RESTORED_SYNTHETIC,
    )

    result = _run(
        _trigger_scenario(
            RollbackTrigger.REQUIRED_HEALTH_RED,
            target_index=target_index,
            stages=tuple(stages),
        )
    )

    assert result.outcome is RehearsalOutcome.ROLLBACK_BLOCKED
    assert result.negative_case is NegativeCase.RESTORE_DISPOSITION_INVALID


def test_compatible_writing_surface_forbids_unnecessary_restore() -> None:
    target = _surface(2)
    stages = list(_rollback_stages(target))
    restore_index = ROLLBACK_STAGES.index(RollbackStage.SNAPSHOT_RESTORE_DISPOSITION)
    stages[restore_index] = replace(
        stages[restore_index],
        disposition=RollbackDisposition.RESTORED_SYNTHETIC,
    )

    result = _run(
        _trigger_scenario(
            RollbackTrigger.REQUIRED_HEALTH_RED,
            stages=tuple(stages),
        )
    )

    assert result.outcome is RehearsalOutcome.ROLLBACK_BLOCKED
    assert result.negative_case is NegativeCase.RESTORE_DISPOSITION_INVALID


def test_writing_surface_requires_synthetic_reenable_disposition() -> None:
    target = _surface(2)
    stages = list(_rollback_stages(target))
    reenable_index = ROLLBACK_STAGES.index(
        RollbackStage.OLD_SERVICE_REENABLED_DISPOSITION
    )
    stages[reenable_index] = replace(
        stages[reenable_index],
        disposition=RollbackDisposition.NOT_APPLICABLE_TOOLING,
    )

    result = _run(
        _trigger_scenario(
            RollbackTrigger.REVIEW_GATE_BYPASS,
            stages=tuple(stages),
        )
    )

    assert result.outcome is RehearsalOutcome.ROLLBACK_BLOCKED
    assert result.negative_case is NegativeCase.REENABLE_DISPOSITION_INVALID


@pytest.mark.parametrize("target_index", (0, 1, 7))
def test_nonwriting_surface_requires_exact_reenable_disposition(target_index: int) -> None:
    target = _surface(target_index)
    stages = list(_rollback_stages(target))
    reenable_index = ROLLBACK_STAGES.index(
        RollbackStage.OLD_SERVICE_REENABLED_DISPOSITION
    )
    stages[reenable_index] = replace(
        stages[reenable_index],
        disposition=RollbackDisposition.REENABLED_SYNTHETIC,
    )

    result = _run(
        _trigger_scenario(
            RollbackTrigger.REQUIRED_HEALTH_RED,
            target_index=target_index,
            stages=tuple(stages),
        )
    )

    assert result.outcome is RehearsalOutcome.ROLLBACK_BLOCKED
    assert result.negative_case is NegativeCase.REENABLE_DISPOSITION_INVALID


def test_resume_revalidates_prior_green_chain_and_starts_at_first_incomplete_surface() -> None:
    complete = _run()
    prior = PriorReceiptLedger(complete.receipts[: len(FORWARD_STAGES)])
    resumed_scenario = _scenario(resume_from=CutoverSurface.MCP_UI_READS)

    resumed = _run(resumed_scenario, ledger=prior)

    assert resumed.outcome is RehearsalOutcome.SYNTHETIC_GREEN
    assert resumed.receipts == complete.receipts
    assert resumed.prior_ledger_digest_sha256 == prior.ledger_digest_sha256


def test_terminal_green_ledger_replays_without_new_receipts() -> None:
    complete = _run()
    terminal = _run(
        _scenario(resume_from=None),
        ledger=PriorReceiptLedger(complete.receipts),
    )

    assert terminal.outcome is RehearsalOutcome.SYNTHETIC_GREEN
    assert terminal.receipts == complete.receipts


def test_triggerless_digest_valid_prior_rollback_chain_is_rejected() -> None:
    scenario = _scenario()
    preflight = _preflight()
    attestation = _attestation()
    context = cutover_module._Context(
        preflight_digest_sha256=cutover_module._validate_preflight(preflight),
        scenario_digest_sha256=scenario.scenario_digest_sha256,
        artifact_digest_sha256=attestation.artifact.digest_sha256,
        artifact_attestation_digest_sha256=attestation.attestation_digest_sha256,
    )
    receipts: list[CutoverReceipt] = []

    def append_receipt(
        surface: SyntheticSurfaceScenario,
        chain_kind: ReceiptChainKind,
        stage: ForwardStage | RollbackStage,
    ) -> None:
        predecessor = (
            receipts[-1].receipt_digest_sha256
            if receipts
            else GENESIS_RECEIPT_DIGEST_SHA256
        )
        receipts.append(
            cutover_module._new_receipt(
                scenario=scenario,
                surface=surface,
                context=context,
                chain_kind=chain_kind,
                stage=stage,
                predecessor=predecessor,
                outcome=ReceiptOutcome.PASSED,
                evaluated_at=_NOW,
            )
        )

    for surface in scenario.surfaces[:2]:
        for stage in FORWARD_STAGES:
            append_receipt(surface, ReceiptChainKind.FORWARD, stage)
    target = scenario.surfaces[2]
    for stage in FORWARD_STAGES[:-1]:
        append_receipt(target, ReceiptChainKind.FORWARD, stage)
    for rollback_stage in ROLLBACK_STAGES:
        append_receipt(target, ReceiptChainKind.ROLLBACK, rollback_stage)

    with pytest.raises(OperationsValidationError, match="trigger"):
        rehearse_cutover(
            preflight,
            scenario,
            prior_ledger=PriorReceiptLedger(tuple(receipts)),
            artifact_attestation=attestation,
            evaluated_at=_NOW,
        )


def test_invalid_resume_or_changed_input_under_same_run_is_rejected() -> None:
    complete = _run()
    prior = PriorReceiptLedger(complete.receipts[: len(FORWARD_STAGES)])
    with pytest.raises(OperationsValidationError, match="resume"):
        _run(_scenario(resume_from=CutoverSurface.IOS_RAW_CAPTURE), ledger=prior)
    with pytest.raises(OperationsValidationError, match="receipt"):
        _run(
            _scenario(
                resume_from=CutoverSurface.MCP_UI_READS,
                input_digest=_C,
            ),
            ledger=prior,
        )


def test_changed_payload_under_same_receipt_identity_is_rejected() -> None:
    complete = _run()
    forged = replace(complete.receipts[0], outcome=ReceiptOutcome.BLOCKED)
    forged = replace(
        forged,
        receipt_digest_sha256=cutover_module._receipt_digest(forged),
    )

    with pytest.raises(OperationsValidationError, match="conflicting"):
        _run(ledger=PriorReceiptLedger((forged,)))


@pytest.mark.parametrize(
    "case",
    (
        "duplicate",
        "missing",
        "reordered",
    ),
)
def test_duplicate_missing_or_reordered_prior_receipt_is_rejected(case: str) -> None:
    complete = _run()
    receipts = {
        "duplicate": complete.receipts[:1] + complete.receipts[:1],
        "missing": complete.receipts[:1] + complete.receipts[2:3],
        "reordered": complete.receipts[1:2] + complete.receipts[:1],
    }[case]
    with pytest.raises(OperationsValidationError, match="receipt"):
        _run(ledger=PriorReceiptLedger(receipts))


def test_preflight_artifact_and_owner_gates_reject_before_receipts() -> None:
    with pytest.raises(OperationsValidationError, match="preflight"):
        _run(preflight=_preflight(CutoverDoctorOutcome.NOT_READY))
    with pytest.raises(OperationsValidationError, match="attestation"):
        _run(attestation=_attestation(expires_at=_NOW))
    with pytest.raises(OperationsValidationError, match="owner gate"):
        _scenario(real_capture_gate=cast(OwnerGateState, "performed"))


@pytest.mark.parametrize(
    "change",
    (
        lambda value: replace(value, smoke_outcome=SurfaceCheckOutcome.FAILED),
        lambda value: replace(value, verification_outcome=SurfaceCheckOutcome.FAILED),
    ),
)
def test_failed_smoke_or_verification_without_trigger_is_blocked(
    change: Callable[[SyntheticSurfaceScenario], SyntheticSurfaceScenario],
) -> None:
    surfaces = list(_surface(index) for index in range(8))
    surfaces[2] = change(surfaces[2])
    result = _run(_scenario(surfaces=tuple(surfaces)))
    assert result.outcome is RehearsalOutcome.BLOCKED
    assert result.negative_case in {
        NegativeCase.SMOKE_FAILED_WITHOUT_TRIGGER,
        NegativeCase.VERIFICATION_FAILED_WITHOUT_TRIGGER,
    }


def test_serialized_result_is_metadata_only_and_owner_gated() -> None:
    result = _run()
    payload = result.to_dict()
    serialized = json.dumps(payload, sort_keys=True)

    assert payload["real_capture_gate"] == "not-performed-owner-gated"
    assert payload["live_transition_gate"] == "not-performed-owner-gated"
    assert payload["production_cutover_gate"] == "not-performed-owner-gated"
    assert payload["redacted"] is True
    assert not (
        _nested_keys(payload)
        & {"path", "url", "content", "credential", "error", "message", "exception"}
    )
    assert "not-performed-owner-gated" in serialized


def test_public_api_is_pure_and_accepts_no_capability_or_ambient_discovery() -> None:
    assert tuple(inspect.signature(rehearse_cutover).parameters) == (
        "preflight",
        "scenario",
        "prior_ledger",
        "artifact_attestation",
        "evaluated_at",
    )
    source = inspect.getsource(cutover_module)
    for forbidden_import in (
        "import os",
        "import pathlib",
        "import socket",
        "import subprocess",
        "import urllib",
    ):
        assert forbidden_import not in source
    assert "run_cutover_doctor" not in source


def test_result_revalidates_digest_on_public_access() -> None:
    result = _run()
    object.__setattr__(result, "_result_digest_sha256", _B)

    with pytest.raises(OperationsValidationError, match="result digest"):
        _ = result.outcome


def test_closed_models_reject_arbitrary_rollback_values() -> None:
    with pytest.raises(OperationsValidationError, match="rollback stage"):
        RollbackStageAttempt(
            cast(RollbackStage, "custom"),
            SurfaceCheckOutcome.PASSED,
            RollbackDisposition.NOT_REQUIRED,
        )
    assert {field.name for field in fields(SyntheticSurfaceScenario)}.isdisjoint(
        {"callback", "adapter", "service", "path", "command", "provider"}
    )


def test_phase6_doctor_remains_preflight_only_and_never_cutover_ready() -> None:
    preflight = _preflight()

    assert preflight.synthetic_ready is True
    assert preflight.cutover_ready is False
    assert tuple(check.probe for check in preflight.checks) == tuple(CutoverProbeName)
