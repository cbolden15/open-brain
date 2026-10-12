import json
import os
import stat
import time
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.ids import portable_canonical_json_bytes

from open_brain.services.harness_cli import HarnessDeadlineExceeded, _deadline
from open_brain.services.harness_events import HarnessCaptureEvent
from open_brain.services.harness_memory import MemoryMessage
from open_brain.services.harness_outbox import HarnessOutbox
from open_brain.services.harness_policy import HarnessPolicy
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


def test_enqueue_is_durable_and_replay_identity_is_deterministic(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    first = outbox.enqueue(event(tmp_path))
    second = HarnessOutbox(tmp_path / "outbox", BRAIN).enqueue(event(tmp_path))
    assert first == second
    assert first["status"] == "queued"
    assert first["brain_id"] == BRAIN
    assert len(list((tmp_path / "outbox" / "requests").glob("*.json"))) == 1
    assert "lentil" not in str(first)


@pytest.mark.parametrize("reason", ["source_denied", "credential_signal"])
@pytest.mark.parametrize("inheritance", ["session", "parent", "origin"])
def test_hermes_latched_restriction_refuses_before_source_publication(
    tmp_path: Path, reason: str, inheritance: str,
) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    source = replace(
        event(tmp_path), client="hermes", session_id="parent",
        messages=(MemoryMessage("user", "I prefer lentil soup.", 12, True, "native-12"),),
    )
    if inheritance == "origin":
        # Establish an actual inherited origin, then restrict it after the child
        # no longer names its parent in subsequent events.
        assert outbox.enqueue(source)["status"] == "queued"
        source = replace(source, session_id="child", parent_session_id="parent")
        assert outbox.enqueue(source)["status"] == "queued"
        source = replace(source, parent_session_id=None)
    outbox.restrict_session("hermes", "parent", tmp_path, reason=reason)
    if inheritance == "parent":
        source = replace(
            source, session_id="child", parent_session_id="parent",
            session_timestamp="2026-01-01T00:00:00Z",
        )
    marker = "SYNTHETIC_REFUSED_HERMES_BODY"
    source = replace(
        source,
        messages=(
            *(source.messages if inheritance == "origin" else ()),
            MemoryMessage("user", f"I prefer {marker} pencils.", 20, True, "native-20"),
        ),
        timestamps={20: "2026-01-02T00:00:00Z"},
    )
    before = {
        item: item.read_bytes() for item in outbox.root.rglob("*.json")
        if item.parent.name == "requests" or item.name.startswith("message-")
    }
    original_metadata = {
        item: item.read_bytes() for item in outbox.root.rglob("*") if item.is_file()
    }
    assert outbox.capture_guard(source)["status"] == "refused"
    assert {
        item: item.read_bytes() for item in outbox.root.rglob("*") if item.is_file()
    } == original_metadata
    result = outbox.enqueue(source)
    assert result["status"] == "refused"
    assert result["error_code"] == "source_denied"
    after = {
        item: item.read_bytes() for item in outbox.root.rglob("*.json")
        if item.parent.name == "requests" or item.name.startswith("message-")
    }
    assert after == before
    assert all(
        marker.encode() not in item.read_bytes()
        for item in outbox.root.rglob("*") if item.is_file()
    )
    assert outbox.cloud_read_guard(cloud_policy(tmp_path))["status"] == "refused"


@pytest.mark.parametrize("text,local_only,allowed", [
    ("I prefer lentil soup.", False, True),
    ("I prefer lentil soup.", True, False),
    ("Confidential: I prefer lentil soup.", False, False),
    ("-----BEGIN PRIVATE KEY----- synthetic fixture", False, False),
])
def test_capture_guard_screens_new_source_without_creating_custody(
    tmp_path: Path, text: str, local_only: bool, allowed: bool,
) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    source = replace(
        event(tmp_path), client="hermes", local_only=local_only,
        messages=(MemoryMessage("user", text, 0, True, "native-0"),),
    )
    assert outbox.capture_guard(source)["status"] == ("allowed" if allowed else "refused")
    assert not outbox.root.exists()


@pytest.mark.parametrize("inheritance", ["session", "origin"])
@pytest.mark.parametrize("operation", ["capture_guard", "enqueue"])
def test_known_origin_requires_retained_session_state(
    tmp_path: Path, inheritance: str, operation: str,
) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    source = replace(
        event(tmp_path), client="hermes", session_id="parent",
        messages=(MemoryMessage("user", "I prefer lentil soup.", 12, True, "native-12"),),
    )
    assert outbox.enqueue(source)["status"] == "queued"
    if inheritance == "origin":
        source = replace(source, session_id="child", parent_session_id="parent")
        assert outbox.enqueue(source)["status"] == "queued"
        source = replace(source, parent_session_id=None)
    outbox.restrict_session("hermes", "parent", tmp_path, reason="source_denied")
    marker = "SYNTHETIC_DAMAGED_LINEAGE_BODY"
    source = replace(
        source,
        messages=(*source.messages, MemoryMessage("user", marker, 20, True, "native-20")),
        timestamps={**source.timestamps, 20: "2026-01-02T00:00:00Z"},
    )
    assert outbox.capture_guard(source)["error_code"] == "source_denied"
    states = list(outbox.root.rglob("session-*.json"))
    parent = next(
        path for path in states if json.loads(path.read_bytes())["session_id"] == "parent"
    )
    parent.unlink()  # Only this test's disposable restriction state is damaged.
    before = {path: path.read_bytes() for path in outbox.root.rglob("*") if path.is_file()}
    result = getattr(outbox, operation)(source)
    assert result["status"] == "refused"
    assert result["error_code"] == "lineage_unavailable"
    after = {path: path.read_bytes() for path in outbox.root.rglob("*") if path.is_file()}
    assert after == before
    assert all(marker.encode() not in body for body in after.values())


def test_capture_guard_refuses_incomplete_custody_or_missing_lineage(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    source = replace(
        event(tmp_path), client="hermes",
        messages=(MemoryMessage("user", "I prefer lentil soup.", 0, True, "native-0"),),
    )
    assert outbox.capture_guard(replace(source, parent_session_id="missing"))["status"] == "refused"
    outbox.root.mkdir(mode=0o700)
    assert outbox.capture_guard(source)["status"] == "refused"
    assert not list(outbox.root.iterdir())


def test_direct_hermes_local_marking_retains_only_denial_metadata(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    marker = "SYNTHETIC_MARKED_HERMES_BODY"
    source = replace(
        event(tmp_path), client="hermes", local_only=True,
        messages=(MemoryMessage("user", f"I prefer {marker}.", 0, True, "native-0"),),
    )
    result = outbox.enqueue(source)
    assert result["status"] == "refused"
    assert result["error_code"] == "source_denied"
    assert not list((outbox.root / "requests").iterdir())
    assert not list(outbox.root.rglob("message-*.json"))
    assert all(
        marker.encode() not in item.read_bytes()
        for item in outbox.root.rglob("*") if item.is_file()
    )
    assert outbox.cloud_read_guard(cloud_policy(tmp_path))["status"] == "refused"


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


def test_drain_deadline_escapes_delivery_error_handlers(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    for index in range(2):
        assert (
            outbox.enqueue(replace(event(tmp_path), session_id=f"session-{index}"))["status"]
            == "queued"
        )

    class SlowWorkflow(FakeWorkflow):
        def capture(self, *args: object, **kwargs: object) -> dict[str, object]:
            self.calls.append((*args, kwargs))
            time.sleep(0.2)
            return self.result

    slow = SlowWorkflow()
    started = time.monotonic()
    with pytest.raises(HarnessDeadlineExceeded), _deadline(0.03):
        outbox.drain(slow.workflow())
    assert time.monotonic() - started < 0.15
    assert len(slow.calls) == 1
    assert not list((outbox.root / "receipts").iterdir())
    assert outbox.status()["counts"] == {
        "pending": 2,
        "delivered": 0,
        "refused": 0,
        "unavailable": 0,
    }


def test_refused_prefix_cannot_starve_new_requests(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    ordered = []
    for index in range(17):
        source = replace(event(tmp_path), session_id=f"session-{index}")
        queued = outbox.enqueue(source)
        ordered.append((str(queued["request_id"]), source.session_id))
    allowed = sorted(ordered)[-1][1]

    class SelectiveWorkflow(FakeWorkflow):
        def capture(self, *args: object, **kwargs: object) -> dict[str, object]:
            self.calls.append((*args, kwargs))
            return (
                self.result
                if args[1] == allowed
                else {"status": "refused", "error_code": "source_excluded"}
            )

    selective = SelectiveWorkflow()
    outbox.drain(selective.workflow())
    assert len(selective.calls) == 16
    assert all(call[1] != allowed for call in selective.calls)
    outbox.drain(selective.workflow())
    assert selective.calls[16][1] == allowed
    assert len(list((outbox.root / "receipts").iterdir())) == 1
    # The next retry batch includes the oldest withheld retry from the prior batch.
    withheld = selective.calls[15][1]
    before = len(selective.calls)
    outbox.drain(selective.workflow())
    assert any(call[1] == withheld for call in selective.calls[before:])


def test_acknowledgement_retains_source_and_durable_receipt_and_skips_redelivery(
    tmp_path: Path,
) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    source = event(tmp_path)
    queued = outbox.enqueue(source)
    request_path = outbox.root / "requests" / f"{queued['request_id']}.json"
    before = request_path.read_bytes()
    fake = FakeWorkflow()

    result = outbox.drain(fake.workflow())

    assert result["status"] == "delivered"
    assert fake.calls == [
        (
            source.client,
            source.session_id,
            source.project,
            source.messages,
            {"timestamps": source.timestamps},
        )
    ]
    assert request_path.read_bytes() == before
    receipt = json.loads((outbox.root / "receipts" / request_path.name).read_bytes())
    assert receipt["request_id"] == queued["request_id"]
    assert receipt["brain_id"] == BRAIN
    assert receipt["status"] == "delivered"
    assert receipt["acknowledgement"]["capture_ids"] == ["capture-sample"]
    again = HarnessOutbox(outbox.root, BRAIN).drain(fake.workflow())
    assert again["status"] == "delivered"
    assert len(fake.calls) == 1
    assert "lentil" not in json.dumps(result)


def test_wrong_brain_is_refused_before_capture(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    outbox.enqueue(event(tmp_path))
    fake = FakeWorkflow(tenant="tenant_123e4567-e89b-42d3-a456-426614174001")
    result = outbox.drain(fake.workflow())
    assert result["status"] == "refused"
    assert result["error_code"] == "destination_mismatch"
    assert fake.calls == []


@pytest.mark.parametrize("status", ["queued", "unavailable", "refused"])
def test_no_terminal_discard_for_workflow_failure(tmp_path: Path, status: str) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    outbox.enqueue(event(tmp_path))
    fake = FakeWorkflow({"status": status, "error_code": "source_excluded"})
    result = outbox.drain(fake.workflow())
    assert result["status"] == {"queued": "pending"}.get(status, status)
    assert len(list((outbox.root / "requests").glob("*.json"))) == 1
    assert list((outbox.root / "receipts").iterdir()) == []
    fake.result = {"status": "success_empty", "capture_ids": []}
    assert outbox.drain(fake.workflow())["status"] == "delivered"


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
    result = HarnessOutbox(root, BRAIN).enqueue(source)
    assert result["status"] == "refused"
    assert result["error_code"] == "credential_signal"
    assert not list((root / "requests").iterdir())
    assert text not in "".join(path.read_text() for path in root.rglob("*.json"))
    assert HarnessOutbox(root, BRAIN).cloud_read_guard(cloud_policy(tmp_path)) == {
        "status": "refused",
        "error_code": "credential_signal",
    }


def test_file_and_directory_modes_and_fsync_precede_queued(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    original = os.fsync

    def fsync(descriptor: int) -> None:
        calls.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    assert outbox.enqueue(event(tmp_path))["status"] == "queued"
    assert False in calls
    assert calls[-1] is True
    for path in outbox.root.rglob("*"):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def test_fsync_failure_cannot_report_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_descriptor: int) -> None:
        raise OSError("synthetic private failure detail")

    monkeypatch.setattr(os, "fsync", fail)
    result = HarnessOutbox(tmp_path / "outbox", BRAIN).enqueue(event(tmp_path))
    assert result["status"] == "unavailable"
    assert "private failure" not in str(result)


@pytest.mark.parametrize("location", ["root", "requests", "request", "receipt"])
def test_symlink_storage_is_refused_without_workflow(
    tmp_path: Path,
    location: str,
) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    queued = outbox.enqueue(event(tmp_path))
    if location == "root":
        actual = outbox.root.rename(tmp_path / "actual")
        outbox.root.symlink_to(actual, target_is_directory=True)
    elif location == "requests":
        actual = (outbox.root / "requests").rename(tmp_path / "actual")
        (outbox.root / "requests").symlink_to(actual, target_is_directory=True)
    else:
        path = outbox.root / ("requests" if location == "request" else "receipts")
        path /= f"{queued['request_id']}.json"
        if path.exists():
            actual = path.rename(tmp_path / "actual")
        else:
            actual = tmp_path / "actual"
            actual.write_text("{}")
        path.symlink_to(actual)
    fake = FakeWorkflow()
    assert outbox.drain(fake.workflow())["status"] == "refused"
    assert fake.calls == []


@pytest.mark.parametrize(
    "filename,body",
    [
        ("sample.partial", b"{"),
        ("0" * 64 + ".json", b"{"),
        ("0" * 64 + ".json", b"{}"),
    ],
)
def test_partial_or_corrupt_files_are_visible_without_capture(
    tmp_path: Path,
    filename: str,
    body: bytes,
) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    outbox.enqueue(event(tmp_path))
    (outbox.root / "requests" / filename).write_bytes(body)
    (outbox.root / "requests" / filename).chmod(0o600)
    fake = FakeWorkflow()
    result = outbox.drain(fake.workflow())
    assert result["status"] == "refused"
    assert (outbox.root / "requests" / filename).exists()


@pytest.mark.parametrize("target", ["root", "requests", "request", "destination"])
def test_permissive_modes_are_rejected(tmp_path: Path, target: str) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    queued = outbox.enqueue(event(tmp_path))
    paths = {
        "root": outbox.root,
        "requests": outbox.root / "requests",
        "request": outbox.root / "requests" / f"{queued['request_id']}.json",
        "destination": outbox.root / "destination.json",
    }
    paths[target].chmod(0o755 if paths[target].is_dir() else 0o644)
    fake = FakeWorkflow()
    assert outbox.drain(fake.workflow())["status"] == "refused"
    assert fake.calls == []


def test_foreign_owner_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    outbox.enqueue(event(tmp_path))
    actual = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: actual + 1)
    fake = FakeWorkflow()
    assert outbox.drain(fake.workflow())["status"] == "refused"
    assert fake.calls == []


def test_different_root_destination_does_not_replace_binding(tmp_path: Path) -> None:
    root = tmp_path / "outbox"
    HarnessOutbox(root, BRAIN).enqueue(event(tmp_path))
    binding = (root / "destination.json").read_bytes()
    other = derive_brain_id("tenant_123e4567-e89b-42d3-a456-426614174001")
    result = HarnessOutbox(root, other).enqueue(event(tmp_path))
    assert result["status"] == "refused"
    assert result["error_code"] == "destination_mismatch"
    assert (root / "destination.json").read_bytes() == binding


@pytest.mark.parametrize("limit", [0, 17, True])
def test_invalid_drain_limit_never_invokes_workflow(tmp_path: Path, limit: int) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    outbox.enqueue(event(tmp_path))
    fake = FakeWorkflow()
    assert outbox.drain(fake.workflow(), limit=limit)["error_code"] == "invalid_limit"
    assert fake.calls == []


def test_delivery_limit_and_previous_receipts_do_not_starve_new_work(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    for i in range(20):
        source = HarnessCaptureEvent(
            "pi", f"sample-session-{i}", tmp_path, event(tmp_path).messages, {12: None}
        )
        assert outbox.enqueue(source)["status"] == "queued"
    fake = FakeWorkflow()
    first = outbox.drain(fake.workflow())
    assert len(cast(list[object], first["results"])) == 16
    assert len(fake.calls) == 16
    second = outbox.drain(fake.workflow())
    assert len(cast(list[object], second["results"])) == 4
    assert len(fake.calls) == 20


@pytest.mark.parametrize("directory_failure", [False, True])
def test_receipt_fsync_failure_preserves_custody_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    directory_failure: bool,
) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    outbox.enqueue(event(tmp_path))
    receipt_inode = (outbox.root / "receipts").stat().st_ino
    fake = FakeWorkflow()
    original = os.fsync

    def fail_receipt(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if fake.calls and (
            (directory_failure and metadata.st_ino == receipt_inode)
            or (not directory_failure and stat.S_ISREG(metadata.st_mode))
        ):
            raise OSError("synthetic acknowledgement detail")
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fail_receipt)
    failed = outbox.drain(fake.workflow())
    assert failed["status"] == "unavailable"
    assert len(list((outbox.root / "requests").glob("*.json"))) == 1
    assert "acknowledgement detail" not in str(failed)
    monkeypatch.setattr(os, "fsync", original)
    assert HarnessOutbox(outbox.root, BRAIN).drain(fake.workflow())["status"] == "delivered"
    assert len(fake.calls) == (1 if directory_failure else 2)


def test_workflow_exception_cannot_leak_source_or_discard_request(tmp_path: Path) -> None:
    class FailingWorkflow(FakeWorkflow):
        def capture(self, *args: object, **kwargs: object) -> dict[str, object]:
            raise RuntimeError("synthetic sensitive exception")

    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    outbox.enqueue(event(tmp_path))
    result = outbox.drain(FailingWorkflow().workflow())
    assert result["status"] == "unavailable"
    assert "sensitive" not in str(result)
    assert len(list((outbox.root / "requests").glob("*.json"))) == 1


def test_payload_bound_prevents_disk_write(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    source = HarnessCaptureEvent(
        "pi",
        "sample-session",
        tmp_path,
        (MemoryMessage("user", "I prefer " + "x " * 140000, 0),),
        {},
    )
    assert outbox.enqueue(source)["error_code"] == "request_too_large"
    assert not outbox.root.exists()


def test_stable_native_message_identity_and_exact_timestamps_survive_replay(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    messages = (
        MemoryMessage("user", "I prefer  lentil soup.", 12, True, "message-sample"),
        MemoryMessage("assistant", "Generated context", 13, False, "context-sample"),
    )
    source = HarnessCaptureEvent(
        "pi", "sample-session", tmp_path, messages, {12: "2026-01-01T00:00:00.123Z", 13: None}
    )
    assert outbox.enqueue(source)["status"] == "queued"
    fake = FakeWorkflow()
    assert outbox.drain(fake.workflow())["status"] == "delivered"
    assert fake.calls[0][3] == messages
    assert fake.calls[0][4] == {"timestamps": source.timestamps}


def test_status_reads_custody_without_workflow_or_source_content(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    assert outbox.status()["status"] == "success_empty"
    assert not outbox.root.exists()
    outbox.enqueue(event(tmp_path))
    before = outbox.status()
    assert before["status"] == "pending"
    assert before["counts"] == {"pending": 1, "delivered": 0, "refused": 0, "unavailable": 0}
    assert before["complete"] is True
    outbox.drain(FakeWorkflow().workflow())
    after = outbox.status()
    assert after["counts"] == {"pending": 0, "delivered": 1, "refused": 0, "unavailable": 0}
    assert "lentil" not in json.dumps(before) + json.dumps(after)


def test_status_bound_does_not_claim_complete_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain.services import harness_outbox

    monkeypatch.setattr(harness_outbox, "_MAX_STATUS_FILES", 1)
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    outbox.enqueue(event(tmp_path))
    outbox.enqueue(
        HarnessCaptureEvent("pi", "other-session", tmp_path, event(tmp_path).messages, {})
    )
    result = outbox.status()
    assert result["status"] == "unavailable"
    assert result["error_code"] == "status_scan_limit"
    assert result["complete"] is False
    assert result["inspected"] == 1


def test_status_reports_corruption_without_discard(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    metadata = outbox.enqueue(event(tmp_path))
    request = outbox.root / "requests" / f"{metadata['request_id']}.json"
    request.write_text("{")
    result = outbox.status()
    assert result["status"] == "refused"
    assert result["error_count"] == 1
    assert result["counts"] == {"pending": 0, "delivered": 0, "refused": 1, "unavailable": 0}
    assert request.exists()


def test_orphan_delivered_receipt_exposes_missing_source_custody(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    queued = outbox.enqueue(event(tmp_path))
    fake = FakeWorkflow()
    outbox.drain(fake.workflow())
    # Remove only this disposable synthetic source to simulate external damage.
    (outbox.root / "requests" / f"{queued['request_id']}.json").unlink()
    assert outbox.status()["status"] == "refused"
    assert outbox.drain(fake.workflow())["status"] == "refused"
    assert len(fake.calls) == 1


def test_current_real_policy_refusal_and_downstream_deduplication(tmp_path: Path) -> None:
    from open_brain_engine.engine.local import open_local_engine

    from open_brain.profile import compile_single_user_local
    from open_brain.services.harness_policy import HarnessPolicy
    from open_brain.services.session_consent import DurableProviderConsentStore

    project = tmp_path / "sample-project"
    project.mkdir()
    tasks = open_local_engine(compile_single_user_local(tmp_path / "synthetic-brain"))
    brain_id = derive_brain_id(tasks.profile.tenant_id)
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    consent = DurableProviderConsentStore(
        authority / "consent.json", brain_id=brain_id, issuer_epoch=1
    )
    raw: dict[str, object] = {
        "schema_version": "open-brain-harness-policy.v1",
        "enabled": True,
        "providers": {},
        "sources": [],
        "exclusions": [],
    }
    workflow = HarnessWorkflow(tasks, lambda: HarnessPolicy.from_dict(raw), consent_store=consent)
    outbox = HarnessOutbox(tmp_path / "outbox", brain_id)
    queued = outbox.enqueue(event(project))
    raw["exclusions"] = [str(project)]
    refused = outbox.drain(workflow)
    assert refused["status"] == "refused"
    assert "source_excluded" in str(refused)
    assert outbox.status()["counts"] == {
        "pending": 1,
        "delivered": 0,
        "refused": 1,
        "unavailable": 0,
    }
    raw["exclusions"] = []
    first = outbox.drain(workflow)
    assert first["status"] == "delivered"
    # This disposable fault recreates the commit-before-acknowledgement crash window.
    (outbox.root / "receipts" / f"{queued['request_id']}.json").unlink()
    replay = outbox.drain(workflow)
    assert replay["status"] == "delivered"
    first_ack = cast(list[dict[str, object]], first["results"])[0]["acknowledgement"]
    replay_ack = cast(list[dict[str, object]], replay["results"])[0]["acknowledgement"]
    assert first_ack == replay_ack


def test_latest_failure_metadata_survives_restart_without_mutating_source(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    queued = outbox.enqueue(event(tmp_path))
    request = outbox.root / "requests" / f"{queued['request_id']}.json"
    original = request.read_bytes()
    fake = FakeWorkflow({"status": "refused", "error_code": "source_excluded"})
    assert outbox.drain(fake.workflow())["status"] == "refused"
    status = HarnessOutbox(outbox.root, BRAIN).status()
    assert status["counts"] == {"pending": 1, "delivered": 0, "refused": 1, "unavailable": 0}
    failure = cast(dict[str, object], status["last_failure"])
    assert failure["request_id"] == queued["request_id"]
    assert failure["error_code"] == "source_excluded"
    assert "lentil" not in json.dumps(status)
    fake.result = {"status": "unavailable", "error_code": "capture_unavailable"}
    assert outbox.drain(fake.workflow())["status"] == "unavailable"
    status = outbox.status()
    assert status["counts"] == {"pending": 1, "delivered": 0, "refused": 0, "unavailable": 1}
    assert request.read_bytes() == original
    fake.result = {"status": "success_empty", "capture_ids": []}
    assert outbox.drain(fake.workflow())["status"] == "delivered"
    status = outbox.status()
    assert status["counts"] == {"pending": 0, "delivered": 1, "refused": 0, "unavailable": 0}
    assert status["last_failure"] is None


@pytest.mark.parametrize("damage", ["symlink", "mode", "corrupt"])
def test_unsafe_attempt_metadata_is_refused_before_capture(tmp_path: Path, damage: str) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    queued = outbox.enqueue(event(tmp_path))
    failed = FakeWorkflow({"status": "unavailable", "error_code": "capture_unavailable"})
    outbox.drain(failed.workflow())
    path = outbox.root / "attempts" / f"{queued['request_id']}.json"
    if damage == "symlink":
        retained = path.rename(tmp_path / "retained-attempt")
        path.symlink_to(retained)
    elif damage == "mode":
        path.chmod(0o644)
    else:
        path.write_text("{")
    current = FakeWorkflow()
    assert outbox.drain(current.workflow())["status"] == "refused"
    assert current.calls == []
    assert outbox.status()["status"] == "refused"


def cloud_policy(tmp_path: Path) -> HarnessPolicy:
    return HarnessPolicy.from_dict(
        {
            "schema_version": "open-brain-harness-policy.v1",
            "enabled": True,
            "providers": {"codex": ["work"]},
            "sources": [
                {
                    "id": "sample",
                    "project_root": str(tmp_path),
                    "tier": "work",
                    "local_only": False,
                },
            ],
            "exclusions": [],
        }
    )


def test_pending_source_restriction_denies_cloud_until_private_acknowledgement(
    tmp_path: Path,
) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    policy = cloud_policy(tmp_path)
    assert outbox.cloud_read_guard(policy) == {"status": "allowed"}
    outbox.enqueue(event(tmp_path))
    assert outbox.drain(FakeWorkflow().workflow())["status"] == "delivered"
    assert outbox.cloud_read_guard(policy) == {"status": "allowed"}
    private = HarnessCaptureEvent(
        "codex",
        "sample-session",
        tmp_path,
        (MemoryMessage("user", "Confidential:\nI prefer lentil soup.", 12),),
        {12: None},
    )
    outbox.enqueue(private)
    failed = FakeWorkflow({"status": "unavailable", "error_code": "capture_unavailable"})
    assert outbox.drain(failed.workflow())["status"] == "unavailable"
    assert HarnessOutbox(outbox.root, BRAIN).cloud_read_guard(policy) == {
        "status": "refused",
        "error_code": "pending_source_restriction",
    }
    assert outbox.drain(FakeWorkflow().workflow())["status"] == "delivered"
    assert outbox.cloud_read_guard(policy) == {"status": "allowed"}


def test_pending_local_policy_unknown_and_unauthored_markings_do_not_block_unrelated_grants(
    tmp_path: Path,
) -> None:
    project = tmp_path / "shared"
    project.mkdir()
    other = tmp_path / "local"
    other.mkdir()
    policy = cloud_policy(project)
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    outbox.enqueue(
        HarnessCaptureEvent(
            "pi",
            "unknown",
            other,
            (MemoryMessage("user", "Confidential:\nI prefer lentils.", 0),),
            {},
        )
    )
    outbox.enqueue(
        HarnessCaptureEvent(
            "codex",
            "shared",
            project,
            (
                MemoryMessage("user", "Confidential:", 0, False),
                MemoryMessage("user", "I prefer lentils.", 1),
            ),
            {},
        )
    )
    assert outbox.cloud_read_guard(policy) == {"status": "allowed"}


def test_pending_excluded_and_already_local_rules_leave_unrelated_cloud_grants_available(
    tmp_path: Path,
) -> None:
    shared, local, excluded = (tmp_path / name for name in ("shared", "local", "excluded"))
    for project in (shared, local, excluded):
        project.mkdir()
    policy = HarnessPolicy.from_dict(
        {
            "schema_version": "open-brain-harness-policy.v1",
            "enabled": True,
            "providers": {"codex": ["work"]},
            "sources": [
                {"id": name, "project_root": str(project), "tier": "work", "local_only": narrowed}
                for name, project, narrowed in (
                    ("shared", shared, False),
                    ("local", local, True),
                    ("excluded", excluded, False),
                )
            ],
            "exclusions": [str(excluded)],
        }
    )
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    for project in (local, excluded):
        outbox.enqueue(
            HarnessCaptureEvent(
                "pi",
                project.name,
                project,
                (MemoryMessage("user", "Confidential:\nI prefer lentils.", 0),),
                {},
            )
        )
    assert outbox.cloud_read_guard(policy) == {"status": "allowed"}


def test_corrupt_queue_never_allows_cloud_guard(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    queued = outbox.enqueue(event(tmp_path))
    (outbox.root / "requests" / f"{queued['request_id']}.json").write_text("{")
    result = outbox.cloud_read_guard(cloud_policy(tmp_path))
    assert result["status"] == "refused"
    assert "sample-session" not in str(result)


def test_incomplete_guard_scan_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from open_brain.services import harness_outbox

    monkeypatch.setattr(harness_outbox, "_MAX_GUARD_FILES", 1, raising=False)
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    outbox.enqueue(event(tmp_path))
    outbox.enqueue(HarnessCaptureEvent("pi", "other", tmp_path, event(tmp_path).messages, {}))
    assert outbox.cloud_read_guard(cloud_policy(tmp_path))["status"] == "unavailable"


def test_private_acknowledgement_clears_pending_guard_but_engine_denial_remains(
    tmp_path: Path,
) -> None:
    from open_brain_engine.core.models import PrivacyTier
    from open_brain_engine.engine.local import open_local_engine

    from open_brain.profile import compile_single_user_local
    from open_brain.services.session_consent import DurableProviderConsentStore

    project = tmp_path / "sample-project"
    project.mkdir()
    policy = cloud_policy(project)
    tasks = open_local_engine(compile_single_user_local(tmp_path / "synthetic-brain"))
    brain_id = derive_brain_id(tasks.profile.tenant_id)
    authority = tmp_path / "authority"
    authority.mkdir(mode=0o700)
    consent = DurableProviderConsentStore(
        authority / "consent.json", brain_id=brain_id, issuer_epoch=1
    )
    consent.grant(
        provider_id="codex",
        allowed_tiers=frozenset({PrivacyTier.WORK}),
        operation_id="grant-sample",
        decided_at="2026-01-01T00:00:00Z",
    )
    workflow = HarnessWorkflow(tasks, lambda: policy, consent_store=consent, local_only_host=True)
    outbox = HarnessOutbox(tmp_path / "outbox", brain_id)
    messages = (MemoryMessage("user", "I prefer lentil soup.", 12, True, "message-sample"),)
    outbox.enqueue(HarnessCaptureEvent("codex", "sample-session", project, messages, {12: None}))
    assert outbox.drain(workflow)["status"] == "delivered"
    assert workflow.recall("lentil", provider="codex").status == "success"
    private_messages = (*messages, MemoryMessage("user", "Confidential:", 13, True, "mark-sample"))
    outbox.enqueue(
        HarnessCaptureEvent(
            "codex", "sample-session", project, private_messages, {12: None, 13: None}
        )
    )
    assert outbox.cloud_read_guard(policy)["status"] == "refused"
    assert outbox.drain(workflow)["status"] == "delivered"
    assert outbox.cloud_read_guard(policy) == {"status": "allowed"}
    assert workflow.recall("lentil", provider="codex").status == "success_empty"
    assert workflow.recall_local("lentil", project=project).status == "success"


@pytest.mark.parametrize("completed", [129, 257])
@pytest.mark.parametrize("restricted", [False, True])
def test_completed_history_does_not_exhaust_pending_guard_budget(
    tmp_path: Path,
    completed: int,
    restricted: bool,
) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    queued = outbox.enqueue(event(tmp_path))
    outbox.drain(FakeWorkflow().workflow())
    request = outbox.root / "requests" / f"{queued['request_id']}.json"
    receipt = outbox.root / "receipts" / request.name
    # Synthetic format fixtures model completed history; this is not native delivery evidence.
    original_document = json.loads(request.read_bytes())
    original_receipt = json.loads(receipt.read_bytes())
    for index in range(completed):
        document = {**original_document, "session_id": f"completed-{index}"}
        body = portable_canonical_json_bytes(document)
        identity = sha256(body).hexdigest()
        history = outbox.root / "requests" / f"{identity}.json"
        history.write_bytes(body)
        history.chmod(0o600)
        history_receipt = outbox.root / "receipts" / history.name
        history_receipt.write_bytes(
            portable_canonical_json_bytes(
                {
                    **original_receipt,
                    "request_id": identity,
                }
            )
        )
        history_receipt.chmod(0o600)
    text = ("Confidential:\n" if restricted else "") + "I prefer lentil soup."
    outbox.enqueue(
        HarnessCaptureEvent("pi", "pending", tmp_path, (MemoryMessage("user", text, 0),), {})
    )
    result = outbox.cloud_read_guard(cloud_policy(tmp_path))
    assert result == (
        {"status": "refused", "error_code": "pending_source_restriction"}
        if restricted
        else {"status": "allowed"}
    )


def test_backend_startup_failure_records_pending_attempt_without_a_workflow(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    queued = outbox.enqueue(event(tmp_path))
    request = outbox.root / "requests" / f"{queued['request_id']}.json"
    original = request.read_bytes()
    result = outbox.mark_unavailable()
    assert result["status"] == "unavailable"
    assert result["updated"] == 1
    status = HarnessOutbox(outbox.root, BRAIN).status()
    assert status["counts"] == {"pending": 1, "delivered": 0, "refused": 0, "unavailable": 1}
    assert cast(dict[str, object], status["last_failure"])["error_code"] == "capture_unavailable"
    assert request.read_bytes() == original
    assert "lentil" not in str(result) + str(status)


def test_startup_failure_preserves_partial_acknowledgement_and_updates_at_most_16(
    tmp_path: Path,
) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    outbox.enqueue(event(tmp_path))
    outbox.drain(
        FakeWorkflow(
            {
                "status": "queued",
                "capture_ids": ["partial-capture"],
                "delivery_ids": ["partial-delivery"],
            }
        ).workflow()
    )
    outbox.mark_unavailable()
    failure = cast(dict[str, object], outbox.status()["last_failure"])
    assert failure["capture_ids"] == ["partial-capture"]
    assert failure["delivery_ids"] == ["partial-delivery"]
    for index in range(20):
        outbox.enqueue(
            HarnessCaptureEvent("pi", f"pending-{index}", tmp_path, event(tmp_path).messages, {})
        )
    assert outbox.mark_unavailable()["updated"] == 16


def test_completed_receipt_directory_is_fsynced_once_per_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    outbox.enqueue(event(tmp_path))
    outbox.enqueue(HarnessCaptureEvent("pi", "another", tmp_path, event(tmp_path).messages, {}))
    outbox.drain(FakeWorkflow().workflow())
    receipt_inode = (outbox.root / "receipts").stat().st_ino
    calls = 0
    original = os.fsync

    def fsync(descriptor: int) -> None:
        nonlocal calls
        if os.fstat(descriptor).st_ino == receipt_inode:
            calls += 1
        original(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    assert outbox.cloud_read_guard(cloud_policy(tmp_path)) == {"status": "allowed"}
    assert calls == 1


def test_long_event_chunks_replay_all_sources_without_first_page_loss(tmp_path: Path) -> None:
    source = replace(
        event(tmp_path),
        messages=tuple(
            MemoryMessage("user", f"I prefer fixture{i} pencils.", i, True, f"entry-{i}")
            for i in range(300)
        ),
        timestamps={},
    )
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    queued = outbox.enqueue(source)
    assert queued["status"] == "queued"
    assert queued["batch_count"] == 10
    fake = FakeWorkflow()
    assert outbox.drain(fake.workflow())["status"] == "delivered"
    assert sum(len(cast(tuple[object, ...], call[3])) for call in fake.calls) == 300
    assert all(
        len(cast(list[object], cast(dict[str, object], call[-1])["selection"])) <= 32
        for call in fake.calls
    )
    assert outbox.enqueue(source) == queued
    outbox.drain(fake.workflow())
    assert len(fake.calls) == 10


def test_fork_origin_is_retained_metadata_and_unknown_parent_is_visible(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    source = replace(
        event(tmp_path),
        client="pi",
        session_id="parent",
        messages=(MemoryMessage("user", "I prefer lentil soup.", 12, True, "native-12"),),
    )
    assert outbox.enqueue(source)["status"] == "queued"
    child = replace(
        source,
        session_id="child",
        parent_session_id="parent",
        session_timestamp="2026-01-02T00:00:00Z",
        messages=(replace(source.messages[0], ordinal=0),),
        timestamps={},
    )
    assert outbox.enqueue(child)["status"] == "queued"
    fake = FakeWorkflow()
    outbox.drain(fake.workflow())
    child_call = next(call for call in fake.calls if call[1] == "child")
    assert cast(dict[str, object], child_call[-1])["origin_sessions"] == {"native-12": "parent"}
    assert cast(dict[str, object], child_call[-1])["origin_ordinals"] == {"native-12": 12}
    assert (
        outbox.enqueue(replace(child, session_id="unknown", parent_session_id="missing"))[
            "error_code"
        ]
        == "lineage_unavailable"
    )
    new_source = replace(
        child,
        messages=(MemoryMessage("user", "I prefer violet pencils.", 14, True, "native-14"),),
        timestamps={14: "2026-01-02T01:00:00Z"},
    )
    assert outbox.enqueue(new_source)["status"] == "queued"
    ambiguous = replace(
        child,
        messages=(MemoryMessage("user", "I prefer unknown pencils.", 15, True, "native-15"),),
        timestamps={15: None},
    )
    assert outbox.enqueue(ambiguous)["error_code"] == "lineage_unavailable"


def test_unknown_parent_cannot_drop_current_session_privacy_marking(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    source = replace(event(tmp_path), client="pi")
    assert outbox.enqueue(source)["status"] == "queued"
    outbox.drain(FakeWorkflow().workflow())
    assert outbox.cloud_read_guard(cloud_policy(tmp_path)) == {"status": "allowed"}
    marked = replace(
        source, parent_session_id="missing-parent",
        messages=(MemoryMessage("user", "Confidential:", 0, True, "new-marker"),),
        timestamps={},
    )
    assert outbox.enqueue(marked)["error_code"] == "lineage_unavailable"
    assert outbox.cloud_read_guard(cloud_policy(tmp_path)) == {
        "status": "refused", "error_code": "pending_source_restriction",
    }


def test_late_marking_latches_across_rotation_and_requeues_retained_source(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    source = replace(
        event(tmp_path),
        messages=(MemoryMessage("user", "I prefer lentil soup.", 12, True, "native-12"),),
    )
    assert outbox.enqueue(source)["status"] == "queued"
    outbox.drain(FakeWorkflow().workflow())
    marked = replace(source, messages=(MemoryMessage("user", "Confidential:", 99, True, "mark"),))
    assert outbox.enqueue(marked)["status"] == "queued"
    assert (
        outbox.cloud_read_guard(cloud_policy(tmp_path))["error_code"]
        == "pending_source_restriction"
    )
    fake = FakeWorkflow()
    assert outbox.drain(fake.workflow())["status"] == "delivered"
    assert all(cast(dict[str, object], call[-1])["local_only"] is True for call in fake.calls)
    rotated = replace(
        source,
        messages=(MemoryMessage("user", "I prefer violet pencils.", 100, True, "native-100"),),
    )
    assert outbox.enqueue(rotated)["status"] == "queued"
    fake = FakeWorkflow()
    outbox.drain(fake.workflow())
    assert cast(dict[str, object], fake.calls[-1][-1])["local_only"] is True


def test_partial_chunk_publication_retains_custody_and_blocks_cloud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from open_brain.services import harness_outbox

    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    source = replace(
        event(tmp_path),
        messages=tuple(
            MemoryMessage("user", f"I prefer fixture{i} pencils.", i, True, f"entry-{i}")
            for i in range(40)
        ),
        timestamps={},
    )
    original = harness_outbox._publish
    published = 0

    def fail_second(directory: int, name: str, body: bytes, *, replace: bool = False) -> None:
        nonlocal published
        if b'"messages"' in body:
            published += 1
            if published == 2:
                raise OSError(5, "synthetic io")
        original(directory, name, body, replace=replace)

    monkeypatch.setattr(harness_outbox, "_publish", fail_second)
    assert outbox.enqueue(source)["status"] == "unavailable"
    assert len(list((outbox.root / "requests").iterdir())) == 1
    assert outbox.cloud_read_guard(cloud_policy(tmp_path))["status"] == "refused"
    monkeypatch.setattr(harness_outbox, "_publish", original)
    assert outbox.enqueue(source)["status"] == "queued"
    assert len(list((outbox.root / "requests").iterdir())) == 2
    assert outbox.drain(FakeWorkflow().workflow())["status"] == "delivered"
    assert outbox.cloud_read_guard(cloud_policy(tmp_path))["status"] == "allowed"


def test_pending_v1_envelope_expands_without_discarding_its_source(tmp_path: Path) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    queued = outbox.enqueue(event(tmp_path))
    original = outbox.root / "requests" / f"{queued['request_id']}.json"
    document = json.loads(original.read_bytes())
    document["messages"] = [
        {
            "role": "user",
            "text": f"I prefer fixture{i} pencils.",
            "ordinal": i,
            "authored": True,
            "source_id": None,
        }
        for i in range(40)
    ]
    body = portable_canonical_json_bytes(document)
    request_id = sha256(body).hexdigest()
    pending = original.with_name(request_id + ".json")
    pending.write_bytes(body)
    pending.chmod(0o600)
    original.unlink()
    fake = FakeWorkflow()
    assert outbox.drain(fake.workflow())["status"] == "pending"
    assert fake.calls == []
    assert pending.read_bytes() == body
    assert outbox.drain(fake.workflow())["status"] == "delivered"
    assert len(fake.calls) == 2
    assert sum(len(cast(tuple[object, ...], call[3])) for call in fake.calls) == 40
    assert (outbox.root / "receipts" / pending.name).exists()
    assert outbox.drain(fake.workflow())["status"] == "delivered"
    assert len(fake.calls) == 2


def test_child_marking_restricts_known_parent_even_after_inherited_entries_rotate_away(
    tmp_path: Path,
) -> None:
    outbox = HarnessOutbox(tmp_path / "outbox", BRAIN)
    parent = replace(
        event(tmp_path),
        client="pi",
        session_id="parent",
        messages=(MemoryMessage("user", "I prefer lentil soup.", 12, True, "entry-12"),),
    )
    assert outbox.enqueue(parent)["status"] == "queued"
    outbox.drain(FakeWorkflow().workflow())
    marker = replace(
        parent,
        session_id="child",
        parent_session_id="parent",
        session_timestamp="2026-01-02T00:00:00Z",
        messages=(MemoryMessage("user", "Confidential:", 20, True, "mark"),),
    )
    assert outbox.enqueue(marker)["status"] == "queued"
    fake = FakeWorkflow()
    assert outbox.drain(fake.workflow())["status"] == "delivered"
    replay = next(call for call in fake.calls if call[1] == "parent")
    assert cast(dict[str, object], replay[-1])["local_only"] is True
