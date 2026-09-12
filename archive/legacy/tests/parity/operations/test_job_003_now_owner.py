from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.models import (
    DeploymentTarget,
    HostRole,
    JobState,
    WriterScope,
)
from open_brain_legacy.operations.now import (
    NowOwnershipError,
    NowProjectionInput,
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


def _roots(tmp_path: Path) -> NowRoots:
    canonical = tmp_path / "canonical"
    edge = tmp_path / "edge"
    ingress = tmp_path / "ingress"
    canonical.mkdir()
    edge.mkdir()
    ingress.mkdir()
    return NowRoots(
        canonical_output_root=canonical,
        edge_output_root=edge,
        ingress_output_root=ingress,
    )


def test_job_003_is_enabled_read_only_and_cannot_generate_now(tmp_path: Path) -> None:
    job = get_job("JOB-003")
    writer = get_job("JOB-022")
    roots = _roots(tmp_path)
    lease = RecordingLease()
    projection = NowProjectionInput(focus=(), queue=(), life_os=None, messages=None)

    assert job.state is JobState.ENABLED
    assert job.deployment_target is DeploymentTarget.EDGE_OPERATOR
    assert job.host_role is HostRole.PROBE
    assert job.writer_scope is WriterScope.NONE
    assert job.lock_scope is LockScope.NONE
    assert job.command == ("open-brain", "now", "check", "--read-only", "--json")

    with pytest.raises(NowOwnershipError, match="canonical writer"):
        build_now(
            target=job.deployment_target,
            host_role=job.host_role,
            roots=roots,
            lease=lease,
            projection=projection,
        )

    built = build_now(
        target=writer.deployment_target,
        host_role=writer.host_role,
        roots=roots,
        lease=lease,
        projection=projection,
    )
    edge_output = roots.edge_output_root / roots.output_name
    edge_output.write_bytes(built.output_path.read_bytes())
    before = edge_output.read_bytes()
    check = check_now(target=job.deployment_target, roots=roots)
    after = edge_output.read_bytes()

    assert check.available is True
    assert check.generation_id == built.generation_id
    assert check.marker_valid is True
    assert lease.scopes == [LockScope.SHARED_WRITER]
    assert after == before
