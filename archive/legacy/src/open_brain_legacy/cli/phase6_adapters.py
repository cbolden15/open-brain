"""Typed, dependency-injected CLI adapters for Phase 6 operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error
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
    PublicationReceipt,
)
from open_brain_legacy.migrate.state import StateAdoptionPlan, canonical_state_manifest
from open_brain_legacy.operations.cutover_doctor import (
    CutoverCheck,
    CutoverCheckState,
    CutoverDoctorOutcome,
    CutoverDoctorResult,
    CutoverFindingClass,
    CutoverProbeName,
    phase6_cutover_manifest,
)
from open_brain_legacy.operations.models import ExitClass

_DIGEST = re.compile(r"[0-9a-f]{64}")
_STATE_ARTIFACT_COUNT = sum(len(family.artifacts) for family in canonical_state_manifest().families)


class ConfigMigrationPlanner(Protocol):
    """Plan configuration migration without receiving CLI or ambient inputs."""

    def plan_config_migration(self) -> ConfigMigrationPlan: ...


class ConfigMigrationApplier(Protocol):
    """Apply configuration migration through an injected capability-bearing service."""

    def apply_config_migration(self) -> ConfigMigrationResult: ...


class StateAdoptionPlanner(Protocol):
    """Plan state adoption through an injected capability-bearing service."""

    def plan_state_adoption(self) -> StateAdoptionPlan: ...


class StateAdoptionApplier(Protocol):
    """Apply state adoption through an injected capability-bearing service."""

    def apply_state_adoption(self) -> StateAdoptionReceiptEvidence: ...


class StateRestoreVerifier(Protocol):
    """Verify disposable restore evidence through an injected service."""

    def verify_state_restore(self) -> StateAuthorityReceiptEvidence: ...


class CutoverDoctorService(Protocol):
    """Run the already-configured cutover doctor without CLI-supplied host data."""

    def run_cutover_doctor(self, *, strict: bool) -> CutoverDoctorResult: ...


@dataclass(frozen=True, slots=True)
class Phase6CliResult:
    """Closed public result returned through the existing adapter protocol."""

    exit_code: ExitCode
    envelope: dict[str, object]


@dataclass(frozen=True, slots=True)
class ConfigMigrationCommandAdapter:
    """Route exact config-migration argv to injected typed services."""

    planner: ConfigMigrationPlanner | None = None
    applier: ConfigMigrationApplier | None = None

    def dispatch(self, argv: tuple[str, ...]) -> Phase6CliResult:
        options = _family_options(argv, path="migrate")
        if options is None:
            return _invalid("config", "invalid_config_migration_request")
        if options in {(), ("--dry-run",)}:
            return self._plan()
        if options == ("--apply", "--dry-run"):
            return _dry_run("config")
        if options != ("--apply",):
            return _invalid("config", "invalid_config_migration_request")
        if self.applier is None:
            return _deferred("config", "config_migration_apply_unavailable")
        try:
            result = self.applier.apply_config_migration()
            _validate_config_result(result)
            output = result.to_redacted_dict()
        except Exception:
            return _failed("config", "config_migration_apply_failed")
        return Phase6CliResult(
            ExitCode.SUCCESS,
            {
                "command": "config",
                "dry_run": False,
                "output": output,
                "status": result.state.value,
            },
        )

    def _plan(self) -> Phase6CliResult:
        if self.planner is None:
            return _deferred("config", "config_migration_plan_unavailable")
        try:
            plan = self.planner.plan_config_migration()
            if not isinstance(plan, ConfigMigrationPlan):
                raise TypeError("invalid config migration plan")
            output = plan.to_redacted_dict()
        except Exception:
            return _failed("config", "config_migration_plan_failed")
        return Phase6CliResult(
            ExitCode.SUCCESS if plan.ready else ExitCode.FAILURE,
            {
                "command": "config",
                "dry_run": True,
                "plan": output,
                "status": "planned" if plan.ready else "blocked",
            },
        )


@dataclass(frozen=True, slots=True)
class StateAdoptionCommandAdapter:
    """Route exact state plan, apply, and restore-verification argv."""

    planner: StateAdoptionPlanner | None = None
    applier: StateAdoptionApplier | None = None
    restore_verifier: StateRestoreVerifier | None = None
    restore_evaluated_at: datetime | None = None
    restore_plan_fingerprint: str | None = None
    restore_root_bindings: tuple[str, str] | None = None

    def dispatch(self, argv: tuple[str, ...]) -> Phase6CliResult:
        options = _family_options(argv, path="state")
        if options is None:
            return _invalid("migration", "invalid_state_adoption_request")
        if options in {(), ("--dry-run",)}:
            return self._plan()
        if options == ("--apply", "--dry-run"):
            return _dry_run("migration")
        if options == ("--apply",):
            return self._apply()
        if options == ("--verify-restore",):
            return self._verify_restore()
        return _invalid("migration", "invalid_state_adoption_request")

    def _plan(self) -> Phase6CliResult:
        if self.planner is None:
            return _deferred("migration", "state_adoption_plan_unavailable")
        try:
            plan = self.planner.plan_state_adoption()
            if not isinstance(plan, StateAdoptionPlan):
                raise TypeError("invalid state adoption plan")
            output = plan.to_redacted_dict()
        except Exception:
            return _failed("migration", "state_adoption_plan_failed")
        return Phase6CliResult(
            ExitCode.SUCCESS if plan.ready else ExitCode.FAILURE,
            {
                "command": "migration",
                "dry_run": True,
                "plan": output,
                "status": "planned" if plan.ready else "blocked",
            },
        )

    def _apply(self) -> Phase6CliResult:
        if self.applier is None:
            return _deferred("migration", "state_adoption_apply_unavailable")
        try:
            evidence = self.applier.apply_state_adoption()
            _validate_state_apply_evidence(evidence)
            output = evidence.to_redacted_dict()
        except Exception:
            return _failed("migration", "state_adoption_apply_failed")
        return Phase6CliResult(
            ExitCode.SUCCESS,
            {
                "command": "migration",
                "dry_run": False,
                "output": output,
                "status": evidence.state.value,
            },
        )

    def _verify_restore(self) -> Phase6CliResult:
        if self.restore_verifier is None:
            return _deferred("migration", "state_restore_verification_unavailable")
        evaluated_at = _normalized_utc(self.restore_evaluated_at)
        plan_fingerprint = self.restore_plan_fingerprint
        root_bindings = self.restore_root_bindings
        if (
            evaluated_at is None
            or plan_fingerprint is None
            or not _is_digest(plan_fingerprint)
            or root_bindings is None
            or not _valid_root_bindings(root_bindings)
        ):
            return _deferred("migration", "state_restore_verification_unavailable")
        try:
            evidence = self.restore_verifier.verify_state_restore()
            _validate_state_restore_evidence(
                evidence,
                evaluated_at=evaluated_at,
                plan_fingerprint=plan_fingerprint,
                root_bindings=root_bindings,
            )
        except Exception:
            return _failed("migration", "state_restore_verification_failed")
        return Phase6CliResult(
            ExitCode.SUCCESS,
            {
                "command": "migration",
                "output": {
                    "record_count": evidence.tracked_count,
                    "removed_count": evidence.removed_count,
                    "restored_count": evidence.restored_count,
                    "schema_version": evidence.version,
                },
                "status": "verified",
            },
        )


@dataclass(frozen=True, slots=True)
class CutoverDoctorCommandAdapter:
    """Route only strict cutover doctor through an injected typed service."""

    service: CutoverDoctorService | None = None

    def dispatch(self, argv: tuple[str, ...]) -> Phase6CliResult:
        options = _option_only_route(argv)
        if options != ("--cutover",):
            return _invalid("doctor", "invalid_cutover_doctor_request")
        if self.service is None:
            return _deferred("doctor", "cutover_doctor_unavailable")
        try:
            result = self.service.run_cutover_doctor(strict=True)
            _validate_cutover_result(result)
        except Exception:
            return _failed("doctor", "cutover_doctor_failed")
        status = {
            CutoverDoctorOutcome.SYNTHETIC_READY: "verified",
            CutoverDoctorOutcome.NOT_READY: "blocked",
            CutoverDoctorOutcome.UNAVAILABLE: "unavailable",
        }[result.outcome]
        checks = [_cutover_check_metadata(check) for check in result.checks]
        return Phase6CliResult(
            (
                ExitCode.SUCCESS
                if result.outcome is CutoverDoctorOutcome.SYNTHETIC_READY
                else ExitCode.FAILURE
            ),
            {
                "checks": checks,
                "command": "doctor",
                "manifest_digest": result.manifest_digest,
                "schema_version": result.schema_version,
                "status": status,
                "strict": True,
            },
        )


def _family_options(argv: tuple[str, ...], *, path: str) -> tuple[str, ...] | None:
    if not argv or argv[0] != path:
        return None
    return _option_only_route(argv[1:])


def _option_only_route(argv: tuple[str, ...]) -> tuple[str, ...] | None:
    options: list[str] = []
    for argument in argv:
        if argument == "--json":
            continue
        if not argument.startswith("--") or argument in options:
            return None
        options.append(argument)
    return tuple(options)


def _validate_state_apply_evidence(evidence: StateAdoptionReceiptEvidence) -> None:
    if (
        not isinstance(evidence, StateAdoptionReceiptEvidence)
        or type(evidence.schema_version) is not int
        or evidence.schema_version != 1
        or evidence.operation != "apply"
        or not isinstance(evidence.state, MigrationState)
        or any(
            not _is_digest(value)
            for value in (
                evidence.plan_fingerprint,
                evidence.manifest_digest,
                evidence.source_snapshot_digest,
                evidence.target_before_digest,
                evidence.target_after_digest,
            )
        )
        or any(
            type(value) is not int or value < 0
            for value in (
                evidence.write_count,
                evidence.duplicate_idempotency_keys,
                evidence.duplicate_captures,
            )
        )
        or evidence.duplicate_idempotency_keys != 0
        or evidence.duplicate_captures != 0
    ):
        raise TypeError("invalid state adoption evidence")
    if evidence.state is MigrationState.APPLIED:
        if (
            evidence.write_count != _STATE_ARTIFACT_COUNT
            or evidence.source_snapshot_digest != evidence.target_after_digest
            or evidence.target_before_digest == evidence.target_after_digest
            or not isinstance(evidence.backup, StateAuthorityReceipt)
            or not isinstance(evidence.disposable_restore, StateAuthorityReceipt)
            or evidence.backup is evidence.disposable_restore
        ):
            raise TypeError("invalid applied state evidence")
        return
    if evidence.state is MigrationState.NOOP and (
        evidence.write_count != 0
        or evidence.source_snapshot_digest != evidence.target_before_digest
        or evidence.target_before_digest != evidence.target_after_digest
        or evidence.backup is not None
        or evidence.disposable_restore is not None
    ):
        raise TypeError("invalid no-op state evidence")


def _validate_config_result(result: ConfigMigrationResult) -> None:
    if (
        not isinstance(result, ConfigMigrationResult)
        or not isinstance(result.state, ConfigMigrationState)
        or type(result.output_count) is not int
        or not _is_digest(result.plan_digest)
    ):
        raise TypeError("invalid config migration result")
    if result.state is ConfigMigrationState.APPLIED:
        receipt = result.publication_receipt
        if (
            result.output_count != 2
            or not isinstance(receipt, PublicationReceipt)
            or receipt.version != EVIDENCE_VERSION
            or not isinstance(receipt.token, str)
            or not receipt.token
        ):
            raise TypeError("invalid applied config migration result")
        return
    if result.state is ConfigMigrationState.NOOP and (
        result.output_count != 0 or result.publication_receipt is not None
    ):
        raise TypeError("invalid no-op config migration result")


def _validate_state_restore_evidence(
    evidence: StateAuthorityReceiptEvidence,
    *,
    evaluated_at: datetime,
    plan_fingerprint: str,
    root_bindings: tuple[str, str],
) -> None:
    expires_at = _normalized_utc(evidence.expires_at)
    counts = (
        evidence.tracked_count,
        evidence.file_count,
        evidence.restored_count,
        evidence.removed_count,
    )
    if (
        not isinstance(evidence, StateAuthorityReceiptEvidence)
        or type(evidence.version) is not int
        or evidence.version != 1
        or evidence.operation != "restore"
        or evidence.plan_fingerprint != plan_fingerprint
        or not _is_digest(evidence.plan_fingerprint)
        or evidence.root_bindings != root_bindings
        or not _valid_root_bindings(evidence.root_bindings)
        or expires_at is None
        or expires_at <= evaluated_at
        or any(type(value) is not int or value < 0 for value in counts)
        or evidence.tracked_count != _STATE_ARTIFACT_COUNT
        or evidence.file_count > evidence.tracked_count
        or evidence.restored_count != evidence.file_count
        or evidence.removed_count != 0
    ):
        raise TypeError("invalid state restore evidence")


def _normalized_utc(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _valid_root_bindings(value: object) -> bool:
    return (
        type(value) is tuple
        and len(value) == 2
        and all(_is_digest(binding) for binding in value)
        and value[0] != value[1]
    )


def _validate_cutover_result(result: CutoverDoctorResult) -> None:
    manifest = phase6_cutover_manifest()
    if (
        not isinstance(result, CutoverDoctorResult)
        or type(result.schema_version) is not int
        or result.schema_version != 2
        or type(result.manifest_version) is not int
        or result.manifest_version != manifest.schema_version
        or result.manifest_digest != manifest.manifest_digest
        or result.strict is not True
        or result.cutover_ready is not False
        or isinstance(result.exit_code, bool)
        or type(result.exit_code) is not int
        or type(result.checks) is not tuple
        or any(
            not isinstance(check, CutoverCheck) or not isinstance(check.probe, CutoverProbeName)
            for check in result.checks
        )
        or tuple(check.probe for check in result.checks) != tuple(CutoverProbeName)
    ):
        raise TypeError("invalid cutover doctor result")
    unavailable_findings = {
        CutoverFindingClass.PROBE_MISSING,
        CutoverFindingClass.PROBE_TIMEOUT,
        CutoverFindingClass.PROBE_FAILURE,
    }
    for check in result.checks:
        if (
            not isinstance(check, CutoverCheck)
            or not isinstance(check.state, CutoverCheckState)
            or type(check.findings) is not tuple
            or any(not isinstance(finding, CutoverFindingClass) for finding in check.findings)
            or len(set(check.findings)) != len(check.findings)
            or (check.state is CutoverCheckState.HEALTHY) != (not check.findings)
            or (
                check.state is CutoverCheckState.UNAVAILABLE
                and any(finding not in unavailable_findings for finding in check.findings)
            )
            or (
                check.state is CutoverCheckState.UNHEALTHY
                and any(finding in unavailable_findings for finding in check.findings)
            )
        ):
            raise TypeError("invalid cutover doctor check")
    if any(check.state is CutoverCheckState.UNHEALTHY for check in result.checks):
        expected_outcome = CutoverDoctorOutcome.NOT_READY
    elif any(check.state is CutoverCheckState.UNAVAILABLE for check in result.checks):
        expected_outcome = CutoverDoctorOutcome.UNAVAILABLE
    else:
        expected_outcome = CutoverDoctorOutcome.SYNTHETIC_READY
    expected_exit = {
        CutoverDoctorOutcome.SYNTHETIC_READY: int(ExitClass.SUCCESS),
        CutoverDoctorOutcome.NOT_READY: 1,
        CutoverDoctorOutcome.UNAVAILABLE: int(ExitClass.CONFIGURATION),
    }[expected_outcome]
    if (
        result.outcome is not expected_outcome
        or result.exit_code != expected_exit
        or result.synthetic_ready is not (expected_outcome is CutoverDoctorOutcome.SYNTHETIC_READY)
    ):
        raise TypeError("invalid strict cutover doctor outcome")


def _cutover_check_metadata(check: CutoverCheck) -> dict[str, object]:
    return {
        "findings": [finding.value for finding in check.findings],
        "status": check.state.value,
    }


def _dry_run(command: str) -> Phase6CliResult:
    return Phase6CliResult(
        ExitCode.SUCCESS,
        {"command": command, "dry_run": True, "status": "dry_run"},
    )


def _invalid(command: str, code: str) -> Phase6CliResult:
    return Phase6CliResult(
        ExitCode.USAGE,
        {"command": command, "error": redacted_error(code), "status": "invalid"},
    )


def _deferred(command: str, code: str) -> Phase6CliResult:
    return Phase6CliResult(
        ExitCode.DEFERRED,
        {"command": command, "error": redacted_error(code), "status": "deferred"},
    )


def _failed(command: str, code: str) -> Phase6CliResult:
    return Phase6CliResult(
        ExitCode.FAILURE,
        {"command": command, "error": redacted_error(code), "status": "failed"},
    )
