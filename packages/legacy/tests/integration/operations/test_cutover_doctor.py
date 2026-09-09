from __future__ import annotations

import asyncio
import inspect
import json
import multiprocessing as mp
import os
import socket
import subprocess
import time
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from multiprocessing.connection import Connection

import pytest

import open_brain_legacy.operations.cutover_doctor as cutover_doctor_module
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
    CutoverFindingClass,
    CutoverManifest,
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

EVALUATED_AT = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
MIGRATION_DIGEST = "a" * 64
PAIR_DIGEST = "b" * 64
ARTIFACT_DIGEST = "c" * 64
OTHER_DIGEST = "d" * 64


def _manifest() -> CutoverManifest:
    return phase6_cutover_manifest()


def _recovery_receipts() -> tuple[RecoveryReceiptEvidence, RecoveryReceiptEvidence]:
    manifest_digest = _manifest().manifest_digest
    backup = RecoveryReceiptEvidence(
        receipt_id="backup-receipt",
        operation=RecoveryOperation.BACKUP,
        migration_digest=MIGRATION_DIGEST,
        manifest_digest=manifest_digest,
        pair_digest=PAIR_DIGEST,
        artifact_digest=ARTIFACT_DIGEST,
        source_receipt_id=None,
        completed_at=datetime(2026, 8, 14, 11, 30, tzinfo=UTC),
        succeeded=True,
        item_count=7,
    )
    restore = RecoveryReceiptEvidence(
        receipt_id="restore-receipt",
        operation=RecoveryOperation.RESTORE,
        migration_digest=MIGRATION_DIGEST,
        manifest_digest=manifest_digest,
        pair_digest=PAIR_DIGEST,
        artifact_digest=ARTIFACT_DIGEST,
        source_receipt_id=backup.receipt_id,
        completed_at=datetime(2026, 8, 14, 11, 40, tzinfo=UTC),
        succeeded=True,
        item_count=7,
    )
    return backup, restore


def _recovery_evidence() -> RecoveryEvidence:
    backup, restore = _recovery_receipts()
    return RecoveryEvidence(
        manifest_digest=_manifest().manifest_digest,
        backup_destination_available=True,
        backup=backup,
        restore=restore,
    )


def _writer_evidence() -> WriterEvidence:
    manifest_digest = _manifest().manifest_digest
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
                active=False,
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


def _root_evidence(
    *,
    missing: str | None = None,
    unsafe: str | None = None,
    prohibited_remote_count: int = 0,
) -> RootEvidence:
    return RootEvidence(
        manifest_digest=_manifest().manifest_digest,
        roots=tuple(
            RootReading(
                root_id,
                exists=root_id != missing,
                permissions_safe=root_id not in {missing, unsafe},
            )
            for root_id in _manifest().required_root_ids
        ),
        prohibited_remote_count=prohibited_remote_count,
    )


def _healthy_evidence() -> dict[CutoverProbeName, CutoverEvidence]:
    manifest = _manifest()
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
                DependencyReading("capture-service", DependencyKind.SERVICE, reachable=True),
                DependencyReading("local-provider", DependencyKind.PROVIDER, reachable=True),
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


def _healthy_probes(
    calls: list[CutoverProbeName] | None = None,
) -> dict[CutoverProbeName, CutoverProbe]:
    def probe(name: CutoverProbeName, evidence: CutoverEvidence) -> CutoverProbe:
        async def collect() -> CutoverEvidence:
            if calls is not None:
                calls.append(name)
            await asyncio.sleep(0)
            return evidence

        return collect

    return {name: probe(name, evidence) for name, evidence in _healthy_evidence().items()}


def _run(
    probes: dict[CutoverProbeName, CutoverProbe],
    *,
    strict: bool = True,
    timeout_seconds: float = 1.0,
    evaluated_at: datetime = EVALUATED_AT,
) -> CutoverDoctorResult:
    return asyncio.run(
        run_cutover_doctor(
            probes=probes,
            evaluated_at=evaluated_at,
            timeout_seconds=timeout_seconds,
            strict=strict,
        )
    )


def _run_with(
    probe_name: CutoverProbeName,
    evidence: CutoverEvidence,
    *,
    strict: bool = True,
) -> CutoverDoctorResult:
    probes = _healthy_probes()
    probes[probe_name] = _constant_probe(evidence)
    return _run(probes, strict=strict)


