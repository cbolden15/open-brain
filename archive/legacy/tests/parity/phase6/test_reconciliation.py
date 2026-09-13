from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy._compat.open_brain.config import (
    RetainedRootIdentities,
    RetainedRoots,
    SecretRef,
)
from open_brain_legacy.cli._registry import CommandAdapterRegistry
from open_brain_legacy.cli.main import main
from open_brain_legacy.cli.phase6_adapters import (
    ConfigMigrationCommandAdapter,
    CutoverDoctorCommandAdapter,
    StateAdoptionCommandAdapter,
)
from open_brain_legacy.migrate._models import (
    StateAdoptionReceiptEvidence,
    StateAuthorityReceiptEvidence,
)
from open_brain_legacy.migrate.config import (
    EVIDENCE_VERSION,
    AuthorityBinding,
    ConfigMigrationPlan,
    ConfigMigrationResult,
    DestinationSnapshot,
    MigrationPathIdentities,
    PrerequisiteClaims,
    PrerequisiteReceipt,
    PrerequisiteRequest,
    PrivateDestinationClass,
    PublicationReceipt,
    PublicationRequest,
    RecoveryReceipt,
    RecoveryRequest,
    SecretMigrationValue,
    plan_config_migration,
)
from open_brain_legacy.migrate.state import StateFamily, canonical_state_manifest
from open_brain_legacy.operations.cutover_doctor import (
    AcceptanceRow,
    BindEvidence,
    BindExposure,
    BindReading,
    ConfigEvidence,
    CutoverCheckState,
    CutoverDoctorOutcome,
    CutoverDoctorResult,
    CutoverEvidence,
    CutoverProbe,
    CutoverProbeName,
    DependencyEvidence,
    DependencyKind,
    DependencyReading,
    GateEvidence,
    RecoveryEvidence,
    RecoveryOperation,
    RecoveryReceiptEvidence,
    RootEvidence,
    RootReading,
    SchemaEvidence,
    SchemaKind,
    SchemaReading,
    SecretEvidence,
    SecretState,
    WriterEvidence,
    WriterGeneration,
    WriterLeaseEvidence,
    WriterReading,
    WriterRole,
    phase6_cutover_manifest,
    run_cutover_doctor,
)

EXPECTED_ACCEPTANCE_ROWS = frozenset(
    (
        *(f"CFG-{index:03d}" for index in range(1, 9)),
        *(f"STATE-{index:03d}" for index in range(1, 9)),
        *(f"DOC-{index:03d}" for index in range(1, 9)),
        *(f"CLI6-{index:03d}" for index in range(1, 5)),
    )
)

