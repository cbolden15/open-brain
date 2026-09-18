from __future__ import annotations

import threading
from pathlib import Path
from typing import cast

import pytest

from open_brain.profile import compile_single_user_local
from open_brain_collector import custody as custody_module
from open_brain_collector.cli import main as collector_cli
from open_brain_collector.custody import CustodyStore
from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorRunPage,
    CollectorStateStore,
    MemoryCaptureSink,
)
from open_brain_collector.live_capture import LiveCaptureService
from open_brain_collector.live_manager import LiveSourceManager
from open_brain_connectors.runtime import live_storage as live_storage_module
from open_brain_connectors.runtime.live_common import (
    LiveBatch,
    LiveSourceError,
    local_source_privacy,
)
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

_SELECTION = SourceResourceSelection("gmail", "account:test", "label:test", "mail_label")


def _intake(item: str, text: str = "synthetic") -> SourceRecordIntake:
    return SourceRecordIntake(
        SourceRecordKey("gmail", "account:test", "label:test", item, "r1"),
        f"https://example.test/{item}",
        text,
        local_source_privacy(),
        title=f"Synthetic {item}",
    )


class _Runtime:
    def __init__(self, intakes: tuple[SourceRecordIntake, ...]) -> None:
        self.intakes = intakes
        self.acknowledged: list[dict[str, object]] = []

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        return LiveBatch(self.intakes, {"cursor": "after"})

    def acknowledge(
        self, selection: SourceResourceSelection, checkpoint: dict[str, object]
    ) -> None:
        self.acknowledged.append(checkpoint)


def test_custody_quota_counts_serialized_utf8_and_never_evicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CustodyStore(PrivateJsonStore(tmp_path / "custody"))
    monkeypatch.setattr(custody_module, "MAX_RETAINED_ITEMS", 1)
    first = store.stage(
        source_id="source",
        binding="binding",
        generation="a" * 64,
        control_epoch=0,
        intakes=(_intake("one", "å"), _intake("one", "å")),
    )
    assert first[0] == first[1]
    assert store.status()["retained_items"] == 1
    assert cast(int, store.status()["retained_utf8_bytes"]) > len("å".encode())
    with pytest.raises(LiveSourceError, match="collector_custody_quota_exceeded"):
        store.stage(
            source_id="source",
            binding="binding",
            generation="a" * 64,
            control_epoch=0,
            intakes=(_intake("two"),),
        )
    assert store.status()["receipt_ids"] == [first[0]]
    byte_store = CustodyStore(PrivateJsonStore(tmp_path / "byte-custody"))
    monkeypatch.setattr(custody_module, "MAX_RETAINED_ITEMS", 256)
    monkeypatch.setattr(custody_module, "MAX_RETAINED_UTF8_BYTES", 1)
    with pytest.raises(LiveSourceError, match="collector_custody_quota_exceeded"):
        byte_store.stage(
            source_id="source",
            binding="binding",
            generation="a" * 64,
            control_epoch=0,
            intakes=(_intake("unicode", "å"),),
        )
    assert byte_store.status()["retained_items"] == 0


def test_custody_reserves_terminal_metadata_at_exact_byte_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe_private = PrivateJsonStore(tmp_path / "probe")
    probe = CustodyStore(probe_private)
    probe.stage(
        source_id="source",
        binding="binding",
        generation="a" * 64,
        control_epoch=0,
        intakes=(_intake("exact", "å"),),
    )
    state = cast(dict[str, object], probe_private.read("custody.json"))
    exact = custody_module._reserved_size(cast(dict[str, object], state["receipts"]))
    monkeypatch.setattr(custody_module, "MAX_RETAINED_UTF8_BYTES", exact)
    exact_store = CustodyStore(PrivateJsonStore(tmp_path / "exact"))
    receipt_id = exact_store.stage(
        source_id="source",
        binding="binding",
        generation="a" * 64,
        control_epoch=0,
        intakes=(_intake("exact", "å"),),
    )[0]
    exact_store.outcome(
        receipt_id,
        "quarantined",
        reason_code="source_revision_conflict",
    )
    assert exact_store.inspect(receipt_id)["outcome"] == "quarantined"
    monkeypatch.setattr(custody_module, "MAX_RETAINED_UTF8_BYTES", exact - 1)
    with pytest.raises(LiveSourceError, match="collector_custody_quota_exceeded"):
        CustodyStore(PrivateJsonStore(tmp_path / "over")).stage(
            source_id="source",
            binding="binding",
            generation="a" * 64,
            control_epoch=0,
            intakes=(_intake("exact", "å"),),
        )


