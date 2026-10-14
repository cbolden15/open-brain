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
    assert "run-until-caught-up" in value["ProgramArguments"]
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


def test_health_preserves_public_coverage_separately_from_execution(tmp_path: Path) -> None:
    store = CaptureHealthStore(tmp_path / "health", clock=lambda: 100)
    receipt = store.record(
        [
            {
                "source_id": "thread",
                "outcome": "completed",
                "has_more": False,
                "notices": ["partial_thread_coverage", "private provider message"],
            },
            {
                "source_id": "search",
                "outcome": "completed",
                "notices": ["incomplete_search_coverage", "untracked_older_threads_unsupported"],
            },
        ]
    )
    coverage = cast(list[dict[str, object]], receipt["coverage"])
    assert receipt["outcome"] == "completed"
    assert receipt["coverage_status"] == "partial"
    assert coverage[0]["coverage_codes"] == ["partial_thread_coverage"]
    assert coverage[1]["coverage_codes"] == [
        "incomplete_search_coverage",
        "untracked_older_threads_unsupported",
    ]
    assert "private provider message" not in json.dumps(store.health())
    assert store.health()["coverage_status"] == "partial"


def test_legacy_health_receipts_preserve_measurements_with_unknown_coverage(tmp_path: Path) -> None:
    store = CaptureHealthStore(tmp_path / "health", clock=lambda: 100)
    receipt = store.record([{"source_id": "one", "outcome": "completed", "captured_count": 2}])
    receipt.pop("coverage_status")
    for item in cast(list[dict[str, object]], receipt["coverage"]):
        item.pop("coverage_status")
        item.pop("coverage_codes")
    store._store.write("runs.json", {"schema_version": 1, "receipts": [receipt]})
    health = store.health()
    assert health["coverage_status"] == "unknown"
    assert cast(dict[str, object], health["last_run"])["captured_count"] == 2
    store.record([])
    saved = cast(dict[str, object], store._store.read("runs.json"))
    assert saved["schema_version"] == 2
    assert cast(list[dict[str, object]], saved["receipts"])[0]["coverage_status"] == "unknown"


def test_bounded_headless_activation_drains_pages_and_honors_retry_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100]
    calls: list[int] = []
    sleeps: list[float] = []
    runner = CollectorProcessRunner(
        tmp_path / "state.json",
        tmp_path / "brain",
        tmp_path / "lease",
        LocalSourceRuntime(tmp_path / "runtime"),
        clock=lambda: now[0],
    )

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += int(seconds)

    def sync(self: object, **kw: object) -> list[dict[str, object]]:
        calls.append(now[0])
        if len(calls) == 1:
            return [
                {
                    "source_id": "one",
                    "outcome": "completed",
                    "has_more": True,
                    "next_run_epoch": now[0] + 1,
                    "captured_count": 1,
                }
            ]
        if len(calls) == 2:
            return [
                {
                    "source_id": "one",
                    "outcome": "failed",
                    "failure_code": "rate_limited",
                    "retry_at": now[0] + 30,
                }
            ]
        return [
            {"source_id": "one", "outcome": "completed", "has_more": False, "captured_count": 1}
        ]

    monkeypatch.setattr(CollectorProcessRunner, "_sync_sources", sync)
    monkeypatch.setattr("open_brain_collector.runner.time.sleep", sleep)
    monkeypatch.setattr("open_brain_collector.runner.time.monotonic", lambda: float(now[0]))
    result = runner.run_until_caught_up(max_iterations=10, max_seconds=60)
    assert result["outcome"] == "completed"
    assert result["iterations"] == 3
    assert calls == [100, 101, 131]
    assert sum(sleeps) == 31
    with CollectorLease(tmp_path / "lease").acquire(owner="resident"):
        assert runner.run_until_caught_up()["failure_code"] == "collector_already_owned"


