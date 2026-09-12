from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast

import pytest

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli._registry import CommandAdapterRegistry
from open_brain_legacy.cli.main import main
from open_brain_legacy.cli.phase6_adapters import (
    ConfigMigrationCommandAdapter,
    CutoverDoctorCommandAdapter,
    StateAdoptionCommandAdapter,
)
from open_brain_legacy.migrate import MigrationState
from open_brain_legacy.migrate._models import (
    StateAdoptionReceiptEvidence,
    StateAuthorityReceipt,
    StateAuthorityReceiptEvidence,
)
from open_brain_legacy.migrate.config import (
    EVIDENCE_VERSION,
    ConfigMigrationPlan,
    ConfigMigrationResult,
    ConfigMigrationState,
    PrerequisiteReceipt,
    PublicationReceipt,
)
from open_brain_legacy.migrate.state import (
    StateAdoptionPlan,
    StatePlanCapabilities,
    StateTargetState,
    canonical_state_manifest,
)
from open_brain_legacy.operations.cutover_doctor import (
    CutoverCheck,
    CutoverCheckState,
    CutoverDoctorOutcome,
    CutoverDoctorResult,
    CutoverFindingClass,
    CutoverProbeName,
    phase6_cutover_manifest,
)

_DIGEST = "a" * 64
_EVALUATED_AT = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
_BACKUP_ROOT_BINDING = "b" * 64
_RESTORE_ROOT_BINDING = "c" * 64


def _config_plan(*, ready: bool = True) -> ConfigMigrationPlan:
    return ConfigMigrationPlan(
        request_digest=_DIGEST,
        destination_digest=_DIGEST,
        identity_digest=_DIGEST,
        plan_digest=_DIGEST,
        root_count=5,
        secret_count=2,
        prerequisite_count=1,
        existing_output_count=0,
        change_count=2,
        ready=ready,
        prerequisite_receipt=PrerequisiteReceipt("config-migration-evidence-v1", "synthetic"),
    )


@dataclass
class FakeConfigPlanner:
    calls: int = 0

    def plan_config_migration(self) -> ConfigMigrationPlan:
        self.calls += 1
        return _config_plan()


@dataclass
class RaisingConfigPlanner:
    calls: int = 0

    def plan_config_migration(self) -> ConfigMigrationPlan:
        self.calls += 1
        raise RuntimeError("token=synthetic-secret /synthetic/private")


@dataclass
class FakeConfigApplier:
    result: ConfigMigrationResult | None = None
    calls: int = 0

    def apply_config_migration(self) -> ConfigMigrationResult:
        self.calls += 1
        return _config_apply_result() if self.result is None else self.result


@dataclass
class FakeStateServices:
    apply_result: StateAdoptionReceiptEvidence | None = None
    restore_result: StateAuthorityReceiptEvidence | None = None
    plan_calls: int = 0
    apply_calls: int = 0
    restore_calls: int = 0

    def plan_state_adoption(self) -> StateAdoptionPlan:
        self.plan_calls += 1
        return _state_plan()

    def apply_state_adoption(self) -> StateAdoptionReceiptEvidence:
        self.apply_calls += 1
        return _state_apply_evidence() if self.apply_result is None else self.apply_result

    def verify_state_restore(self) -> StateAuthorityReceiptEvidence:
        self.restore_calls += 1
        return _state_restore_evidence() if self.restore_result is None else self.restore_result


@dataclass
class FakeCutoverDoctor:
    result: CutoverDoctorResult
    calls: list[bool]

    def run_cutover_doctor(self, *, strict: bool) -> CutoverDoctorResult:
        self.calls.append(strict)
        return self.result


def _state_plan() -> StateAdoptionPlan:
    return StateAdoptionPlan(
        manifest=canonical_state_manifest(),
        capabilities=StatePlanCapabilities._issued(object()),
        source_root_binding=_DIGEST,
        target_root_binding="b" * 64,
        backup_root_binding="c" * 64,
        target_state=StateTargetState.EMPTY,
        source_snapshot_digest="d" * 64,
        target_snapshot_digest="e" * 64,
        manifest_digest="f" * 64,
        idempotency_key_count=0,
        idempotency_key_digest=_DIGEST,
        artifacts=(),
        snapshot_payloads=(),
        fingerprint="b" * 64,
    )