ROW_EVIDENCE: dict[str, tuple[str, ...]] = {
    "CFG-001": (
        "packages/app/tests/unit/test_config.py::"
        "test_retained_roots_are_absolute_distinct_and_preserved",
    ),
    "CFG-002": (
        "packages/legacy/tests/integration/migrate/test_config.py::"
        "test_plan_is_deterministic_redacted_and_reads_only_explicit_source",
    ),
    "CFG-003": (
        "packages/legacy/tests/integration/migrate/test_config.py::"
        "test_plan_is_deterministic_redacted_and_reads_only_explicit_source",
    ),
    "CFG-004": (
        "packages/legacy/tests/integration/migrate/test_config.py::"
        "test_apply_publishes_typed_public_refs_and_owner_only_private_values",
    ),
    "CFG-005": (
        "packages/legacy/tests/integration/migrate/test_config.py::"
        "test_apply_publishes_typed_public_refs_and_owner_only_private_values",
    ),
    "CFG-006": (
        "packages/legacy/tests/integration/migrate/test_config.py::"
        "test_existing_outputs_require_issuer_bound_backup_and_overwrite_receipts",
    ),
    "CFG-007": (
        "packages/legacy/tests/integration/migrate/test_config.py::"
        "test_forged_expired_and_wrong_scope_prerequisite_receipts_fail_before_noop",
    ),
    "CFG-008": (
        "packages/legacy/tests/integration/migrate/test_config.py::test_idempotent_replay_is_verified_noop",
    ),
    "STATE-001": (
        "packages/legacy/tests/integration/migrate/test_state.py::"
        "test_state_manifest_is_versioned_and_covers_all_seven_families",
    ),
    "STATE-002": (
        "packages/legacy/tests/integration/migrate/test_state.py::"
        "test_state_plan_is_read_only_identity_bound_and_classifies_empty_or_exact_replay",
    ),
    "STATE-003": (
        "packages/legacy/tests/integration/migrate/test_state.py::"
        "test_state_plan_rejects_unsafe_or_unlisted_artifacts_and_root_aliases",
    ),
    "STATE-004": (
        "packages/legacy/tests/integration/migrate/test_state.py::"
        "test_state_plan_blocks_idempotency_conflicts_and_partial_targets",
    ),
    "STATE-005": (
        "packages/legacy/tests/integration/migrate/test_state.py::"
        "test_sqlite_snapshot_includes_wal_rows_without_copying_sidecars",
    ),
    "STATE-006": (
        "packages/legacy/tests/integration/migrate/test_state.py::"
        "test_state_apply_is_backup_first_restorable_and_second_apply_is_noop",
    ),
    "STATE-007": (
        "packages/legacy/tests/integration/migrate/test_state.py::"
        "test_state_generation_recovery_after_process_termination",
    ),
    "STATE-008": (
        "packages/legacy/tests/integration/migrate/test_state.py::"
        "test_state_apply_is_backup_first_restorable_and_second_apply_is_noop",
    ),
    "DOC-001": (
        "packages/legacy/tests/integration/operations/test_cutover_doctor.py::"
        "test_doc_001_through_008_negative_evidence_prevents_readiness",
    ),
    "DOC-002": (
        "packages/legacy/tests/integration/operations/test_cutover_doctor.py::"
        "test_doc_001_through_008_negative_evidence_prevents_readiness",
    ),
    "DOC-003": (
        "packages/legacy/tests/integration/operations/test_cutover_doctor.py::"
        "test_missing_timeout_failure_and_wrong_evidence_fail_closed_and_redacted",
    ),
    "DOC-004": (
        "packages/legacy/tests/integration/operations/test_cutover_doctor.py::"
        "test_doc_001_through_008_negative_evidence_prevents_readiness",
    ),
    "DOC-005": (
        "packages/legacy/tests/integration/operations/test_cutover_doctor.py::"
        "test_p1_004_recovery_requires_successful_bound_receipt_pair",
    ),
    "DOC-006": (
        "packages/legacy/tests/integration/operations/test_cutover_doctor.py::"
        "test_doc_001_through_008_negative_evidence_prevents_readiness",
    ),
    "DOC-007": (
        "packages/legacy/tests/integration/operations/test_cutover_doctor.py::"
        "test_p1_005_writer_inventory_identity_role_and_lease_are_required",
    ),
    "DOC-008": (
        "packages/legacy/tests/integration/operations/test_cutover_doctor.py::"
        "test_p1_001_public_synthetic_preflight_has_no_authority_claim",
    ),
    "CLI6-001": (
        "packages/legacy/tests/integration/cli/test_phase6_adapters.py::"
        "test_dry_run_apply_routes_make_zero_service_calls",
    ),
    "CLI6-002": (
        "packages/legacy/tests/integration/cli/test_phase6_adapters.py::"
        "test_state_adoption_routes_call_only_the_selected_capability_service",
    ),
    "CLI6-003": (
        "packages/legacy/tests/integration/cli/test_phase6_adapters.py::"
        "test_doctor_cutover_is_strict_non_live_and_maps_unready_to_nonzero",
    ),
    "CLI6-004": (
        "packages/legacy/tests/integration/cli/test_phase6_adapters.py::"
        "test_default_composition_does_not_register_phase6_adapters",
    ),
}

_EVALUATED_AT = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
_MIGRATION_DIGEST = "a" * 64
_PAIR_DIGEST = "b" * 64
_ARTIFACT_DIGEST = "c" * 64


