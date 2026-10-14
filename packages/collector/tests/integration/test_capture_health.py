from __future__ import annotations

import json
import plistlib
from pathlib import Path
from typing import Any, cast

import pytest

from open_brain_collector.capture_health import CaptureHealthStore
from open_brain_collector.lifecycle import CollectorLease
from open_brain_collector.runner import CollectorProcessRunner, LocalSourceRuntime
from open_brain_connectors.runtime.live_common import LiveSourceError


def test_health_is_bounded_metadata_only_and_distinguishes_outcomes(tmp_path: Path) -> None:
    store = CaptureHealthStore(tmp_path / "health", clock=lambda: 100)
    assert store.health()["status"] == "never_run"
    assert store.record([])["outcome"] == "empty"
    receipt = store.record(
        [
            {
                "source_id": "synthetic-private-label",
                "outcome": "completed",
                "captured_count": 2,
                "duplicate_count": 1,
                "body": "never retain this",
            },
            {"source_id": "other", "outcome": "failed", "failure_code": "raw private error"},
        ],
        metrics={
            "backlog_count": 3,
            "oldest_pending_epoch": 70,
            "checkpoint_epoch": 80,
            "pending_retries": 1,
            "input_tokens": 12,
            "output_tokens": 4,
            "cost_microusd": 10,
        },
    )
    assert receipt["outcome"] == "partial"
    assert receipt["captured_count"] == 2
    assert receipt["duplicate_count"] == 1
    health = store.health()
    assert health["backlog_age_seconds"] == 30
    assert health["checkpoint_lag_seconds"] == 20
    raw = (tmp_path / "health" / "runs.json").read_text()
    for secret in ("synthetic-private-label", "never retain this", "raw private error"):
        assert secret not in raw
    for _ in range(70):
        store.record([])
    assert store.health()["receipt_count"] == 64
    assert len(raw) < 100_000
    assert (tmp_path / "health" / "runs.json").stat().st_mode & 0o077 == 0
    with pytest.raises(LiveSourceError, match="invalid_health_metrics"):
        store.record([], metrics={"message": "disallowed"})
    with pytest.raises(LiveSourceError, match="health_source_limit"):
        store.record([{}] * 501)


def test_overdue_grace_does_not_change_checkpoint_or_receipts(tmp_path: Path) -> None:
    now = [100]
    store = CaptureHealthStore(tmp_path / "health", clock=lambda: now[0])
    store.record([])
    before = (tmp_path / "health" / "runs.json").read_bytes()
    now[0] = 100 + 86_400 + 7_200
    assert store.health()["status"] == "empty"
    now[0] += 1
    assert store.health()["status"] == "overdue"
    assert (tmp_path / "health" / "runs.json").read_bytes() == before


def test_run_once_and_resident_share_lease_and_record_actual_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state.json"
    runner = CollectorProcessRunner(
        state,
        tmp_path / "brain",
        tmp_path / "lease",
        LocalSourceRuntime(tmp_path / "runtime"),
        clock=lambda: 100,
    )
    monkeypatch.setattr(CollectorProcessRunner, "_sync_sources", lambda self, **kw: [])
    assert runner.run_once()["outcome"] == "completed"
    health = CaptureHealthStore(tmp_path / "capture-health", clock=lambda: 100)
    assert cast(dict[str, Any], health.health()["last_run"])["outcome"] == "empty"
    before = (tmp_path / "capture-health" / "runs.json").read_bytes()
    with CollectorLease(tmp_path / "lease").acquire(owner="resident"):
        assert runner.run_once()["failure_code"] == "collector_already_owned"
        assert runner.run_loop(interval_seconds=1, max_iterations=1)["stop_reason"] == "owned"
    assert (tmp_path / "capture-health" / "runs.json").read_bytes() == before
    assert runner.run_loop(interval_seconds=1, max_iterations=1)["iterations"] == 1
    assert health.health()["receipt_count"] == 2

    def fail(self: object, **kw: object) -> list[dict[str, object]]:
        raise RuntimeError("private exception text")

    monkeypatch.setattr(CollectorProcessRunner, "_sync_sources", fail)
    with pytest.raises(RuntimeError, match="private exception text"):
        runner.run_once()
    assert health.health()["status"] == "failed"
    assert "private exception text" not in json.dumps(health.health())
    with pytest.raises(RuntimeError):
        runner.run_loop(interval_seconds=1, max_iterations=1)
    assert health.health()["receipt_count"] == 4


def test_schedule_example_is_optional_daily_one_shot() -> None:
    root = Path(__file__).resolve().parents[4] / "docs" / "integrations" / "examples"
    value = plistlib.loads((root / "slack-capture.plist").read_bytes())
    assert "run-once" in value["ProgramArguments"]
    assert value["StartCalendarInterval"] == {"Hour": 9, "Minute": 0}
    assert "KeepAlive" not in value
    assert "Persistent=true" in (root / "slack-capture.timer").read_text()


def test_runner_partial_and_usage_are_reported_after_actual_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CollectorProcessRunner(
        tmp_path / "state.json",
        tmp_path / "brain",
        tmp_path / "lease",
        LocalSourceRuntime(tmp_path / "runtime"),
        clock=lambda: 100,
    )
    results = [
        {
            "source_id": "one",
            "outcome": "completed",
            "captured_count": 3,
            "usage": {"input_tokens": 10, "cost_microusd": 5},
            "body": "private body",
        },
        {"source_id": "two", "outcome": "deferred", "failure_code": "rate_limited"},
    ]
    monkeypatch.setattr(CollectorProcessRunner, "_sync_sources", lambda self, **kw: results)
    assert runner.run_once()["results"] == results
    health = CaptureHealthStore(tmp_path / "capture-health", clock=lambda: 100).health()
    assert health["status"] == "partial"
    assert health["metrics"] == {"input_tokens": 10, "cost_microusd": 5}
    assert cast(dict[str, Any], health["last_run"])["captured_count"] == 3
    assert cast(dict[str, Any], health["last_run"])["partial_count"] == 1
    assert "private body" not in json.dumps(health)
