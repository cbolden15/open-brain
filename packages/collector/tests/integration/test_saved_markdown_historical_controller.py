"""Historical receipt truth cannot bypass current collector page eligibility."""

from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine.historical_recovery import _historical_transaction
from open_brain_engine.engine.t03_contracts import T03Error
from open_brain_engine.storage.locks import FileLease, LockBusyError

from open_brain_collector.lifecycle import CollectorController, CollectorStateStore
from open_brain_collector.saved_markdown import SavedMarkdownCollectorRuntime
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from packages.collector.tests.integration.test_saved_markdown import _adapter
from packages.collector.tests.integration.test_saved_markdown_historical import _adopt_saved


def test_baseline_controller_checkpoints_without_new_capture_and_retains_custody(
    tmp_path: Path,
) -> None:
    engine, sink, _, baseline = _adopt_saved(tmp_path)
    adapter = _adapter(tmp_path / "selected")
    runtime = SavedMarkdownCollectorRuntime(adapter, store=PrivateJsonStore(tmp_path / "scan"))
    store = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(store, clock=lambda: 100, brain_root=engine.profile.root)
    controller.enable(source_id="synthetic", selection=adapter.selection, interval_seconds=60)
    result = controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    assert result.duplicate_count == 1 and result.captured_count == 0
    assert controller.custody_status("synthetic")["retained_items"] == 1
    entry = cast(dict[str, object], cast(dict[str, object], store.load()["sources"])["synthetic"])
    assert entry["active_run"] is None and entry["last_success_epoch"] == 100
    known = cast(dict[str, dict[str, object]], runtime._load()["known"])
    assert len(known) == 1
    assert next(iter(known.values()))["head"] == baseline.submission.expected_head
    with closing(engine._store.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM managed_source_deliveries").fetchone()[0] == 0
        )
    # Cancellation after acceptance cannot drop this unprotected baseline body.
    controller.disable("synthetic")
    assert controller.custody_status("synthetic")["retained_items"] == 1


def test_stale_baseline_between_item_lookup_and_page_commit_refuses_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sink, _, _ = _adopt_saved(tmp_path)
    adapter = _adapter(tmp_path / "selected")
    runtime = SavedMarkdownCollectorRuntime(adapter, store=PrivateJsonStore(tmp_path / "scan"))
    store = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(store, clock=lambda: 100, brain_root=engine.profile.root)
    controller.enable(source_id="synthetic", selection=adapter.selection, interval_seconds=60)
    lookup = sink.lookup_baseline
    calls = 0

    def lookup_and_advance(intake: object, *, selection_generation: str) -> object:
        nonlocal calls
        result = lookup(intake, selection_generation=selection_generation)  # type: ignore[arg-type]
        calls += 1
        if calls == 1:
            with _historical_transaction(engine.profile, lambda: None) as connection:
                connection.execute("UPDATE logical_sources SET route_version=route_version+1")
        return result

    monkeypatch.setattr(sink, "lookup_baseline", lookup_and_advance)
    with pytest.raises(T03Error, match="revision_changed"):
        controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    entry = cast(dict[str, object], cast(dict[str, object], store.load()["sources"])["synthetic"])
    assert entry["active_run"] is not None and entry["last_success_epoch"] is None
    assert runtime._load()["page"] is not None
    assert controller.custody_status("synthetic")["retained_items"] == 1


def test_runtime_acknowledgement_holds_canonical_writer_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sink, _, _ = _adopt_saved(tmp_path)
    adapter = _adapter(tmp_path / "selected")
    runtime = SavedMarkdownCollectorRuntime(adapter)
    controller = CollectorController(
        CollectorStateStore(tmp_path / "state.json"),
        clock=lambda: 100,
        brain_root=engine.profile.root,
    )
    controller.enable(source_id="synthetic", selection=adapter.selection, interval_seconds=60)
    contender = FileLease(
        engine.profile.root / ".open-brain",
        "synthetic-ack-contender",
        parent_root_identity=engine.profile.root_identity,
    )
    acknowledge = runtime.acknowledge_page
    reached = False

    def assert_fenced_ack() -> None:
        nonlocal reached
        reached = True
        with pytest.raises(LockBusyError), contender.acquire_shared_writer():
            pytest.fail("writer admitted during runtime acknowledgement")
        acknowledge()

    monkeypatch.setattr(runtime, "acknowledge_page", assert_fenced_ack)
    controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    assert reached