@dataclass
class _SyntheticPrerequisiteAuthority:
    requests: list[PrerequisiteRequest] = field(default_factory=list)
    records: dict[str, tuple[AuthorityBinding, PrerequisiteClaims]] = field(default_factory=dict)

    def probe(self, request: PrerequisiteRequest) -> PrerequisiteClaims:
        self.requests.append(request)
        return PrerequisiteClaims(count=2, ready=True)

    def issue(self, binding: AuthorityBinding, claims: PrerequisiteClaims) -> PrerequisiteReceipt:
        token = f"synthetic-prerequisite-{len(self.records) + 1}"
        self.records[token] = (binding, claims)
        return PrerequisiteReceipt(EVIDENCE_VERSION, token)

    def verify(
        self, receipt: PrerequisiteReceipt, binding: AuthorityBinding
    ) -> PrerequisiteClaims | None:
        record = self.records.get(receipt.token)
        if receipt.version != EVIDENCE_VERSION or record is None or record[0] != binding:
            return None
        return record[1]


@dataclass
class _PlanOnlyConfigPublisher:
    identities: MigrationPathIdentities
    inspect_calls: int = 0
    mutation_calls: int = 0

    def inspect(self, public_path: Path, private_path: Path) -> DestinationSnapshot:
        self.inspect_calls += 1
        return DestinationSnapshot(
            public_payload=None,
            private_payload=None,
            public_identity=self.identities.public_destination,
            private_identity=self.identities.private_destination,
            private_classification=PrivateDestinationClass.PRIVATE,
            private_confined=True,
            private_no_follow=True,
            private_is_symlink=False,
            private_owner=None,
            private_mode=None,
        )

    def recover(self, request: RecoveryRequest) -> RecoveryReceipt:
        self.mutation_calls += 1
        raise AssertionError("config planning attempted recovery")

    def verify_recovery(self, receipt: RecoveryReceipt, request: RecoveryRequest) -> bool:
        raise AssertionError("config planning attempted recovery verification")

    def publish(self, request: PublicationRequest) -> PublicationReceipt:
        self.mutation_calls += 1
        raise AssertionError("config planning attempted publication")

    def verify_publication(
        self,
        receipt: PublicationReceipt,
        request: PublicationRequest,
        observed: DestinationSnapshot,
    ) -> bool:
        raise AssertionError("config planning attempted publication verification")


@dataclass
class _StaticConfigPlanner:
    plan: ConfigMigrationPlan
    calls: int = 0

    def plan_config_migration(self) -> ConfigMigrationPlan:
        self.calls += 1
        return self.plan


class _RaisingConfigPlanner:
    def plan_config_migration(self) -> ConfigMigrationPlan:
        raise RuntimeError("token=synthetic-secret /synthetic/private")


@dataclass
class _MutationSentinel:
    config_apply_calls: int = 0
    state_apply_calls: int = 0
    restore_calls: int = 0

    def apply_config_migration(self) -> ConfigMigrationResult:
        self.config_apply_calls += 1
        raise AssertionError("config apply must remain unreachable")

    def apply_state_adoption(self) -> StateAdoptionReceiptEvidence:
        self.state_apply_calls += 1
        raise AssertionError("state apply must remain unreachable")

    def verify_state_restore(self) -> StateAuthorityReceiptEvidence:
        self.restore_calls += 1
        raise AssertionError("state restore must remain unreachable")


@dataclass
class _StaticDoctorService:
    result: CutoverDoctorResult
    strict_calls: list[bool] = field(default_factory=list)

    def run_cutover_doctor(self, *, strict: bool) -> CutoverDoctorResult:
        self.strict_calls.append(strict)
        return self.result


def _config_identities() -> MigrationPathIdentities:
    return MigrationPathIdentities(
        roots=RetainedRootIdentities(
            work="physical-work",
            personal="physical-personal",
            capture="physical-capture",
            saved_content="physical-saved-content",
            state="physical-state",
        ),
        public_destination="physical-public-config",
        private_destination="physical-private-config",
        public_tree="physical-public-tree",
    )


def _retained_roots(base: Path) -> RetainedRoots:
    return RetainedRoots(
        work=base / "retained-work",
        personal=base / "retained-personal",
        capture=base / "retained-capture",
        saved_content=base / "retained-saved-content",
        state=base / "runtime-state",
    )


