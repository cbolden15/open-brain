from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.now import (
    NowItem,
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


def _item(title: str, source_ref: str, priority: int, tier: PrivacyTier) -> NowItem:
    return NowItem(
        title=title,
        source_ref=source_ref,
        priority=priority,
        privacy_tier=tier,
    )


def test_job_022_writes_deterministic_work_only_now_with_generation_marker(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)
    projection = NowProjectionInput(
        focus=(
            _item("Ship docs", "work/focus-b", 2, PrivacyTier.WORK),
            _item("Private appointment", "personal/calendar", 1, PrivacyTier.PERSONAL),
            _item("Fix index", "work/focus-a", 1, PrivacyTier.WORK),
        ),
        queue=(
            _item("Secret reminder", "secret/note", 1, PrivacyTier.SECRET),
            _item("Review parity", "work/queue", 3, PrivacyTier.WORK),
        ),
        life_os=None,
        messages=None,
    )
    lease = RecordingLease()
    job = get_job("JOB-022")

    first = build_now(
        target=job.deployment_target,
        host_role=job.host_role,
        roots=roots,
        lease=lease,
        projection=projection,
    )
    payload = first.output_path.read_text(encoding="utf-8")
    body = (
        "# NOW\n\n"
        "Queue pressure: 1 work item.\n\n"
        "## Focus\n\n"
        "- [P1] Fix index (`work/focus-a`)\n"
        "- [P2] Ship docs (`work/focus-b`)\n\n"
        "## Queue\n\n"
        "- [P3] Review parity (`work/queue`)\n\n"
        "## LifeOS\n\n"
        "_Unavailable: optional input not configured._\n\n"
        "## Messages\n\n"
        "_Unavailable: optional input not configured._\n"
    )
    generation_id = "now_" + sha256(body.encode("utf-8")).hexdigest()
    expected = body.replace(
        "# NOW\n\n",
        f"# NOW\n\n<!-- open-brain-now-generation:{generation_id} -->\n\n",
        1,
    )

    assert payload == expected
    assert first.generation_id == generation_id
    assert first.work_item_count == 3
    assert first.filtered_item_count == 2
    assert lease.scopes == [LockScope.SHARED_WRITER]
    assert "Private appointment" not in payload
    assert "Secret reminder" not in payload

    second = build_now(
        target=job.deployment_target,
        host_role=job.host_role,
        roots=roots,
        lease=lease,
        projection=projection,
    )
    check = check_now(target=job.deployment_target, roots=roots)

    assert second.generation_id == first.generation_id
    assert second.output_path.read_bytes() == first.output_path.read_bytes()
    assert check.available is True
    assert check.generation_id == generation_id
    assert check.marker_valid is True
    assert not list(roots.canonical_output_root.glob(".NOW.md.*.tmp"))


def test_job_022_crash_preserves_previous_now_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _roots(tmp_path)
    lease = RecordingLease()
    job = get_job("JOB-022")
    stable_projection = NowProjectionInput(
        focus=(_item("Stable item", "work/stable", 1, PrivacyTier.WORK),),
        queue=(),
        life_os=(),
        messages=(),
    )
    stable = build_now(
        target=job.deployment_target,
        host_role=job.host_role,
        roots=roots,
        lease=lease,
        projection=stable_projection,
    )
    stable_bytes = stable.output_path.read_bytes()

    def crash_before_replace(source: Path, destination: Path) -> None:
        assert source.parent == destination.parent == roots.canonical_output_root
        assert source.exists()
        raise RuntimeError("synthetic replace crash")

    monkeypatch.setattr('open_brain_legacy.operations.now._replace_file', crash_before_replace)
    changed_projection = NowProjectionInput(
        focus=(_item("Changed item", "work/changed", 1, PrivacyTier.WORK),),
        queue=(),
        life_os=(),
        messages=(),
    )

    with pytest.raises(RuntimeError, match="synthetic replace crash"):
        build_now(
            target=job.deployment_target,
            host_role=job.host_role,
            roots=roots,
            lease=lease,
            projection=changed_projection,
        )

    assert stable.output_path.read_bytes() == stable_bytes
    assert not list(roots.canonical_output_root.glob(".NOW.md.*.tmp"))