def _never_finish_regression_target(sender: Connection) -> None:
    async def suppresses_cancellation_forever() -> CutoverEvidence:
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                continue

    probes = _healthy_probes()
    probes[CutoverProbeName.CONFIG_SECRETS] = suppresses_cancellation_forever
    result = _run(probes, timeout_seconds=0.005)
    sender.send(
        (
            result.outcome.value,
            result.synthetic_ready,
            result.cutover_ready,
            result.checks[0].findings,
        )
    )
    sender.close()


def test_cutover_doctor_is_deterministic_bounded_and_metadata_only() -> None:
    first_calls: list[CutoverProbeName] = []
    second_calls: list[CutoverProbeName] = []
    first = _run(
        dict(reversed(tuple(_healthy_probes(first_calls).items()))),
        timeout_seconds=2.5,
    )
    second = _run(_healthy_probes(second_calls), timeout_seconds=2.5)

    assert first.to_dict() == second.to_dict()
    assert first.outcome is CutoverDoctorOutcome.SYNTHETIC_READY
    assert first.exit_code == 0
    assert first.synthetic_ready is True
    assert first.cutover_ready is False
    assert first_calls == []
    assert second_calls == []
    assert [check.probe for check in first.checks] == list(CutoverProbeName)
    assert all(check.findings == () for check in first.checks)

    serialized = json.dumps(first.to_dict()).lower()
    for forbidden_field in (
        '"error":',
        '"message":',
        '"path":',
        '"content":',
        '"url":',
        '"exception":',
        "provider-key",
        "capture-service",
        "canonical-writer",
        "host-readiness-adapter",
    ):
        assert forbidden_field not in serialized


def test_p1_001_public_synthetic_preflight_has_no_authority_claim() -> None:
    result = _run(_healthy_probes())

    assert not hasattr(cutover_doctor_module, "EvidenceScope")
    assert "scope" not in GateEvidence.__dataclass_fields__
    assert "authoritative_rows" not in GateEvidence.__dataclass_fields__
    assert "requirements" not in inspect.signature(run_cutover_doctor).parameters
    assert result.synthetic_ready is True
    assert result.cutover_ready is False
    assert result.to_dict()["cutover_ready"] is False
    with pytest.raises(TypeError, match="created by run_cutover_doctor"):
        CutoverDoctorResult()


def test_p1_002_fixed_manifest_rejects_shrunken_and_unbound_inventories() -> None:
    manifest = _manifest()
    assert manifest.schema_version == 1
    assert manifest.required_root_ids == (
        "capture",
        "personal",
        "saved-content",
        "state",
        "work",
    )
    assert manifest.required_secret_ids
    assert manifest.required_dependencies
    assert {row.kind for row in manifest.required_dependencies} == set(DependencyKind)
    assert manifest.current_schemas
    assert {row.kind for row in manifest.current_schemas} == set(SchemaKind)
    assert manifest.required_bind_ids
    assert manifest.required_writers
    assert manifest.expected_owner_gate_ids
    with pytest.raises(FrozenInstanceError):
        manifest.schema_version = 2  # type: ignore[misc]

    shrunk = _run_with(
        CutoverProbeName.CONFIG_SECRETS,
        ConfigEvidence(
            manifest_digest=manifest.manifest_digest,
            config_valid=True,
            secrets=(),
        ),
    )
    unbound = _run_with(
        CutoverProbeName.CONFIG_SECRETS,
        replace(
            _healthy_evidence()[CutoverProbeName.CONFIG_SECRETS],
            manifest_digest=OTHER_DIGEST,
        ),
    )

    assert shrunk.synthetic_ready is False
    assert shrunk.checks[0].findings == (CutoverFindingClass.SECRET_EVIDENCE_INCOMPLETE,)
    assert unbound.checks[0].findings == (CutoverFindingClass.MANIFEST_DIGEST_MISMATCH,)


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("schema_version", 99),
        ("manifest_id", "forged-manifest"),
        ("required_secret_ids", ()),
        ("required_root_ids", ("forged-root",)),
        ("required_dependencies", ()),
        ("current_schemas", ()),
        ("required_bind_ids", ()),
        ("required_writers", ()),
        ("expected_owner_gate_ids", ("forged-gate",)),
        ("max_recovery_age_seconds", 999_999),
    ],
)
def test_p1_r1_mutating_every_public_manifest_field_cannot_change_evaluation(
    field: str,
    forged_value: object,
) -> None:
    public_manifest = phase6_cutover_manifest()
    original_values = {
        name: getattr(public_manifest, name) for name in CutoverManifest.__dataclass_fields__
    }
    original_digest = public_manifest.manifest_digest

    object.__setattr__(public_manifest, field, forged_value)
    try:
        result = _run(_healthy_probes())
        fresh_public_manifest = phase6_cutover_manifest()
    finally:
        object.__setattr__(public_manifest, field, original_values[field])

    assert fresh_public_manifest is not public_manifest
    assert {
        name: getattr(fresh_public_manifest, name) for name in CutoverManifest.__dataclass_fields__
    } == original_values
    assert fresh_public_manifest.manifest_digest == original_digest
    assert result.manifest_digest == original_digest
    assert result.manifest_version == 1
    assert result.synthetic_ready is True
    assert result.cutover_ready is False


