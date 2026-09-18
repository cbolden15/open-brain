from __future__ import annotations

import json
import multiprocessing
import stat
import threading
from pathlib import Path

import pytest
from open_brain_engine.engine import PrivacyDecision

from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorRunPage,
    CollectorSourceRuntime,
    CollectorStateStore,
    MemoryCaptureSink,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


def test_enable_pause_resume_and_disable_are_durable(tmp_path: Path) -> None:
    clock = _Clock(100)
    controller = CollectorController(CollectorStateStore(tmp_path / "collector.json"), clock=clock)
    selection = _selection()

    enabled = controller.enable(
        source_id="github.fixture",
        selection=selection,
        interval_seconds=60,
    )
    clock.value = 105
    paused = controller.pause("github.fixture")
    reloaded = CollectorController(CollectorStateStore(tmp_path / "collector.json"), clock=clock)
    resumed = reloaded.resume("github.fixture")
    disabled = reloaded.disable("github.fixture")

    assert enabled.status == "enabled"
    assert enabled.next_run_epoch == 100
    assert paused.status == "paused"
    assert paused.pause_ack_epoch == 105
    assert resumed.status == "enabled"
    assert resumed.pause_ack_epoch is None
    assert resumed.next_run_epoch == 105
    assert disabled.status == "disabled"
    assert disabled.next_run_epoch is None
    assert stat.S_IMODE((tmp_path / "collector.json").stat().st_mode) == 0o600
    persisted = json.loads((tmp_path / "collector.json").read_text(encoding="utf-8"))
    assert persisted["sources"]["github.fixture"]["status"] == "disabled"


def test_schedule_updates_interval_through_shared_lifecycle_contract(tmp_path: Path) -> None:
    clock = _Clock(100)
    controller = CollectorController(CollectorStateStore(tmp_path / "collector.json"), clock=clock)
    selection = _selection()

    controller.enable(source_id="github.fixture", selection=selection, interval_seconds=60)
    clock.value = 120
    scheduled = controller.schedule("github.fixture", 120)
    controller.pause("github.fixture")
    clock.value = 140
    paused_schedule = controller.schedule("github.fixture", 240)
    controller.disable("github.fixture")
    clock.value = 160
    disabled_schedule = controller.schedule("github.fixture", 480)

    assert scheduled.next_run_epoch == 120
    assert paused_schedule.status == "paused"
    assert paused_schedule.next_run_epoch == 120
    assert disabled_schedule.status == "disabled"
    assert disabled_schedule.next_run_epoch is None
    persisted = json.loads((tmp_path / "collector.json").read_text(encoding="utf-8"))
    assert persisted["sources"]["github.fixture"]["interval_seconds"] == 480


def test_pause_acknowledgement_prevents_scheduled_import_until_resume(tmp_path: Path) -> None:
    clock = _Clock(100)
    controller = CollectorController(CollectorStateStore(tmp_path / "collector.json"), clock=clock)
    selection = _selection()
    source = _Source(selection)
    sink = MemoryCaptureSink()

    controller.enable(source_id="github.fixture", selection=selection, interval_seconds=30)
    first = controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)
    clock.value = 130
    controller.pause("github.fixture")
    source.revision = "updated:2026-09-15T00:01:00Z"
    source.body = "Synthetic body B."
    paused = controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)
    controller.resume("github.fixture")
    resumed = controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)
    replay = controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)

    assert first.outcome == "completed"
    assert first.captured_count == 1
    assert paused.outcome == "deferred"
    assert paused.captured_count == 0
    assert resumed.outcome == "completed"
    assert resumed.captured_count == 1
    assert replay.outcome == "deferred"
    assert sink.delivery_ids == [_delivery_id(selection), _delivery_id(selection)]


def test_pause_during_active_run_is_not_overwritten_by_collector_completion(
    tmp_path: Path,
) -> None:
    clock = _Clock(100)
    state_store = CollectorStateStore(tmp_path / "collector.json")
    controller = CollectorController(state_store, clock=clock)
    control = CollectorController(state_store, clock=clock)
    selection = _selection()
    source = _Source(selection)
    entered = threading.Event()
    release = threading.Event()
    sink = _BlockingSink(entered, release)

    controller.enable(source_id="github.fixture", selection=selection, interval_seconds=30)
    results: list[object] = []
    sync_thread = threading.Thread(
        target=lambda: results.append(
            controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)
        )
    )
    sync_thread.start()
    assert entered.wait(2)
    pause_results: list[object] = []
    pause_thread = threading.Thread(
        target=lambda: pause_results.append(control.pause("github.fixture"))
    )
    pause_thread.start()
    assert not pause_results
    release.set()
    sync_thread.join(2)
    pause_thread.join(2)
    assert len(results) == len(pause_results) == 1
    result = results[0]
    persisted = json.loads((tmp_path / "collector.json").read_text(encoding="utf-8"))
    source_state = persisted["sources"]["github.fixture"]

    assert result.outcome == "failed"  # type: ignore[attr-defined]
    assert sink.returned.is_set()
    assert source_state["status"] == "paused"
    assert source_state["pause_ack_epoch"] == 100