@pytest.mark.parametrize(
    ("seam", "durable"),
    (("file_fsync", False), ("replace", False), ("directory_fsync", True)),
)
def test_custody_atomic_write_failure_seams_are_restart_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seam: str,
    durable: bool,
) -> None:
    original_fsync = live_storage_module.os.fsync
    original_replace = live_storage_module.os.replace
    calls = 0

    def fail_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if seam == "file_fsync" or (seam == "directory_fsync" and calls == 2):
            raise OSError("synthetic fsync interruption")
        original_fsync(fd)

    def fail_replace(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic replace interruption")

    if seam == "replace":
        monkeypatch.setattr(live_storage_module.os, "replace", fail_replace)
    else:
        monkeypatch.setattr(live_storage_module.os, "fsync", fail_fsync)
    store = CustodyStore(PrivateJsonStore(tmp_path / "custody"))
    with pytest.raises(LiveSourceError, match="source_storage_unavailable"):
        store.stage(
            source_id="source",
            binding="binding",
            generation="a" * 64,
            control_epoch=0,
            intakes=(_intake("atomic"),),
        )
    monkeypatch.setattr(live_storage_module.os, "fsync", original_fsync)
    monkeypatch.setattr(live_storage_module.os, "replace", original_replace)
    restarted = CustodyStore(PrivateJsonStore(tmp_path / "custody"))
    assert restarted.status()["retained_items"] == int(durable)


def test_live_manager_custody_status_inspect_and_retry_journey(tmp_path: Path) -> None:
    from open_brain_engine.engine import DeliveryConflict

    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("a"),))
    service = LiveCaptureService(
        tmp_path / "capture",
        brain,
        runtime=runtime,
        sink=lambda _: (_ for _ in ()).throw(DeliveryConflict()),
    )
    service.configure("source", _SELECTION, {})
    preview = service.preview("source")
    service.apply("source", cast(str, preview["preview_id"]))
    manager = LiveSourceManager(tmp_path / "manager", brain)
    manager.capture = service
    status = manager.dispatch("sources.custody_status", {"source_id": "source"})
    receipt_id = cast(list[str], status["receipt_ids"])[0]
    assert (
        manager.dispatch("sources.custody_inspect", {"receipt_id": receipt_id})["outcome"]
        == "quarantined"
    )
    service._sink = lambda _: None
    retried = manager.dispatch("sources.custody_retry", {"receipt_id": receipt_id})
    assert retried["outcome"] == "captured"


def test_concurrent_custody_admission_obeys_one_global_item_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(custody_module, "MAX_RETAINED_ITEMS", 1)
    stores = [
        CustodyStore(PrivateJsonStore(tmp_path / "custody")),
        CustodyStore(PrivateJsonStore(tmp_path / "custody")),
    ]
    start = threading.Barrier(2)
    outcomes: list[str] = []

    def admit(index: int) -> None:
        start.wait()
        try:
            stores[index].stage(
                source_id=f"source-{index}",
                binding="binding",
                generation="a" * 64,
                control_epoch=0,
                intakes=(_intake(str(index)),),
            )
        except LiveSourceError as error:
            outcomes.append(error.code)
        else:
            outcomes.append("accepted")

    threads = [threading.Thread(target=admit, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert sorted(outcomes) == ["accepted", "collector_custody_quota_exceeded"]
    assert stores[0].status()["retained_items"] == 1


def test_resolved_receipt_retention_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(custody_module, "MAX_RESOLVED_RECEIPTS", 2)
    store = CustodyStore(PrivateJsonStore(tmp_path / "custody"))
    for index in range(5):
        receipt_id = store.stage(
            source_id="source",
            binding="binding",
            generation="a" * 64,
            control_epoch=0,
            intakes=(_intake(str(index)),),
        )[0]
        store.outcome(receipt_id, "captured")
        store.release_completed((receipt_id,))
    assert store.status()["retained_items"] == 0
    state = cast(dict[str, object], PrivateJsonStore(tmp_path / "custody").read("custody.json"))
    assert len(cast(dict[str, object], state["receipts"])) == 2


def test_resolved_retention_never_prunes_cleanup_protected_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(custody_module, "MAX_RESOLVED_RECEIPTS", 1)
    store = CustodyStore(PrivateJsonStore(tmp_path / "custody"))
    protected = store.stage(
        source_id="source",
        binding="binding",
        generation="a" * 64,
        control_epoch=0,
        intakes=(_intake("protected"),),
    )[0]
    store.outcome(protected, "captured")
    store.release_completed((protected,), protected_ids=frozenset({protected}))
    for item in ("other", "latest"):
        receipt_id = store.stage(
            source_id="source",
            binding="binding",
            generation="a" * 64,
            control_epoch=0,
            intakes=(_intake(item),),
        )[0]
        store.outcome(receipt_id, "captured")
        store.release_completed((receipt_id,), protected_ids=frozenset({protected, receipt_id}))
    assert store.receipt(protected)["outcome"] == "captured"


def test_generic_value_error_is_global_and_does_not_quarantine_or_checkpoint(
    tmp_path: Path,
) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("a"), _intake("b")))

    def sink(intake: SourceRecordIntake) -> None:
        raise ValueError("synthetic global failure")

    service = LiveCaptureService(tmp_path / "state", brain, runtime=runtime, sink=sink)
    service.configure("source", _SELECTION, {})
    preview = service.preview("source")
    with pytest.raises(ValueError, match="synthetic global failure"):
        service.apply("source", cast(str, preview["preview_id"]))
    status = service.custody_status("source")
    assert status["counts"]["pending"] == 2
    assert status["counts"]["quarantined"] == 0
    assert runtime.acknowledged == []
    assert service._load("source")["checkpoint"] is None


