from dataclasses import replace
from datetime import UTC, datetime, timedelta
from inspect import signature

import pytest
from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.catalog import JOB_CATALOG, JOBS_BY_ID, get_job
from open_brain_legacy.operations.models import (
    DeploymentTarget,
    ExitClass,
    HostRole,
    JobSpec,
    JobState,
    OutputPolicy,
    RetryPolicy,
    SchedulerPlatform,
    TriggerKind,
    TriggerSpec,
    WriterScope,
)
from open_brain_legacy.operations.runlog import (
    RunErrorClass,
    RunMetadata,
    RunOutcome,
    classify_exit_code,
)
from open_brain_legacy.operations.scheduler import JobCatalogValidationError, validate_job_catalog


def test_job_spec_requires_argv_tuple_and_stable_exit_classes() -> None:
    job = JobSpec(
        id="JOB-001",
        command=("open-brain", "doctor", "--json", "--role=probe"),
        deployment_target=DeploymentTarget.EDGE_OPERATOR,
        allowed_platforms=frozenset({SchedulerPlatform.LAUNCHD}),
        host_role=HostRole.PROBE,
        trigger=TriggerSpec(kind=TriggerKind.INTERVAL, interval_seconds=3600),
        writer_scope=WriterScope.NONE,
        lock_scope=LockScope.NONE,
        timeout_seconds=300,
        retry=RetryPolicy.NEVER,
        env_refs=(),
        output_policy=OutputPolicy.METADATA_ONLY,
        state=JobState.ENABLED,
    )

    assert job.command == ("open-brain", "doctor", "--json", "--role=probe")
    assert (ExitClass.SUCCESS, ExitClass.LOCK_HELD, ExitClass.CONFIGURATION) == (0, 75, 78)

    with pytest.raises(ValueError, match="argv tuple"):
        replace(job, command="open-brain doctor --json")  # type: ignore[arg-type]


def test_catalog_covers_job_001_through_job_030_exactly_once() -> None:
    expected_ids = tuple(f"JOB-{number:03d}" for number in range(1, 31))

    assert tuple(job.id for job in JOB_CATALOG) == expected_ids
    assert tuple(JOBS_BY_ID) == expected_ids
    assert len({job.id for job in JOB_CATALOG}) == 30
    assert tuple(get_job(job_id) for job_id in expected_ids) == JOB_CATALOG
    assert validate_job_catalog(JOB_CATALOG) == JOB_CATALOG

    with pytest.raises(JobCatalogValidationError, match="exactly once"):
        validate_job_catalog((*JOB_CATALOG[:-1], JOB_CATALOG[0]))


def test_catalog_has_exact_abstract_target_and_platform_mapping() -> None:
    edge = DeploymentTarget.EDGE_OPERATOR
    canonical = DeploymentTarget.CANONICAL_WRITER
    ingress = DeploymentTarget.INGRESS_NODE
    launchd = frozenset({SchedulerPlatform.LAUNCHD})
    systemd = frozenset({SchedulerPlatform.SYSTEMD})
    expected = (
        *((f"JOB-{number:03d}", edge, launchd) for number in range(1, 10)),
        *((f"JOB-{number:03d}", canonical, launchd) for number in range(10, 28)),
        *((f"JOB-{number:03d}", ingress, systemd) for number in range(28, 31)),
    )

    assert tuple(
        (job.id, job.deployment_target, job.allowed_platforms) for job in JOB_CATALOG
    ) == expected
    assert {
        job.deployment_target for job in JOB_CATALOG if job.host_role is HostRole.WRITER
    } == {DeploymentTarget.CANONICAL_WRITER}

    with pytest.raises(JobCatalogValidationError, match="deployment mapping"):
        validate_job_catalog(
            (
                replace(
                    JOB_CATALOG[0],
                    deployment_target=DeploymentTarget.CANONICAL_WRITER,
                ),
                *JOB_CATALOG[1:],
            )
        )


def test_catalog_uses_direct_public_cli_argv_and_generic_values_only() -> None:
    forbidden_executables = {"bash", "sh", "zsh", "env", "flock"}
    serialized = repr(JOB_CATALOG).lower()

    for job in JOB_CATALOG:
        assert isinstance(job.command, tuple)
        assert job.command[0] == "open-brain"
        assert forbidden_executables.isdisjoint(job.command)
        assert all("\n" not in argument and "\x00" not in argument for argument in job.command)

    for private_marker in (
        "/users/",
        "/home/",
        "http://",
        "https://",
        "laptop",
        "macmini",
        "mac mini",
        "open brain contributors",
    ):
        assert private_marker not in serialized


