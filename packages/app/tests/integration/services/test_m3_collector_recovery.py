"""Synthetic composed T09 proof through production collector sinks and owner CLI."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain.services.local_entrypoints import run_cli
from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorRunPage,
    CollectorStateStore,
    EngineCaptureSink,
)
from open_brain_collector.live_capture import LiveCaptureService
from open_brain_collector.runner import collector_capture_sink
from open_brain_connectors.runtime.live_common import (
    LiveBatch,
    LiveSourceError,
    local_source_privacy,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

_SELECTION = SourceResourceSelection("gmail", "synthetic", "label:INBOX", "mail_label")


def _intake(external: str, text: str) -> SourceRecordIntake:
    return SourceRecordIntake(
        SourceRecordKey("gmail", "synthetic", "label:INBOX", external, "r1"),
        "https://example.invalid/" + external,
        text,
        local_source_privacy(),
        title="Synthetic intake",
    )


class _Runtime:
    def __init__(self) -> None:
        self.intakes: tuple[SourceRecordIntake, ...] = ()
        self.cursor = "first"
        self.fetches = 0
        self.acknowledged: list[dict[str, object]] = []

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        assert selection == _SELECTION
        self.fetches += 1
        return LiveBatch(self.intakes, {"cursor": self.cursor})

    def acknowledge(
        self, selection: SourceResourceSelection, checkpoint: dict[str, object]
    ) -> None:
        assert selection == _SELECTION
        self.acknowledged.append(checkpoint)

    def fetch_page(
        self, selection: SourceResourceSelection, cursor: str | None
    ) -> CollectorRunPage:
        assert selection == _SELECTION
        self.fetches += 1
        return CollectorRunPage(selection, self.intakes, self.cursor)


@pytest.mark.parametrize("item_order", ["conflict_first", "independent_first"])
@pytest.mark.parametrize("collector_kind", ["live", "legacy"])
@pytest.mark.parametrize("change", ["new_revision", "same_key_body", "same_key_title"])
def test_real_conflicting_item_does_not_block_independent_capture(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    collector_kind: str,
    change: str,
    item_order: str,
) -> None:
    brain = tmp_path / "brain"
    profile = compile_single_user_local(brain)
    runtime = _Runtime()
    original = _intake("a", "retainedalphaneedle")
    runtime.intakes = (original,)
    now = [100]
    state = tmp_path / "collector" / "state.json"
    if collector_kind == "live":
        live = LiveCaptureService(state.parent / "live", brain, runtime=runtime)
        live.configure("synthetic", _SELECTION, {})

        def apply() -> dict[str, object]:
            preview = live.preview("synthetic")
            return live.apply("synthetic", cast(str, preview["preview_id"]))
    else:
        controller = CollectorController(
            CollectorStateStore(state), clock=lambda: now[0], brain_root=brain
        )
        controller.enable(source_id="synthetic", selection=_SELECTION, interval_seconds=1)
        sink = EngineCaptureSink(collector_capture_sink(brain))

        def apply() -> dict[str, object]:
            return controller.sync_due(
                source_id="synthetic", runtime=runtime, capture_sink=sink
            ).to_dict()

    assert apply()["captured_count"] == 1
    tasks = open_local_engine(profile)
    initial_hit = tasks.retrieval.search("retainedalphaneedle")[0]
    if change == "same_key_title":
        changed = replace(original, title="Changed synthetic title")
    else:
        changed = replace(original, text="conflictingbetaneedle")
        if change == "new_revision":
            changed = replace(changed, key=replace(changed.key, revision_id="r2"))
    independent = _intake("b", "independentgammanneedle")
    runtime.intakes = (
        (changed, independent) if item_order == "conflict_first" else (independent, changed)
    )
    runtime.cursor = "after-mixed"
    now[0] += 1
    result = apply()
    assert result["captured_count"] == 1
    assert result["quarantined_count"] == 1
    if collector_kind == "live":
        assert runtime.acknowledged[-1] == {"cursor": "after-mixed"}
    else:
        entry = json.loads(state.read_text())["sources"]["synthetic"]
        assert entry["next_cursor"] == "after-mixed"
    custody = (
        live.custody_status("synthetic")
        if collector_kind == "live"
        else controller.custody_status("synthetic")
    )
    assert cast(dict[str, int], custody["counts"])["quarantined"] == 1
    assert cast(dict[str, int], custody["counts"])["pending"] == 0
    receipt_ids = cast(list[str], custody["receipt_ids"])
    assert len(receipt_ids) == 1
    receipt_id = receipt_ids[0]
    fetches = runtime.fetches
    if collector_kind == "live":
        restarted_live = LiveCaptureService(state.parent / "live", brain, runtime=runtime)
        inspected = restarted_live.custody_inspect(receipt_id)
        retried = restarted_live.custody_retry(receipt_id)
    else:
        restarted_legacy = CollectorController(
            CollectorStateStore(state), clock=lambda: now[0], brain_root=brain
        )
        inspected = restarted_legacy.custody_inspect(receipt_id)
        retried = restarted_legacy.retry(
            receipt_id, EngineCaptureSink(collector_capture_sink(brain))
        )
    assert retried == inspected
    assert inspected["outcome"] == "quarantined"
    assert inspected["reason_code"] == "source_revision_conflict"
    assert runtime.fetches == fetches
    assert not any(
        word in json.dumps(inspected) for word in (original.text, changed.text, original.url)
    )
    reopened = open_local_engine(profile)
    assert reopened.retrieval.search("retainedalphaneedle")[0].capture_id == initial_hit.capture_id
    assert not reopened.retrieval.search("conflictingbetaneedle")
    assert run_cli(("search", "independentgammanneedle", "--data-dir", str(brain), "--json")) == 0
    public = json.loads(capsys.readouterr().out)
    assert len(public["results"]) == 1
    assert public["results"][0]["source_origin"] == "third_party"


def test_legacy_custody_cannot_be_replayed_into_a_different_brain(tmp_path: Path) -> None:
    first_brain, other_brain = tmp_path / "first", tmp_path / "other"
    compile_single_user_local(first_brain)
    other_profile = compile_single_user_local(other_brain)
    state = tmp_path / "collector" / "state.json"
    now = [100]
    controller = CollectorController(
        CollectorStateStore(state), clock=lambda: now[0], brain_root=first_brain
    )
    controller.enable(source_id="synthetic", selection=_SELECTION, interval_seconds=1)
    runtime = _Runtime()
    runtime.intakes = (_intake("a", "brainbindingneedle"),)
    controller.sync_due(
        source_id="synthetic",
        runtime=runtime,
        capture_sink=EngineCaptureSink(collector_capture_sink(first_brain)),
    )
    now[0] += 1
    with pytest.raises(LiveSourceError, match="source_brain_mismatch"):
        other_controller = CollectorController(
            CollectorStateStore(state), clock=lambda: now[0], brain_root=other_brain
        )
        other_controller.sync_due(
            source_id="synthetic",
            runtime=runtime,
            capture_sink=EngineCaptureSink(collector_capture_sink(other_brain)),
        )
    assert not open_local_engine(other_profile).retrieval.search("brainbindingneedle")


def test_legacy_pause_barrier_coordinates_separate_controller_instances(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    state = tmp_path / "collector" / "state.json"
    collector = CollectorController(CollectorStateStore(state), clock=lambda: 100, brain_root=brain)
    owner = CollectorController(CollectorStateStore(state), clock=lambda: 100, brain_root=brain)
    collector.enable(source_id="synthetic", selection=_SELECTION, interval_seconds=1)
    runtime = _Runtime()
    runtime.intakes = (_intake("a", "firstbarrierneedle"), _intake("b", "secondbarrierneedle"))
    production = EngineCaptureSink(collector_capture_sink(brain))
    entered, release, pause_started, pause_acked = (threading.Event() for _ in range(4))
    ordering: list[str] = []

    class SlowSink:
        def submit(self, intake: SourceRecordIntake) -> None:
            if intake.key.external_id == "a":
                entered.set()
                assert release.wait(5)
            production.submit(intake)
            ordering.append("captured")

    def pause() -> None:
        pause_started.set()
        owner.pause("synthetic")
        ordering.append("pause_ack")
        pause_acked.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(
            collector.sync_due, source_id="synthetic", runtime=runtime, capture_sink=SlowSink()
        )
        assert entered.wait(5)
        pausing = pool.submit(pause)
        assert pause_started.wait(5)
        try:
            assert not pause_acked.wait(0.15), (
                "pause acknowledged before in-flight capture finished"
            )
        finally:
            release.set()
        running.result(timeout=5)
        pausing.result(timeout=5)
    assert ordering[-1] == "pause_ack"
    assert owner.status("synthetic").status == "paused"


@pytest.mark.parametrize("collector_kind", ["live", "legacy"])
def test_mixed_custody_replays_after_checkpoint_save_failure_without_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collector_kind: str
) -> None:
    brain = tmp_path / "brain"
    profile = compile_single_user_local(brain)
    state = tmp_path / "collector" / "state.json"
    runtime = _Runtime()
    original = _intake("a", "retainedcheckpointneedle")
    runtime.intakes = (original,)
    now = [100]
    if collector_kind == "live":
        service = LiveCaptureService(state.parent / "live", brain, runtime=runtime)
        service.configure("synthetic", _SELECTION, {})
        initial = service.preview("synthetic")
        service.apply("synthetic", cast(str, initial["preview_id"]))
    else:
        store = CollectorStateStore(state)
        controller = CollectorController(store, clock=lambda: now[0], brain_root=brain)
        controller.enable(source_id="synthetic", selection=_SELECTION, interval_seconds=1)
        sink = EngineCaptureSink(collector_capture_sink(brain))
        controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
    runtime.intakes = (
        replace(original, key=replace(original.key, revision_id="r2"), text="heldcheckpointneedle"),
        _intake("b", "independentcheckpointneedle"),
    )
    runtime.cursor = "after-mixed"
    now[0] += 1
    if collector_kind == "live":
        pending = service.preview("synthetic")
        original_save = service._save

        def fail_checkpoint(entry: dict[str, object]) -> None:
            if entry["checkpoint"] == {"cursor": "after-mixed"}:
                raise OSError("synthetic checkpoint failure")
            original_save(entry)

        monkeypatch.setattr(service, "_save", fail_checkpoint)
        prior_acknowledgements = list(runtime.acknowledged)
        with pytest.raises(LiveSourceError, match="source_storage_unavailable"):
            service.apply("synthetic", cast(str, pending["preview_id"]))
        assert runtime.acknowledged == prior_acknowledgements
        fetches = runtime.fetches
        restarted = LiveCaptureService(state.parent / "live", brain, runtime=runtime)
        assert restarted.preview("synthetic")["preview_id"] == pending["preview_id"]
        recovered = restarted.apply("synthetic", cast(str, pending["preview_id"]))
        assert recovered["quarantined_count"] == 1
        assert runtime.acknowledged[-1] == {"cursor": "after-mixed"}
    else:
        original_store_save = store.save

        def fail_legacy_checkpoint(value: object) -> None:
            entry = cast(dict[str, object], value)["sources"]
            selected = cast(dict[str, dict[str, object]], entry)["synthetic"]
            if selected["next_cursor"] == "after-mixed":
                raise OSError("synthetic checkpoint failure")
            original_store_save(cast(dict[str, object], value))

        monkeypatch.setattr(store, "save", fail_legacy_checkpoint)
        with pytest.raises(LiveSourceError, match="source_storage_unavailable"):
            controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
        assert json.loads(state.read_text())["sources"]["synthetic"]["next_cursor"] == "first"
        fetches = runtime.fetches
        restarted_controller = CollectorController(
            CollectorStateStore(state), clock=lambda: now[0], brain_root=brain
        )
        result = restarted_controller.sync_due(
            source_id="synthetic", runtime=runtime, capture_sink=sink
        )
        assert result.quarantined_count == 1
        assert json.loads(state.read_text())["sources"]["synthetic"]["next_cursor"] == "after-mixed"
    assert runtime.fetches == fetches
    reopened = open_local_engine(profile)
    assert len(reopened.retrieval.search("independentcheckpointneedle")) == 1
    assert len(reopened.retrieval.search("retainedcheckpointneedle")) == 1
    assert not reopened.retrieval.search("heldcheckpointneedle")


@pytest.mark.parametrize("collector_kind", ["live", "legacy"])
def test_abrupt_staging_interruption_can_cancel_without_stranding_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, collector_kind: str
) -> None:
    from open_brain_collector import custody

    class SyntheticInterruption(BaseException):
        pass

    monkeypatch.setattr(custody, "MAX_RETAINED_ITEMS", 1)
    brain = tmp_path / "brain"
    profile = compile_single_user_local(brain)
    state = tmp_path / "collector" / "state.json"
    runtime = _Runtime()
    runtime.intakes = (_intake("a", "interruptedstagingneedle"),)
    if collector_kind == "live":
        service = LiveCaptureService(state.parent / "live", brain, runtime=runtime)
        service.configure("synthetic", _SELECTION, {})
        pending = service.preview("synthetic")
        original_stage = service._custody.stage
    else:
        controller = CollectorController(
            CollectorStateStore(state), clock=lambda: 100, brain_root=brain
        )
        controller.enable(source_id="synthetic", selection=_SELECTION, interval_seconds=1)
        sink = EngineCaptureSink(collector_capture_sink(brain))
        original_stage = controller._custody.stage

    def terminate_after_stage(
        *,
        source_id: str,
        binding: str,
        generation: str,
        control_epoch: int,
        intakes: tuple[SourceRecordIntake, ...],
    ) -> tuple[str, ...]:
        original_stage(
            source_id=source_id,
            binding=binding,
            generation=generation,
            control_epoch=control_epoch,
            intakes=intakes,
        )
        raise SyntheticInterruption

    if collector_kind == "live":
        monkeypatch.setattr(service._custody, "stage", terminate_after_stage)
        with pytest.raises(SyntheticInterruption):
            service.apply("synthetic", cast(str, pending["preview_id"]))
        restarted_live = LiveCaptureService(state.parent / "live", brain, runtime=runtime)
        restarted_live.control("synthetic", "pause")
        restarted_live.control("synthetic", "resume")
        assert restarted_live.custody_status("synthetic")["retained_items"] == 0
        runtime.intakes = (_intake("b", "freshafterstagingneedle"),)
        preview = restarted_live.preview("synthetic")
        assert (
            restarted_live.apply("synthetic", cast(str, preview["preview_id"]))["captured_count"]
            == 1
        )
    else:
        monkeypatch.setattr(controller._custody, "stage", terminate_after_stage)
        with pytest.raises(SyntheticInterruption):
            controller.sync_due(source_id="synthetic", runtime=runtime, capture_sink=sink)
        restarted_legacy = CollectorController(
            CollectorStateStore(state), clock=lambda: 100, brain_root=brain
        )
        restarted_legacy.pause("synthetic")
        restarted_legacy.resume("synthetic")
        assert restarted_legacy.custody_status("synthetic")["retained_items"] == 0
        runtime.intakes = (_intake("b", "freshafterstagingneedle"),)
        assert (
            restarted_legacy.sync_due(
                source_id="synthetic", runtime=runtime, capture_sink=sink
            ).captured_count
            == 1
        )
    tasks = open_local_engine(profile)
    assert not tasks.retrieval.search("interruptedstagingneedle")
    assert len(tasks.retrieval.search("freshafterstagingneedle")) == 1


@pytest.mark.parametrize("root_name", ["bråin", "bra\u030ain"])
def test_legacy_production_sink_matches_unicode_brain_binding(
    tmp_path: Path, root_name: str
) -> None:
    brain = tmp_path / root_name
    profile = compile_single_user_local(brain)
    controller = CollectorController(
        CollectorStateStore(tmp_path / "collector" / "state.json"),
        clock=lambda: 100,
        brain_root=brain,
    )
    controller.enable(source_id="synthetic", selection=_SELECTION, interval_seconds=1)
    runtime = _Runtime()
    runtime.intakes = (_intake("a", "unicodebrainbindingneedle"),)
    result = controller.sync_due(
        source_id="synthetic",
        runtime=runtime,
        capture_sink=EngineCaptureSink(collector_capture_sink(brain)),
    )
    assert result.captured_count == 1
    assert len(open_local_engine(profile).retrieval.search("unicodebrainbindingneedle")) == 1


def test_custody_cli_roundtrip_over_private_control_socket(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from open_brain_collector.control import control_endpoint, control_server
    from open_brain_collector.sources_cli import main as sources_main

    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    state = tmp_path / "collector" / "state.json"
    runtime = _Runtime()
    original = _intake("a", "privateipc_original_needle")
    runtime.intakes = (original,)
    capture = LiveCaptureService(state.parent / "live" / "capture", brain, runtime=runtime)
    capture.configure("synthetic", _SELECTION, {})
    first = capture.preview("synthetic")
    capture.apply("synthetic", cast(str, first["preview_id"]))
    runtime.intakes = (replace(original, text="privateipc_conflict_needle"),)
    second = capture.preview("synthetic")
    assert capture.apply("synthetic", cast(str, second["preview_id"]))["quarantined_count"] == 1
    fetches = runtime.fetches
    prefix = ["--state", str(state), "--brain-root", str(brain)]
    endpoint = control_endpoint(state, brain)
    with control_server(state, brain):
        assert endpoint.is_socket()
        assert endpoint.stat().st_mode & 0o777 == 0o600
        assert sources_main([*prefix, "custody-status", "--source-id", "synthetic"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["counts"]["quarantined"] == 1
        receipt_id = status["receipt_ids"][0]
        inspected = None
        for operation in ("custody-inspect", "custody-retry"):
            assert sources_main([*prefix, operation, "--receipt-id", receipt_id]) == 0
            raw = capsys.readouterr().out
            result = json.loads(raw)
            assert result["outcome"] == "quarantined"
            assert result["reason_code"] == "source_revision_conflict"
            assert not any(
                item in raw
                for item in (original.text, runtime.intakes[0].text, original.url, str(brain))
            )
            if inspected is None:
                inspected = result
            else:
                assert result == inspected
    assert runtime.fetches == fetches
    assert not endpoint.exists()