def test_quarantine_retry_is_fenced_after_disable(tmp_path: Path) -> None:
    from open_brain_engine.engine import DeliveryConflict

    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("a"),))

    def conflict(intake: SourceRecordIntake) -> None:
        raise DeliveryConflict()

    service = LiveCaptureService(tmp_path / "state", brain, runtime=runtime, sink=conflict)
    service.configure("source", _SELECTION, {})
    preview = service.preview("source")
    service.apply("source", cast(str, preview["preview_id"]))
    receipt_id = cast(list[str], service.custody_status("source")["receipt_ids"])[0]
    service.control("source", "disable")
    with pytest.raises(LiveSourceError, match="collector_custody_stale"):
        service.custody_retry(receipt_id)
    assert service.custody_inspect(receipt_id)["outcome"] == "quarantined"


def test_live_retry_release_failure_leaves_durable_cleanup_obligation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import DeliveryConflict

    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("a"),))
    service = LiveCaptureService(
        tmp_path / "state",
        brain,
        runtime=runtime,
        sink=lambda _: (_ for _ in ()).throw(DeliveryConflict()),
    )
    service.configure("source", _SELECTION, {})
    preview = service.preview("source")
    service.apply("source", cast(str, preview["preview_id"]))
    receipt_id = cast(list[str], service.custody_status("source")["receipt_ids"])[0]
    service._sink = lambda _: None

    def fail_release(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic cleanup interruption")

    monkeypatch.setattr(service._custody, "release_completed", fail_release)
    with pytest.raises(LiveSourceError, match="source_storage_unavailable"):
        service.custody_retry(receipt_id)
    assert service._load("source")["retry_receipts"] == [receipt_id]
    restarted = LiveCaptureService(tmp_path / "state", brain, runtime=runtime, sink=lambda _: None)
    restarted._drain_cleanup("source")
    assert restarted.custody_status("source")["retained_items"] == 0


@pytest.mark.parametrize("interruption", ["marker", "before_outcome", "after_outcome"])
def test_live_retry_intent_recovers_every_persistence_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    from open_brain_engine.engine import DeliveryConflict

    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("a"),))
    service = LiveCaptureService(
        tmp_path / "state",
        brain,
        runtime=runtime,
        sink=lambda _: (_ for _ in ()).throw(DeliveryConflict()),
    )
    service.configure("source", _SELECTION, {})
    preview = service.preview("source")
    service.apply("source", cast(str, preview["preview_id"]))
    receipt_id = cast(list[str], service.custody_status("source")["receipt_ids"])[0]
    submitted: list[str] = []
    service._sink = lambda intake: submitted.append(intake.key.external_id)
    original_save = service._save
    original_outcome = service._custody.outcome

    if interruption == "marker":
        monkeypatch.setattr(
            service,
            "_save",
            lambda entry: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
    else:

        def interrupt_outcome(*args: object, **kwargs: object) -> None:
            if interruption == "after_outcome":
                original_outcome(*args, **kwargs)  # type: ignore[arg-type]
            raise KeyboardInterrupt

        monkeypatch.setattr(service._custody, "outcome", interrupt_outcome)
    with pytest.raises(KeyboardInterrupt):
        service.custody_retry(receipt_id)
    monkeypatch.setattr(service, "_save", original_save)
    monkeypatch.setattr(service._custody, "outcome", original_outcome)
    restarted = LiveCaptureService(tmp_path / "state", brain, runtime=runtime, sink=lambda _: None)
    restarted._drain_cleanup("source")
    inspected = restarted.custody_inspect(receipt_id)
    if interruption == "after_outcome":
        assert inspected["outcome"] == "captured"
        assert restarted.custody_status("source")["retained_items"] == 0
    else:
        assert inspected["outcome"] == "quarantined"
        assert restarted.custody_status("source")["retained_items"] == 1
    assert submitted == ([] if interruption == "marker" else ["a"])


def test_live_retry_releases_with_unrelated_pending_preview(tmp_path: Path) -> None:
    from open_brain_engine.engine import DeliveryConflict

    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("a"),))
    service = LiveCaptureService(
        tmp_path / "state",
        brain,
        runtime=runtime,
        sink=lambda _: (_ for _ in ()).throw(DeliveryConflict()),
    )
    service.configure("source", _SELECTION, {})
    preview_a = service.preview("source")
    service.apply("source", cast(str, preview_a["preview_id"]))
    receipt_id = cast(list[str], service.custody_status("source")["receipt_ids"])[0]
    runtime.intakes = (_intake("b"),)
    preview_b = service.preview("source")
    service._sink = lambda _: None
    assert service.custody_retry(receipt_id)["outcome"] == "captured"
    assert service.custody_status("source")["retained_items"] == 0
    service.apply("source", cast(str, preview_b["preview_id"]))
    assert service.custody_status("source")["retained_items"] == 0