def test_catalog_applies_production_enabled_and_manual_states() -> None:
    manual = {"JOB-006", "JOB-009", "JOB-024"}

    assert {job.id for job in JOB_CATALOG if job.state is JobState.DISABLED} == set()
    assert {job.id for job in JOB_CATALOG if job.state is JobState.MANUAL} == manual
    assert {job.id for job in JOB_CATALOG if job.state is JobState.ENABLED} == set(
        JOBS_BY_ID
    ) - manual
    assert all("--dry-run" in get_job(job_id).command for job_id in manual)
    assert all(
        job.state is JobState.ENABLED for job in JOB_CATALOG if job.host_role is HostRole.SERVICE
    )


def test_catalog_preserves_synthetic_writer_and_ingress_ownership() -> None:
    assert get_job("JOB-002").host_role is HostRole.PROBE
    assert get_job("JOB-003").host_role is HostRole.PROBE
    assert get_job("JOB-030").host_role is HostRole.PROBE
    assert get_job("JOB-004").state is JobState.ENABLED
    assert get_job("JOB-004").host_role is HostRole.PROBE
    assert get_job("JOB-004").writer_scope is WriterScope.NONE
    assert get_job("JOB-004").lock_scope is LockScope.NONE

    for job_id in ("JOB-005", "JOB-007", "JOB-027", "JOB-028", "JOB-029"):
        job = get_job(job_id)
        assert job.host_role is HostRole.INGRESS
        assert job.writer_scope is WriterScope.CAPTURE_INGRESS
        assert job.lock_scope is LockScope.INGRESS

    social_ledger = get_job("JOB-010")
    assert social_ledger.host_role is HostRole.WRITER
    assert social_ledger.writer_scope is WriterScope.CONTENT
    assert social_ledger.lock_scope is LockScope.SHARED_WRITER
    assert social_ledger.state is JobState.ENABLED

    youtube = get_job("JOB-029")
    assert "--mode=ingress" in youtube.command
    assert "--write-notes" not in youtube.command


def test_catalog_has_one_enabled_canonical_index_and_now_writer() -> None:
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
    assert get_job("JOB-016").lock_scope in {LockScope.INDEX, LockScope.SHARED_WRITER}
    assert get_job("JOB-022").lock_scope is LockScope.SHARED_WRITER


def test_catalog_backup_and_content_writers_use_their_owned_locks() -> None:
    for job in JOB_CATALOG:
        if job.writer_scope is WriterScope.BACKUP:
            assert job.host_role is HostRole.WRITER
            assert job.lock_scope is LockScope.BACKUP_PROFILE
        elif job.writer_scope in {WriterScope.CONTENT, WriterScope.STATE}:
            assert job.host_role is HostRole.WRITER
            assert job.lock_scope is LockScope.SHARED_WRITER

    assert {
        job.id
        for job in JOB_CATALOG
        if job.writer_scope is WriterScope.BACKUP and job.state is JobState.ENABLED
    } == {"JOB-011", "JOB-014", "JOB-023", "JOB-025"}


def test_job_spec_rejects_writer_authority_on_probe_and_ingress_roles() -> None:
    with pytest.raises(ValueError, match="non-writer role"):
        replace(
            get_job("JOB-001"),
            writer_scope=WriterScope.CONTENT,
            lock_scope=LockScope.SHARED_WRITER,
        )
    with pytest.raises(ValueError, match="append-only"):
        replace(
            get_job("JOB-005"),
            writer_scope=WriterScope.STATE,
            lock_scope=LockScope.SHARED_WRITER,
        )


@pytest.mark.parametrize(
    ("exit_code", "outcome"),
    [
        (0, RunOutcome.SUCCEEDED),
        (75, RunOutcome.SKIPPED_LOCKED),
        (78, RunOutcome.CONFIGURATION_FAILED),
        (23, RunOutcome.FAILED),
    ],
)
def test_run_metadata_classifies_stable_and_job_specific_exits(
    exit_code: int, outcome: RunOutcome
) -> None:
    assert classify_exit_code(exit_code) is outcome


