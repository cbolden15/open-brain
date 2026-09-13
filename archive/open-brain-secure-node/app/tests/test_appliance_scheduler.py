from __future__ import annotations

import inspect
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import LocalEngineContext, canonical_json_bytes

import open_brain.services.appliance_scheduler as scheduler_module
from open_brain.profile import compile_single_user_local
from open_brain.services.appliance_scheduler import (
    APPLIANCE_SCHEDULER_DIRECTORY,
    ApplianceJobResult,
    ApplianceRunContext,
    ApplianceRunReceipt,
    ApplianceScheduler,
    ApplianceSchedulerInterruptedError,
    ApplianceSchedulerRetryableError,
)

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _scheduler_profile(tmp_path: Path, name: str = "brain") -> LocalEngineContext:
    return compile_single_user_local(tmp_path / name)


def test_scheduler_inventory_is_exact_without_legacy_job_catalog_dependency(
    tmp_path: Path,
) -> None:
    profile = _scheduler_profile(tmp_path)
    scheduler = ApplianceScheduler(profile, now=_NOW)

    inventory = scheduler.inventory()

    assert [job.name for job in inventory] == [
        "engine-recover",
        "markdown-reconcile",
        "portable-export",
        "portable-import",
        "backup-create",
    ]
    assert [job.name for job in inventory if job.recurring] == [
        "engine-recover",
        "markdown-reconcile",
    ]
    source = inspect.getsource(scheduler_module)
    assert "open_brain.operations.scheduler" not in source
    assert "open_brain_legacy.operations.scheduler" not in source
    assert "JOB_CATALOG" not in source
    assert "JOB-001" not in source


def test_scheduler_defers_owner_requested_jobs_without_making_them_recurring(
    tmp_path: Path,
) -> None:
    profile = _scheduler_profile(tmp_path)
    scheduler = ApplianceScheduler(profile, now=_NOW)

    scheduler.request("portable-export")
    scheduler.request("portable-import")
    receipts = scheduler.run_due(now=_NOW)

    assert [(receipt.job_name, receipt.status) for receipt in receipts] == [
        ("engine-recover", "empty"),
        ("markdown-reconcile", "deferred"),
        ("portable-export", "deferred"),
        ("portable-import", "deferred"),
    ]
    state = scheduler.read_state()
    jobs = cast(dict[str, dict[str, object]], state["jobs"])
    assert jobs["portable-export"]["next_due_at"] is None
    assert jobs["portable-export"]["recurring"] is False
    assert jobs["portable-import"]["next_due_at"] is None
    assert jobs["portable-import"]["recurring"] is False


def test_scheduler_rejects_unallowlisted_connector_names_and_defaults_to_none(
    tmp_path: Path,
) -> None:
    profile = _scheduler_profile(tmp_path)

    assert all(
        not job.name.startswith("connector-run:")
        for job in ApplianceScheduler(profile, now=_NOW).inventory()
    )

    with pytest.raises(ValueError, match="connector allow-list"):
        ApplianceScheduler(profile, connector_names=("bad:name",), now=_NOW)

    with pytest.raises(ValueError, match="connector allow-list"):
        ApplianceScheduler(profile, connector_names=("youtube", "youtube"), now=_NOW)

    with pytest.raises(ValueError, match="connector allow-list"):
        ApplianceScheduler(
            profile,
            connector_names=tuple(f"connector-{index}" for index in range(9)),
            now=_NOW,
        )

    connector_profile = _scheduler_profile(tmp_path, "connector-brain")
    scheduler = ApplianceScheduler(
        connector_profile,
        connector_names=("youtube",),
        handlers={"connector-run:youtube": lambda _context: ApplianceJobResult.empty()},
        now=_NOW,
    )
    assert [job.name for job in scheduler.inventory() if job.name.startswith("connector-run:")] == [
        "connector-run:youtube"
    ]