@pytest.mark.parametrize("finish", ["apply", "pause", "reset"])
def test_live_retry_retains_exact_pending_preview_until_finished_or_cancelled(
    tmp_path: Path, finish: str
) -> None:
    from open_brain_engine.engine import DeliveryConflict

    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("a"),))
    service = LiveCaptureService(
        tmp_path / "state",
        brain,
        runtime=runtime,
        sink=lambda _: (_ for _ in ()).throw(DeliveryConflict()),
    )
    service.configure("source", _SELECTION, {})
    preview_a = service.preview("source")
    service.apply("source", cast(str, preview_a["preview_id"]))
    receipt_id = cast(list[str], service.custody_status("source")["receipt_ids"])[0]
    pending = service.preview("source")
    service._sink = lambda _: None
    service.custody_retry(receipt_id)
    assert service.custody_status("source")["retained_items"] == 1
    if finish == "apply":
        service.apply("source", cast(str, pending["preview_id"]))
    elif finish == "pause":
        service.control("source", "pause")
    else:
        replacement = SourceResourceSelection(
            "gmail", "account:test", "label:replacement", "mail_label"
        )
        service.configure("source", replacement, {}, reset=True)
    assert service.custody_status("source")["retained_items"] == 0


def test_live_cancel_resolves_terminal_retry_intent_overlapping_active_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import DeliveryConflict

    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("a"), _intake("b")))

    def mixed(intake: SourceRecordIntake) -> None:
        if intake.key.external_id == "a":
            raise DeliveryConflict()
        raise RuntimeError("synthetic global failure")

    service = LiveCaptureService(tmp_path / "state", brain, runtime=runtime, sink=mixed)
    service.configure("source", _SELECTION, {})
    preview = service.preview("source")
    with pytest.raises(RuntimeError, match="synthetic global failure"):
        service.apply("source", cast(str, preview["preview_id"]))
    receipt_id = next(
        item
        for item in cast(list[str], service.custody_status("source")["receipt_ids"])
        if service.custody_inspect(item)["outcome"] == "quarantined"
    )
    service._sink = lambda _: None
    original = service._custody.outcome

    def crash_after_outcome(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    monkeypatch.setattr(service._custody, "outcome", crash_after_outcome)
    with pytest.raises(KeyboardInterrupt):
        service.custody_retry(receipt_id)
    restarted = LiveCaptureService(tmp_path / "state", brain, runtime=runtime, sink=lambda _: None)
    restarted.control("source", "pause")
    assert restarted.custody_status("source")["retained_items"] == 0


def test_receipt_write_failure_keeps_checkpoint_and_ack_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("a"), _intake("b")))
    submitted: list[str] = []

    def sink(intake: SourceRecordIntake) -> None:
        submitted.append(intake.key.external_id)

    service = LiveCaptureService(tmp_path / "state", brain, runtime=runtime, sink=sink)
    service.configure("source", _SELECTION, {})
    preview = service.preview("source")

    def fail_outcome(*args: object, **kwargs: object) -> None:
        raise LiveSourceError("source_storage_unavailable")

    monkeypatch.setattr(service._custody, "outcome", fail_outcome)
    with pytest.raises(LiveSourceError, match="source_storage_unavailable"):
        service.apply("source", cast(str, preview["preview_id"]))
    assert submitted == ["a"]
    assert runtime.acknowledged == []
    assert service._load("source")["checkpoint"] is None