def test_p1_003_orchestration_cancels_late_and_never_completing_probes() -> None:
    async def scenario() -> tuple[CutoverDoctorResult, float, bool, bool]:
        late_cancelled = asyncio.Event()
        never_cancelled = asyncio.Event()
        healthy = _healthy_evidence()

        async def late_return() -> CutoverEvidence:
            try:
                await asyncio.sleep(1.0)
                return healthy[CutoverProbeName.CONFIG_SECRETS]
            finally:
                late_cancelled.set()

        async def never_completes() -> CutoverEvidence:
            try:
                await asyncio.Event().wait()
                raise AssertionError("never-completing probe returned")
            finally:
                never_cancelled.set()

        probes = _healthy_probes()
        probes[CutoverProbeName.CONFIG_SECRETS] = late_return
        probes[CutoverProbeName.DEPENDENCIES] = never_completes
        started = time.monotonic()
        result = await run_cutover_doctor(
            probes=probes,
            evaluated_at=EVALUATED_AT,
            timeout_seconds=0.01,
            strict=True,
        )
        return (
            result,
            time.monotonic() - started,
            late_cancelled.is_set(),
            never_cancelled.is_set(),
        )

    result, elapsed, late_cancelled, never_cancelled = asyncio.run(scenario())

    assert elapsed < 0.25
    assert late_cancelled is False
    assert never_cancelled is False
    assert result.synthetic_ready is False
    assert result.checks[0].findings == (CutoverFindingClass.PROBE_TIMEOUT,)
    assert result.checks[2].findings == (CutoverFindingClass.PROBE_TIMEOUT,)


def test_probe_supervisor_drains_a_result_after_the_worker_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = CutoverProbeName.CONFIG_SECRETS
    worker = cutover_doctor_module._start_probe_worker(
        mp.get_context("fork"),
        _constant_probe(_healthy_evidence()[probe]),
        1.0,
    )
    worker.process.join(5.0)
    assert worker.process.is_alive() is False
    receive = cutover_doctor_module._receive_worker_result
    calls = 0

    def delay_first_receive(
        candidate: cutover_doctor_module._ProbeWorker,
    ) -> tuple[str, CutoverEvidence | None] | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else receive(candidate)

    monkeypatch.setattr(cutover_doctor_module, "_receive_worker_result", delay_first_receive)

    results = asyncio.run(cutover_doctor_module._supervise_probe_workers({probe: worker}))

    assert calls == 2
    assert results[probe][0] == "healthy"


def test_p1_003_rejects_synchronous_probe_callbacks() -> None:
    evidence = _healthy_evidence()[CutoverProbeName.CONFIG_SECRETS]

    def synchronous_probe() -> CutoverEvidence:
        return evidence

    probes = _healthy_probes()
    probes[CutoverProbeName.CONFIG_SECRETS] = synchronous_probe  # type: ignore[assignment]
    with pytest.raises(ValueError, match="must be async"):
        _run(probes)


def test_p1_r2_cancellation_suppression_followed_by_healthy_return_times_out() -> None:
    healthy_config = _healthy_evidence()[CutoverProbeName.CONFIG_SECRETS]
    parent_mutations: list[str] = []

    async def suppresses_cancellation_then_returns_healthy() -> CutoverEvidence:
        try:
            await asyncio.Event().wait()
            raise AssertionError("cancellation-suppression probe returned")
        except asyncio.CancelledError:
            parent_mutations.append("child-mutated")
            return healthy_config

    probes = _healthy_probes()
    probes[CutoverProbeName.CONFIG_SECRETS] = suppresses_cancellation_then_returns_healthy
    result = _run(probes, timeout_seconds=0.005)

    assert result.synthetic_ready is False
    assert result.cutover_ready is False
    assert parent_mutations == []
    assert result.checks[0].state is CutoverCheckState.UNAVAILABLE
    assert result.checks[0].findings == (CutoverFindingClass.PROBE_TIMEOUT,)