def _config_apply_result() -> ConfigMigrationResult:
    return ConfigMigrationResult(
        ConfigMigrationState.APPLIED,
        2,
        _DIGEST,
        PublicationReceipt(EVIDENCE_VERSION, "synthetic-publication"),
    )


def _config_noop_result() -> ConfigMigrationResult:
    return ConfigMigrationResult(ConfigMigrationState.NOOP, 0, _DIGEST)


def _malformed_config_results() -> list[tuple[str, ConfigMigrationResult]]:
    applied = _config_apply_result()
    noop = _config_noop_result()
    return [
        (
            "review-reproduction",
            ConfigMigrationResult(
                ConfigMigrationState.APPLIED,
                -1,
                "not-a-digest",
            ),
        ),
        (
            "wrong-state-type",
            ConfigMigrationResult("applied", 2, _DIGEST, applied.publication_receipt),  # type: ignore[arg-type]
        ),
        ("uppercase-digest", replace(applied, plan_digest="A" * 64)),
        ("short-digest", replace(applied, plan_digest="a" * 63)),
        ("nonhex-digest", replace(applied, plan_digest="z" * 64)),
        ("boolean-output-count", replace(applied, output_count=True)),
        ("negative-output-count", replace(applied, output_count=-1)),
        ("applied-zero-outputs", replace(applied, output_count=0)),
        ("applied-one-output", replace(applied, output_count=1)),
        ("applied-three-outputs", replace(applied, output_count=3)),
        ("applied-missing-receipt", replace(applied, publication_receipt=None)),
        (
            "applied-wrong-receipt-version",
            replace(
                applied,
                publication_receipt=PublicationReceipt("wrong-version", "synthetic"),
            ),
        ),
        (
            "applied-empty-receipt-token",
            replace(
                applied,
                publication_receipt=PublicationReceipt(EVIDENCE_VERSION, ""),
            ),
        ),
        ("noop-with-output", replace(noop, output_count=1)),
        (
            "noop-with-receipt",
            replace(
                noop,
                publication_receipt=PublicationReceipt(
                    EVIDENCE_VERSION,
                    "synthetic-publication",
                ),
            ),
        ),
    ]


def _state_apply_evidence() -> StateAdoptionReceiptEvidence:
    return StateAdoptionReceiptEvidence(
        schema_version=1,
        operation="apply",
        state=MigrationState.APPLIED,
        plan_fingerprint=_DIGEST,
        manifest_digest="b" * 64,
        source_snapshot_digest="e" * 64,
        target_before_digest="d" * 64,
        target_after_digest="e" * 64,
        write_count=9,
        duplicate_idempotency_keys=0,
        duplicate_captures=0,
        backup=StateAuthorityReceipt._issued(object()),
        disposable_restore=StateAuthorityReceipt._issued(object()),
    )


def _state_noop_evidence() -> StateAdoptionReceiptEvidence:
    return replace(
        _state_apply_evidence(),
        state=MigrationState.NOOP,
        source_snapshot_digest="d" * 64,
        target_after_digest="d" * 64,
        write_count=0,
        backup=None,
        disposable_restore=None,
    )


def _state_restore_evidence() -> StateAuthorityReceiptEvidence:
    return StateAuthorityReceiptEvidence(
        version=1,
        operation="restore",
        plan_fingerprint=_DIGEST,
        root_bindings=(_BACKUP_ROOT_BINDING, _RESTORE_ROOT_BINDING),
        expires_at=datetime(2026, 8, 14, 13, 0, tzinfo=UTC),
        tracked_count=9,
        file_count=0,
        restored_count=0,
        removed_count=0,
    )


def _state_adapter(services: FakeStateServices) -> StateAdoptionCommandAdapter:
    return StateAdoptionCommandAdapter(
        planner=services,
        applier=services,
        restore_verifier=services,
        restore_evaluated_at=_EVALUATED_AT,
        restore_plan_fingerprint=_DIGEST,
        restore_root_bindings=(_BACKUP_ROOT_BINDING, _RESTORE_ROOT_BINDING),
    )