def test_pause_during_multi_record_page_stops_remaining_imports(
    tmp_path: Path,
) -> None:
    clock = _Clock(100)
    state_store = CollectorStateStore(tmp_path / "collector.json")
    controller = CollectorController(state_store, clock=clock)
    control = CollectorController(state_store, clock=clock)
    selection = _selection()
    source = _MultiRecordSource(selection)
    entered = threading.Event()
    release = threading.Event()
    sink = _BlockingSink(entered, release)

    controller.enable(source_id="github.fixture", selection=selection, interval_seconds=30)
    results: list[object] = []
    sync_thread = threading.Thread(
        target=lambda: results.append(
            controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)
        )
    )
    sync_thread.start()
    assert entered.wait(2)
    pause_results: list[object] = []
    pause_thread = threading.Thread(
        target=lambda: pause_results.append(control.pause("github.fixture"))
    )
    pause_thread.start()
    assert not pause_results
    release.set()
    sync_thread.join(2)
    pause_thread.join(2)
    assert len(results) == len(pause_results) == 1
    result = results[0]
    persisted = json.loads((tmp_path / "collector.json").read_text(encoding="utf-8"))
    source_state = persisted["sources"]["github.fixture"]

    assert result.outcome in {"completed", "failed"}  # type: ignore[attr-defined]
    assert sink.external_ids
    assert source_state["status"] == "paused"
    assert sink.returned.is_set()
    assert source_state["pause_ack_epoch"] == 100


def test_selection_reset_waits_for_capture_in_another_process(tmp_path: Path) -> None:
    state_path = tmp_path / "collector.json"
    controller = CollectorController(CollectorStateStore(state_path), clock=lambda: 100)
    controller.enable(source_id="github.fixture", selection=_selection(), interval_seconds=30)
    context = multiprocessing.get_context("fork")
    entered = context.Event()
    release = context.Event()
    process = context.Process(target=_capture_in_process, args=(state_path, entered, release))
    process.start()
    assert entered.wait(2)

    reset_done = threading.Event()
    replacement = SourceResourceSelection(
        connector_name="github",
        connection_id="account:fixture",
        resource_id="repo:replacement",
        resource_type="repository",
    )

    def reset() -> None:
        CollectorController(CollectorStateStore(state_path), clock=lambda: 101).enable(
            source_id="github.fixture", selection=replacement, interval_seconds=30
        )
        reset_done.set()

    reset_thread = threading.Thread(target=reset)
    reset_thread.start()
    assert not reset_done.wait(0.1)
    release.set()
    process.join(3)
    reset_thread.join(3)
    assert process.exitcode == 0
    assert reset_done.is_set()
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["sources"]["github.fixture"]["resource_id"] == "repo:replacement"


@pytest.mark.parametrize("schedule", ["reset_disable", "pause_resume"])
def test_stale_cancellation_save_cannot_overwrite_acknowledged_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schedule: str
) -> None:
    state_path = tmp_path / "collector.json"
    capture_store = CollectorStateStore(state_path)
    capture = CollectorController(capture_store, clock=lambda: 100)
    owner = CollectorController(CollectorStateStore(state_path), clock=lambda: 101)
    capture.enable(source_id="github.fixture", selection=_selection(), interval_seconds=30)
    first_outcome = threading.Event()
    first_control_done = threading.Event()
    stale_read = threading.Event()
    allow_stale_reader = threading.Event()
    original_outcome = capture._custody.outcome
    outcome_calls = 0

    def pause_after_first_outcome(*args: object, **kwargs: object) -> None:
        nonlocal outcome_calls
        original_outcome(*args, **kwargs)  # type: ignore[arg-type]
        outcome_calls += 1
        if outcome_calls == 1:
            first_outcome.set()
            assert first_control_done.wait(2)

    monkeypatch.setattr(capture._custody, "outcome", pause_after_first_outcome)
    original_load = capture_store.load

    def block_stale_load() -> dict[str, object]:
        state = original_load()
        source = state["sources"]["github.fixture"]  # type: ignore[index]
        assert isinstance(source, dict)
        selected = (
            source["resource_id"] == "repo:replacement"
            if schedule == "reset_disable"
            else source["status"] == "paused"
        )
        if selected and not stale_read.is_set():
            stale_read.set()
            assert allow_stale_reader.wait(2)
        return state

    monkeypatch.setattr(capture_store, "load", block_stale_load)
    results: list[object] = []
    sync_thread = threading.Thread(
        target=lambda: results.append(
            capture.sync_due(
                source_id="github.fixture",
                runtime=_MultiRecordSource(_selection()),
                capture_sink=MemoryCaptureSink(),
            )
        )
    )
    sync_thread.start()
    assert first_outcome.wait(2)
    if schedule == "reset_disable":
        replacement = SourceResourceSelection(
            connector_name="github",
            connection_id="account:fixture",
            resource_id="repo:replacement",
            resource_type="repository",
        )
        owner.enable(source_id="github.fixture", selection=replacement, interval_seconds=30)
    else:
        owner.pause("github.fixture")
    first_control_done.set()
    assert stale_read.wait(2)
    final_control_done = threading.Event()

    def final_control() -> None:
        if schedule == "reset_disable":
            owner.disable("github.fixture")
        else:
            owner.resume("github.fixture")
        final_control_done.set()

    control_thread = threading.Thread(target=final_control)
    control_thread.start()
    assert not final_control_done.wait(0.1)
    allow_stale_reader.set()
    sync_thread.join(3)
    control_thread.join(3)
    assert len(results) == 1 and final_control_done.is_set()
    expected = "disabled" if schedule == "reset_disable" else "enabled"
    assert owner.status("github.fixture").status == expected