def test_p1_r2_cancellation_suppression_forever_has_independent_guard() -> None:
    context = mp.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_never_finish_regression_target,
        args=(sender,),
    )
    process.start()
    sender.close()
    try:
        completed_within_guard = receiver.poll(0.5)
        payload = receiver.recv() if completed_within_guard else None
    finally:
        if process.is_alive():
            process.terminate()
        process.join(0.1)
        if process.is_alive():
            process.kill()
            process.join(0.1)
        receiver.close()

    assert completed_within_guard is True
    assert process.is_alive() is False
    assert payload == (
        CutoverDoctorOutcome.UNAVAILABLE.value,
        False,
        False,
        (CutoverFindingClass.PROBE_TIMEOUT,),
    )


def test_p1_004_recovery_requires_successful_bound_receipt_pair() -> None:
    assert "last_backup_at" not in RecoveryEvidence.__dataclass_fields__
    assert "last_restore_at" not in RecoveryEvidence.__dataclass_fields__
    healthy = _recovery_evidence()
    assert healthy.backup is not None
    assert healthy.restore is not None

    failed = _run_with(
        CutoverProbeName.RECOVERY,
        replace(healthy, restore=replace(healthy.restore, succeeded=False)),
    )
    mismatched = _run_with(
        CutoverProbeName.RECOVERY,
        replace(
            healthy,
            restore=replace(healthy.restore, migration_digest=OTHER_DIGEST),
        ),
    )

    assert CutoverFindingClass.RECOVERY_NOT_SUCCESSFUL in failed.checks[4].findings
    assert CutoverFindingClass.RECOVERY_RECEIPT_MISMATCH in mismatched.checks[4].findings
    assert failed.synthetic_ready is False
    assert mismatched.synthetic_ready is False


def test_p1_005_writer_inventory_identity_role_and_lease_are_required() -> None:
    assert "legacy_active_count" not in WriterEvidence.__dataclass_fields__
    assert "new_active_count" not in WriterEvidence.__dataclass_fields__
    assert "canonical_new_count" not in WriterEvidence.__dataclass_fields__
    healthy = _writer_evidence()
    legacy, primary = healthy.writers
    assert primary.lease is not None

    shrunken = _run_with(
        CutoverProbeName.WRITERS,
        replace(healthy, writers=(primary,)),
    )
    wrong_identity = _run_with(
        CutoverProbeName.WRITERS,
        replace(
            healthy,
            writers=(legacy, replace(primary, identity_id="unapproved-writer")),
        ),
    )
    missing_lease = _run_with(
        CutoverProbeName.WRITERS,
        replace(healthy, writers=(legacy, replace(primary, lease=None))),
    )
    expired_lease = _run_with(
        CutoverProbeName.WRITERS,
        replace(
            healthy,
            writers=(
                legacy,
                replace(
                    primary,
                    lease=replace(
                        primary.lease,
                        acquired_at=datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
                        expires_at=datetime(2026, 8, 14, 11, 0, tzinfo=UTC),
                    ),
                ),
            ),
        ),
    )

    assert CutoverFindingClass.WRITER_EVIDENCE_INCOMPLETE in shrunken.checks[6].findings
    assert CutoverFindingClass.WRITER_IDENTITY_UNAPPROVED in wrong_identity.checks[6].findings
    assert CutoverFindingClass.WRITER_LEASE_MISSING in missing_lease.checks[6].findings
    assert CutoverFindingClass.WRITER_LEASE_NOT_CURRENT in expired_lease.checks[6].findings