def test_headless_bounds_preserve_pending_work_and_return_deferred(
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
    monkeypatch.setattr(
        CollectorProcessRunner,
        "_sync_sources",
        lambda self, **kw: [
            {"source_id": "one", "outcome": "completed", "has_more": True, "next_run_epoch": 101}
        ],
    )
    assert runner.run_until_caught_up(max_iterations=1)["stop_reason"] == "iteration_budget"
    assert runner.run_until_caught_up(max_iterations=1)["outcome"] == "deferred"
    monkeypatch.setattr(
        CollectorProcessRunner,
        "_sync_sources",
        lambda self, **kw: [
            {
                "source_id": "one",
                "outcome": "failed",
                "failure_code": "rate_limited",
                "retry_at": 1000,
            }
        ],
    )
    assert runner.run_until_caught_up(max_seconds=10)["stop_reason"] == "deadline_budget"


def test_headless_cli_accepts_explicit_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_collector.cli import main

    received: dict[str, object] = {}

    def run(self: object, **kw: object) -> dict[str, object]:
        received.update(kw)
        return {"outcome": "deferred", "failure_code": None, "results": []}

    monkeypatch.setattr(CollectorProcessRunner, "run_until_caught_up", run)
    assert (
        main(
            [
                "run-until-caught-up",
                "--state",
                str(tmp_path / "state.json"),
                "--brain-root",
                str(tmp_path / "brain"),
                "--lease",
                str(tmp_path / "lease"),
                "--max-iterations",
                "12",
                "--max-seconds",
                "30",
            ]
        )
        == 75
    )
    assert received["max_iterations"] == 12
    assert received["max_seconds"] == 30


def test_headless_drains_actual_live_capture_with_retry_and_no_duplicate_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain.profile import compile_single_user_local
    from open_brain_collector.live_manager import LiveSourceManager, PriorityRuntime
    from open_brain_connectors.runtime.live_common import LiveBatch, local_source_privacy
    from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
    from open_brain_connectors.runtime.source_registry import SourceResourceSelection

    now = [100]
    profile = compile_single_user_local(tmp_path / "brain")
    selected = SourceResourceSelection("slack", "account:synthetic", "channel:CONE", "channel")

    class Pages(PriorityRuntime):
        calls: list[int]

        def fetch(
            self,
            selection: SourceResourceSelection,
            options: dict[str, object],
            checkpoint: dict[str, object] | None,
        ) -> LiveBatch:
            self.calls.append(now[0])
            if len(self.calls) == 2:
                raise LiveSourceError("rate_limited", retry_after_seconds=30)
            page = cast(int, (checkpoint or {}).get("page", 0)) + 1
            intake = SourceRecordIntake(
                SourceRecordKey(
                    "slack",
                    selected.connection_id,
                    selected.resource_id,
                    f"message:179190000{page}.000001",
                    "edited:1",
                ),
                "https://slack.com/archives/CONE/synthetic",
                f"Catchupneedle page {page}",
                local_source_privacy(),
            )
            return LiveBatch((intake,), {"page": page}, has_more=page < 3)

    runtime = Pages(tmp_path / "live")
    runtime.calls = []
    manager = LiveSourceManager(tmp_path / "live", profile.root, runtime=runtime)
    manager.capture._clock = lambda: now[0]
    manager.capture.configure("slack.selected", selected, {})
    manager.capture.control("slack.selected", "enable")
    runner = CollectorProcessRunner(
        tmp_path / "state.json",
        profile.root,
        tmp_path / "lease",
        LocalSourceRuntime(tmp_path / "runtime"),
        clock=lambda: now[0],
    )

    def sleep(seconds: float) -> None:
        now[0] += int(seconds)

    monkeypatch.setattr(
        CollectorProcessRunner, "_sync_live", lambda self, **kw: manager.sync_due(**kw)
    )
    monkeypatch.setattr("open_brain_collector.runner.time.sleep", sleep)
    monkeypatch.setattr("open_brain_collector.runner.time.monotonic", lambda: float(now[0]))
    result = runner.run_until_caught_up(max_iterations=10, max_seconds=60)
    assert result["outcome"] == "completed"
    assert runtime.calls == [100, 101, 131, 132]
    assert result["captured_count"] == 3
    state = manager.capture.status()["sources"]
    assert cast(list[dict[str, object]], state)[0]["last_run"] is not None
    assert manager.capture._load("slack.selected")["checkpoint"] == {"page": 3}
    from open_brain_engine.engine import open_local_engine

    assert len(open_local_engine(profile).retrieval.search("Catchupneedle")) == 3
    receipts = CaptureHealthStore(tmp_path / "capture-health")._read()
    assert sum(cast(int, item["captured_count"]) for item in receipts) == 3
    assert runner.run_until_caught_up()["outcome"] == "completed"
    assert len(runtime.calls) == 4


def test_headless_processing_budget_exhaustion_stops_even_with_ingress_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain_collector.capture_contracts import Development, SourceObservation
    from open_brain_collector.capture_processing import CaptureProcessingStore, ProcessingPolicy
    from open_brain_connectors.runtime.live_common import local_source_privacy
    from open_brain_connectors.runtime.live_storage import PrivateJsonStore

    queue = CaptureProcessingStore(PrivateJsonStore(tmp_path / "queue"))
    for index in range(3):
        queue.admit(
            SourceObservation(
                "workspace",
                "conversation",
                f"123.{index}",
                "revision",
                "2026-10-14",
                "https://example.invalid/thread",
                f"Decision: distinct budget claim {index}",
                "",
                f"raw_{index}",
                local_source_privacy(),
            )
        )
    accepted: list[str] = []

    def capture(development: Development, observation: SourceObservation, delivery: str) -> str:
        accepted.append(delivery)
        return "derived_" + str(len(accepted))

    runner = CollectorProcessRunner(
        tmp_path / "state.json",
        tmp_path / "brain",
        tmp_path / "lease",
        LocalSourceRuntime(tmp_path / "runtime"),
        clock=lambda: 100,
    )

    def sync(self: object, **kw: object) -> list[dict[str, object]]:
        return [
            {
                "source_id": "slack-development",
                **queue.process(ProcessingPolicy(max_calls=1), capture),
            },
            {
                "source_id": "ingress",
                "outcome": "completed",
                "has_more": True,
                "next_run_epoch": 100,
            },
        ]

    monkeypatch.setattr(CollectorProcessRunner, "_sync_sources", sync)
    result = runner.run_until_caught_up(max_iterations=5, max_seconds=60)
    assert result["outcome"] == "deferred"
    assert result["stop_reason"] == "processing_budget"
    assert result["iterations"] == 1
    assert len(accepted) == 1
    observations = cast(dict[str, dict[str, object]], queue._load()["observations"])
    assert sum(item["state"] == "pending" for item in observations.values()) == 2


@pytest.mark.parametrize("missing_usage", [False, True])
def test_activation_health_retains_all_source_coverage_and_honest_cumulative_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_usage: bool,
) -> None:
    now = [100]
    calls = [0]
    runner = CollectorProcessRunner(
        tmp_path / "state.json",
        tmp_path / "brain",
        tmp_path / "lease",
        LocalSourceRuntime(tmp_path / "runtime"),
        clock=lambda: now[0],
    )

    def sync(self: object, **kw: object) -> list[dict[str, object]]:
        calls[0] += 1
        if calls[0] == 1:
            return [
                {
                    "source_id": "limited",
                    "outcome": "completed",
                    "captured_count": 2,
                    "usage": {"input_tokens": 3},
                    "notices": ["partial_thread_coverage"],
                },
                {
                    "source_id": "paged",
                    "outcome": "completed",
                    "captured_count": 1,
                    "has_more": True,
                    "next_run_epoch": 101,
                    "usage": {"input_tokens": 5},
                },
            ]
        return [
            {
                "source_id": "paged",
                "outcome": "completed",
                "captured_count": 1,
                "duplicate_count": 1,
                "has_more": False,
                **({} if missing_usage else {"usage": {"input_tokens": 7}}),
            }
        ]

    def sleep(seconds: float) -> None:
        now[0] += int(seconds)

    monkeypatch.setattr(CollectorProcessRunner, "_sync_sources", sync)
    monkeypatch.setattr("open_brain_collector.runner.time.sleep", sleep)
    monkeypatch.setattr("open_brain_collector.runner.time.monotonic", lambda: float(now[0]))
    result = runner.run_until_caught_up(max_iterations=5, max_seconds=60)
    assert result["outcome"] == "completed"
    health = CaptureHealthStore(tmp_path / "capture-health", clock=lambda: now[0]).health()
    receipt = cast(dict[str, object], health["last_run"])
    assert health["receipt_count"] == 1
    assert receipt["source_count"] == 2
    assert receipt["captured_count"] == result["captured_count"] == 4
    assert receipt["duplicate_count"] == 1
    assert health["coverage_status"] == "partial"
    coverage = cast(list[dict[str, object]], receipt["coverage"])
    assert any(item["coverage_codes"] == ["partial_thread_coverage"] for item in coverage)
    metrics = cast(dict[str, int], receipt["metrics"])
    assert metrics == ({} if missing_usage else {"input_tokens": 15})