@pytest.mark.parametrize(
    ("exit_code", "expected_error_class"),
    [
        (0, None),
        (75, RunErrorClass.LOCK_HELD),
        (78, RunErrorClass.CONFIGURATION),
        (23, RunErrorClass.JOB_FAILURE),
    ],
)
def test_run_metadata_requires_exact_error_class_for_every_outcome(
    exit_code: int,
    expected_error_class: RunErrorClass | None,
) -> None:
    started_at = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    metadata = RunMetadata.create(
        job_id="JOB-001",
        started_at=started_at,
        finished_at=started_at,
        exit_code=exit_code,
        error_class=expected_error_class,
        metrics={},
    )

    assert metadata.to_dict()["error_class"] == (
        expected_error_class.value if expected_error_class is not None else None
    )

    invalid_error_classes = {None, *RunErrorClass} - {expected_error_class}
    for invalid_error_class in invalid_error_classes:
        with pytest.raises(ValueError, match="error class for run outcome"):
            RunMetadata.create(
                job_id="JOB-001",
                started_at=started_at,
                finished_at=started_at,
                exit_code=exit_code,
                error_class=invalid_error_class,
                metrics={},
            )


def test_run_metadata_is_bounded_deterministic_and_redacted() -> None:
    started_at = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    metadata = RunMetadata.create(
        job_id="JOB-012",
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=1250),
        exit_code=23,
        error_class=RunErrorClass.JOB_FAILURE,
        metrics={"items_considered": 4, "items_written": 0},
    )

    assert metadata.to_dict() == {
        "schema_version": 1,
        "job_id": "JOB-012",
        "started_at": "2026-08-14T12:00:00Z",
        "finished_at": "2026-08-14T12:00:01.250000Z",
        "duration_ms": 1250,
        "exit_code": 23,
        "outcome": "failed",
        "error_class": "job-failure",
        "metrics": {"items_considered": 4, "items_written": 0},
    }
    assert {parameter for parameter in signature(RunMetadata.create).parameters} == {
        "job_id",
        "started_at",
        "finished_at",
        "exit_code",
        "error_class",
        "metrics",
    }

    for invalid_error in (
        "job-failure",
        "synthetic_failure",
        "/private/synthetic/path",
        "https://example.invalid/private",
        "token=synthetic-secret",
        "two\nlines",
    ):
        with pytest.raises(ValueError, match="error class"):
            RunMetadata.create(
                job_id="JOB-012",
                started_at=started_at,
                finished_at=started_at,
                exit_code=1,
                error_class=invalid_error,  # type: ignore[arg-type]
                metrics={},
            )


def test_run_metadata_constructor_cannot_bypass_derived_and_redacted_fields() -> None:
    started_at = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

    with pytest.raises(TypeError, match="factory"):
        RunMetadata()

    with pytest.raises(TypeError):
        RunMetadata(  # type: ignore[call-arg]
            schema_version=1,
            job_id="JOB-999",
            started_at=started_at,
            finished_at=started_at,
            duration_ms=-1,
            exit_code=0,
            outcome="succeeded",
            error_class="token=synthetic-secret /private/synthetic/path",
            metrics=(("token", 1),),
        )


def test_run_metadata_uses_bounded_per_job_metric_allow_lists() -> None:
    started_at = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="not allowed for job"):
        RunMetadata.create(
            job_id="JOB-001",
            started_at=started_at,
            finished_at=started_at,
            exit_code=0,
            error_class=None,
            metrics={"items_written": 1},
        )

    with pytest.raises(ValueError, match="too many run metrics"):
        RunMetadata.create(
            job_id="JOB-012",
            started_at=started_at,
            finished_at=started_at,
            exit_code=0,
            error_class=None,
            metrics={f"metric_{number}": number for number in range(9)},
        )

    for invalid_metrics in (
        {"items_written": -1},
        {"items_written": True},
        {"items_written": float("inf")},
        {"token": 1},
        {"/private/synthetic/path": 1},
    ):
        with pytest.raises(ValueError, match="run metrics"):
            RunMetadata.create(
                job_id="JOB-012",
                started_at=started_at,
                finished_at=started_at,
                exit_code=0,
                error_class=None,
                metrics=invalid_metrics,
            )