def _negative_cases() -> list[tuple[str, CutoverProbeName, CutoverEvidence, CutoverFindingClass]]:
    manifest_digest = _manifest().manifest_digest
    recovery = _recovery_evidence()
    assert recovery.backup is not None
    assert recovery.restore is not None
    writers = _writer_evidence()
    legacy, primary = writers.writers
    assert primary.lease is not None
    return [
        (
            "DOC-001-invalid-config",
            CutoverProbeName.CONFIG_SECRETS,
            ConfigEvidence(
                manifest_digest,
                config_valid=False,
                secrets=(SecretEvidence("provider-key", SecretState.PRESENT),),
            ),
            CutoverFindingClass.CONFIG_INVALID,
        ),
        (
            "DOC-001-missing-secret",
            CutoverProbeName.CONFIG_SECRETS,
            ConfigEvidence(
                manifest_digest,
                config_valid=True,
                secrets=(SecretEvidence("provider-key", SecretState.MISSING),),
            ),
            CutoverFindingClass.REQUIRED_SECRET_MISSING,
        ),
        (
            "DOC-001-empty-secret",
            CutoverProbeName.CONFIG_SECRETS,
            ConfigEvidence(
                manifest_digest,
                config_valid=True,
                secrets=(SecretEvidence("provider-key", SecretState.EMPTY),),
            ),
            CutoverFindingClass.REQUIRED_SECRET_EMPTY,
        ),
        (
            "DOC-002-missing-root",
            CutoverProbeName.ROOTS_REMOTES,
            _root_evidence(missing="work"),
            CutoverFindingClass.ROOT_MISSING,
        ),
        (
            "DOC-002-unsafe-permissions",
            CutoverProbeName.ROOTS_REMOTES,
            _root_evidence(unsafe="state"),
            CutoverFindingClass.ROOT_PERMISSIONS_UNSAFE,
        ),
        (
            "DOC-002-prohibited-remote",
            CutoverProbeName.ROOTS_REMOTES,
            _root_evidence(prohibited_remote_count=1),
            CutoverFindingClass.PROHIBITED_REMOTE,
        ),
        (
            "DOC-003-unreachable-dependency",
            CutoverProbeName.DEPENDENCIES,
            DependencyEvidence(
                manifest_digest,
                dependencies=(
                    DependencyReading(
                        "capture-service",
                        DependencyKind.SERVICE,
                        reachable=False,
                    ),
                    DependencyReading(
                        "local-provider",
                        DependencyKind.PROVIDER,
                        reachable=True,
                    ),
                ),
            ),
            CutoverFindingClass.DEPENDENCY_UNREACHABLE,
        ),
        (
            "DOC-003-incomplete-dependencies",
            CutoverProbeName.DEPENDENCIES,
            DependencyEvidence(manifest_digest, dependencies=()),
            CutoverFindingClass.DEPENDENCY_EVIDENCE_INCOMPLETE,
        ),
        (
            "DOC-004-stale-queue-schema",
            CutoverProbeName.SCHEMAS,
            SchemaEvidence(
                manifest_digest,
                schemas=(
                    SchemaReading("capture-queue", SchemaKind.QUEUE, 1),
                    SchemaReading("events", SchemaKind.DATABASE, 3),
                ),
            ),
            CutoverFindingClass.SCHEMA_NOT_CURRENT,
        ),
        (
            "DOC-004-stale-database-schema",
            CutoverProbeName.SCHEMAS,
            SchemaEvidence(
                manifest_digest,
                schemas=(
                    SchemaReading("capture-queue", SchemaKind.QUEUE, 2),
                    SchemaReading("events", SchemaKind.DATABASE, 2),
                ),
            ),
            CutoverFindingClass.SCHEMA_NOT_CURRENT,
        ),
        (
            "DOC-005-backup-destination",
            CutoverProbeName.RECOVERY,
            replace(recovery, backup_destination_available=False),
            CutoverFindingClass.BACKUP_DESTINATION_UNAVAILABLE,
        ),
        (
            "DOC-005-missing-backup",
            CutoverProbeName.RECOVERY,
            replace(recovery, backup=None),
            CutoverFindingClass.BACKUP_EVIDENCE_MISSING,
        ),
        (
            "DOC-005-missing-restore",
            CutoverProbeName.RECOVERY,
            replace(recovery, restore=None),
            CutoverFindingClass.RESTORE_EVIDENCE_MISSING,
        ),
        (
            "DOC-005-stale-backup",
            CutoverProbeName.RECOVERY,
            replace(
                recovery,
                backup=replace(
                    recovery.backup,
                    completed_at=datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
                ),
            ),
            CutoverFindingClass.BACKUP_EVIDENCE_STALE,
        ),
        (
            "DOC-005-stale-restore",
            CutoverProbeName.RECOVERY,
            replace(
                recovery,
                backup=replace(
                    recovery.backup,
                    completed_at=datetime(2026, 8, 14, 9, 50, tzinfo=UTC),
                ),
                restore=replace(
                    recovery.restore,
                    completed_at=datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
                ),
            ),
            CutoverFindingClass.RESTORE_EVIDENCE_STALE,
        ),
        (
            "DOC-005-failed-restore",
            CutoverProbeName.RECOVERY,
            replace(recovery, restore=replace(recovery.restore, succeeded=False)),
            CutoverFindingClass.RECOVERY_NOT_SUCCESSFUL,
        ),
        (
            "DOC-005-unpaired-restore",
            CutoverProbeName.RECOVERY,
            replace(
                recovery,
                restore=replace(recovery.restore, source_receipt_id="other-backup"),
            ),
            CutoverFindingClass.RECOVERY_RECEIPT_MISMATCH,
        ),
        (
            "DOC-006-unsafe-bind",
            CutoverProbeName.NETWORK_BINDS,
            BindEvidence(
                manifest_digest,
                binds=(BindReading("capture-service", BindExposure.WILDCARD),),
            ),
            CutoverFindingClass.UNSAFE_NETWORK_BIND,
        ),
        (
            "DOC-006-incomplete-binds",
            CutoverProbeName.NETWORK_BINDS,
            BindEvidence(manifest_digest, binds=()),
            CutoverFindingClass.BIND_EVIDENCE_INCOMPLETE,
        ),
        (
            "DOC-007-writer-collision",
            CutoverProbeName.WRITERS,
            replace(writers, writers=(replace(legacy, active=True), primary)),
            CutoverFindingClass.WRITER_COLLISION,
        ),
        (
            "DOC-007-wrong-role",
            CutoverProbeName.WRITERS,
            replace(
                writers,
                writers=(legacy, replace(primary, role=WriterRole.LEGACY)),
            ),
            CutoverFindingClass.WRITER_ROLE_UNAPPROVED,
        ),
        (
            "DOC-007-wrong-lease-owner",
            CutoverProbeName.WRITERS,
            replace(
                writers,
                writers=(
                    legacy,
                    replace(
                        primary,
                        lease=replace(
                            primary.lease,
                            owner_identity_id="unapproved-writer",
                        ),
                    ),
                ),
            ),
            CutoverFindingClass.WRITER_LEASE_NOT_CURRENT,
        ),
        (
            "DOC-008-incomplete-rows",
            CutoverProbeName.GATES_SCOPE,
            GateEvidence(
                manifest_digest,
                validated_rows=tuple(AcceptanceRow)[:-1],
                unresolved_owner_gate_ids=_manifest().expected_owner_gate_ids,
            ),
            CutoverFindingClass.VALIDATED_ROWS_INCOMPLETE,
        ),
        (
            "DOC-008-shrunken-owner-gates",
            CutoverProbeName.GATES_SCOPE,
            GateEvidence(
                manifest_digest,
                validated_rows=tuple(AcceptanceRow),
                unresolved_owner_gate_ids=("cutover-approval",),
            ),
            CutoverFindingClass.OWNER_GATE_INVENTORY_INCOMPLETE,
        ),
    ]


