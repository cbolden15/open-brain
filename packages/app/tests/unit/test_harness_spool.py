"""Thin retry spool: inline delivery failures wait here; delivery IDs dedupe replays."""

import json
import os
import signal
import stat
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id

from open_brain.services import harness_spool
from open_brain.services.harness_cli import HarnessDeadlineExceeded, _deadline
from open_brain.services.harness_events import HarnessCaptureEvent
from open_brain.services.harness_memory import MemoryMessage
from open_brain.services.harness_spool import HarnessSpool
from open_brain.services.harness_workflow import HarnessWorkflow

TENANT = "tenant_123e4567-e89b-42d3-a456-426614174000"
BRAIN = derive_brain_id(TENANT)


def event(tmp_path: Path) -> HarnessCaptureEvent:
    return HarnessCaptureEvent(
        "codex",
        "sample-session",
        tmp_path,
        (MemoryMessage("user", "I prefer lentil soup.", 12),),
        {12: "2026-01-01T00:00:00Z"},
    )


class FakeWorkflow:
    def __init__(self, result: dict[str, object] | None = None, *, tenant: str = TENANT) -> None:
        self.tasks = SimpleNamespace(profile=SimpleNamespace(tenant_id=tenant))
        self.result = result or {
            "status": "acknowledged",
            "capture_ids": ["capture-sample"],
            "delivery_ids": ["delivery-sample"],
        }
        self.calls: list[tuple[object, ...]] = []

    def capture(self, *args: object, **kwargs: object) -> dict[str, object]:
        self.calls.append((*args, kwargs))
        return self.result

    def workflow(self) -> HarnessWorkflow:
        return cast(HarnessWorkflow, self)