def _malformed_state_apply_results() -> list[tuple[str, StateAdoptionReceiptEvidence]]:
    applied = _state_apply_evidence()
    noop = _state_noop_evidence()
    applied_bool_schema = replace(applied)
    object.__setattr__(applied_bool_schema, "schema_version", True)
    noop_float_schema = replace(noop)
    object.__setattr__(noop_float_schema, "schema_version", 1.0)
    results = [
        (
            "review-reproduction",
            replace(
                applied,
                write_count=-1,
                duplicate_idempotency_keys=2,
                duplicate_captures=3,
                backup=None,
                disposable_restore=None,
            ),
        ),
        ("bool-schema-applied", applied_bool_schema),
        ("float-schema-noop", noop_float_schema),
        ("wrong-schema", replace(applied, schema_version=2)),
        ("wrong-operation", replace(applied, operation="restore")),
        ("negative-write-count", replace(applied, write_count=-1)),
        ("duplicate-idempotency", replace(applied, duplicate_idempotency_keys=1)),
        ("negative-idempotency", replace(applied, duplicate_idempotency_keys=-1)),
        ("duplicate-capture", replace(applied, duplicate_captures=1)),
        ("negative-capture", replace(applied, duplicate_captures=-1)),
        ("applied-zero-writes", replace(applied, write_count=0)),
        ("applied-short-write-count", replace(applied, write_count=8)),
        ("applied-long-write-count", replace(applied, write_count=10)),
        ("applied-equal-digests", replace(applied, target_after_digest="d" * 64)),
        (
            "applied-source-target-mismatch",
            replace(applied, source_snapshot_digest="c" * 64),
        ),
        ("applied-missing-backup", replace(applied, backup=None)),
        ("applied-missing-restore", replace(applied, disposable_restore=None)),
        ("applied-reused-authority", replace(applied, disposable_restore=applied.backup)),
        ("noop-writes", replace(noop, write_count=1)),
        ("noop-backup", replace(noop, backup=StateAuthorityReceipt._issued(object()))),
        (
            "noop-restore",
            replace(noop, disposable_restore=StateAuthorityReceipt._issued(object())),
        ),
        ("noop-digest-mismatch", replace(noop, target_after_digest="e" * 64)),
        ("noop-source-mismatch", replace(noop, source_snapshot_digest="c" * 64)),
    ]
    results.extend(
        (
            ("invalid-plan-fingerprint", replace(applied, plan_fingerprint="not-a-digest")),
            ("invalid-manifest-digest", replace(applied, manifest_digest="not-a-digest")),
            (
                "invalid-source-snapshot-digest",
                replace(applied, source_snapshot_digest="not-a-digest"),
            ),
            (
                "invalid-target-before-digest",
                replace(applied, target_before_digest="not-a-digest"),
            ),
            (
                "invalid-target-after-digest",
                replace(applied, target_after_digest="not-a-digest"),
            ),
        )
    )
    return results


def _malformed_state_restore_results() -> list[tuple[str, StateAuthorityReceiptEvidence]]:
    valid = _state_restore_evidence()
    bool_version = replace(valid)
    object.__setattr__(bool_version, "version", True)
    float_version = replace(valid)
    object.__setattr__(float_version, "version", 1.0)
    return [
        (
            "review-reproduction",
            replace(
                valid,
                plan_fingerprint="not-a-digest",
                root_bindings=(),
                expires_at=datetime(2026, 8, 14, 11, 0, tzinfo=UTC),
                file_count=-1,
            ),
        ),
        ("bool-version", bool_version),
        ("float-version", float_version),
        ("version-zero", replace(valid, version=0)),
        ("version-two", replace(valid, version=2)),
        ("wrong-operation", replace(valid, operation="backup")),
        ("invalid-fingerprint", replace(valid, plan_fingerprint="not-a-digest")),
        ("wrong-fingerprint", replace(valid, plan_fingerprint="d" * 64)),
        ("missing-roots", replace(valid, root_bindings=())),
        ("one-root", replace(valid, root_bindings=(_BACKUP_ROOT_BINDING,))),
        (
            "extra-root",
            replace(
                valid,
                root_bindings=(
                    _BACKUP_ROOT_BINDING,
                    _RESTORE_ROOT_BINDING,
                    "d" * 64,
                ),
            ),
        ),
        (
            "invalid-root-binding",
            replace(valid, root_bindings=("not-a-digest", _RESTORE_ROOT_BINDING)),
        ),
        (
            "wrong-root-binding",
            replace(valid, root_bindings=("d" * 64, _RESTORE_ROOT_BINDING)),
        ),
        (
            "duplicate-root-binding",
            replace(valid, root_bindings=(_BACKUP_ROOT_BINDING, _BACKUP_ROOT_BINDING)),
        ),
        ("expired", replace(valid, expires_at=_EVALUATED_AT)),
        (
            "naive-expiry",
            replace(valid, expires_at=datetime(2026, 8, 14, 13, 0)),
        ),
        ("negative-tracked", replace(valid, tracked_count=-1)),
        ("negative-file", replace(valid, file_count=-1)),
        ("negative-restored", replace(valid, restored_count=-1)),
        ("negative-removed", replace(valid, removed_count=-1)),
        ("short-tracked-count", replace(valid, tracked_count=8)),
        ("long-tracked-count", replace(valid, tracked_count=10)),
        ("files-exceed-tracked", replace(valid, tracked_count=0, file_count=1)),
        ("restore-count-mismatch", replace(valid, file_count=1, restored_count=0)),
        ("unexpected-removal", replace(valid, removed_count=1)),
    ]