@pytest.mark.parametrize("action", ["pause", "disable"])
def test_baseline_metadata_only_active_run_control_and_reenable(
    tmp_path: Path, action: str
) -> None:
    store = CollectorStateStore(tmp_path / "collector.json")
    controller = CollectorController(store, clock=lambda: 100)
    controller.enable(source_id="github.fixture", selection=_selection(), interval_seconds=30)
    state = store.load()
    source = state["sources"]["github.fixture"]  # type: ignore[index]
    assert isinstance(source, dict)
    source["active_run"] = {
        "cursor": "next",
        "intakes": [],
        "run_id": "old",
        "started_epoch": 100,
    }
    store.save(state)
    result = getattr(controller, action)("github.fixture")
    assert result.status == ("paused" if action == "pause" else "disabled")
    assert store.load()["sources"]["github.fixture"]["active_run"] is None  # type: ignore[index]
    if action == "pause":
        assert controller.resume("github.fixture").status == "enabled"
    else:
        assert (
            controller.enable(
                source_id="github.fixture", selection=_selection(), interval_seconds=30
            ).status
            == "enabled"
        )


def test_pause_during_failed_active_run_is_not_overwritten_by_collector_completion(
    tmp_path: Path,
) -> None:
    clock = _Clock(100)
    state_store = CollectorStateStore(tmp_path / "collector.json")
    controller = CollectorController(state_store, clock=clock)
    selection = _selection()
    source = _Source(selection)
    sink = _FailingPausingSink(controller, "github.fixture")

    controller.enable(source_id="github.fixture", selection=selection, interval_seconds=30)
    try:
        controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)
    except RuntimeError as error:
        assert str(error) == "synthetic capture failure"
    else:
        raise AssertionError("expected synthetic capture failure")
    persisted = json.loads((tmp_path / "collector.json").read_text(encoding="utf-8"))
    source_state = persisted["sources"]["github.fixture"]

    assert source_state["status"] == "paused"
    assert source_state["pause_ack_epoch"] == 100
    assert source_state["active_run"] is None
    assert source_state["last_run"]["outcome"] == "failed"


def test_schedule_defers_before_due_and_revisions_keep_one_delivery_key(tmp_path: Path) -> None:
    clock = _Clock(100)
    controller = CollectorController(CollectorStateStore(tmp_path / "collector.json"), clock=clock)
    selection = _selection()
    source = _Source(selection)
    sink = MemoryCaptureSink()

    controller.enable(source_id="github.fixture", selection=selection, interval_seconds=30)
    first = controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)
    clock.value = 110
    early = controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)
    clock.value = 130
    unchanged = controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)
    clock.value = 160
    source.revision = "updated:2026-09-15T00:02:00Z"
    changed = controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)

    assert first.outcome == "completed"
    assert early.outcome == "deferred"
    assert unchanged.outcome == "empty"
    assert unchanged.duplicate_count == 1
    assert changed.outcome == "completed"
    assert changed.captured_count == 1
    assert len(set(sink.delivery_ids)) == 1
    persisted = json.loads((tmp_path / "collector.json").read_text(encoding="utf-8"))
    source_state = persisted["sources"]["github.fixture"]
    assert source_state["last_success_epoch"] == 160