def test_quarantine_before_checkpoint_failure_replays_without_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import DeliveryConflict

    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("a"), _intake("b")))
    submissions: list[str] = []

    def sink(intake: SourceRecordIntake) -> None:
        submissions.append(intake.key.external_id)
        if intake.key.external_id == "a":
            raise DeliveryConflict()

    service = LiveCaptureService(tmp_path / "state", brain, runtime=runtime, sink=sink)
    service.configure("source", _SELECTION, {})
    preview = service.preview("source")
    original = service._custody.validate_terminal
    attempts = 0

    def fail_once(receipt_ids: tuple[str, ...]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LiveSourceError("source_storage_unavailable")
        original(receipt_ids)

    monkeypatch.setattr(service._custody, "validate_terminal", fail_once)
    with pytest.raises(LiveSourceError, match="source_storage_unavailable"):
        service.apply("source", cast(str, preview["preview_id"]))
    assert service._load("source")["checkpoint"] is None
    result = service.apply("source", cast(str, preview["preview_id"]))
    assert result["quarantined_count"] == 1
    assert submissions == ["a", "b"]
    assert service._load("source")["checkpoint"] == {"cursor": "after"}


def test_mixed_batch_ack_failure_retries_without_recapture(tmp_path: Path) -> None:
    from open_brain_engine.engine import DeliveryConflict

    class FailingAckRuntime(_Runtime):
        fail = True

        def acknowledge(
            self, selection: SourceResourceSelection, checkpoint: dict[str, object]
        ) -> None:
            if self.fail:
                raise LiveSourceError("collector_lost_response")
            super().acknowledge(selection, checkpoint)

    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = FailingAckRuntime((_intake("a"), _intake("b")))
    submissions: list[str] = []

    def sink(intake: SourceRecordIntake) -> None:
        submissions.append(intake.key.external_id)
        if intake.key.external_id == "a":
            raise DeliveryConflict()

    service = LiveCaptureService(tmp_path / "state", brain, runtime=runtime, sink=sink)
    service.configure("source", _SELECTION, {})
    preview = service.preview("source")
    result = service.apply("source", cast(str, preview["preview_id"]))
    assert "source_queue_cleanup_pending" in result["notices"]
    runtime.fail = False
    service.apply("source", cast(str, preview["preview_id"]))
    assert submissions == ["a", "b"]
    assert runtime.acknowledged == [{"cursor": "after"}]


def test_corrupt_custody_blocks_checkpoint(tmp_path: Path) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("a"),))
    service = LiveCaptureService(tmp_path / "state", brain, runtime=runtime, sink=lambda _: None)
    service.configure("source", _SELECTION, {})
    preview = service.preview("source")
    service._custody.stage(
        source_id="source",
        binding=service._brain_binding,
        generation=cast(str, service._load("source")["generation"]),
        control_epoch=0,
        intakes=runtime.intakes,
    )
    value = cast(dict[str, object], service._store.read("custody.json"))
    receipts = cast(dict[str, object], value["receipts"])
    receipt = cast(dict[str, object], next(iter(receipts.values())))
    cast(dict[str, object], receipt["intake"])["text"] = "tampered"
    service._store.write("custody.json", value)
    with pytest.raises(LiveSourceError, match="collector_invalid_custody"):
        service.apply("source", cast(str, preview["preview_id"]))
    assert service._load("source")["checkpoint"] is None


def test_legacy_restart_replays_staged_payload_without_fetch(tmp_path: Path) -> None:
    class Runtime:
        calls = 0

        def fetch_page(
            self, selection: SourceResourceSelection, cursor: str | None
        ) -> CollectorRunPage:
            self.calls += 1
            return CollectorRunPage(selection, (_intake("a"),), "after")

    class CrashSink:
        def submit(self, intake: SourceRecordIntake) -> None:
            raise RuntimeError("synthetic crash")

    state = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(state, clock=lambda: 100)
    controller.enable(source_id="source", selection=_SELECTION, interval_seconds=1)
    runtime = Runtime()
    with pytest.raises(RuntimeError, match="synthetic crash"):
        controller.sync_due(source_id="source", runtime=runtime, capture_sink=CrashSink())

    class NoFetch(Runtime):
        def fetch_page(
            self, selection: SourceResourceSelection, cursor: str | None
        ) -> CollectorRunPage:
            raise AssertionError("provider refetch")

    result = CollectorController(state, clock=lambda: 100).sync_due(
        source_id="source", runtime=NoFetch(), capture_sink=MemoryCaptureSink()
    )
    assert result.captured_count == 1
    assert runtime.calls == 1


def test_legacy_custody_cli_returns_safe_unknown_receipt_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = collector_cli(
        (
            "source",
            "--state",
            str(tmp_path / "state.json"),
            "custody-inspect",
            "--receipt-id",
            "custody:" + "0" * 64,
        )
    )
    assert code == 78
    output = capsys.readouterr().out
    assert "collector_custody_not_found" in output
    assert str(tmp_path) not in output


def test_legacy_custody_cli_rejects_malformed_receipt_safely(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = collector_cli(
        (
            "source",
            "--state",
            str(tmp_path / "state.json"),
            "custody-inspect",
            "--receipt-id",
            "private/raw/upstream/key",
        )
    )
    assert code == 78
    output = capsys.readouterr().out
    assert "collector_invalid_custody_id" in output
    assert "private/raw/upstream/key" not in output


def test_legacy_custody_cli_redacts_unexpected_retry_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)

    def fail_retry(*args: object, **kwargs: object) -> dict[str, object]:
        raise ValueError("private synthetic sink detail")

    monkeypatch.setattr(CollectorController, "retry", fail_retry)
    code = collector_cli(
        (
            "source",
            "--state",
            str(tmp_path / "state.json"),
            "--brain-root",
            str(brain),
            "custody-retry",
            "--receipt-id",
            "custody:" + "0" * 64,
        )
    )
    assert code == 78
    output = capsys.readouterr().out
    assert "source_operation_failed" in output
    assert "private synthetic sink detail" not in output