def _doctor_result(outcome: CutoverDoctorOutcome) -> CutoverDoctorResult:
    if outcome is CutoverDoctorOutcome.SYNTHETIC_READY:
        state = CutoverCheckState.HEALTHY
        findings: tuple[CutoverFindingClass, ...] = ()
        exit_code = 0
    elif outcome is CutoverDoctorOutcome.NOT_READY:
        state = CutoverCheckState.UNHEALTHY
        findings = (CutoverFindingClass.CONFIG_INVALID,)
        exit_code = 1
    else:
        state = CutoverCheckState.UNAVAILABLE
        findings = (CutoverFindingClass.PROBE_MISSING,)
        exit_code = 78
    checks = tuple(CutoverCheck(probe, state, findings) for probe in CutoverProbeName)
    return CutoverDoctorResult._create(
        manifest=phase6_cutover_manifest(),
        strict=True,
        outcome=outcome,
        exit_code=exit_code,
        checks=checks,
    )


def _forge_doctor_result(
    *,
    outcome: CutoverDoctorOutcome = CutoverDoctorOutcome.SYNTHETIC_READY,
    exit_code: int = 0,
    checks: tuple[CutoverCheck, ...] | None = None,
    schema_version: object = 2,
    manifest_version: object | None = None,
    manifest_digest: str | None = None,
) -> CutoverDoctorResult:
    manifest = phase6_cutover_manifest()
    selected_checks = (
        tuple(CutoverCheck(probe, CutoverCheckState.HEALTHY, ()) for probe in CutoverProbeName)
        if checks is None
        else checks
    )
    result = CutoverDoctorResult._create(
        manifest=manifest,
        strict=True,
        outcome=outcome,
        exit_code=exit_code,
        checks=selected_checks,
    )
    object.__setattr__(result, "schema_version", schema_version)
    object.__setattr__(
        result,
        "manifest_version",
        manifest.schema_version if manifest_version is None else manifest_version,
    )
    object.__setattr__(
        result,
        "manifest_digest",
        manifest.manifest_digest if manifest_digest is None else manifest_digest,
    )
    return result


