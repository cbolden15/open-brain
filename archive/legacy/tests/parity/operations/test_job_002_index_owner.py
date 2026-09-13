from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.index import (
    IndexOwnershipError,
    IndexRoots,
    build_index,
    check_index,
)
from open_brain_legacy.operations.models import (
    DeploymentTarget,
    HostRole,
    JobState,
    WriterScope,
)


class RecordingLease:
    def __init__(self) -> None:
        self.scopes: list[LockScope] = []

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        self.scopes.append(scope)
        yield


class LocalEmbedder:
    model_id = "synthetic-local-v1"
    requires_cloud_authority = False
    requires_external_egress = False

    def embed(self, text: str) -> tuple[float, ...]:
        return (float(len(text)),)


def _roots(tmp_path: Path) -> IndexRoots:
    pages = tmp_path / "pages"
    captures = tmp_path / "captures"
    output = tmp_path / "index-output"
    pages.mkdir()
    captures.mkdir()
    output.mkdir()
    return IndexRoots(pages_root=pages, captures_root=captures, output_root=output)


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PERSONAL,
        reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def test_job_002_is_enabled_probe_only_and_cannot_rebuild(tmp_path: Path) -> None:
    job = get_job("JOB-002")
    writer = get_job("JOB-016")
    roots = _roots(tmp_path)
    lease = RecordingLease()

    assert job.state is JobState.ENABLED
    assert job.deployment_target is DeploymentTarget.EDGE_OPERATOR
    assert job.host_role is HostRole.PROBE
    assert job.writer_scope is WriterScope.NONE
    assert job.lock_scope is LockScope.NONE
    assert job.command == ("open-brain", "index", "--check", "--read-only", "--json")

    with pytest.raises(IndexOwnershipError, match="canonical writer"):
        build_index(
            target=job.deployment_target,
            host_role=job.host_role,
            roots=roots,
            lease=lease,
            embedder=LocalEmbedder(),
            privacy=_privacy(),
        )

    built = build_index(
        target=writer.deployment_target,
        host_role=writer.host_role,
        roots=roots,
        lease=lease,
        embedder=LocalEmbedder(),
        privacy=_privacy(),
    )
    database = roots.output_root / roots.database_name
    before = database.read_bytes()
    check = check_index(target=job.deployment_target, roots=roots)
    after = database.read_bytes()

    assert check.available is True
    assert check.generation_id == built.generation_id
    assert check.document_count == 0
    assert check.chunk_count == 0
    assert lease.scopes == [LockScope.INDEX]
    assert after == before