@pytest.mark.parametrize(
    ("probe_name", "evidence", "expected_finding"),
    [(probe, evidence, finding) for _, probe, evidence, finding in _negative_cases()],
    ids=[case_id for case_id, *_ in _negative_cases()],
)
def test_doc_001_through_008_negative_evidence_prevents_readiness(
    probe_name: CutoverProbeName,
    evidence: CutoverEvidence,
    expected_finding: CutoverFindingClass,
) -> None:
    result = _run_with(probe_name, evidence)
    check = result.checks[list(CutoverProbeName).index(probe_name)]

    assert result.outcome is CutoverDoctorOutcome.NOT_READY
    assert result.exit_code == 1
    assert result.synthetic_ready is False
    assert result.cutover_ready is False
    assert check.state is CutoverCheckState.UNHEALTHY
    assert expected_finding in check.findings


def test_missing_timeout_failure_and_wrong_evidence_fail_closed_and_redacted() -> None:
    canary = "secret=fixture path=/synthetic/private url=https://private.invalid"

    async def timed_out() -> CutoverEvidence:
        raise TimeoutError(canary)

    async def failed() -> CutoverEvidence:
        raise RuntimeError(canary)

    strict_missing = _run({})
    informational_missing = _run({}, strict=False)
    timed_out_probes = _healthy_probes()
    timed_out_probes[CutoverProbeName.CONFIG_SECRETS] = timed_out
    failed_probes = _healthy_probes()
    failed_probes[CutoverProbeName.CONFIG_SECRETS] = failed
    timed_out_result = _run(timed_out_probes)
    failed_result = _run(failed_probes)
    wrong_type_result = _run_with(CutoverProbeName.CONFIG_SECRETS, _root_evidence())

    assert strict_missing.outcome is CutoverDoctorOutcome.UNAVAILABLE
    assert strict_missing.exit_code == 78
    assert strict_missing.cutover_ready is False
    assert all(
        check.findings == (CutoverFindingClass.PROBE_MISSING,) for check in strict_missing.checks
    )
    assert informational_missing.exit_code == 0
    assert informational_missing.cutover_ready is False
    assert timed_out_result.checks[0].findings == (CutoverFindingClass.PROBE_TIMEOUT,)
    assert failed_result.checks[0].findings == (CutoverFindingClass.PROBE_FAILURE,)
    assert wrong_type_result.checks[0].findings == (CutoverFindingClass.PROBE_FAILURE,)
    assert canary not in json.dumps(timed_out_result.to_dict())
    assert canary not in json.dumps(failed_result.to_dict())