def test_deferred_run_preserves_previous_success_epoch(tmp_path: Path) -> None:
    clock = _Clock(100)
    controller = CollectorController(CollectorStateStore(tmp_path / "collector.json"), clock=clock)
    selection = _selection()
    source = _Source(selection)
    sink = MemoryCaptureSink()

    controller.enable(source_id="github.fixture", selection=selection, interval_seconds=30)
    first = controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)
    clock.value = 110
    deferred = controller.sync_due(source_id="github.fixture", runtime=source, capture_sink=sink)
    persisted = json.loads((tmp_path / "collector.json").read_text(encoding="utf-8"))
    source_state = persisted["sources"]["github.fixture"]

    assert first.outcome == "completed"
    assert deferred.outcome == "deferred"
    assert source_state["last_run"]["outcome"] == "deferred"
    assert source_state["last_success_epoch"] == 100


class _Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Source(CollectorSourceRuntime):
    def __init__(self, selection: SourceResourceSelection) -> None:
        self.selection = selection
        self.revision = "updated:2026-09-15T00:00:00Z"
        self.body = "Synthetic body A."

    def fetch_page(
        self,
        selection: SourceResourceSelection,
        cursor: str | None,
    ) -> CollectorRunPage:
        assert selection == self.selection
        assert cursor is None
        return CollectorRunPage(selection=selection, intakes=(self._intake(),), next_cursor=None)

    def _intake(self) -> SourceRecordIntake:
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name="github",
                connection_id=self.selection.connection_id,
                resource_id=self.selection.resource_id,
                external_id="issue:70",
                revision_id=self.revision,
            ),
            url="https://github.com/cbolden15/open-brain-fixture/issues/70",
            title="D3 lifecycle fixture",
            text=self.body,
            privacy=_privacy(),
        )


class _BlockingSink:
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        self._entered = entered
        self._release = release
        self.returned = threading.Event()
        self.external_ids: list[str] = []

    def submit(self, intake: SourceRecordIntake) -> None:
        if type(intake) is not SourceRecordIntake:
            raise AssertionError("unexpected intake")
        self.external_ids.append(intake.key.external_id)
        self._entered.set()
        assert self._release.wait(2)
        self.returned.set()


class _ProcessBlockingSink:
    def __init__(self, entered: object, release: object) -> None:
        self._entered = entered
        self._release = release

    def submit(self, intake: SourceRecordIntake) -> None:
        self._entered.set()  # type: ignore[attr-defined]
        assert self._release.wait(2)  # type: ignore[attr-defined]


def _capture_in_process(state_path: Path, entered: object, release: object) -> None:
    controller = CollectorController(CollectorStateStore(state_path), clock=lambda: 100)
    controller.sync_due(
        source_id="github.fixture",
        runtime=_Source(_selection()),
        capture_sink=_ProcessBlockingSink(entered, release),
    )


class _PausingSink:
    def __init__(self, controller: CollectorController, source_id: str) -> None:
        self._controller = controller
        self._source_id = source_id

    def submit(self, intake: SourceRecordIntake) -> None:
        self._controller.pause(self._source_id)


class _FailingPausingSink(_PausingSink):
    def submit(self, intake: SourceRecordIntake) -> None:
        super().submit(intake)
        raise RuntimeError("synthetic capture failure")


class _MultiRecordSource(_Source):
    def fetch_page(
        self,
        selection: SourceResourceSelection,
        cursor: str | None,
    ) -> CollectorRunPage:
        assert selection == self.selection
        assert cursor is None
        return CollectorRunPage(
            selection=selection,
            intakes=(
                self._intake(),
                SourceRecordIntake(
                    key=SourceRecordKey(
                        connector_name="github",
                        connection_id=self.selection.connection_id,
                        resource_id=self.selection.resource_id,
                        external_id="issue:71",
                        revision_id=self.revision,
                    ),
                    url="https://github.com/cbolden15/open-brain-fixture/issues/71",
                    title="D3 lifecycle fixture second",
                    text="Synthetic body C.",
                    privacy=_privacy(),
                ),
            ),
            next_cursor=None,
        )


def _selection() -> SourceResourceSelection:
    return SourceResourceSelection(
        connector_name="github",
        connection_id="account:cbolden15",
        resource_id="repo:cbolden15/open-brain-fixture",
        resource_type="repository",
    )


def _delivery_id(selection: SourceResourceSelection) -> str:
    return SourceRecordKey(
        connector_name="github",
        connection_id=selection.connection_id,
        resource_id=selection.resource_id,
        external_id="issue:70",
        revision_id="ignored",
    ).delivery_id()


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.from_dict(
        {
            "authority": {"cloud": False, "external_egress": True},
            "confirmation_ref": None,
            "policy_version": "privacy-v1",
            "reason": "policy_public",
            "tier": "public",
        }
    )
