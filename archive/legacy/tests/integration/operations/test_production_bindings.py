from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

import pytest
from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.catalog import JOB_CATALOG
from open_brain_legacy.operations.models import HostRole
from open_brain_legacy.operations.production_bindings import (
    ApplicationFamily,
    BindingAuthority,
    ProductionBindingError,
    ProductionBindingInventory,
    ProductionJobBinding,
    ScheduledDispatchResult,
    ScheduledDispatchStatus,
    ScheduledInvocation,
    compose_production_bindings,
    dispatch_production_job,
)
from open_brain_legacy.operations.scheduler import EXPECTED_JOB_IDS


@dataclass(slots=True)
class RecordingCapability:
    authority: BindingAuthority
    lock_scope: LockScope
    writer_identity: str | None
    result: ScheduledDispatchResult | None = None
    raises: bool = False
    calls: list[ScheduledInvocation] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def dispatch(self, invocation: ScheduledInvocation) -> ScheduledDispatchResult:
        self.calls.append(invocation)
        if self.raises:
            raise RuntimeError("synthetic-secret-value at /synthetic/private/production.log")
        return self.result or ScheduledDispatchResult.completed(invocation.job.id)


def _authority(job_role: HostRole) -> BindingAuthority:
    if job_role is HostRole.WRITER:
        return BindingAuthority.CANONICAL_WRITER
    if job_role is HostRole.INGRESS:
        return BindingAuthority.APPEND_ONLY_INGRESS
    return BindingAuthority.NONE


def _capabilities() -> dict[str, RecordingCapability]:
    return {
        job.id: RecordingCapability(
            authority=_authority(job.host_role),
            lock_scope=job.lock_scope,
            writer_identity="synthetic-canonical-writer"
            if job.host_role is HostRole.WRITER
            else None,
        )
        for job in JOB_CATALOG
    }


def test_scheduled_result_records_are_owned_by_operations() -> None:
    assert ScheduledDispatchResult.__module__ == "open_brain_legacy.operations.scheduled_results"
    assert ScheduledDispatchStatus.__module__ == "open_brain_legacy.operations.scheduled_results"


def test_complete_synthetic_inventory_binds_and_dispatches_all_catalog_rows() -> None:
    capabilities = _capabilities()
    inventory = compose_production_bindings(capabilities)

    results = tuple(
        dispatch_production_job(
            inventory,
            job_id=job_id,
            replay_key=f"synthetic-{index:03d}",
        )
        for index, job_id in enumerate(EXPECTED_JOB_IDS, start=1)
    )

    assert tuple(binding.job_id for binding in inventory.bindings) == EXPECTED_JOB_IDS
    assert len(inventory.bindings) == 30
    assert {binding.family for binding in inventory.bindings} >= {
        ApplicationFamily.BACKUP,
        ApplicationFamily.CAPTURE,
        ApplicationFamily.CURATION,
        ApplicationFamily.GIT_SYNC,
        ApplicationFamily.INDEX,
        ApplicationFamily.LIFEOS,
        ApplicationFamily.MESSAGING,
        ApplicationFamily.NOW,
        ApplicationFamily.RETENTION,
    }
    assert all(result.status is ScheduledDispatchStatus.COMPLETED for result in results)
    assert all(result.status is not ScheduledDispatchStatus.UNAVAILABLE for result in results)
    assert all(len(capability.calls) == 1 for capability in capabilities.values())


def test_replay_key_is_preserved_for_each_idempotent_capability_dispatch() -> None:
    capabilities = _capabilities()
    inventory = compose_production_bindings(capabilities)

    first = dispatch_production_job(
        inventory, job_id="JOB-022", replay_key="synthetic-now-replay"
    )
    replay = dispatch_production_job(
        inventory, job_id="JOB-022", replay_key="synthetic-now-replay"
    )

    assert first.status is ScheduledDispatchStatus.COMPLETED
    assert replay.status is ScheduledDispatchStatus.COMPLETED
    assert [call.replay_key for call in capabilities["JOB-022"].calls] == [
        "synthetic-now-replay",
        "synthetic-now-replay",
    ]


