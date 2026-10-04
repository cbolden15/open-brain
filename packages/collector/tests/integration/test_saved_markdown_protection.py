"""Accepted revision custody cannot be released by unprotected cleanup."""

from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine.local_schema import open_local_database_read_only

from open_brain_collector import custody as custody_module
from open_brain_collector.lifecycle import CollectorController, CollectorStateStore
from open_brain_collector.saved_markdown import SavedMarkdownCollectorRuntime
from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from packages.collector.tests.integration.test_saved_markdown import (
    _adapter,
    _intake,
    _revision_sink,
    _root,
)


@pytest.mark.parametrize("cleanup", ["completed", "pause", "disable"])
def test_unprotected_real_revision_retains_exact_intake_across_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup: str,
) -> None:
    selected = _root(tmp_path)
    intake = _intake(selected)
    tasks, sink = _revision_sink(tmp_path)
    runtime = SavedMarkdownCollectorRuntime(
        _adapter(selected), store=PrivateJsonStore(tmp_path / "scan")
    )
    store = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(store, clock=lambda: 100, brain_root=tasks.profile.root)
    controller.enable(
        source_id="synthetic", selection=_adapter(selected).selection, interval_seconds=60
    )
    if cleanup == "completed":
        assert (
            controller.sync_due(
                source_id="synthetic", runtime=runtime, capture_sink=sink
            ).captured_count
            == 1
        )
    else:
        original = sink.submit

        def lose_response(value: SourceRecordIntake) -> object:
            original(value)
            raise KeyboardInterrupt

        with monkeypatch.context() as patch:
            patch.setattr(sink, "submit", lose_response)
            with pytest.raises(KeyboardInterrupt):
                controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
        assert controller.custody_status("synthetic")["retained_items"] == 1
        getattr(controller, cleanup)("synthetic")
    # Canonical acceptance and a durable local cache are not independent
    # protection. Normal completion and cancellation must retain sender custody.
    restarted = CollectorController(store, clock=lambda: 160, brain_root=tasks.profile.root)
    status = restarted.custody_status("synthetic")
    assert status["retained_items"] == 1
    receipt_id = cast(list[str], status["receipt_ids"])[0]
    assert restarted._custody.intake(receipt_id) == intake
    cache = PrivateJsonStore(tmp_path / "revisions")
    assert len(cache.names("saved-revision-")) == 1
    receipt = restarted._custody.receipt(receipt_id)
    assert receipt["outcome"] == ("captured" if cleanup == "completed" else "pending")
    assert tasks.sources is not None


def test_unprotected_revision_backpressure_never_evicts_accepted_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(custody_module, "MAX_RETAINED_ITEMS", 1)
    selected = _root(tmp_path)
    original = _intake(selected)
    tasks, sink = _revision_sink(tmp_path)
    runtime = SavedMarkdownCollectorRuntime(
        _adapter(selected), store=PrivateJsonStore(tmp_path / "scan")
    )
    store = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(store, clock=lambda: 100, brain_root=tasks.profile.root)
    controller.enable(
        source_id="synthetic", selection=_adapter(selected).selection, interval_seconds=60
    )
    controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    before = controller.custody_status("synthetic")
    receipt_id = cast(list[str], before["receipt_ids"])[0]
    (selected / "saved.md").write_text("# new version cannot replace retained custody\nbody\n")
    controller.sync_now("synthetic")
    with pytest.raises(LiveSourceError, match="collector_custody_quota_exceeded"):
        controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    controller.disable("synthetic")
    assert controller.custody_status("synthetic") == before
    assert controller._custody.intake(receipt_id) == original
    with open_local_database_read_only(tasks.profile) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM managed_source_deliveries").fetchone()[0] == 1
        )
