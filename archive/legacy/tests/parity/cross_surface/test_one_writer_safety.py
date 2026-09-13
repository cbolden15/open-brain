from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.catalog import JOB_CATALOG, get_job
from open_brain_legacy.operations.models import (
    DeploymentTarget,
    HostRole,
    JobState,
    WriterScope,
)


def test_enabled_canonical_writes_are_confined_to_the_one_writer_target_and_lock() -> None:
    expected_locks = {
        WriterScope.BACKUP: {LockScope.BACKUP_PROFILE},
        WriterScope.CONTENT: {LockScope.SHARED_WRITER},
        WriterScope.INDEX: {LockScope.INDEX, LockScope.SHARED_WRITER},
        WriterScope.STATE: {LockScope.SHARED_WRITER},
    }

    for job in JOB_CATALOG:
        if job.state is not JobState.ENABLED or job.writer_scope not in expected_locks:
            continue
        assert job.deployment_target is DeploymentTarget.CANONICAL_WRITER
        assert job.host_role is HostRole.WRITER
        assert job.lock_scope in expected_locks[job.writer_scope]


def test_enabled_index_now_and_sqlite_probe_rows_cannot_run_as_writers() -> None:
    for job_id in ("JOB-002", "JOB-003", "JOB-004", "JOB-030"):
        job = get_job(job_id)
        assert job.state is JobState.ENABLED
        assert job.host_role is HostRole.PROBE
        assert job.writer_scope is WriterScope.NONE
        assert job.lock_scope is LockScope.NONE

    enabled_index_writers = [
        job.id
        for job in JOB_CATALOG
        if job.state is JobState.ENABLED and job.writer_scope is WriterScope.INDEX
    ]
    enabled_now_writers = [
        job.id
        for job in JOB_CATALOG
        if job.state is JobState.ENABLED
        and job.host_role is HostRole.WRITER
        and "now" in job.command
    ]
    assert enabled_index_writers == ["JOB-016"]
    assert enabled_now_writers == ["JOB-022"]


def test_noncanonical_enabled_writes_are_append_only_ingress() -> None:
    noncanonical = [
        job
        for job in JOB_CATALOG
        if job.state is JobState.ENABLED
        and job.deployment_target is not DeploymentTarget.CANONICAL_WRITER
        and job.writer_scope is not WriterScope.NONE
    ]

    assert {job.id for job in noncanonical} == {
        "JOB-005",
        "JOB-007",
        "JOB-028",
        "JOB-029",
    }
    assert all(job.host_role is HostRole.INGRESS for job in noncanonical)
    assert all(job.writer_scope is WriterScope.CAPTURE_INGRESS for job in noncanonical)
    assert all(job.lock_scope is LockScope.INGRESS for job in noncanonical)