def _wrong_family(binding: ProductionJobBinding) -> ProductionJobBinding:
    return replace(binding, family=ApplicationFamily.BACKUP)


def _wrong_authority(binding: ProductionJobBinding) -> ProductionJobBinding:
    return replace(binding, authority=BindingAuthority.NONE)


def _wrong_lock_scope(binding: ProductionJobBinding) -> ProductionJobBinding:
    return replace(binding, lock_scope=LockScope.INDEX)


@pytest.mark.parametrize(
    "mutate", [_wrong_family, _wrong_authority, _wrong_lock_scope]
)
def test_inventory_rejects_mismatched_job_bindings(
    mutate: Callable[[ProductionJobBinding], ProductionJobBinding],
) -> None:
    inventory = compose_production_bindings(_capabilities())
    binding = inventory.binding_for("JOB-022")
    changed = tuple(
        mutate(current)
        if current.job_id == binding.job_id
        else current
        for current in inventory.bindings
    )

    with pytest.raises(ProductionBindingError, match="catalog authority"):
        ProductionBindingInventory(changed)


def test_inventory_rejects_missing_duplicate_wrong_authority_and_multiple_writers() -> None:
    inventory = compose_production_bindings(_capabilities())
    missing = inventory.bindings[:-1]
    duplicate = (*inventory.bindings[:-1], inventory.bindings[0])
    wrong_capability = replace(
        inventory.binding_for("JOB-022"),
        capability=RecordingCapability(
            BindingAuthority.NONE,
            LockScope.SHARED_WRITER,
            "synthetic-canonical-writer",
        ),
    )
    capability = inventory.binding_for("JOB-022").capability
    assert isinstance(capability, RecordingCapability)
    multiple_writers = tuple(
        replace(binding, capability=replace_capability_identity(capability, "other-writer"))
        if binding.job_id == "JOB-022"
        else binding
        for binding in inventory.bindings
    )

    invalid_inventories = (
        missing,
        duplicate,
        (wrong_capability, *inventory.bindings[1:]),
        multiple_writers,
    )
    for bindings in invalid_inventories:
        with pytest.raises(ProductionBindingError):
            ProductionBindingInventory(bindings)


def replace_capability_identity(
    capability: RecordingCapability, identity: str
) -> RecordingCapability:
    return RecordingCapability(capability.authority, capability.lock_scope, identity)


def test_dispatch_normalizes_lock_authority_and_capability_failures_without_error_text() -> None:
    capabilities = _capabilities()
    capabilities["JOB-022"].result = ScheduledDispatchResult.lock_held("JOB-022")
    capabilities["JOB-005"].raises = True
    capabilities["JOB-001"].result = ScheduledDispatchResult.unavailable("JOB-001")
    inventory = compose_production_bindings(capabilities)

    locked = dispatch_production_job(inventory, job_id="JOB-022", replay_key="synthetic-lock")
    failed = dispatch_production_job(inventory, job_id="JOB-005", replay_key="synthetic-failure")
    unavailable = dispatch_production_job(
        inventory, job_id="JOB-001", replay_key="synthetic-unavailable"
    )

    assert locked.exit_code == 75
    assert locked.status is ScheduledDispatchStatus.FAILED
    assert failed.status is ScheduledDispatchStatus.FAILED
    assert unavailable.status is ScheduledDispatchStatus.FAILED
    serialized = repr(failed)
    assert "synthetic-secret-value" not in serialized
    assert "/synthetic/private/production.log" not in serialized


def test_dispatch_rejects_invalid_replay_key_as_a_closed_configuration_failure() -> None:
    result = dispatch_production_job(
        compose_production_bindings(_capabilities()),
        job_id="JOB-022",
        replay_key="not valid",
    )

    assert result.status is ScheduledDispatchStatus.FAILED
    assert result.exit_code == 78