def test_contradictory_and_forged_evidence_cannot_false_green() -> None:
    recovery = _recovery_evidence()
    assert recovery.backup is not None
    assert recovery.restore is not None
    future_recovery = _run_with(
        CutoverProbeName.RECOVERY,
        replace(
            recovery,
            backup=replace(
                recovery.backup,
                completed_at=datetime(2026, 8, 14, 12, 1, tzinfo=UTC),
            ),
            restore=replace(
                recovery.restore,
                completed_at=datetime(2026, 8, 14, 12, 2, tzinfo=UTC),
            ),
        ),
    )
    forged_writer = object.__new__(WriterEvidence)
    object.__setattr__(forged_writer, "manifest_digest", _manifest().manifest_digest)
    object.__setattr__(forged_writer, "writers", (object(),))
    forged_result = _run_with(CutoverProbeName.WRITERS, forged_writer)

    assert future_recovery.cutover_ready is False
    assert CutoverFindingClass.RECOVERY_EVIDENCE_CONTRADICTORY in (
        future_recovery.checks[4].findings
    )
    assert forged_result.outcome is CutoverDoctorOutcome.UNAVAILABLE
    assert forged_result.exit_code == 78
    assert forged_result.cutover_ready is False
    assert forged_result.checks[6].findings == (CutoverFindingClass.PROBE_FAILURE,)


def test_cutover_doctor_does_not_discover_ambient_or_service_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_access(*args: object, **kwargs: object) -> None:
        raise AssertionError("cutover doctor attempted live discovery")

    monkeypatch.setattr(os, "getenv", unexpected_access)
    monkeypatch.setattr(socket, "create_connection", unexpected_access)
    monkeypatch.setattr(subprocess, "run", unexpected_access)

    result = _run(_healthy_probes())

    assert "open" not in cutover_doctor_module.__dict__
    assert result.synthetic_ready is True
    assert result.cutover_ready is False


def test_cutover_doctor_rejects_unbounded_timeouts_and_ambient_time() -> None:
    for timeout_seconds in (0, -1, 30.1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError, match="probe timeout"):
            _run(_healthy_probes(), timeout_seconds=timeout_seconds)

    with pytest.raises(ValueError, match="evaluation timestamp"):
        _run(
            _healthy_probes(),
            evaluated_at=datetime(2026, 8, 14, 12, 0),
        )


def test_every_healthy_evidence_item_uses_one_manifest_digest() -> None:
    manifest_digest = _manifest().manifest_digest
    evidence = _healthy_evidence()

    assert len(manifest_digest) == 64
    assert {item.manifest_digest for item in evidence.values()} == {manifest_digest}
    recovery = evidence[CutoverProbeName.RECOVERY]
    writers = evidence[CutoverProbeName.WRITERS]
    assert isinstance(recovery, RecoveryEvidence)
    assert recovery.backup is not None
    assert recovery.restore is not None
    assert recovery.backup.manifest_digest == manifest_digest
    assert recovery.restore.manifest_digest == manifest_digest
    assert isinstance(writers, WriterEvidence)
    assert writers.writers[1].lease is not None
    assert writers.writers[1].lease.manifest_digest == manifest_digest