def test_retry_keeps_payload_referenced_by_unfinished_legacy_batch(tmp_path: Path) -> None:
    from open_brain_engine.engine import DeliveryConflict

    class Runtime:
        calls = 0

        def fetch_page(
            self, selection: SourceResourceSelection, cursor: str | None
        ) -> CollectorRunPage:
            self.calls += 1
            return CollectorRunPage(selection, (_intake("a"), _intake("b")), "after")

    class MixedFailure:
        def submit(self, intake: SourceRecordIntake) -> None:
            if intake.key.external_id == "a":
                raise DeliveryConflict()
            raise RuntimeError("synthetic global failure")

    state = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(state, clock=lambda: 100)
    controller.enable(source_id="source", selection=_SELECTION, interval_seconds=1)
    runtime = Runtime()
    with pytest.raises(RuntimeError, match="synthetic global failure"):
        controller.sync_due(source_id="source", runtime=runtime, capture_sink=MixedFailure())
    receipt_id = next(
        item
        for item in cast(list[str], controller.custody_status("source")["receipt_ids"])
        if controller.custody_inspect(item)["outcome"] == "quarantined"
    )
    controller.retry(receipt_id, MemoryCaptureSink())
    assert controller.custody_inspect(receipt_id)["outcome"] == "captured"

    class NoFetch(Runtime):
        def fetch_page(
            self, selection: SourceResourceSelection, cursor: str | None
        ) -> CollectorRunPage:
            raise AssertionError("provider refetch")

    result = controller.sync_due(
        source_id="source", runtime=NoFetch(), capture_sink=MemoryCaptureSink()
    )
    assert result.duplicate_count == 1 and result.captured_count == 1
    assert controller.status("source").next_cursor == "after"


