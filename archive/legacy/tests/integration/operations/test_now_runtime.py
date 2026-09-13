from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import LockScope

import open_brain_legacy.operations.now_runtime as now_runtime
from open_brain_legacy.operations.models import DeploymentTarget
from open_brain_legacy.operations.now import NowItem, NowProjectionInput, NowRoots, check_now
from open_brain_legacy.operations.now_runtime import (
    NowEffectCapability,
    NowRuntimeApplication,
    SharedWriterAuthority,
)
from open_brain_legacy.operations.replay_journal import SqliteReplayJournal
from open_brain_legacy.operations.writer_jobs import (
    JobRunDisposition,
    WriterJobError,
    run_writer_job,
)
from tests.unit.storage._factories import FixedClock


class RecordingLease:
    def __init__(self) -> None:
        self.scopes: list[LockScope] = []

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        self.scopes.append(scope)
        yield


def _roots(tmp_path: Path) -> NowRoots:
    tmp_path.mkdir(parents=True, exist_ok=True)
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


def _projection() -> NowProjectionInput:
    return NowProjectionInput(
        focus=(
            _item("Fix index", "work/focus-a", 1, PrivacyTier.WORK),
            _item("Private appointment", "personal/calendar", 2, PrivacyTier.PERSONAL),
        ),
        queue=(
            _item("Review parity", "work/queue", 3, PrivacyTier.WORK),
            _item("Secret reminder", "secret/note", 1, PrivacyTier.SECRET),
        ),
        life_os=(_item("Plan launch", "work/plan", 2, PrivacyTier.WORK),),
        messages=None,
    )


def test_now_runtime_writes_reserves_and_replays(tmp_path: Path) -> None:
    roots = _roots(tmp_path / "outputs")
    projection = _projection()
    capability = NowEffectCapability(
        root=tmp_path,
        projection=projection,
        roots=roots,
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )
    app = NowRuntimeApplication(projection)
    lease = RecordingLease()

    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        first = run_writer_job(
            job_id="JOB-022",
            root=tmp_path,
            replay_key="now-2026-08-17",
            journal=journal,
            application=app,
            effect_capability=capability,
            lease=lease,
        )
        replay = run_writer_job(
            job_id="JOB-022",
            root=tmp_path,
            replay_key="now-2026-08-17",
            journal=journal,
            application=app,
            effect_capability=capability,
            lease=lease,
        )

    reservation, pointer = now_runtime._paths("JOB-022", "now-2026-08-17")
    payload = (roots.canonical_output_root / "NOW.md").read_text(encoding="utf-8")
    check = check_now(target=DeploymentTarget.CANONICAL_WRITER, roots=roots)

    assert first.disposition is JobRunDisposition.APPLIED
    assert first.effect_count == 1
    assert replay.disposition is JobRunDisposition.REPLAYED
    assert (tmp_path / reservation).exists()
    assert (tmp_path / pointer).exists()
    assert check.available is True
    assert check.marker_valid is True
    assert "Private appointment" not in payload
    assert "Secret reminder" not in payload
    assert "Fix index" in payload
    assert "Review parity" in payload
    assert "Plan launch" in payload
    assert lease.scopes == [LockScope.SHARED_WRITER, LockScope.SHARED_WRITER]


def test_now_runtime_recovers_reserved_projection_after_pointer_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _roots(tmp_path / "outputs")
    projection = _projection()
    capability = NowEffectCapability(
        root=tmp_path,
        projection=projection,
        roots=roots,
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )
    app = NowRuntimeApplication(projection)
    original = now_runtime._write_applied_pointer
    armed = {"value": True}

    def fail_once(root: Path, relative: Any, effect_digest_sha256: str) -> None:
        if armed["value"]:
            armed["value"] = False
            raise RuntimeError("synthetic pointer interruption")
        original(root, relative, effect_digest_sha256)

    monkeypatch.setattr(now_runtime, "_write_applied_pointer", fail_once)

    with SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal:
        with pytest.raises(RuntimeError, match="synthetic pointer interruption"):
            run_writer_job(
                job_id="JOB-022",
                root=tmp_path,
                replay_key="now-crash-retry",
                journal=journal,
                application=app,
                effect_capability=capability,
                lease=RecordingLease(),
            )
        recovered = run_writer_job(
            job_id="JOB-022",
            root=tmp_path,
            replay_key="now-crash-retry",
            journal=journal,
            application=app,
            effect_capability=capability,
            lease=RecordingLease(),
        )

    assert recovered.disposition is JobRunDisposition.REPLAYED
    assert (
        check_now(target=DeploymentTarget.CANONICAL_WRITER, roots=roots).marker_valid is True
    )


def test_now_runtime_refuses_corrupt_reservation(tmp_path: Path) -> None:
    roots = _roots(tmp_path / "outputs")
    projection = _projection()
    capability = NowEffectCapability(
        root=tmp_path,
        projection=projection,
        roots=roots,
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )
    reservation, _pointer = now_runtime._paths("JOB-022", "now-corrupt")
    reservation_path = tmp_path / reservation
    reservation_path.parent.mkdir(parents=True, exist_ok=True)
    reservation_path.write_text('{"version":1,"broken":true}', encoding="utf-8")

    with (
        SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal,
        pytest.raises(WriterJobError, match="invalid NOW effect reservation"),
    ):
        run_writer_job(
            job_id="JOB-022",
            root=tmp_path,
            replay_key="now-corrupt",
            journal=journal,
            application=NowRuntimeApplication(projection),
            effect_capability=capability,
            lease=RecordingLease(),
        )


def test_now_runtime_refuses_symlinked_pointer_path(tmp_path: Path) -> None:
    roots = _roots(tmp_path / "outputs")
    projection = NowProjectionInput(focus=(), queue=(), life_os=(), messages=())
    capability = NowEffectCapability(
        root=tmp_path,
        projection=projection,
        roots=roots,
        authority=SharedWriterAuthority(LockScope.SHARED_WRITER),
    )
    _reservation, pointer = now_runtime._paths("JOB-022", "now-symlink")
    pointer_path = tmp_path / pointer
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.symlink_to(tmp_path / "elsewhere")

    with (
        SqliteReplayJournal(root=tmp_path, clock=FixedClock()) as journal,
        pytest.raises(WriterJobError, match="NOW applied pointer conflict"),
    ):
        run_writer_job(
            job_id="JOB-022",
            root=tmp_path,
            replay_key="now-symlink",
            journal=journal,
            application=NowRuntimeApplication(projection),
            effect_capability=capability,
            lease=RecordingLease(),
        )