def _malformed_doctor_results() -> list[tuple[str, CutoverDoctorResult]]:
    healthy = tuple(
        CutoverCheck(probe, CutoverCheckState.HEALTHY, ()) for probe in CutoverProbeName
    )
    unhealthy = tuple(
        CutoverCheck(
            probe,
            CutoverCheckState.UNHEALTHY,
            (CutoverFindingClass.CONFIG_INVALID,),
        )
        for probe in CutoverProbeName
    )
    unavailable = tuple(
        CutoverCheck(
            probe,
            CutoverCheckState.UNAVAILABLE,
            (CutoverFindingClass.PROBE_MISSING,),
        )
        for probe in CutoverProbeName
    )
    string_probes = tuple(
        CutoverCheck(
            cast(CutoverProbeName, probe.value),
            CutoverCheckState.HEALTHY,
            (),
        )
        for probe in CutoverProbeName
    )
    return [
        (
            "review-reproduction-unhealthy-as-ready",
            _forge_doctor_result(checks=(unhealthy[0],)),
        ),
        ("empty-checks", _forge_doctor_result(checks=())),
        ("missing-check", _forge_doctor_result(checks=healthy[:-1])),
        (
            "duplicate-check",
            _forge_doctor_result(checks=(*healthy[:-1], healthy[0])),
        ),
        ("reordered-checks", _forge_doctor_result(checks=tuple(reversed(healthy)))),
        ("unavailable-as-ready", _forge_doctor_result(checks=unavailable)),
        (
            "healthy-with-finding",
            _forge_doctor_result(
                checks=(
                    CutoverCheck(
                        CutoverProbeName.CONFIG_SECRETS,
                        CutoverCheckState.HEALTHY,
                        (CutoverFindingClass.CONFIG_INVALID,),
                    ),
                    *healthy[1:],
                )
            ),
        ),
        (
            "unhealthy-without-finding",
            _forge_doctor_result(
                outcome=CutoverDoctorOutcome.NOT_READY,
                exit_code=1,
                checks=(
                    CutoverCheck(
                        CutoverProbeName.CONFIG_SECRETS,
                        CutoverCheckState.UNHEALTHY,
                        (),
                    ),
                    *healthy[1:],
                ),
            ),
        ),
        (
            "unavailable-without-finding",
            _forge_doctor_result(
                outcome=CutoverDoctorOutcome.UNAVAILABLE,
                exit_code=78,
                checks=(
                    CutoverCheck(
                        CutoverProbeName.CONFIG_SECRETS,
                        CutoverCheckState.UNAVAILABLE,
                        (),
                    ),
                    *healthy[1:],
                ),
            ),
        ),
        (
            "duplicate-finding",
            _forge_doctor_result(
                outcome=CutoverDoctorOutcome.NOT_READY,
                exit_code=1,
                checks=(
                    CutoverCheck(
                        CutoverProbeName.CONFIG_SECRETS,
                        CutoverCheckState.UNHEALTHY,
                        (
                            CutoverFindingClass.CONFIG_INVALID,
                            CutoverFindingClass.CONFIG_INVALID,
                        ),
                    ),
                    *healthy[1:],
                ),
            ),
        ),
        (
            "not-ready-wrong-exit",
            _forge_doctor_result(
                outcome=CutoverDoctorOutcome.NOT_READY,
                exit_code=78,
                checks=unhealthy,
            ),
        ),
        (
            "unavailable-wrong-exit",
            _forge_doctor_result(
                outcome=CutoverDoctorOutcome.UNAVAILABLE,
                exit_code=1,
                checks=unavailable,
            ),
        ),
        ("wrong-result-schema", _forge_doctor_result(schema_version=1)),
        ("float-result-schema", _forge_doctor_result(schema_version=2.0)),
        ("wrong-manifest-version", _forge_doctor_result(manifest_version=2)),
        ("bool-manifest-version", _forge_doctor_result(manifest_version=True)),
        ("plain-string-probes", _forge_doctor_result(checks=string_probes)),
        (
            "wrong-manifest-digest",
            _forge_doctor_result(manifest_digest="b" * 64),
        ),
    ]


def test_config_migration_defaults_to_a_redacted_plan(
    capsys: pytest.CaptureFixture[str],
) -> None:
    planner = FakeConfigPlanner()
    adapter = ConfigMigrationCommandAdapter(planner=planner)

    exit_code = main(
        ["config", "migrate", "--json"],
        command_adapters=CommandAdapterRegistry({"config": adapter}),
    )

    assert exit_code is ExitCode.SUCCESS
    assert planner.calls == 1
    assert json.loads(capsys.readouterr().out) == {
        "command": "config",
        "dry_run": True,
        "plan": _config_plan().to_redacted_dict(),
        "status": "planned",
    }


def test_explicit_dry_run_routes_still_select_only_planning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = FakeConfigPlanner()
    state = FakeStateServices()
    adapters = CommandAdapterRegistry(
        {
            "config": ConfigMigrationCommandAdapter(planner=config),
            "migrate": StateAdoptionCommandAdapter(planner=state),
        }
    )

    assert (
        main(
            ["config", "migrate", "--dry-run", "--json"],
            command_adapters=adapters,
        )
        is ExitCode.SUCCESS
    )
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert (
        main(
            ["migrate", "state", "--dry-run", "--json"],
            command_adapters=adapters,
        )
        is ExitCode.SUCCESS
    )
    assert json.loads(capsys.readouterr().out)["status"] == "planned"
    assert config.calls == 1
    assert (state.plan_calls, state.apply_calls, state.restore_calls) == (1, 0, 0)