def test_headless_ingestion_pages_share_one_final_processing_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain.profile import compile_single_user_local
    from open_brain_collector.live_manager import LiveSourceManager, PriorityRuntime
    from open_brain_connectors.runtime.live_common import LiveBatch, local_source_privacy
    from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
    from open_brain_connectors.runtime.source_registry import SourceResourceSelection

    now = [100]
    profile = compile_single_user_local(tmp_path / "brain")
    selected = SourceResourceSelection("slack", "account:synthetic", "channel:CONE", "channel")

    class Pages(PriorityRuntime):
        def fetch(
            self,
            selection: SourceResourceSelection,
            options: dict[str, object],
            checkpoint: dict[str, object] | None,
        ) -> LiveBatch:
            page = cast(int, (checkpoint or {}).get("page", 0)) + 1
            item = SourceRecordIntake(
                SourceRecordKey(
                    "slack",
                    selected.connection_id,
                    selected.resource_id,
                    f"message:179190000{page}.000001",
                    "edited:1",
                ),
                "https://slack.com/archives/CONE/synthetic",
                f"Decision: Budgetneedle page {page}",
                local_source_privacy(),
            )
            return LiveBatch((item,), {"page": page}, has_more=page < 3)

    manager = LiveSourceManager(tmp_path / "live", profile.root, runtime=Pages(tmp_path / "live"))
    manager.capture._clock = lambda: now[0]
    manager.workflow.configure({"enabled": True, "ai_exclusions_verified": True, "max_calls": 1})
    manager._slack_policy.setup(selected.connection_id, {})
    monkeypatch.setattr(manager._slack_policy, "accounts", lambda: ())
    manager.capture.configure("slack.selected", selected, {})
    manager.capture.control("slack.selected", "enable")
    runner = CollectorProcessRunner(
        tmp_path / "state.json",
        profile.root,
        tmp_path / "lease",
        LocalSourceRuntime(tmp_path / "runtime"),
        clock=lambda: now[0],
    )

    def sleep(seconds: float) -> None:
        now[0] += int(seconds)

    monkeypatch.setattr(
        CollectorProcessRunner, "_sync_live", lambda self, **kw: manager.sync_due(**kw)
    )
    monkeypatch.setattr(
        CollectorProcessRunner,
        "_process_live",
        lambda self: [{"source_id": "slack-development", **manager.workflow.process()}],
        raising=False,
    )
    monkeypatch.setattr("open_brain_collector.runner.time.sleep", sleep)
    monkeypatch.setattr("open_brain_collector.runner.time.monotonic", lambda: float(now[0]))
    result = runner.run_until_caught_up(max_iterations=5, max_seconds=60)
    assert result["outcome"] == "deferred"
    assert result["stop_reason"] == "processing_budget"
    assert manager.workflow.status()["development_count"] == 1
    assert manager.workflow.status()["pending_count"] == 2, result
    assert manager.capture._load("slack.selected")["checkpoint"] == {"page": 3}
    assert result["captured_count"] == 4