def test_legacy_retry_release_failure_leaves_durable_cleanup_obligation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import DeliveryConflict

    class Conflict:
        def submit(self, intake: SourceRecordIntake) -> None:
            raise DeliveryConflict()

    runtime = type(
        "Runtime",
        (),
        {
            "fetch_page": lambda self, selection, cursor: CollectorRunPage(
                selection, (_intake("a"),), "after"
            )
        },
    )()
    state = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(state, clock=lambda: 100)
    controller.enable(source_id="source", selection=_SELECTION, interval_seconds=1)
    controller.sync_due(source_id="source", runtime=runtime, capture_sink=Conflict())
    receipt_id = cast(list[str], controller.custody_status("source")["receipt_ids"])[0]

    def fail_release(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic cleanup interruption")

    monkeypatch.setattr(controller._custody, "release_completed", fail_release)
    with pytest.raises(LiveSourceError, match="source_storage_unavailable"):
        controller.retry(receipt_id, MemoryCaptureSink())
    source = cast(dict[str, object], state.load()["sources"])["source"]
    assert cast(dict[str, object], source)["retry_receipts"] == [receipt_id]
    restarted = CollectorController(state, clock=lambda: 100)
    restarted._drain_cleanup("source")
    assert restarted.custody_status("source")["retained_items"] == 0


@pytest.mark.parametrize("interruption", ["marker", "before_outcome", "after_outcome"])
def test_legacy_retry_intent_recovers_every_persistence_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    from open_brain_engine.engine import DeliveryConflict

    class Conflict:
        def submit(self, intake: SourceRecordIntake) -> None:
            raise DeliveryConflict()

    runtime = type(
        "Runtime",
        (),
        {
            "fetch_page": lambda self, selection, cursor: CollectorRunPage(
                selection, (_intake("a"),), "after"
            )
        },
    )()
    state = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(state, clock=lambda: 100)
    controller.enable(source_id="source", selection=_SELECTION, interval_seconds=1)
    controller.sync_due(source_id="source", runtime=runtime, capture_sink=Conflict())
    receipt_id = cast(list[str], controller.custody_status("source")["receipt_ids"])[0]
    submitted: list[str] = []
    sink = type(
        "Sink",
        (),
        {"submit": lambda self, intake: submitted.append(intake.key.external_id)},
    )()
    original_save = state.save
    original_outcome = controller._custody.outcome
    if interruption == "marker":
        monkeypatch.setattr(
            state,
            "save",
            lambda value: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
    else:

        def interrupt_outcome(*args: object, **kwargs: object) -> None:
            if interruption == "after_outcome":
                original_outcome(*args, **kwargs)  # type: ignore[arg-type]
            raise KeyboardInterrupt

        monkeypatch.setattr(controller._custody, "outcome", interrupt_outcome)
    with pytest.raises(KeyboardInterrupt):
        controller.retry(receipt_id, sink)
    monkeypatch.setattr(state, "save", original_save)
    monkeypatch.setattr(controller._custody, "outcome", original_outcome)
    restarted = CollectorController(state, clock=lambda: 100)
    restarted._drain_cleanup("source")
    inspected = restarted.custody_inspect(receipt_id)
    if interruption == "after_outcome":
        assert inspected["outcome"] == "captured"
        assert restarted.custody_status("source")["retained_items"] == 0
    else:
        assert inspected["outcome"] == "quarantined"
        assert restarted.custody_status("source")["retained_items"] == 1
    assert submitted == ([] if interruption == "marker" else ["a"])


def test_legacy_cancel_resolves_terminal_retry_intent_overlapping_active_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import DeliveryConflict

    class Runtime:
        def fetch_page(
            self, selection: SourceResourceSelection, cursor: str | None
        ) -> CollectorRunPage:
            return CollectorRunPage(selection, (_intake("a"), _intake("b")), "after")

    class Mixed:
        def submit(self, intake: SourceRecordIntake) -> None:
            if intake.key.external_id == "a":
                raise DeliveryConflict()
            raise RuntimeError("synthetic global failure")

    state = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(state, clock=lambda: 100)
    controller.enable(source_id="source", selection=_SELECTION, interval_seconds=1)
    with pytest.raises(RuntimeError, match="synthetic global failure"):
        controller.sync_due(source_id="source", runtime=Runtime(), capture_sink=Mixed())
    receipt_id = next(
        item
        for item in cast(list[str], controller.custody_status("source")["receipt_ids"])
        if controller.custody_inspect(item)["outcome"] == "quarantined"
    )
    original = controller._custody.outcome

    def crash_after_outcome(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    monkeypatch.setattr(controller._custody, "outcome", crash_after_outcome)
    with pytest.raises(KeyboardInterrupt):
        controller.retry(receipt_id, MemoryCaptureSink())
    restarted = CollectorController(state, clock=lambda: 100)
    restarted.pause("source")
    assert restarted.custody_status("source")["retained_items"] == 0


def test_pause_resume_detaches_failed_batch_and_refetches_old_checkpoint(tmp_path: Path) -> None:
    class Runtime:
        calls = 0

        def fetch_page(
            self, selection: SourceResourceSelection, cursor: str | None
        ) -> CollectorRunPage:
            assert cursor is None
            self.calls += 1
            return CollectorRunPage(selection, (_intake("a"),), "after")

    class Fail:
        def submit(self, intake: SourceRecordIntake) -> None:
            raise RuntimeError("synthetic failure")

    state = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(state, clock=lambda: 100)
    controller.enable(source_id="source", selection=_SELECTION, interval_seconds=1)
    runtime = Runtime()
    with pytest.raises(RuntimeError):
        controller.sync_due(source_id="source", runtime=runtime, capture_sink=Fail())
    controller.pause("source")
    controller.resume("source")
    result = controller.sync_due(
        source_id="source", runtime=runtime, capture_sink=MemoryCaptureSink()
    )
    assert result.captured_count == 1 and runtime.calls == 2


def test_repeated_cancelled_batches_do_not_consume_tiny_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(custody_module, "MAX_RETAINED_ITEMS", 1)

    class Runtime:
        calls = 0

        def fetch_page(
            self, selection: SourceResourceSelection, cursor: str | None
        ) -> CollectorRunPage:
            self.calls += 1
            return CollectorRunPage(selection, (_intake(str(self.calls)),), "after")

    class Fail:
        def submit(self, intake: SourceRecordIntake) -> None:
            raise RuntimeError("synthetic failure")

    state = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(state, clock=lambda: 100)
    controller.enable(source_id="source", selection=_SELECTION, interval_seconds=1)
    runtime = Runtime()
    for _ in range(3):
        with pytest.raises(RuntimeError, match="synthetic failure"):
            controller.sync_due(source_id="source", runtime=runtime, capture_sink=Fail())
        controller.pause("source")
        assert controller.custody_status("source")["retained_items"] == 0
        controller.resume("source")
    assert runtime.calls == 3


def test_legacy_stage_crash_marker_discards_orphan_before_refetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Runtime:
        calls = 0

        def fetch_page(
            self, selection: SourceResourceSelection, cursor: str | None
        ) -> CollectorRunPage:
            self.calls += 1
            return CollectorRunPage(selection, (_intake("crash"),), "after")

    state = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(state, clock=lambda: 100)
    controller.enable(source_id="source", selection=_SELECTION, interval_seconds=1)
    runtime = Runtime()
    original = controller._custody.stage

    def crash_after_stage(**kwargs: object) -> tuple[str, ...]:
        original(**kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    monkeypatch.setattr(controller._custody, "stage", crash_after_stage)
    with pytest.raises(KeyboardInterrupt):
        controller.sync_due(source_id="source", runtime=runtime, capture_sink=MemoryCaptureSink())
    assert controller.custody_status("source")["retained_items"] == 1
    restarted = CollectorController(state, clock=lambda: 100)
    result = restarted.sync_due(
        source_id="source", runtime=runtime, capture_sink=MemoryCaptureSink()
    )
    assert result.captured_count == 1
    assert runtime.calls == 2


def test_live_stage_crash_marker_discards_orphan_before_reapply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("crash"),))
    service = LiveCaptureService(tmp_path / "state", brain, runtime=runtime, sink=lambda _: None)
    service.configure("source", _SELECTION, {})
    preview = service.preview("source")
    original = service._custody.stage

    def crash_after_stage(**kwargs: object) -> tuple[str, ...]:
        original(**kwargs)  # type: ignore[arg-type]
        raise KeyboardInterrupt

    monkeypatch.setattr(service._custody, "stage", crash_after_stage)
    with pytest.raises(KeyboardInterrupt):
        service.apply("source", cast(str, preview["preview_id"]))
    assert service.custody_status("source")["retained_items"] == 1
    restarted = LiveCaptureService(tmp_path / "state", brain, runtime=runtime, sink=lambda _: None)
    result = restarted.apply("source", cast(str, preview["preview_id"]))
    assert result["captured_count"] == 1


def test_legacy_brain_binding_is_coherent_and_checks_actual_sink(tmp_path: Path) -> None:
    from open_brain_collector.lifecycle import EngineCaptureSink
    from open_brain_collector.runner import collector_capture_sink

    first = tmp_path / "first"
    other = tmp_path / "other"
    compile_single_user_local(first)
    compile_single_user_local(other)
    state = CollectorStateStore(tmp_path / "state.json")
    runtime = type(
        "EmptyRuntime",
        (),
        {"fetch_page": lambda self, selection, cursor: CollectorRunPage(selection, (), None)},
    )()
    implicit = CollectorController(state, clock=lambda: 100)
    implicit.enable(source_id="source", selection=_SELECTION, interval_seconds=1)
    implicit.sync_due(
        source_id="source",
        runtime=runtime,
        capture_sink=EngineCaptureSink(collector_capture_sink(first)),
    )
    explicit = CollectorController(state, clock=lambda: 101, brain_root=first)
    explicit.schedule("source", 1)
    with pytest.raises(LiveSourceError, match="source_brain_mismatch"):
        explicit.sync_due(
            source_id="source",
            runtime=runtime,
            capture_sink=EngineCaptureSink(collector_capture_sink(other)),
        )


def test_live_checkpoint_cleanup_obligation_drains_before_new_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    runtime = _Runtime((_intake("same"),))
    service = LiveCaptureService(tmp_path / "live", brain, runtime=runtime, sink=lambda _: None)
    service.configure("source", _SELECTION, {})
    preview = service.preview("source")

    def fail_release(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic cleanup crash")

    monkeypatch.setattr(service._custody, "release_completed", fail_release)
    with pytest.raises(LiveSourceError, match="source_storage_unavailable"):
        service.apply("source", cast(str, preview["preview_id"]))
    assert service._load("source")["cleanup_receipts"]
    fetches = runtime.intakes
    restarted = LiveCaptureService(tmp_path / "live", brain, runtime=runtime, sink=lambda _: None)
    restarted.apply("source", cast(str, preview["preview_id"]))
    assert restarted._load("source")["cleanup_receipts"] == []
    assert restarted.custody_status("source")["retained_items"] == 0
    assert runtime.intakes == fetches


def test_legacy_checkpoint_cleanup_obligation_drains_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Runtime:
        def __init__(self, intakes: tuple[SourceRecordIntake, ...]) -> None:
            self.intakes = intakes
            self.calls = 0

        def fetch_page(
            self, selection: SourceResourceSelection, cursor: str | None
        ) -> CollectorRunPage:
            self.calls += 1
            return CollectorRunPage(selection, self.intakes, "after")

    state = CollectorStateStore(tmp_path / "state.json")
    controller = CollectorController(state, clock=lambda: 100)
    controller.enable(source_id="source", selection=_SELECTION, interval_seconds=1)
    runtime = Runtime((_intake("same"),))

    def fail_release(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic cleanup crash")

    monkeypatch.setattr(controller._custody, "release_completed", fail_release)
    with pytest.raises(LiveSourceError, match="source_storage_unavailable"):
        controller.sync_due(source_id="source", runtime=runtime, capture_sink=MemoryCaptureSink())
    persisted = state.load()
    assert cast(dict[str, object], persisted["sources"])["source"]["cleanup_receipts"]
    restarted = CollectorController(state, clock=lambda: 100)
    result = restarted.sync_due(
        source_id="source", runtime=runtime, capture_sink=MemoryCaptureSink()
    )
    assert result.outcome == "deferred"
    assert restarted.custody_status("source")["retained_items"] == 0