def test_service_failure_is_redacted_without_echoing_exception_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    planner = RaisingConfigPlanner()

    assert (
        main(
            ["config", "migrate", "--json"],
            command_adapters=CommandAdapterRegistry(
                {"config": ConfigMigrationCommandAdapter(planner=planner)}
            ),
        )
        is ExitCode.FAILURE
    )

    assert planner.calls == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "failed"
    rendered = json.dumps(output)
    assert "synthetic-secret" not in rendered
    assert "/synthetic/private" not in rendered


def test_config_migration_apply_requires_the_explicit_apply_route(
    capsys: pytest.CaptureFixture[str],
) -> None:
    planner = FakeConfigPlanner()
    applier = FakeConfigApplier()
    adapter = ConfigMigrationCommandAdapter(planner=planner, applier=applier)

    exit_code = main(
        ["config", "migrate", "--apply", "--json"],
        command_adapters=CommandAdapterRegistry({"config": adapter}),
    )

    assert exit_code is ExitCode.SUCCESS
    assert planner.calls == 0
    assert applier.calls == 1
    assert json.loads(capsys.readouterr().out) == {
        "command": "config",
        "dry_run": False,
        "output": _config_apply_result().to_redacted_dict(),
        "status": "applied",
    }


def test_config_noop_result_remains_valid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    applier = FakeConfigApplier(result=_config_noop_result())

    assert (
        main(
            ["config", "migrate", "--apply", "--json"],
            command_adapters=CommandAdapterRegistry(
                {"config": ConfigMigrationCommandAdapter(applier=applier)}
            ),
        )
        is ExitCode.SUCCESS
    )
    assert applier.calls == 1
    assert json.loads(capsys.readouterr().out)["status"] == "noop"