def test_scheduler_retries_a_failed_job_after_provider_outage(
    tmp_path: Path,
) -> None:
    profile = _scheduler_profile(tmp_path)
    root = profile.root
    attempts = {"count": 0}

    def recover(_context: ApplianceRunContext) -> ApplianceJobResult:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ApplianceSchedulerRetryableError("provider_unavailable", retry_delay_seconds=60)
        return ApplianceJobResult.empty()

    scheduler = ApplianceScheduler(profile, handlers={"engine-recover": recover}, now=_NOW)

    first = scheduler.run_due(now=_NOW)
    second = scheduler.run_due(now=_NOW + timedelta(seconds=30))
    third = scheduler.run_due(now=_NOW + timedelta(seconds=61))

    assert [(receipt.job_name, receipt.status) for receipt in first] == [
        ("engine-recover", "failed"),
        ("markdown-reconcile", "deferred"),
    ]
    assert second == ()
    assert [(receipt.job_name, receipt.status) for receipt in third] == [
        ("engine-recover", "empty"),
    ]
    assert attempts["count"] == 2
    runs = tuple((root / APPLIANCE_SCHEDULER_DIRECTORY / "runs" / "engine-recover").glob("*.json"))
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in runs]
    assert len(payloads) == 2
    assert all(
        set(payload) == {
            "attempt",
            "finished_at",
            "job_name",
            "next_due_at",
            "reason",
            "run_id",
            "started_at",
            "status",
        }
        for payload in payloads
    )
    assert {payload["status"] for payload in payloads} == {"empty", "failed"}


def test_scheduler_replays_an_interrupted_run_with_the_same_run_id(
    tmp_path: Path,
) -> None:
    profile = _scheduler_profile(tmp_path)
    root = profile.root
    marker = root / APPLIANCE_SCHEDULER_DIRECTORY / "marker.json"
    observed: list[tuple[str, int]] = []

    def recover(context: ApplianceRunContext) -> ApplianceJobResult:
        run_id = context.run_id
        attempt = context.attempt
        observed.append((run_id, attempt))
        if attempt == 1:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
            raise ApplianceSchedulerInterruptedError("synthetic interruption")
        persisted = json.loads(marker.read_text(encoding="utf-8"))
        assert persisted == {"run_id": run_id}
        return ApplianceJobResult.completed()

    scheduler = ApplianceScheduler(profile, handlers={"engine-recover": recover}, now=_NOW)

    with pytest.raises(ApplianceSchedulerInterruptedError, match="interruption"):
        scheduler.run_due(now=_NOW)

    restarted = ApplianceScheduler(profile, handlers={"engine-recover": recover}, now=_NOW)
    receipts = restarted.run_due(now=_NOW + timedelta(seconds=1))

    assert [(receipt.job_name, receipt.status) for receipt in receipts] == [
        ("engine-recover", "completed"),
        ("markdown-reconcile", "deferred"),
    ]
    assert observed == [(observed[0][0], 1), (observed[0][0], 2)]


def test_scheduler_rejects_noncanonical_or_out_of_inventory_state(
    tmp_path: Path,
) -> None:
    profile = _scheduler_profile(tmp_path)
    root = profile.root
    scheduler = ApplianceScheduler(profile, now=_NOW)
    state = scheduler.read_state()
    jobs = cast(dict[str, dict[str, object]], state["jobs"])
    jobs["legacy-job"] = {
        "active_attempt": 0,
        "active_run_id": None,
        "active_started_at": None,
        "interval_seconds": None,
        "last_status": None,
        "name": "legacy-job",
        "next_due_at": None,
        "recurring": False,
    }
    state_path = root / APPLIANCE_SCHEDULER_DIRECTORY / "state.json"
    state_path.write_bytes(canonical_json_bytes(state))

    with pytest.raises(ValueError, match="scheduler state"):
        ApplianceScheduler(profile, now=_NOW).read_state()

    state_path.write_text('{"jobs":{},"schema_version":1}', encoding="utf-8")
    with pytest.raises(ValueError, match="scheduler state"):
        ApplianceScheduler(profile, now=_NOW).read_state()