def _config_source(base: Path, secret: str) -> Mapping[str, object]:
    return {
        "paths": {
            **_retained_roots(base).to_dict(),
            "backup_root": str(base / "backup"),
        },
        "host": {"identity": "synthetic-writer"},
        "providers": {"default": "synthetic-provider", "cloud_enabled": False},
        "egress": {"enabled": False},
        "secrets": {
            "provider_token": SecretMigrationValue(
                reference=SecretRef.parse("env:SYNTHETIC_PROVIDER_TOKEN"),
                value=secret,
            )
        },
    }


def _recovery_evidence() -> RecoveryEvidence:
    manifest_digest = phase6_cutover_manifest().manifest_digest
    backup = RecoveryReceiptEvidence(
        receipt_id="backup-receipt",
        operation=RecoveryOperation.BACKUP,
        migration_digest=_MIGRATION_DIGEST,
        manifest_digest=manifest_digest,
        pair_digest=_PAIR_DIGEST,
        artifact_digest=_ARTIFACT_DIGEST,
        source_receipt_id=None,
        completed_at=datetime(2026, 8, 14, 11, 30, tzinfo=UTC),
        succeeded=True,
        item_count=7,
    )
    restore = RecoveryReceiptEvidence(
        receipt_id="restore-receipt",
        operation=RecoveryOperation.RESTORE,
        migration_digest=_MIGRATION_DIGEST,
        manifest_digest=manifest_digest,
        pair_digest=_PAIR_DIGEST,
        artifact_digest=_ARTIFACT_DIGEST,
        source_receipt_id=backup.receipt_id,
        completed_at=datetime(2026, 8, 14, 11, 40, tzinfo=UTC),
        succeeded=True,
        item_count=7,
    )
    return RecoveryEvidence(
        manifest_digest=manifest_digest,
        backup_destination_available=True,
        backup=backup,
        restore=restore,
    )


def _writer_evidence(*, legacy_active: bool = False) -> WriterEvidence:
    manifest_digest = phase6_cutover_manifest().manifest_digest
    lease = WriterLeaseEvidence(
        lease_id="canonical-lease",
        owner_identity_id="canonical-writer",
        manifest_digest=manifest_digest,
        generation=4,
        acquired_at=datetime(2026, 8, 14, 11, 0, tzinfo=UTC),
        expires_at=datetime(2026, 8, 14, 13, 0, tzinfo=UTC),
    )
    return WriterEvidence(
        manifest_digest=manifest_digest,
        writers=(
            WriterReading(
                writer_id="legacy-writer",
                generation=WriterGeneration.LEGACY,
                identity_id="legacy-writer",
                role=WriterRole.LEGACY,
                active=legacy_active,
                canonical=False,
                lease=None,
            ),
            WriterReading(
                writer_id="primary-writer",
                generation=WriterGeneration.NEW,
                identity_id="canonical-writer",
                role=WriterRole.CANONICAL,
                active=True,
                canonical=True,
                lease=lease,
            ),
        ),
    )


def _root_evidence(*, missing: str | None = None) -> RootEvidence:
    manifest = phase6_cutover_manifest()
    return RootEvidence(
        manifest_digest=manifest.manifest_digest,
        roots=tuple(
            RootReading(
                root_id=root_id,
                exists=root_id != missing,
                permissions_safe=root_id != missing,
            )
            for root_id in manifest.required_root_ids
        ),
        prohibited_remote_count=0,
    )


