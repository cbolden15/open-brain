from __future__ import annotations

import json
from pathlib import Path

import pytest
from open_brain_engine.engine import PrivacyDecision

from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorRunPage,
    CollectorSourceRuntime,
    CollectorStateStore,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


def test_replay_after_capture_before_checkpoint_keeps_one_active_delivery(
    tmp_path: Path,
) -> None:
    clock = _Clock(100)
    controller = CollectorController(CollectorStateStore(tmp_path / "state.json"), clock=clock)
    selection = _selection()
    source = _PagedSource(selection)
    sink = _IdempotentCrashSink(crash_after=1)
    controller.enable(source_id="github.fixture.recovery", selection=selection, interval_seconds=30)

    with pytest.raises(RuntimeError, match="synthetic crash"):
        controller.sync_due(
            source_id="github.fixture.recovery",
            runtime=source,
            capture_sink=sink,
        )

    crashed = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    entry = crashed["sources"]["github.fixture.recovery"]
    assert entry["active_run"]["intakes"][0]["delivery_id"] == _delivery_id(selection, "issue:70")
    assert entry["last_run"]["outcome"] == "failed"

    restarted = CollectorController(CollectorStateStore(tmp_path / "state.json"), clock=clock)
    sink.crash_after = None
    replay = restarted.sync_due(
        source_id="github.fixture.recovery",
        runtime=source,
        capture_sink=sink,
    )

    assert replay.outcome == "completed"
    assert replay.captured_count == 1
    assert sink.created_delivery_ids == [_delivery_id(selection, "issue:70")]
    assert sink.duplicate_delivery_ids == [_delivery_id(selection, "issue:70")]
    recovered = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    recovered_entry = recovered["sources"]["github.fixture.recovery"]
    assert recovered_entry["active_run"] is None
    assert recovered_entry["last_run"]["outcome"] == "completed"
    assert recovered_entry["committed_revisions"] == {
        _delivery_id(selection, "issue:70"): source.revision_identity("issue:70"),
    }


def test_replay_after_checkpoint_before_receipt_publication_is_empty(
    tmp_path: Path,
) -> None:
    clock = _Clock(100)
    state_store = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(state_store, clock=clock)
    selection = _selection()
    source = _PagedSource(selection)
    sink = _IdempotentCrashSink()
    controller.enable(source_id="github.fixture.receipt", selection=selection, interval_seconds=30)

    first = controller.sync_due(
        source_id="github.fixture.receipt",
        runtime=source,
        capture_sink=sink,
    )
    clock.value = 130
    source.cursor = "cursor:page2"
    restarted = CollectorController(state_store, clock=clock)
    replay = restarted.sync_due(
        source_id="github.fixture.receipt",
        runtime=source,
        capture_sink=sink,
    )

    assert first.outcome == "completed"
    assert replay.outcome == "empty"
    assert replay.duplicate_count == 1
    assert sink.created_delivery_ids == [_delivery_id(selection, "issue:70")]


def test_wake_clock_jump_replays_cursor_without_duplicate_delivery(
    tmp_path: Path,
) -> None:
    clock = _Clock(100)
    controller = CollectorController(CollectorStateStore(tmp_path / "state.json"), clock=clock)
    selection = _selection()
    source = _PagedSource(selection)
    sink = _IdempotentCrashSink()
    controller.enable(source_id="github.fixture.wake", selection=selection, interval_seconds=60)
    first = controller.sync_due(source_id="github.fixture.wake", runtime=source, capture_sink=sink)

    clock.value = 86_500
    source.cursor = "cursor:page2"
    source.external_id = "issue:71"
    source.revision = "updated:2026-09-15T00:02:00Z"
    wake = controller.sync_due(source_id="github.fixture.wake", runtime=source, capture_sink=sink)
    same_wake = controller.sync_due(
        source_id="github.fixture.wake",
        runtime=source,
        capture_sink=sink,
    )

    assert first.next_cursor == "cursor:page2"
    assert wake.outcome == "completed"
    assert wake.next_cursor is None
    assert same_wake.outcome == "deferred"
    assert sink.created_delivery_ids == [
        _delivery_id(selection, "issue:70"),
        _delivery_id(selection, "issue:71"),
    ]


class _Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _PagedSource(CollectorSourceRuntime):
    def __init__(self, selection: SourceResourceSelection) -> None:
        self.selection = selection
        self.cursor: str | None = None
        self.external_id = "issue:70"
        self.revision = "updated:2026-09-15T00:00:00Z"

    def fetch_page(
        self,
        selection: SourceResourceSelection,
        cursor: str | None,
    ) -> CollectorRunPage:
        assert selection == self.selection
        assert cursor == self.cursor
        next_cursor = "cursor:page2" if cursor is None else None
        return CollectorRunPage(
            selection=selection,
            intakes=(self.intake(self.external_id),),
            next_cursor=next_cursor,
        )

    def intake(self, external_id: str) -> SourceRecordIntake:
        return SourceRecordIntake(
            key=SourceRecordKey(
                connector_name="github",
                connection_id=self.selection.connection_id,
                resource_id=self.selection.resource_id,
                external_id=external_id,
                revision_id=self.revision,
            ),
            url=f"https://github.com/cbolden15/open-brain-fixture/issues/{external_id[-2:]}",
            title="D3 recovery fixture",
            text=f"Synthetic recovery body for {external_id}.",
            privacy=_privacy(),
        )

    def revision_identity(self, external_id: str) -> str:
        return self.intake(external_id).key.revision_identity()


class _IdempotentCrashSink:
    def __init__(self, crash_after: int | None = None) -> None:
        self.crash_after = crash_after
        self.created_delivery_ids: list[str] = []
        self.duplicate_delivery_ids: list[str] = []

    def submit(self, intake: SourceRecordIntake) -> None:
        delivery_id = intake.key.delivery_id()
        if delivery_id in self.created_delivery_ids:
            self.duplicate_delivery_ids.append(delivery_id)
        else:
            self.created_delivery_ids.append(delivery_id)
        if self.crash_after is not None:
            self.crash_after -= 1
            if self.crash_after == 0:
                raise RuntimeError("synthetic crash after durable capture")


def _selection() -> SourceResourceSelection:
    return SourceResourceSelection(
        connector_name="github",
        connection_id="account:cbolden15",
        resource_id="repo:cbolden15/open-brain-fixture",
        resource_type="repository",
    )


def _delivery_id(selection: SourceResourceSelection, external_id: str) -> str:
    return SourceRecordKey(
        connector_name="github",
        connection_id=selection.connection_id,
        resource_id=selection.resource_id,
        external_id=external_id,
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