def lines(spool: HarnessSpool) -> list[dict[str, object]]:
    path = spool.root / "spool.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_append_is_durable_owner_only_and_idempotent(tmp_path: Path) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    first = spool.append(event(tmp_path))
    second = HarnessSpool(tmp_path / "outbox", BRAIN).append(event(tmp_path))
    assert first == second
    assert first["status"] == "queued"
    assert first["brain_id"] == BRAIN
    assert "lentil" not in json.dumps(first)
    assert len(lines(spool)) == 1
    assert stat.S_IMODE(spool.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((spool.root / "spool.jsonl").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "text",
    [
        "password=synthetic-example",
        "Bearer synthetic_sample_token",
        "-----BEGIN PRIVATE KEY-----",
        "https://example:synthetic@example.invalid",
    ],
)
def test_credentials_are_rejected_before_any_persistence(tmp_path: Path, text: str) -> None:
    root = tmp_path / "outbox"
    source = HarnessCaptureEvent(
        "pi", "sample-session", tmp_path, (MemoryMessage("user", text, 0),), {0: None}
    )
    result = HarnessSpool(root, BRAIN).append(source)
    assert result == {"status": "refused", "brain_id": BRAIN, "error_code": "credential_signal"}
    assert not root.exists()


def test_delivery_removes_the_line_and_keeps_stable_identity(tmp_path: Path) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    messages = (
        MemoryMessage("user", "I prefer  lentil soup.", 12, True, "message-sample"),
        MemoryMessage("assistant", "Generated context", 13, False, "context-sample"),
    )
    source = HarnessCaptureEvent(
        "pi", "sample-session", tmp_path, messages, {12: "2026-01-01T00:00:00.123Z", 13: None}
    )
    assert spool.append(source)["status"] == "queued"
    fake = FakeWorkflow()
    result = spool.drain(fake.workflow())
    assert result["status"] == "delivered"
    assert fake.calls == [
        ("pi", "sample-session", tmp_path, messages, {"timestamps": source.timestamps})
    ]
    assert lines(spool) == []
    assert spool.status()["counts"] == {"pending": 0, "refused": 0, "unavailable": 0}
    assert "lentil" not in json.dumps(result)


def test_local_only_marking_is_retained_for_delivery(tmp_path: Path) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    spool.append(replace(event(tmp_path), local_only=True))
    fake = FakeWorkflow()
    spool.drain(fake.workflow())
    assert fake.calls[0][-1] == {"timestamps": event(tmp_path).timestamps, "local_only": True}


@pytest.mark.parametrize("status", ["queued", "unavailable", "refused"])
def test_failed_delivery_is_retried_without_discard(tmp_path: Path, status: str) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    spool.append(event(tmp_path))
    fake = FakeWorkflow({"status": status, "error_code": "source_excluded"})
    result = spool.drain(fake.workflow())
    assert result["status"] == {"queued": "pending"}.get(status, status)
    assert len(lines(spool)) == 1
    fake.result = {"status": "success_empty", "capture_ids": []}
    assert spool.drain(fake.workflow())["status"] == "delivered"
    assert lines(spool) == []


def test_workflow_exception_cannot_leak_or_discard(tmp_path: Path) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    spool.append(event(tmp_path))

    class Broken(FakeWorkflow):
        def capture(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise RuntimeError("private lentil failure detail")

    result = spool.drain(Broken().workflow())
    assert result["status"] == "unavailable"
    assert "lentil" not in json.dumps(result)
    assert spool.status()["counts"] == {"pending": 1, "refused": 0, "unavailable": 1}


def test_wrong_brain_is_refused_before_capture(tmp_path: Path) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    spool.append(event(tmp_path))
    fake = FakeWorkflow(tenant="tenant_123e4567-e89b-42d3-a456-426614174001")
    result = spool.drain(fake.workflow())
    assert result == {"status": "refused", "brain_id": BRAIN, "error_code": "destination_mismatch"}
    assert fake.calls == []
    assert len(lines(spool)) == 1


def test_drain_caps_at_sixteen_and_rotates_failures_so_none_starve(tmp_path: Path) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    for index in range(17):
        spool.append(replace(event(tmp_path), session_id=f"session-{index}"))
    allowed = "session-16"

    class Selective(FakeWorkflow):
        def capture(self, *args: object, **kwargs: object) -> dict[str, object]:
            self.calls.append((*args, kwargs))
            return (
                self.result
                if args[1] == allowed
                else {"status": "refused", "error_code": "source_excluded"}
            )

    selective = Selective()
    spool.drain(selective.workflow())
    assert len(selective.calls) == 16
    assert all(call[1] != allowed for call in selective.calls)
    # Failed lines move behind the untried line, so the newest is not starved.
    spool.drain(selective.workflow())
    assert selective.calls[16][1] == allowed
    assert len(lines(spool)) == 16
    assert {call[1] for call in selective.calls[17:]} == {f"session-{i}" for i in range(15)}
    before = len(selective.calls)
    spool.drain(selective.workflow())
    assert "session-15" in {call[1] for call in selective.calls[before:]}


def test_invalid_drain_limit_never_invokes_workflow(tmp_path: Path) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    spool.append(event(tmp_path))
    fake = FakeWorkflow()
    for limit in (0, 17, True):
        assert spool.drain(fake.workflow(), limit=limit)["error_code"] == "invalid_limit"
    assert fake.calls == []


def test_drain_deadline_escapes_delivery_error_handlers(tmp_path: Path) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    for index in range(2):
        spool.append(replace(event(tmp_path), session_id=f"session-{index}"))

    class Slow(FakeWorkflow):
        completed = False

        def capture(self, *args: object, **kwargs: object) -> dict[str, object]:
            self.calls.append((*args, kwargs))
            assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
            signal.setitimer(signal.ITIMER_REAL, 0.03)
            threading.Event().wait(timeout=1)
            self.completed = True
            return self.result

    slow = Slow()
    with pytest.raises(HarnessDeadlineExceeded), _deadline(2):
        spool.drain(slow.workflow())
    assert not slow.completed
    assert len(slow.calls) == 1
    assert spool.status()["counts"] == {"pending": 2, "refused": 0, "unavailable": 0}


def test_status_counts_without_workflow_or_source_content(tmp_path: Path) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    empty = spool.status()
    assert empty["status"] == "success_empty"
    assert empty["complete"] is True and empty["error_count"] == 0
    assert not spool.root.exists()
    spool.append(event(tmp_path))
    spool.append(replace(event(tmp_path), session_id="second"))
    before = spool.status()
    assert before["status"] == "pending"
    assert before["counts"] == {"pending": 2, "refused": 0, "unavailable": 0}
    spool.drain(FakeWorkflow({"status": "refused", "error_code": "source_excluded"}).workflow())
    after = spool.status()
    assert after["status"] == "refused"
    assert after["counts"] == {"pending": 2, "refused": 2, "unavailable": 0}
    assert after["error_count"] == 2
    assert "lentil" not in json.dumps(before) + json.dumps(after)


def test_mark_unavailable_is_visible_in_status(tmp_path: Path) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    spool.append(event(tmp_path))
    spool.mark_unavailable()
    status = spool.status()
    assert status["counts"] == {"pending": 1, "refused": 0, "unavailable": 1}
    assert status["error_count"] == 1


def test_long_event_chunks_deliver_every_source(tmp_path: Path) -> None:
    source = replace(
        event(tmp_path),
        messages=tuple(
            MemoryMessage("user", f"I prefer fixture{i} pencils.", i, True, f"entry-{i}")
            for i in range(300)
        ),
        timestamps={},
    )
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    queued = spool.append(source)
    assert queued["status"] == "queued"
    assert queued["batch_count"] == 10
    fake = FakeWorkflow()
    assert spool.drain(fake.workflow())["status"] == "delivered"
    assert sum(len(cast(tuple[object, ...], call[3])) for call in fake.calls) == 300
    assert all(
        len(cast(list[object], cast(dict[str, object], call[-1])["selection"])) <= 32
        for call in fake.calls
    )
    assert lines(spool) == []


def test_corrupt_spool_is_visible_and_never_discarded(tmp_path: Path) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    spool.append(event(tmp_path))
    path = spool.root / "spool.jsonl"
    path.write_bytes(path.read_bytes() + b"{not json\n")
    before = path.read_bytes()
    fake = FakeWorkflow()
    assert spool.drain(fake.workflow())["error_code"] == "invalid_spool"
    assert spool.append(replace(event(tmp_path), session_id="new"))["error_code"] == (
        "invalid_spool"
    )
    status = spool.status()
    assert status["complete"] is False and status["error_code"] == "invalid_spool"
    assert fake.calls == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("unsafe", ["mode", "symlink"])
def test_unsafe_spool_storage_is_refused(tmp_path: Path, unsafe: str) -> None:
    root = tmp_path / "outbox"
    spool = HarnessSpool(root, BRAIN)
    spool.append(event(tmp_path))
    if unsafe == "mode":
        (root / "spool.jsonl").chmod(0o644)
    else:
        target = tmp_path / "elsewhere"
        root.rename(target)
        root.symlink_to(target)
    assert spool.append(replace(event(tmp_path), session_id="new"))["status"] == "refused"
    assert spool.status()["complete"] is False
    assert spool.drain(FakeWorkflow().workflow())["status"] == "refused"


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_crash_during_rewrite_keeps_the_previous_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    spool.append(event(tmp_path))
    before = (spool.root / "spool.jsonl").read_bytes()

    def crashed(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic crash before the rename is durable")

    monkeypatch.setattr(os, failure, crashed)
    result = spool.append(replace(event(tmp_path), session_id="second"))
    monkeypatch.undo()
    # An interrupted write never reports custody and never damages earlier lines.
    assert result["status"] == "unavailable"
    assert (spool.root / "spool.jsonl").read_bytes() == before
    assert sorted(os.listdir(spool.root)) == ["spool.jsonl", "spool.lock"]
    assert spool.status()["counts"] == {"pending": 1, "refused": 0, "unavailable": 0}


def test_leftover_temporary_file_from_a_crash_is_ignored(tmp_path: Path) -> None:
    spool = HarnessSpool(tmp_path / "outbox", BRAIN)
    spool.append(event(tmp_path))
    (spool.root / "spool.jsonl.tmp-synthetic").write_text("partial")
    assert spool.status()["counts"] == {"pending": 1, "refused": 0, "unavailable": 0}
    assert spool.drain(FakeWorkflow().workflow())["status"] == "delivered"


def test_oversized_request_is_refused_before_disk_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness_spool, "_MAX_LINE_BYTES", 64)
    root = tmp_path / "outbox"
    result = HarnessSpool(root, BRAIN).append(event(tmp_path))
    assert result["error_code"] == "request_too_large"
    assert not (root / "spool.jsonl").exists()


def test_invalid_destination_format_is_rejected() -> None:
    for root, brain in ((Path("relative"), BRAIN), (Path("/tmp/x"), "not-a-brain")):
        with pytest.raises(ValueError, match="invalid_spool"):
            HarnessSpool(root, brain)
