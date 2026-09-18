from __future__ import annotations

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