def test_scheduler_finalizes_from_a_persisted_receipt_without_replaying_the_handler(
    tmp_path: Path,
) -> None:
    profile = _scheduler_profile(tmp_path)
    root = profile.root
    scheduler = ApplianceScheduler(profile, now=_NOW)
    state = scheduler.read_state()
    jobs = cast(dict[str, dict[str, object]], state["jobs"])
    run_id = "run_123e4567-e89b-42d3-a456-426614174199"
    jobs["engine-recover"] = {
        "active_attempt": 1,
        "active_run_id": run_id,
        "active_started_at": "2026-09-01T12:00:00Z",
        "interval_seconds": 300,
        "last_status": None,
        "name": "engine-recover",
        "next_due_at": "2026-09-01T12:00:00Z",
        "recurring": True,
    }
    (root / APPLIANCE_SCHEDULER_DIRECTORY).mkdir(parents=True, exist_ok=True)
    (root / APPLIANCE_SCHEDULER_DIRECTORY / "state.json").write_bytes(canonical_json_bytes(state))

    persisted = ApplianceRunReceipt(
        job_name="engine-recover",
        run_id=run_id,
        attempt=1,
        status="completed",
        started_at="2026-09-01T12:00:00Z",
        finished_at="2026-09-01T12:00:01Z",
        next_due_at="2026-09-01T12:05:01Z",
    )
    receipt_path = (
        root
        / APPLIANCE_SCHEDULER_DIRECTORY
        / "runs"
        / "engine-recover"
        / f"{run_id}.json"
    )
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_bytes(canonical_json_bytes(persisted.to_dict()))

    called = {"count": 0}

    def fail_if_replayed(_context: ApplianceRunContext) -> ApplianceJobResult:
        called["count"] += 1
        raise AssertionError("persisted receipt must prevent handler replay")

    restarted = ApplianceScheduler(
        profile,
        handlers={"engine-recover": fail_if_replayed},
        now=_NOW,
    )
    receipts = restarted.run_due(now=_NOW + timedelta(seconds=2))

    assert receipts[0] == persisted
    assert called["count"] == 0
    resumed = cast(dict[str, dict[str, object]], restarted.read_state()["jobs"])["engine-recover"]
    assert resumed["active_run_id"] is None
    assert resumed["active_attempt"] == 0
    assert resumed["last_status"] == "completed"
    assert resumed["next_due_at"] == "2026-09-01T12:05:01Z"


def test_scheduler_state_is_owner_only_and_rejects_oversized_state(tmp_path: Path) -> None:
    profile = _scheduler_profile(tmp_path)
    ApplianceScheduler(profile, now=_NOW)
    directory = profile.root / APPLIANCE_SCHEDULER_DIRECTORY
    state_path = directory / "state.json"

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

    state_path.write_bytes(b"x" * 4_097)
    with pytest.raises(ValueError, match="scheduler state"):
        ApplianceScheduler(profile, now=_NOW)


def test_scheduler_rejects_root_and_state_directory_replacements(tmp_path: Path) -> None:
    profile = _scheduler_profile(tmp_path)
    scheduler = ApplianceScheduler(profile, now=_NOW)
    original_root = profile.root
    moved_root = tmp_path / "moved-brain"
    original_root.rename(moved_root)
    original_root.mkdir()

    with pytest.raises(ValueError, match="scheduler state"):
        scheduler.read_state()
    assert not (original_root / APPLIANCE_SCHEDULER_DIRECTORY).exists()

    original_root.rmdir()
    moved_root.rename(original_root)
    scheduler_directory = original_root / APPLIANCE_SCHEDULER_DIRECTORY
    retained = original_root / ".open-brain" / "state" / "retained-scheduler"
    scheduler_directory.rename(retained)
    outside = tmp_path / "outside"
    outside.mkdir()
    scheduler_directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="scheduler state"):
        ApplianceScheduler(profile, now=_NOW)
    assert tuple(outside.iterdir()) == ()