def _healthy_evidence() -> dict[CutoverProbeName, CutoverEvidence]:
    manifest = phase6_cutover_manifest()
    manifest_digest = manifest.manifest_digest
    return {
        CutoverProbeName.CONFIG_SECRETS: ConfigEvidence(
            manifest_digest=manifest_digest,
            config_valid=True,
            secrets=(SecretEvidence("provider-key", SecretState.PRESENT),),
        ),
        CutoverProbeName.ROOTS_REMOTES: _root_evidence(),
        CutoverProbeName.DEPENDENCIES: DependencyEvidence(
            manifest_digest=manifest_digest,
            dependencies=(
                DependencyReading("capture-service", DependencyKind.SERVICE, True),
                DependencyReading("local-provider", DependencyKind.PROVIDER, True),
            ),
        ),
        CutoverProbeName.SCHEMAS: SchemaEvidence(
            manifest_digest=manifest_digest,
            schemas=(
                SchemaReading("capture-queue", SchemaKind.QUEUE, 2),
                SchemaReading("events", SchemaKind.DATABASE, 3),
            ),
        ),
        CutoverProbeName.RECOVERY: _recovery_evidence(),
        CutoverProbeName.NETWORK_BINDS: BindEvidence(
            manifest_digest=manifest_digest,
            binds=(BindReading("capture-service", BindExposure.LOOPBACK),),
        ),
        CutoverProbeName.WRITERS: _writer_evidence(),
        CutoverProbeName.GATES_SCOPE: GateEvidence(
            manifest_digest=manifest_digest,
            validated_rows=tuple(AcceptanceRow),
            unresolved_owner_gate_ids=manifest.expected_owner_gate_ids,
        ),
    }


def _constant_probe(evidence: CutoverEvidence) -> CutoverProbe:
    async def collect() -> CutoverEvidence:
        await asyncio.sleep(0)
        return evidence

    return collect


def _run_doctor(
    evidence: Mapping[CutoverProbeName, CutoverEvidence] | None = None,
) -> CutoverDoctorResult:
    selected = _healthy_evidence() if evidence is None else evidence
    return asyncio.run(
        run_cutover_doctor(
            probes={name: _constant_probe(value) for name, value in selected.items()},
            evaluated_at=_EVALUATED_AT,
            timeout_seconds=1.0,
            strict=True,
        )
    )


def _negative_doctor_evidence(row: str) -> dict[CutoverProbeName, CutoverEvidence]:
    evidence = _healthy_evidence()
    manifest = phase6_cutover_manifest()
    manifest_digest = manifest.manifest_digest
    if row == "DOC-001":
        evidence[CutoverProbeName.CONFIG_SECRETS] = ConfigEvidence(
            manifest_digest=manifest_digest,
            config_valid=False,
            secrets=(SecretEvidence("provider-key", SecretState.PRESENT),),
        )
    elif row == "DOC-002":
        evidence[CutoverProbeName.ROOTS_REMOTES] = _root_evidence(missing="work")
    elif row == "DOC-003":
        evidence[CutoverProbeName.DEPENDENCIES] = DependencyEvidence(
            manifest_digest=manifest_digest,
            dependencies=(
                DependencyReading("capture-service", DependencyKind.SERVICE, False),
                DependencyReading("local-provider", DependencyKind.PROVIDER, True),
            ),
        )
    elif row == "DOC-004":
        evidence[CutoverProbeName.SCHEMAS] = SchemaEvidence(
            manifest_digest=manifest_digest,
            schemas=(
                SchemaReading("capture-queue", SchemaKind.QUEUE, 1),
                SchemaReading("events", SchemaKind.DATABASE, 3),
            ),
        )
    elif row == "DOC-005":
        recovery = _recovery_evidence()
        evidence[CutoverProbeName.RECOVERY] = RecoveryEvidence(
            manifest_digest=manifest_digest,
            backup_destination_available=False,
            backup=recovery.backup,
            restore=recovery.restore,
        )
    elif row == "DOC-006":
        evidence[CutoverProbeName.NETWORK_BINDS] = BindEvidence(
            manifest_digest=manifest_digest,
            binds=(BindReading("capture-service", BindExposure.PUBLIC),),
        )
    elif row == "DOC-007":
        evidence[CutoverProbeName.WRITERS] = _writer_evidence(legacy_active=True)
    elif row == "DOC-008":
        evidence[CutoverProbeName.GATES_SCOPE] = GateEvidence(
            manifest_digest=manifest_digest,
            validated_rows=tuple(AcceptanceRow),
            unresolved_owner_gate_ids=("cutover-approval",),
        )
    else:
        raise AssertionError(f"unknown acceptance row: {row}")
    return evidence


