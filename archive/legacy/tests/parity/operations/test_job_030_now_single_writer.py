from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.models import DeploymentTarget, HostRole, JobState
from open_brain_legacy.operations.now import (
    NowOwnershipError,
    NowProjectionInput,
    NowRootError,
    NowRoots,
    build_now,
    check_now,
)


class RecordingLease:
    def __init__(self) -> None:
        self.scopes: list[LockScope] = []

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        self.scopes.append(scope)
        yield


def test_job_030_is_enabled_read_only_and_uses_a_distinct_output_root(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    edge = tmp_path / "edge"
    ingress = tmp_path / "ingress"
    canonical.mkdir()
    edge.mkdir()
    ingress.mkdir()
    roots = NowRoots(
        canonical_output_root=canonical,
        edge_output_root=edge,
        ingress_output_root=ingress,
    )
    (ingress / "NOW.md").write_text(
        "# NOW\n\n<!-- open-brain-now-generation:now_invalid -->\n", encoding="utf-8"
    )
    before = (ingress / "NOW.md").read_bytes()
    job = get_job("JOB-030")
    lease = RecordingLease()

    assert job.state is JobState.ENABLED
    assert job.deployment_target is DeploymentTarget.INGRESS_NODE
    assert job.host_role is HostRole.PROBE

    check = check_now(target=job.deployment_target, roots=roots)
    assert check.available is True
    assert check.marker_valid is False
    assert (ingress / "NOW.md").read_bytes() == before

    with pytest.raises(NowOwnershipError, match="canonical writer"):
        build_now(
            target=job.deployment_target,
            host_role=job.host_role,
            roots=roots,
            lease=lease,
            projection=NowProjectionInput(focus=(), queue=(), life_os=None, messages=None),
        )
    assert lease.scopes == []
    assert not (canonical / "NOW.md").exists()


def test_now_roots_reject_shared_or_aliased_projection_roots(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    edge = tmp_path / "edge"
    canonical.mkdir()
    edge.mkdir()

    with pytest.raises(NowRootError, match="distinct"):
        NowRoots(
            canonical_output_root=canonical,
            edge_output_root=edge,
            ingress_output_root=canonical,
        )

    alias = tmp_path / "alias"
    alias.symlink_to(canonical, target_is_directory=True)
    with pytest.raises(NowRootError, match="safe directories"):
        NowRoots(
            canonical_output_root=canonical,
            edge_output_root=edge,
            ingress_output_root=alias,
        )


def test_now_builder_has_exactly_one_enabled_catalog_writer() -> None:
    candidates = (get_job("JOB-003"), get_job("JOB-022"), get_job("JOB-030"))
    enabled_writers = [
        job.id
        for job in candidates
        if job.state is JobState.ENABLED
        and job.deployment_target is DeploymentTarget.CANONICAL_WRITER
        and job.host_role is HostRole.WRITER
    ]

    assert enabled_writers == ["JOB-022"]
