"""Guide crash, lost-acknowledgement, concurrency and guard recovery (RECOVERY.md).

Every P2 write is interrupted at three positions: before the authority call,
after the collector committed but before its result reached the guide, and
after the result but before the journal recorded it. Recovery must reconcile
before any new operation: import replays its exact saved request; configure
and preview halt without an authority call. Real collector children run with
only the Slack HTTP boundary synthetic.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

from open_brain.services import source_guide_adapter, source_guide_flow
from open_brain.services.source_guide_journal import WorkflowJournal
from packages.app.tests.integration.services import source_guide_support as support
from packages.app.tests.integration.services.test_source_guide import (
    DATE_FLOOR,
    Guide,
    new_guide,
)


@pytest.fixture
def guide(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Guide:
    return new_guide(tmp_path, capsys, monkeypatch)


class _Crash(BaseException):
    """A supervisor death: nothing after this point runs in this process."""


def _configured(guide: Guide) -> str:
    guide.through_choose()
    code, configured = guide.confirm("configure")
    assert code == 0, configured
    return str(configured["data"]["source_id"])


def _pages(guide: Guide, count: int = 1) -> None:
    support.queue(
        guide.fixture,
        "conversations.history",
        [
            support.history_page(
                [support.message(f"176000000{index}.000001", f"recovery message {index}")],
                f"page-{index + 1}",
            )
            for index in range(count)
        ],
    )


def _previewed(guide: Guide) -> str:
    _configured(guide)
    _pages(guide)
    code, previewed = guide.confirm("preview")
    assert code == 0, previewed
    return str(previewed["data"]["preview_id"])


def _health(guide: Guide) -> dict[str, Any]:
    code, status, _ = guide.run("status", *guide.common(), "--json")
    assert code == 0 and status is not None, status
    return dict(status["data"]["report"])


def _journal(guide: Guide) -> dict[str, Any]:
    return dict(json.loads(guide.journal.read_bytes()))


def _interrupt(
    monkeypatch: pytest.MonkeyPatch, name: str, position: str
) -> None:
    """Crash around the named adapter call or the journal result write."""
    if position == "after_result_before_journal":
        original_write = WorkflowJournal.write

        def write(self: WorkflowJournal, document: Any, **keywords: Any) -> Any:
            if keywords.get("reserve_bytes") == 0:
                raise _Crash
            return original_write(self, document, **keywords)

        monkeypatch.setattr(WorkflowJournal, "write", write)
        return
    original: Callable[..., Any] = getattr(source_guide_flow, name)

    def call(*arguments: Any, **keywords: Any) -> Any:
        if position == "before_authority":
            raise _Crash
        original(*arguments, **keywords)
        raise _Crash

    monkeypatch.setattr(source_guide_flow, name, call)


POSITIONS = ("before_authority", "after_commit_before_result", "after_result_before_journal")


def _crash(guide: Guide, action: str, arguments: dict[str, object] | None = None) -> int:
    """Propose, then crash during the confirmed run; return the crashed operation sequence."""
    code, proposed = guide.advance(action, arguments)
    assert code == 3, proposed
    with pytest.raises(_Crash):
        guide.advance(
            action,
            arguments,
            confirmation={
                "intent_sha256": proposed["intent"]["intent_sha256"],
                "decision": "approve",
            },
        )
    guide.capsys.readouterr()
    journal = _journal(guide)
    guide.revision = journal["revision"]
    executing = [item for item in journal["operations"] if item["state"] == "executing"]
    assert len(executing) == 1 and executing[0]["action"] == action
    assert executing[0]["executing_pid"] == os.getpid()
    return int(executing[0]["sequence"])


def _settled(guide: Guide, sequence: int) -> None:
    code, unknown = guide.advance("accounts")
    assert (code, unknown["code"]) == (78, "operation_unknown"), unknown
    assert unknown["next_actions"] == ["reconcile", "cancel"]
    assert _journal(guide)["operations"][sequence]["state"] == "unknown"
    # No new operation ID is created before reconciling.
    code, blocked = guide.advance("preview")
    assert (code, blocked["code"]) == (78, "operation_unknown")


@pytest.mark.parametrize("position", POSITIONS)
def test_configure_ambiguity_is_halt_only_then_new_intent(
    guide: Guide, monkeypatch: pytest.MonkeyPatch, position: str
) -> None:
    guide.through_choose()
    with monkeypatch.context() as patch:
        _interrupt(patch, "configure_source", position)
        sequence = _crash(guide, "configure")
    _settled(guide, sequence)

    code, halted = guide.advance("reconcile", {"operation_sequence": sequence})
    assert (code, halted["outcome"], halted["code"]) == (
        78, "unknown_outcome", "operation_unknown"
    )
    assert halted["intent"] is None and halted["next_actions"] == ["configure"]
    journal = _journal(guide)
    assert journal["operations"][sequence]["state"] == "unknown"
    assert journal["operations"][-1]["action"] == "reconcile"
    assert journal["operations"][-1]["state"] == "refused"

    # The owner explicitly chooses a new intent; an existing exact config is reused, never reset.
    code, configured = guide.confirm("configure")
    assert (code, configured["outcome"]) == (0, "completed"), configured
    report = _health(guide)
    assert report["source"]["control_epoch"] == 0
    assert report["source"]["configured_status"] == "disabled"
    assert guide.history() == 0


@pytest.mark.parametrize("position", POSITIONS)
def test_preview_ambiguity_halts_and_metadata_identifies_retained_page(
    guide: Guide, monkeypatch: pytest.MonkeyPatch, position: str
) -> None:
    _configured(guide)
    _pages(guide)
    with monkeypatch.context() as patch:
        _interrupt(patch, "preview_source", position)
        sequence = _crash(guide, "preview")
    _settled(guide, sequence)
    fetched = guide.history()
    assert fetched == (0 if position == "before_authority" else 1)

    code, halted = guide.advance("reconcile", {"operation_sequence": sequence})
    assert (code, halted["code"]) == (78, "operation_unknown")
    # Reconciliation never calls preview.
    assert guide.history() == fetched
    if position == "before_authority":
        assert halted["data"] is None
        code, previewed = guide.confirm("preview")
        assert code == 0
        preview_id = previewed["data"]["preview_id"]
    else:
        assert halted["next_actions"] == ["import", "preview"]
        preview_id = halted["data"]["preview_id"]
        assert preview_id == _health(guide)["pending_preview"]["preview_id"]
        code, retained = guide.advance("preview")
        assert (code, retained["code"]) == (78, "inspection_required")
        assert retained["data"]["preview_id"] == preview_id
    code, imported = guide.confirm("import", {"preview_id": preview_id})
    assert (code, imported["outcome"]) == (0, "completed"), imported
    assert guide.history() == 1


@pytest.mark.parametrize("position", POSITIONS)
def test_import_ambiguity_replays_the_exact_saved_request(
    guide: Guide, monkeypatch: pytest.MonkeyPatch, position: str
) -> None:
    preview_id = _previewed(guide)
    with monkeypatch.context() as patch:
        _interrupt(patch, "import_preview", position)
        sequence = _crash(guide, "import", {"preview_id": preview_id})
    _settled(guide, sequence)
    saved = _journal(guide)["operations"][sequence]

    code, proposed = guide.advance("reconcile", {"operation_sequence": sequence})
    assert code == 3, proposed
    request = json.loads(proposed["intent"]["canonical_request_utf8"])
    assert request == {"operation_sequence": sequence, "request_sha256": saved["request_sha256"]}
    code, replayed = guide.advance(
        "reconcile",
        {"operation_sequence": sequence},
        confirmation={
            "intent_sha256": proposed["intent"]["intent_sha256"],
            "decision": "approve",
        },
    )
    assert (code, replayed["outcome"]) == (0, "completed"), replayed
    receipts = replayed["data"]["receipt_ids"]
    assert len(receipts) == 1
    journal = _journal(guide)
    original = journal["operations"][sequence]
    # Byte-exact preservation of the saved request and intent across processes.
    assert original["canonical_request_utf8"] == saved["canonical_request_utf8"]
    assert original["canonical_intent_utf8"] == saved["canonical_intent_utf8"]
    assert original["state"] == "completed"
    assert original["evidence"] == {
        "source_id": json.loads(saved["canonical_request_utf8"])["source_id"],
        "preview_id": preview_id,
        "receipt_ids": receipts,
    }
    assert journal["retained_receipt_ids"] == receipts
    report = _health(guide)
    assert report["execution"]["captured_count"] == 1
    assert report["pending_preview"]["status"] == "absent"
    assert guide.history() == 1
    code, inspected = guide.advance("inspect", {"receipt_id": receipts[0]})
    assert code == 0 and "recovery message 0" in inspected["data"]["body"]


def test_refused_import_replay_halts_without_altering_the_saved_request(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_collector.live_manager import LiveSourceManager

    preview_id = _previewed(guide)
    with monkeypatch.context() as patch:
        _interrupt(patch, "import_preview", "before_authority")
        sequence = _crash(guide, "import", {"preview_id": preview_id})
    _settled(guide, sequence)
    # A separate owner disable cancels the pending page while the guide is stopped.
    source_id = json.loads(
        _journal(guide)["operations"][sequence]["canonical_request_utf8"]
    )["source_id"]
    manager = LiveSourceManager(guide.state.parent / "live", guide.brain)
    entry = manager.capture.status(source_id)
    manager.capture.configure(
        source_id,
        support_selection(entry["selection"]),
        cast(dict[str, object], entry["options"]),
        disable=True,
    )
    code, replay = guide.confirm("reconcile", {"operation_sequence": sequence})
    assert (code, replay["code"]) == (78, "authority_refused"), replay
    journal = _journal(guide)
    assert journal["operations"][sequence]["state"] == "unknown"
    assert journal["operations"][-1]["state"] == "refused"
    assert journal["retained_receipt_ids"] == []
    # A refusal never proves the import did not commit: the pending page is gone,
    # so the workflow halts instead of fetching or importing another page.
    assert journal["status"] == "halted"
    _pages(guide)
    code, blocked = guide.advance("preview")
    assert (code, blocked["code"]) == (78, "receipt_unresolved")
    assert guide.history() == 1


def _collector(guide: Guide, *arguments: str) -> dict[str, Any]:
    """Another actor using the same real collector directly (Slack HTTP still synthetic)."""
    completed = subprocess.run(
        [str(guide.executable), "sources", "--state", str(guide.state), "--brain-root",
         str(guide.brain), "--foreground", *arguments],
        capture_output=True, check=True, text=True,
    )
    return dict(json.loads(completed.stdout))


def test_replaced_last_run_after_lost_import_ack_halts(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview_id = _previewed(guide)
    with monkeypatch.context() as patch:
        _interrupt(patch, "import_preview", "after_commit_before_result")
        sequence = _crash(guide, "import", {"preview_id": preview_id})
    _settled(guide, sequence)
    source_id = json.loads(
        _journal(guide)["operations"][sequence]["canonical_request_utf8"]
    )["source_id"]
    _pages(guide)
    other = _collector(guide, "preview", "--source-id", source_id)
    _collector(guide, "import", "--source-id", source_id, "--preview-id", other["preview_id"])
    assert _health(guide)["pending_preview"]["status"] == "absent"

    code, replay = guide.confirm("reconcile", {"operation_sequence": sequence})
    assert (code, replay["code"]) == (78, "authority_refused"), replay
    journal = _journal(guide)
    assert journal["status"] == "halted"
    # Saved evidence never manufactures the committed receipts.
    assert journal["retained_receipt_ids"] == []
    assert journal["operations"][sequence]["state"] == "unknown"
    _pages(guide)
    code, blocked = guide.advance("preview")
    assert (code, blocked["code"]) == (78, "receipt_unresolved")
    assert blocked["next_actions"] == ["cancel"]
    # The owner exit from a halted journal is journal-only: no authority call, and the
    # unresolved operation and every receipt stay recorded.
    code, cancelled = guide.advance("cancel")
    assert (code, cancelled["outcome"]) == (0, "cancelled"), cancelled
    journal = _journal(guide)
    assert journal["status"] == "cancelled"
    assert journal["operations"][sequence]["state"] == "unknown"
    assert journal["operations"][-1]["action"] == "cancel"
    code, after = guide.advance("reconcile", {"operation_sequence": sequence})
    assert after["outcome"] == "refused" and code != 0
    assert guide.history() == 2


def test_refused_import_with_page_still_pending_stays_active(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain.services.source_guide_adapter import GuideAdapterError

    preview_id = _previewed(guide)

    def refuse(*_arguments: object, **_keywords: object) -> dict[str, object]:
        raise GuideAdapterError("operation_failed", native="synthetic_unmapped")

    monkeypatch.setattr(source_guide_flow, "import_preview", refuse)
    code, refused = guide.confirm("import", {"preview_id": preview_id})
    assert (code, refused["code"]) == (78, "operation_failed")
    assert _journal(guide)["status"] == "active"
    monkeypatch.undo()
    code, imported = guide.confirm("import", {"preview_id": preview_id})
    assert code == 0, imported


def test_unmapped_refusal_after_page_left_pending_halts(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain.services.source_guide_adapter import GuideAdapterError

    preview_id = _previewed(guide)
    original: Callable[..., Any] = source_guide_adapter.import_preview

    def commit_then_refuse(*arguments: Any, **keywords: Any) -> dict[str, object]:
        original(*arguments, **keywords)
        raise GuideAdapterError("operation_failed", native="synthetic_unmapped")

    monkeypatch.setattr(source_guide_flow, "import_preview", commit_then_refuse)
    code, refused = guide.confirm("import", {"preview_id": preview_id})
    assert (code, refused["code"]) == (78, "operation_failed")
    journal = _journal(guide)
    assert journal["status"] == "halted"
    assert journal["retained_receipt_ids"] == []
    _pages(guide)
    code, blocked = guide.advance("preview")
    assert (code, blocked["code"]) == (78, "receipt_unresolved")
    assert guide.history() == 1


def test_private_temporary_name_collision_is_unsafe_path(guide: Guide) -> None:
    guide.through_choose()
    collision = guide.journal.with_name(f".{guide.journal.name}.selection-{os.getpid()}.json")
    collision.write_text("{}")
    code, refused = guide.advance("configure")
    assert (code, refused["outcome"], refused["code"]) == (78, "refused", "unsafe_path")
    assert collision.read_text() == "{}"


def test_near_full_journal_can_still_be_cancelled(guide: Guide) -> None:
    from open_brain.services.source_guide_journal import (
        MAX_JOURNAL_BYTES,
        canonical_json,
        operation_record,
    )

    guide.advance("start")
    journal = WorkflowJournal(guide.journal)
    document = _journal(guide)
    binding = {
        "brain_id": document["binding"]["brain_id"],
        "issuer_epoch": document["binding"]["issuer_epoch"],
        "collector_binding": document["binding"]["collector_binding"],
        "source_generation": None,
        "control_epoch": None,
        "executable": document["binding"]["executable"],
    }
    while len(canonical_json(document).encode()) < MAX_JOURNAL_BYTES - 6_000:
        item = operation_record(
            sequence=len(document["operations"]), action="accounts", request={},
            expected_versions=(), execution_binding=binding,
        )
        item["state"] = "completed"
        document["operations"].append(item)
    document = journal.write(document, expected_revision=document["revision"], reserve_bytes=0)
    guide.revision = int(cast(int, document["revision"]))
    code, accounts = guide.advance("accounts")
    assert (code, accounts["code"]) == (78, "capacity_reached")
    code, cancelled = guide.advance("cancel")
    assert (code, cancelled["outcome"]) == (0, "cancelled"), cancelled


def support_selection(value: Any) -> Any:
    from open_brain_connectors.runtime.source_registry import SourceResourceSelection

    return SourceResourceSelection(**value)


def test_changed_versions_invalidate_a_shown_confirmation(guide: Guide) -> None:
    from open_brain_collector.live_manager import LiveSourceManager
    from open_brain_connectors.runtime.source_registry import SourceResourceSelection

    guide.through_choose()
    code, proposed = guide.advance("configure")
    assert code == 3
    LiveSourceManager(guide.state.parent / "live", guide.brain).dispatch(
        "sources.configure",
        {
            "selection": asdict(
                SourceResourceSelection("slack", support.ACCOUNT, "channel:CGENERAL", "channel")
            ),
            "options": {"date_floor": DATE_FLOOR, "privacy_tier": "work"},
        },
    )
    code, refused = guide.advance(
        "configure",
        confirmation={"intent_sha256": proposed["intent"]["intent_sha256"], "decision": "approve"},
    )
    assert (code, refused["code"]) == (78, "revision_conflict")
    operations = _journal(guide)["operations"]
    assert operations[-1]["state"] == "refused"
    assert not any(item["state"] == "executing" for item in operations)


def test_same_journal_second_process_is_busy_without_changes(guide: Guide) -> None:
    _configured(guide)
    journal = WorkflowJournal(guide.journal)
    before = guide.journal.read_bytes()
    guard = journal.acquire_guard(guide.workflow_id)
    try:
        code, busy = guide.advance("accounts")
        assert (code, busy["code"]) == (78, "journal_busy")
        _pages(guide)
        code, proposed = guide.advance("preview")
        assert code == 3
        code, busy = guide.advance(
            "preview",
            confirmation={
                "intent_sha256": proposed["intent"]["intent_sha256"],
                "decision": "approve",
            },
        )
        assert (code, busy["code"]) == (78, "journal_busy")
        assert guide.history() == 0
        assert not any(
            item["state"] == "executing" for item in _journal(guide)["operations"]
        )
    finally:
        os.close(guard)
    import fcntl

    lock = os.open(journal.lock_path, os.O_RDWR)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stored = guide.journal.read_bytes()
        code, busy = guide.advance("accounts")
        assert (code, busy["code"]) == (78, "journal_busy")
        assert guide.journal.read_bytes() == stored
    finally:
        os.close(lock)
    assert before != guide.journal.read_bytes()


def test_different_journals_rely_on_collector_state(guide: Guide, tmp_path: Path) -> None:
    _configured(guide)
    second_journal = tmp_path / "guide-two" / "journal.json"
    second_journal.parent.mkdir(mode=0o700)
    first_journal = guide.journal
    first_revision = guide.revision
    guide.journal, guide.workflow_id, guide.revision = second_journal, "guide:synthetic-2", 0
    assert guide.advance("start")[1]["outcome"] == "completed"
    assert guide.advance("preflight", guide.preflight_arguments())[1]["outcome"] == "completed"
    support.queue(
        guide.fixture,
        "conversations.list",
        [support.channel_page([("CGENERAL", "general"), ("COTHER", "general")])] * 2,
    )
    guide.advance("resources", {"connection_id": support.ACCOUNT, "cursor": None})
    guide.advance("choose", guide.choice())
    code, reused = guide.confirm("configure")
    assert code == 0, reused
    _pages(guide)
    code, previewed = guide.confirm("preview")
    assert code == 0
    preview_id = previewed["data"]["preview_id"]
    guide.journal, guide.workflow_id, guide.revision = (
        first_journal, "guide:synthetic-1", first_revision
    )
    code, retained = guide.advance("preview")
    assert (code, retained["code"]) == (78, "inspection_required")
    assert retained["data"]["preview_id"] == preview_id
    assert guide.history() == 1


@pytest.mark.parametrize("damage", ["missing", "forged", "unsafe"])
def test_missing_forged_or_unsafe_guard_halts_before_any_child(
    guide: Guide, damage: str
) -> None:
    _configured(guide)
    guard = WorkflowJournal(guide.journal).guard_path
    if damage == "missing":
        guard.unlink()
    elif damage == "forged":
        guard.write_text('{"journal":"journal.json","workflow_id":"guide:forged"}')
    else:
        guard.chmod(0o644)
    _pages(guide)
    code, proposed = guide.advance("preview")
    assert code == 3
    code, busy = guide.advance(
        "preview",
        confirmation={"intent_sha256": proposed["intent"]["intent_sha256"], "decision": "approve"},
    )
    assert (code, busy["code"]) == (78, "journal_busy")
    assert guide.history() == 0
    assert not any(item["state"] == "executing" for item in _journal(guide)["operations"])
    if damage == "missing":
        assert not guard.exists()


def test_expired_preview_never_imports(guide: Guide) -> None:
    from open_brain_collector.live_manager import LiveSourceManager

    preview_id = _previewed(guide)
    code, proposed = guide.advance("import", {"preview_id": preview_id})
    assert code == 3
    source_id = json.loads(proposed["intent"]["canonical_request_utf8"])["source_id"]
    manager = LiveSourceManager(guide.state.parent / "live", guide.brain)
    entry = manager.capture.status(source_id)
    manager.capture.configure(
        source_id,
        support_selection(entry["selection"]),
        cast(dict[str, object], entry["options"]),
        disable=True,
    )
    code, refused = guide.advance(
        "import",
        {"preview_id": preview_id},
        confirmation={"intent_sha256": proposed["intent"]["intent_sha256"], "decision": "approve"},
    )
    assert (code, refused["code"]) == (78, "authority_refused")
    assert refused["next_actions"] == ["preview"]
    assert _journal(guide)["retained_receipt_ids"] == []


def test_lost_start_reads_the_exact_journal_and_never_replaces_it(guide: Guide) -> None:
    code, started = guide.advance("start")
    assert code == 0
    stored = guide.journal.read_bytes()
    code, again = guide.advance("start", expected=0)
    assert (code, again["outcome"], again["revision"]) == (0, "completed", started["revision"])
    assert guide.journal.read_bytes() == stored
    guide.workflow_id = "guide:other"
    code, other = guide.advance("start", expected=0)
    assert (code, other["code"]) == (78, "binding_mismatch")
    assert guide.journal.read_bytes() == stored


def test_cancel_after_import_retains_receipts_and_custody(guide: Guide) -> None:
    preview_id = _previewed(guide)
    code, imported = guide.confirm("import", {"preview_id": preview_id})
    assert code == 0
    code, cancelled = guide.advance("cancel")
    assert (code, cancelled["outcome"]) == (0, "cancelled")
    journal = _journal(guide)
    assert journal["status"] == "cancelled"
    assert journal["retained_receipt_ids"] == imported["data"]["receipt_ids"]
    report = _health(guide)
    assert report["execution"]["captured_count"] == 1
    assert report["source"]["configured_status"] == "disabled"


def test_supervisor_sigkill_while_collector_child_holds_inherited_guard(guide: Guide) -> None:
    _configured(guide)
    _pages(guide)
    code, proposed = guide.advance("preview")
    assert code == 3
    decision = guide.root / "decisions" / "killed.json"
    fd = os.open(decision, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(
        fd,
        json.dumps(
            {
                "schema_version": 1,
                "workflow_id": guide.workflow_id,
                "expected_revision": guide.revision,
                "action": "preview",
                "confirmation": {
                    "intent_sha256": proposed["intent"]["intent_sha256"],
                    "decision": "approve",
                },
                "arguments": {},
            }
        ).encode(),
    )
    os.close(fd)
    (guide.fixture / "hold").write_text("1")
    supervisor = subprocess.Popen(
        [sys.executable, "-m", "open_brain", "source", "guide", "advance", *guide.common(),
         "--decision-file", str(decision), "--json"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ},
    )
    try:
        deadline = time.monotonic() + 60
        while not (guide.fixture / "started").exists():
            assert time.monotonic() < deadline and supervisor.poll() is None
            time.sleep(0.05)
        os.kill(supervisor.pid, signal.SIGKILL)
        supervisor.wait(timeout=10)
        journal = WorkflowJournal(guide.journal)
        # The supervisor is gone, but its collector child still holds the inherited guard.
        assert journal.guard_free(guide.workflow_id) is False
        guide.revision = _journal(guide)["revision"]
        code, busy = guide.advance("accounts")
        assert (code, busy["code"]) == (78, "journal_busy")
        assert _journal(guide)["operations"][-1]["state"] == "executing"
    finally:
        (guide.fixture / "release").write_text("1")
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait()
    deadline = time.monotonic() + 60
    while not WorkflowJournal(guide.journal).guard_free(guide.workflow_id):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    sequence = len(_journal(guide)["operations"]) - 1
    _settled(guide, sequence)
    code, halted = guide.advance("reconcile", {"operation_sequence": sequence})
    assert (code, halted["code"]) == (78, "operation_unknown")
    preview_id = halted["data"]["preview_id"]
    code, imported = guide.confirm("import", {"preview_id": preview_id})
    assert code == 0, imported
    assert guide.history() == 1