def _nested_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(str(key) for key in value)
        for nested in value.values():
            keys.update(_nested_keys(nested))
        return keys
    if isinstance(value, list):
        for nested in value:
            keys.update(_nested_keys(nested))
        return keys
    return keys


def test_all_28_acceptance_rows_have_concrete_evidence() -> None:
    assert set(ROW_EVIDENCE) == EXPECTED_ACCEPTANCE_ROWS
    assert all(evidence for evidence in ROW_EVIDENCE.values())


def test_every_evidence_node_exists_in_the_focused_phase6_suites() -> None:
    for row, nodes in ROW_EVIDENCE.items():
        for node in nodes:
            raw_path, function_name = node.split("::", 1)
            path = Path(raw_path)
            assert path.is_file(), row
            assert f"def {function_name}(" in path.read_text(), row


def test_config_plan_is_explicit_deterministic_redacted_and_non_mutating(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture_value = "synthetic-private-value"
    source = _config_source(tmp_path, fixture_value)
    public_tree = tmp_path / "public-tree"
    public_path = public_tree / "config.toml"
    private_path = tmp_path / "private" / "config.env"
    identities = _config_identities()
    publisher = _PlanOnlyConfigPublisher(identities)
    authority = _SyntheticPrerequisiteAuthority()

    def plan() -> ConfigMigrationPlan:
        return plan_config_migration(
            source=source,
            public_path=public_path,
            private_path=private_path,
            public_tree=public_tree,
            expected_owner="synthetic-owner",
            publisher=publisher,
            prerequisite_authority=authority,
            identities=identities,
        )

    first = plan()
    second = plan()

    assert first.to_redacted_dict() == second.to_redacted_dict()
    assert first.root_count == 5
    assert first.secret_count == 1
    assert first.change_count == 2
    assert first.ready is True
    assert publisher.inspect_calls == 2
    assert publisher.mutation_calls == 0
    assert len(authority.requests) == 2
    assert all(request.provider == "synthetic-provider" for request in authority.requests)
    assert set(PrerequisiteRequest.__dataclass_fields__) == {
        "provider",
        "request_digest",
        "identity_digest",
        "evidence_version",
    }

    rendered_plan = json.dumps(first.to_redacted_dict(), sort_keys=True)
    assert fixture_value not in rendered_plan
    assert str(tmp_path) not in rendered_plan
    assert "SYNTHETIC_PROVIDER_TOKEN" not in rendered_plan
    assert set(first.to_redacted_dict()) == {
        "change_count",
        "existing_output_count",
        "output_count",
        "plan_digest",
        "prerequisite_count",
        "ready",
        "root_count",
        "secret_count",
    }

    planner = _StaticConfigPlanner(first)
    exit_code = main(
        ["config", "migrate", "--json"],
        command_adapters=CommandAdapterRegistry(
            {"config": ConfigMigrationCommandAdapter(planner=planner)}
        ),
    )
    envelope = json.loads(capsys.readouterr().out)
    assert exit_code is ExitCode.SUCCESS
    assert planner.calls == 1
    assert envelope["dry_run"] is True
    assert envelope["plan"] == first.to_redacted_dict()
    assert not _nested_keys(envelope) & {
        "cutover_ready",
        "live",
        "parity_ready",
    }


def test_config_state_and_doctor_inventories_are_exact_and_consistent(
    tmp_path: Path,
) -> None:
    config_root_ids = tuple(
        sorted(
            key.removesuffix("_root").replace("_", "-")
            for key in _retained_roots(tmp_path).to_dict()
        )
    )
    state_manifest = canonical_state_manifest()
    doctor_manifest = phase6_cutover_manifest()
    artifacts = tuple(
        artifact for family in state_manifest.families for artifact in family.artifacts
    )

    assert config_root_ids == doctor_manifest.required_root_ids
    assert state_manifest.schema_version == 1
    assert tuple(family.family for family in state_manifest.families) == tuple(StateFamily)
    assert len(state_manifest.families) == 7
    assert len(artifacts) == 9
    assert len({artifact.relative for artifact in artifacts}) == 9
    assert tuple(AcceptanceRow) == tuple(AcceptanceRow(f"DOC-{index:03d}") for index in range(1, 9))
    assert tuple(CutoverProbeName) == (
        CutoverProbeName.CONFIG_SECRETS,
        CutoverProbeName.ROOTS_REMOTES,
        CutoverProbeName.DEPENDENCIES,
        CutoverProbeName.SCHEMAS,
        CutoverProbeName.RECOVERY,
        CutoverProbeName.NETWORK_BINDS,
        CutoverProbeName.WRITERS,
        CutoverProbeName.GATES_SCOPE,
    )
    assert doctor_manifest.expected_owner_gate_ids == (
        "cutover-approval",
        "host-readiness-adapter",
    )
    assert len(doctor_manifest.manifest_digest) == 64
    assert doctor_manifest.manifest_digest == phase6_cutover_manifest().manifest_digest


def test_healthy_synthetic_doctor_never_claims_live_cutover_readiness(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = _run_doctor()

    assert result.outcome is CutoverDoctorOutcome.SYNTHETIC_READY
    assert result.exit_code == 0
    assert result.synthetic_ready is True
    assert result.cutover_ready is False
    assert result.strict is True
    assert tuple(check.probe for check in result.checks) == tuple(CutoverProbeName)
    assert all(check.state is CutoverCheckState.HEALTHY for check in result.checks)
    public_result = result.to_dict()
    serialized = json.dumps(public_result, sort_keys=True)
    for private_value in (
        "provider-key",
        "capture-service",
        "canonical-writer",
        "host-readiness-adapter",
    ):
        assert private_value not in serialized

    service = _StaticDoctorService(result)
    exit_code = main(
        ["doctor", "--cutover", "--json"],
        command_adapters=CommandAdapterRegistry(
            {"doctor": CutoverDoctorCommandAdapter(service=service)}
        ),
    )
    envelope = json.loads(capsys.readouterr().out)
    assert exit_code is ExitCode.SUCCESS
    assert service.strict_calls == [True]
    assert envelope["status"] == "verified"
    assert envelope["strict"] is True
    assert envelope["manifest_digest"] == result.manifest_digest
    assert len(envelope["checks"]) == len(CutoverProbeName)
    assert not _nested_keys(envelope) & {
        "cutover_ready",
        "live",
        "live_healthy",
        "parity_ready",
        "synthetic_ready",
    }


@pytest.mark.parametrize("row", [f"DOC-{index:03d}" for index in range(1, 9)])
def test_each_doctor_acceptance_row_fails_closed(
    row: str,
) -> None:
    expected_probe = {
        "DOC-001": CutoverProbeName.CONFIG_SECRETS,
        "DOC-002": CutoverProbeName.ROOTS_REMOTES,
        "DOC-003": CutoverProbeName.DEPENDENCIES,
        "DOC-004": CutoverProbeName.SCHEMAS,
        "DOC-005": CutoverProbeName.RECOVERY,
        "DOC-006": CutoverProbeName.NETWORK_BINDS,
        "DOC-007": CutoverProbeName.WRITERS,
        "DOC-008": CutoverProbeName.GATES_SCOPE,
    }[row]

    result = _run_doctor(_negative_doctor_evidence(row))
    selected = next(check for check in result.checks if check.probe is expected_probe)

    assert result.outcome is CutoverDoctorOutcome.NOT_READY
    assert result.exit_code == 1
    assert result.synthetic_ready is False
    assert result.cutover_ready is False
    assert selected.state is CutoverCheckState.UNHEALTHY
    assert selected.findings


def test_missing_probe_evidence_is_strictly_unavailable_and_non_live() -> None:
    result = _run_doctor({})

    assert result.outcome is CutoverDoctorOutcome.UNAVAILABLE
    assert result.exit_code == 78
    assert result.synthetic_ready is False
    assert result.cutover_ready is False
    assert all(check.state is CutoverCheckState.UNAVAILABLE for check in result.checks)


def test_explicit_config_and_state_dry_run_routes_make_zero_mutation_calls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = _MutationSentinel()
    adapters = CommandAdapterRegistry(
        {
            "config": ConfigMigrationCommandAdapter(applier=sentinel),
            "migrate": StateAdoptionCommandAdapter(
                applier=sentinel,
                restore_verifier=sentinel,
            ),
        }
    )
    routes = (
        ["--dry-run", "config", "migrate", "--apply", "--json"],
        ["config", "migrate", "--apply", "--dry-run", "--json"],
        ["--dry-run", "migrate", "state", "--apply", "--json"],
        ["migrate", "state", "--apply", "--dry-run", "--json"],
    )

    for argv in routes:
        assert main(argv, command_adapters=adapters) is ExitCode.SUCCESS
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["status"] == "dry_run"
        assert envelope["dry_run"] is True

    assert (
        sentinel.config_apply_calls,
        sentinel.state_apply_calls,
        sentinel.restore_calls,
    ) == (0, 0, 0)


def test_default_composition_cannot_inspect_or_mutate_host_or_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ambient_calls: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> None:
        ambient_calls.append("ambient-access")
        raise AssertionError("default composition attempted ambient access")

    monkeypatch.setattr(Path, "home", forbidden)
    monkeypatch.setattr(Path, "cwd", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    routes = (
        (["config", "migrate", "--json"], "config"),
        (["migrate", "state", "--json"], "migrate"),
        (["doctor", "--cutover", "--json"], "doctor"),
    )
    for argv, command in routes:
        assert main(argv) is ExitCode.FAILURE
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["command"] == command
        assert envelope["status"] == "unavailable"
        assert envelope["error"]["redacted"] is True
        assert not _nested_keys(envelope) & {
            "cutover_ready",
            "live",
            "parity_ready",
        }

    assert ambient_calls == []


def test_owner_gated_services_defer_without_dispatch_or_authority() -> None:
    sentinel = _MutationSentinel()
    config = ConfigMigrationCommandAdapter()
    state = StateAdoptionCommandAdapter()
    doctor = CutoverDoctorCommandAdapter()

    results = (
        config.dispatch(("migrate", "--apply")),
        state.dispatch(("state", "--apply")),
        state.dispatch(("state", "--verify-restore")),
        doctor.dispatch(("--cutover",)),
    )

    assert all(result.exit_code is ExitCode.DEFERRED for result in results)
    assert all(result.envelope["status"] == "deferred" for result in results)
    assert (
        sentinel.config_apply_calls,
        sentinel.state_apply_calls,
        sentinel.restore_calls,
    ) == (0, 0, 0)


def test_failure_redaction_is_consistent_across_config_and_doctor(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_exit = main(
        ["config", "migrate", "--json"],
        command_adapters=CommandAdapterRegistry(
            {"config": ConfigMigrationCommandAdapter(planner=_RaisingConfigPlanner())}
        ),
    )
    config_envelope = json.loads(capsys.readouterr().out)

    async def raising_probe() -> CutoverEvidence:
        raise RuntimeError("token=synthetic-secret /synthetic/private")

    probes = {name: _constant_probe(evidence) for name, evidence in _healthy_evidence().items()}
    probes[CutoverProbeName.CONFIG_SECRETS] = raising_probe
    doctor_result = asyncio.run(
        run_cutover_doctor(
            probes=probes,
            evaluated_at=_EVALUATED_AT,
            timeout_seconds=1.0,
            strict=True,
        )
    )
    doctor_service = _StaticDoctorService(doctor_result)
    doctor_exit = main(
        ["doctor", "--cutover", "--json"],
        command_adapters=CommandAdapterRegistry(
            {"doctor": CutoverDoctorCommandAdapter(service=doctor_service)}
        ),
    )
    doctor_envelope = json.loads(capsys.readouterr().out)

    assert config_exit is ExitCode.FAILURE
    assert config_envelope["status"] == "failed"
    assert doctor_exit is ExitCode.FAILURE
    assert doctor_envelope["status"] == "unavailable"
    assert doctor_result.cutover_ready is False
    rendered = json.dumps((config_envelope, doctor_envelope), sort_keys=True)
    assert "synthetic-secret" not in rendered
    assert "/synthetic/private" not in rendered
    assert not _nested_keys(doctor_envelope) & {
        "cutover_ready",
        "live",
        "parity_ready",
        "synthetic_ready",
    }