@pytest.mark.parametrize(
    ("_case", "result"),
    _malformed_config_results(),
    ids=[case for case, _result in _malformed_config_results()],
)
def test_config_apply_rejects_malformed_results(
    _case: str,
    result: ConfigMigrationResult,
    capsys: pytest.CaptureFixture[str],
) -> None:
    applier = FakeConfigApplier(result=result)

    assert (
        main(
            ["config", "migrate", "--apply", "--json"],
            command_adapters=CommandAdapterRegistry(
                {"config": ConfigMigrationCommandAdapter(applier=applier)}
            ),
        )
        is ExitCode.FAILURE
    )
    assert applier.calls == 1
    assert json.loads(capsys.readouterr().out) == {
        "command": "config",
        "error": {
            "code": "config_migration_apply_failed",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }


@pytest.mark.parametrize("family", ["config", "migrate"])
def test_dry_run_apply_routes_make_zero_service_calls(
    family: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    planner = FakeConfigPlanner()
    applier = FakeConfigApplier()
    state = FakeStateServices()
    adapters = CommandAdapterRegistry(
        {
            "config": ConfigMigrationCommandAdapter(planner=planner, applier=applier),
            "migrate": _state_adapter(state),
        }
    )
    argv = (
        ["config", "migrate", "--apply", "--dry-run", "--json"]
        if family == "config"
        else ["migrate", "state", "--apply", "--dry-run", "--json"]
    )

    assert main(argv, command_adapters=adapters) is ExitCode.SUCCESS
    assert planner.calls == 0
    assert applier.calls == 0
    assert (state.plan_calls, state.apply_calls, state.restore_calls) == (0, 0, 0)
    assert json.loads(capsys.readouterr().out) == {
        "command": "config" if family == "config" else "migration",
        "dry_run": True,
        "status": "dry_run",
    }


@pytest.mark.parametrize(
    ("argv", "called", "status"),
    [
        (["migrate", "state", "--json"], "plan", "planned"),
        (["migrate", "state", "--apply", "--json"], "apply", "applied"),
        (
            ["migrate", "state", "--verify-restore", "--json"],
            "restore",
            "verified",
        ),
    ],
)
def test_state_adoption_routes_call_only_the_selected_capability_service(
    argv: list[str],
    called: str,
    status: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    services = FakeStateServices()
    adapter = _state_adapter(services)

    exit_code = main(
        argv,
        command_adapters=CommandAdapterRegistry({"migrate": adapter}),
    )

    assert exit_code is ExitCode.SUCCESS
    assert (
        services.plan_calls,
        services.apply_calls,
        services.restore_calls,
    ) == (
        int(called == "plan"),
        int(called == "apply"),
        int(called == "restore"),
    )
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "migration"
    assert output["status"] == status
    if called == "plan":
        assert output["dry_run"] is True
        assert output["plan"] == _state_plan().to_redacted_dict()
    elif called == "apply":
        assert output["dry_run"] is False
        assert output["output"] == _state_apply_evidence().to_redacted_dict()
    else:
        assert output["output"] == {
            "record_count": 9,
            "removed_count": 0,
            "restored_count": 0,
            "schema_version": 1,
        }


def test_state_noop_evidence_remains_a_valid_apply_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    services = FakeStateServices(apply_result=_state_noop_evidence())

    assert (
        main(
            ["migrate", "state", "--apply", "--json"],
            command_adapters=CommandAdapterRegistry(
                {"migrate": StateAdoptionCommandAdapter(applier=services)}
            ),
        )
        is ExitCode.SUCCESS
    )
    assert services.apply_calls == 1
    assert json.loads(capsys.readouterr().out)["status"] == "noop"


@pytest.mark.parametrize(
    ("_case", "evidence"),
    _malformed_state_apply_results(),
    ids=[case for case, _evidence in _malformed_state_apply_results()],
)
def test_state_apply_rejects_contradictory_evidence(
    _case: str,
    evidence: StateAdoptionReceiptEvidence,
    capsys: pytest.CaptureFixture[str],
) -> None:
    services = FakeStateServices(apply_result=evidence)

    assert (
        main(
            ["migrate", "state", "--apply", "--json"],
            command_adapters=CommandAdapterRegistry(
                {"migrate": StateAdoptionCommandAdapter(applier=services)}
            ),
        )
        is ExitCode.FAILURE
    )
    assert services.apply_calls == 1
    assert json.loads(capsys.readouterr().out) == {
        "command": "migration",
        "error": {
            "code": "state_adoption_apply_failed",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }


@pytest.mark.parametrize(
    ("_case", "evidence"),
    _malformed_state_restore_results(),
    ids=[case for case, _evidence in _malformed_state_restore_results()],
)
def test_state_restore_rejects_expired_unbound_or_unreconciled_evidence(
    _case: str,
    evidence: StateAuthorityReceiptEvidence,
    capsys: pytest.CaptureFixture[str],
) -> None:
    services = FakeStateServices(restore_result=evidence)

    assert (
        main(
            ["migrate", "state", "--verify-restore", "--json"],
            command_adapters=CommandAdapterRegistry({"migrate": _state_adapter(services)}),
        )
        is ExitCode.FAILURE
    )
    assert services.restore_calls == 1
    assert json.loads(capsys.readouterr().out) == {
        "command": "migration",
        "error": {
            "code": "state_restore_verification_failed",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }


@pytest.mark.parametrize(
    ("outcome", "expected_exit", "expected_status"),
    [
        (CutoverDoctorOutcome.SYNTHETIC_READY, ExitCode.SUCCESS, "verified"),
        (CutoverDoctorOutcome.NOT_READY, ExitCode.FAILURE, "blocked"),
        (CutoverDoctorOutcome.UNAVAILABLE, ExitCode.FAILURE, "unavailable"),
    ],
)
def test_doctor_cutover_is_strict_non_live_and_maps_unready_to_nonzero(
    outcome: CutoverDoctorOutcome,
    expected_exit: ExitCode,
    expected_status: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = FakeCutoverDoctor(_doctor_result(outcome), [])
    adapter = CutoverDoctorCommandAdapter(service=service)

    exit_code = main(
        ["doctor", "--cutover", "--json"],
        command_adapters=CommandAdapterRegistry({"doctor": adapter}),
    )

    assert exit_code is expected_exit
    assert service.calls == [True]
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == "doctor"
    assert output["status"] == expected_status
    assert output["strict"] is True
    assert output["schema_version"] == 2
    assert len(output["checks"]) == len(CutoverProbeName)
    rendered = json.dumps(output, sort_keys=True).casefold()
    assert "cutover_ready" not in rendered
    assert "synthetic_ready" not in rendered
    assert '"live"' not in rendered


@pytest.mark.parametrize(
    ("_case", "result"),
    _malformed_doctor_results(),
    ids=[case for case, _result in _malformed_doctor_results()],
)
def test_doctor_rejects_contradictory_or_noncanonical_results(
    _case: str,
    result: CutoverDoctorResult,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = FakeCutoverDoctor(result, [])

    assert (
        main(
            ["doctor", "--cutover", "--json"],
            command_adapters=CommandAdapterRegistry(
                {"doctor": CutoverDoctorCommandAdapter(service=service)}
            ),
        )
        is ExitCode.FAILURE
    )

    assert service.calls == [True]
    assert json.loads(capsys.readouterr().out) == {
        "command": "doctor",
        "error": {
            "code": "cutover_doctor_failed",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "failed",
    }


@pytest.mark.parametrize(
    "argv",
    [
        ["config", "migrate", "--apply", "--apply", "token=synthetic-secret", "--json"],
        ["config", "migrate", "--dry-run", "--apply", "--json"],
        ["config", "migrate", "--dry-run", "--apply", "token=synthetic-secret", "--json"],
        ["config", "--apply", "migrate", "token=synthetic-secret", "--json"],
        ["config", "migrate", "--verify-restore", "token=synthetic-secret", "--json"],
        [
            "migrate",
            "state",
            "--apply",
            "--verify-restore",
            "token=synthetic-secret",
            "--json",
        ],
        ["migrate", "state", "--dry-run", "--apply", "--json"],
        ["migrate", "state", "--dry-run", "--apply", "token=synthetic-secret", "--json"],
        ["migrate", "state", "extra", "token=synthetic-secret", "--json"],
        ["doctor", "--cutover", "--cutover", "token=synthetic-secret", "--json"],
        ["doctor", "--unknown", "--cutover", "token=synthetic-secret", "--json"],
    ],
)
def test_unknown_duplicate_mixed_reordered_and_canary_argv_fail_closed(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_planner = FakeConfigPlanner()
    config_applier = FakeConfigApplier()
    state = FakeStateServices()
    doctor = FakeCutoverDoctor(_doctor_result(CutoverDoctorOutcome.SYNTHETIC_READY), [])
    adapters = CommandAdapterRegistry(
        {
            "config": ConfigMigrationCommandAdapter(
                planner=config_planner,
                applier=config_applier,
            ),
            "migrate": _state_adapter(state),
            "doctor": CutoverDoctorCommandAdapter(service=doctor),
        }
    )

    assert main(argv, command_adapters=adapters) is ExitCode.USAGE
    assert (config_planner.calls, config_applier.calls) == (0, 0)
    assert (state.plan_calls, state.apply_calls, state.restore_calls) == (0, 0, 0)
    assert doctor.calls == []
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "invalid"
    assert "synthetic-secret" not in json.dumps(output)


@pytest.mark.parametrize(
    ("argv", "command"),
    [
        (["config", "migrate", "--json"], "config"),
        (["migrate", "state", "--json"], "migrate"),
        (["doctor", "--cutover", "--json"], "doctor"),
    ],
)
def test_default_composition_does_not_register_phase6_adapters(
    argv: list[str],
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(argv) is ExitCode.FAILURE
    assert json.loads(capsys.readouterr().out) == {
        "command": command,
        "error": {
            "code": "command_adapter_unavailable",
            "message": "operation unavailable; details redacted",
            "redacted": True,
        },
        "status": "unavailable",
    }


@pytest.mark.parametrize(
    ("family", "argv"),
    [
        ("config", ["config", "migrate", "--apply", "--json"]),
        ("migrate", ["migrate", "state", "--apply", "--json"]),
        ("migrate", ["migrate", "state", "--verify-restore", "--json"]),
        ("doctor", ["doctor", "--cutover", "--json"]),
    ],
)
def test_missing_capability_service_is_nonzero_and_does_not_dispatch(
    family: str,
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = FakeConfigPlanner()
    state = FakeStateServices()
    adapters = CommandAdapterRegistry(
        {
            "config": ConfigMigrationCommandAdapter(planner=config),
            "migrate": StateAdoptionCommandAdapter(planner=state),
            "doctor": CutoverDoctorCommandAdapter(),
        }
    )

    assert main(argv, command_adapters=adapters) != ExitCode.SUCCESS
    assert config.calls == 0
    assert (state.plan_calls, state.apply_calls, state.restore_calls) == (0, 0, 0)
    output = json.loads(capsys.readouterr().out)
    assert output["command"] == ("migration" if family == "migrate" else family)
    assert output["status"] in {"deferred", "unavailable"}
