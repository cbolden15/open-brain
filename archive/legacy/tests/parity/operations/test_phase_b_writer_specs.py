from __future__ import annotations

import pytest
from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.models import DeploymentTarget, HostRole
from open_brain_legacy.operations.writer_jobs import (
    ReviewBoundary,
    ScheduledEffect,
    get_writer_job_spec,
)


@pytest.mark.parametrize(
    ("job_id", "effect", "lock_scope"),
    [
        ("JOB-011", ScheduledEffect.BACKUP_SNAPSHOT, LockScope.BACKUP_PROFILE),
        ("JOB-014", ScheduledEffect.BACKUP_SNAPSHOT, LockScope.BACKUP_PROFILE),
        ("JOB-016", ScheduledEffect.INDEX_REBUILD, LockScope.INDEX),
        ("JOB-022", ScheduledEffect.NOW_PROJECTION, LockScope.SHARED_WRITER),
        ("JOB-023", ScheduledEffect.BACKUP_SNAPSHOT, LockScope.BACKUP_PROFILE),
        ("JOB-025", ScheduledEffect.BACKUP_SNAPSHOT, LockScope.BACKUP_PROFILE),
    ],
)
def test_phase_b_writer_specs_bind_exact_catalog_authority(
    job_id: str,
    effect: ScheduledEffect,
    lock_scope: LockScope,
) -> None:
    job = get_job(job_id)
    spec = get_writer_job_spec(job_id)

    assert spec.command == job.command
    assert spec.deployment_target is DeploymentTarget.CANONICAL_WRITER
    assert spec.host_role is HostRole.WRITER
    assert spec.lock_scope is lock_scope
    assert spec.effect is effect
    assert spec.review_boundary is ReviewBoundary.NONE
    assert spec.local_only is True
    assert spec.dry_run is False
    assert spec.planned_actions == ()


def test_phase_b_effect_inventory_is_closed_and_stable() -> None:
    assert tuple(effect.value for effect in ScheduledEffect) == (
        "operator-artifact",
        "append-only-signals",
        "diagnostics",
        "hook-sync-plan",
        "ledger-write",
        "curation-promotion",
        "local-git-sync",
        "backup-snapshot",
        "index-rebuild",
        "now-projection",
    )
