from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from open_brain_engine.core.ids import canonical_json_bytes

from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.appliance_history import (
    last_successful_run,
    read_appliance_run_history,
)
from open_brain.services.appliance_scheduler import (
    APPLIANCE_SCHEDULER_DIRECTORY,
    MAXIMUM_RETAINED_RUN_RECEIPTS,
    ApplianceJobResult,
    ApplianceRunReceipt,
    ApplianceScheduler,
)

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def test_appliance_run_history_is_bounded_metadata_only_and_skips_invalid_receipts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    compile_single_user_local(root)
    profile = open_existing_single_user_local(root)
    scheduler = ApplianceScheduler(
        profile,
        handlers={"engine-recover": lambda _context: ApplianceJobResult.completed()},
        now=_NOW,
    )
    scheduler.run_due(now=_NOW)
    invalid = (
        root
        / APPLIANCE_SCHEDULER_DIRECTORY
        / "runs"
        / "engine-recover"
        / "run_invalid.json"
    )
    invalid.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_bytes(
        canonical_json_bytes(
            {
                "argv": ["secret", "/private/path"],
                "body": "csrf-token=encoded",
                "cookie": "session=encoded",
            }
        )
    )
    older = (
        root
        / APPLIANCE_SCHEDULER_DIRECTORY
        / "runs"
        / "engine-recover"
        / "run_11111111-1111-4111-8111-111111111111.json"
    )
    older.write_bytes(
        canonical_json_bytes(
            ApplianceRunReceipt(
                job_name="engine-recover",
                run_id="run_11111111-1111-4111-8111-111111111111",
                attempt=1,
                status="completed",
                started_at="2026-09-01T13:00:00Z",
                finished_at="2026-09-01T13:00:01Z",
                next_due_at="2026-09-01T13:05:00Z",
            ).to_dict()
        )
    )
    untrusted_job = (
        root
        / APPLIANCE_SCHEDULER_DIRECTORY
        / "runs"
        / "credential_secret"
        / "run_33333333-3333-4333-8333-333333333333.json"
    )
    untrusted_job.parent.mkdir(parents=True)
    untrusted_job.write_bytes(
        canonical_json_bytes(
            {
                "attempt": 1,
                "finished_at": "2026-09-01T14:00:01Z",
                "job_name": "credential_secret",
                "next_due_at": None,
                "reason": None,
                "run_id": "run_33333333-3333-4333-8333-333333333333",
                "started_at": "2026-09-01T14:00:00Z",
                "status": "completed",
            }
        )
    )

    history = read_appliance_run_history(root, limit=1, maximum_bytes=1_024)
    payload = history.to_dict()
    runs = payload["runs"]
    rendered = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "ok"
    assert payload["truncated"] is True
    assert isinstance(runs, list)
    assert len(runs) == 1
    assert last_successful_run(history) is not None
    assert "csrf-token" not in rendered
    assert "/private/path" not in rendered
    assert "cookie" not in rendered
    assert "credential_secret" not in rendered


def test_appliance_run_receipts_reject_unknown_job_names() -> None:
    with pytest.raises(ValueError, match="invalid appliance run receipt"):
        ApplianceRunReceipt(
            job_name="credential_secret",
            run_id="run_44444444-4444-4444-8444-444444444444",
            attempt=1,
            status="completed",
            started_at="2026-09-01T14:00:00Z",
            finished_at="2026-09-01T14:00:01Z",
            next_due_at=None,
        )


def test_appliance_scheduler_prunes_old_run_receipts_durably(tmp_path: Path) -> None:
    root = tmp_path / "brain"
    profile = open_existing_single_user_local(compile_single_user_local(root).root)
    scheduler = ApplianceScheduler(profile, now=_NOW)

    for index in range(MAXIMUM_RETAINED_RUN_RECEIPTS + 3):
        current = _NOW + timedelta(minutes=index)
        scheduler.request("portable-export", now=current)
        scheduler.run_due(now=current)

    run_directory = root / APPLIANCE_SCHEDULER_DIRECTORY / "runs" / "portable-export"
    retained = sorted(run_directory.glob("*.json"))
    history = read_appliance_run_history(root, limit=20, maximum_bytes=8_192)
    portable_runs = [run for run in history.runs if run.job_name == "portable-export"]

    assert len(retained) == MAXIMUM_RETAINED_RUN_RECEIPTS
    assert len(portable_runs) == MAXIMUM_RETAINED_RUN_RECEIPTS
